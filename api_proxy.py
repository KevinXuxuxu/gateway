#!/usr/bin/env python
"""Front proxy for the local model pair: multi-key auth, model routing, usage log.

Why this exists: MTPLX's own `--api-key-file` reads the file as ONE secret, so it
cannot serve several issued keys from one file. This proxy owns the key list, and
forwards to the two backends by model name:

    :8000  (this proxy)  ->  :8100  27B Q8       (serial, 256k ctx)
                             :8101  35B-A3B      (ar_batch, 64k ctx)

Clients point at ONE base URL and pick a model:
    http://localhost:8000/v1  +  model=mtplx-qwen38-27b-optimized-quality
                                 model=mtplx-qwen36-35b-a3b-optimized-speed

Every request is appended to ~/.llm-gateway/usage.jsonl with the key name, model,
token counts and timing (usage is taken from the OpenAI `usage` object; streaming
requests are rewritten to ask for `stream_options.include_usage`).

Prometheus metrics (request/token counters, upstream latency histogram, in-flight
gauge) are served on a SEPARATE port: GATEWAY_METRICS_PORT, default 0 = off. Never on
the app port - the ingress forwards every path on :8000 and these series carry per-key
usage, while Prometheus scrapes the pod directly.

Usage:
    GATEWAY_METRICS_PORT=9091 python scripts/api_proxy.py --port 8000 --backends 27b=http://127.0.0.1:8100,35b=http://127.0.0.1:8101
"""

import argparse
import hashlib
import json
import os
import signal
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from prometheus_client import Counter, Gauge, Histogram, start_wsgi_server

HOME = os.path.expanduser("~")


def _env(name, default=""):
    """GATEWAY_* env vars, with one-release fallback to the legacy BENCH_* names."""
    v = os.environ.get("GATEWAY_" + name)
    return os.environ.get("BENCH_" + name, default) if v is None else v


STATE_DIR = _env("USAGE_DIR", os.path.join(HOME, ".llm-gateway"))
KEYS_FILE = _env("KEYS_FILE", os.path.join(STATE_DIR, "keys.json"))
USAGE_LOG = os.path.join(STATE_DIR, "usage.jsonl")
ACCESS_LOG = os.path.join(STATE_DIR, "access.jsonl")  # every request incl. rejections
NO_AUTH = _env("PROXY_NO_AUTH") == "1"
# the backends authenticate with their own engine key; the proxy authenticates
# clients against the multi-key list and forwards this credential upstream
BACKEND_KEY = _env("BACKEND_KEY")
MAX_BODY_BYTES = int(_env("MAX_BODY_BYTES", str(16 * 1024 * 1024)))
MAX_COMPLETION_TOKENS = int(_env("MAX_TOKENS", "8192"))
# The context window is the real limit (262k on the 27B), so this is only an
# anti-abuse ceiling for absurd bodies - not a working limit for long sessions.
MAX_PROMPT_CHARS = int(_env("MAX_PROMPT_CHARS", str(1_200_000)))  # ~300k tokens
RATE_PER_MIN = int(_env("RATE_PER_MIN", "120"))  # per key
# Optional CORS for browser/webview clients (native apps don't need it).
# Empty = disabled. Set to e.g. "*" or a specific origin to enable.
CORS_ORIGIN = _env("CORS_ORIGIN").strip()
# In a cluster, ship logs via stdout (container log) instead of a shared volume.
LOG_STDOUT = _env("LOG_STDOUT") == "1"
# Quota is measured in *compute* tokens: newly prefilled + generated. Cached
# prompt tokens are free (they cost ~nothing and caching is what makes 100k-token
# agent sessions affordable), so they must not consume a daily allowance.
DAILY_TOKEN_CAP = int(_env("DAILY_TOKEN_CAP", "200000000"))  # per key, compute tokens
_rl = {}
_rl_lock = threading.Lock()

_lock = threading.Lock()

_inflight = 0  # connections being served, for bounded drain on shutdown
_readyz = {"ts": 0.0, "ok": False}  # cached upstream probe for /readyz

# --------------------------------------------------------------- Prometheus
# Metrics port is separate from the app port on purpose (see the module docstring):
# :8000 is public and serves no /metrics at all.
METRICS_PORT = int(_env("METRICS_PORT", "0"))
METRICS_ADDR = _env("METRICS_ADDR", "127.0.0.1")

PROBE_PATHS = ("/health", "/healthz", "/readyz")  # kubelet chatter, not traffic
LOGGED_PATHS = (
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/messages",
    "/v1/responses",
    "/v1/models",
    "/usage/summary",
)

# Every label value below is chosen by the gateway, never taken from client input:
# a label built from a client-supplied model id or URL is unbounded cardinality.
# `backend` is the routing needle from GATEWAY_BACKENDS, which is 1:1 with a model.
REQ_TOTAL = Counter(
    "gateway_requests_total",
    "Requests finished, probes excluded",
    ["status", "key", "path"],
)
# `prompt` includes `cached` and `charged` = (prompt - cached) + completion, so the
# kinds overlap: never sum them. `charged` is the quota formula of README §3.
TOKEN_TOTAL = Counter(
    "gateway_tokens_total", "Tokens counted at the gateway", ["kind", "key", "backend"]
)
UPSTREAM_SECONDS = Histogram(
    "gateway_upstream_seconds",
    "Time in one upstream call, including the SSE drain",
    ["backend", "stream"],
    buckets=(0.2, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 900),
)
UPSTREAM_INFLIGHT = Gauge(
    "gateway_upstream_inflight", "Requests pinned to an engine right now", ["backend"]
)
QUOTA_DENIALS = Counter(
    "gateway_quota_denials_total", "Requests refused by a quota check", ["reason"]
)


def ensure_state():
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(KEYS_FILE):
        import secrets

        key = secrets.token_hex(20)
        json.dump(
            {
                "keys": [
                    {
                        "name": "default",
                        "key": key,
                        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                ]
            },
            open(KEYS_FILE, "w"),
            indent=2,
        )
        os.chmod(KEYS_FILE, 0o600)
        print(f"created {KEYS_FILE} with a default key: {key}", flush=True)


def load_keys():
    try:
        data = json.load(open(KEYS_FILE))
    except Exception:
        return {}
    out = {}
    for e in data.get("keys", []):
        k = e.get("key")
        if k:
            out[k] = e.get("name") or "unnamed"
    return out


def key_name(presented):
    if NO_AUTH:
        return "no-auth"
    if not presented:
        return None
    import hmac

    name = None
    for k, n in load_keys().items():  # constant-time compare per key
        if hmac.compare_digest(k, presented):
            name = n
    return name


def check_quota(who):
    """Per-key requests/minute and daily token cap. Returns (ok, reason)."""
    now = time.time()
    with _rl_lock:
        st = _rl.setdefault(
            who, {"min_ts": now, "min_count": 0, "day": None, "tokens": 0}
        )
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if st["day"] != day:
            st["day"], st["tokens"] = day, 0
        if now - st["min_ts"] >= 60:
            st["min_ts"], st["min_count"] = now, 0
        if st["min_count"] >= RATE_PER_MIN:
            QUOTA_DENIALS.labels("rate_limit").inc()
            return False, f"rate limit: {RATE_PER_MIN} requests/min"
        if st["tokens"] >= DAILY_TOKEN_CAP:
            QUOTA_DENIALS.labels("daily_token_cap").inc()
            return False, (
                f"daily compute-token cap reached ({st['tokens']:,} / "
                f"{DAILY_TOKEN_CAP:,}); cached prompt tokens are not counted"
            )
        st["min_count"] += 1
    return True, ""


def add_tokens(who, n):
    if not n:
        return
    with _rl_lock:
        st = _rl.setdefault(
            who, {"min_ts": time.time(), "min_count": 0, "day": None, "tokens": 0}
        )
        st["tokens"] = st.get("tokens", 0) + int(n)


def log_access(rec):
    """Append one line per HTTP request: accepted *and* rejected."""
    line = json.dumps(rec)
    if LOG_STDOUT:
        print(json.dumps({"log": "access", **rec}), flush=True)
    with _lock:
        with open(ACCESS_LOG, "a") as f:
            f.write(line + "\n")
    count_request(rec)


def count_request(rec):
    """The one place a finished request is counted (probes aside)."""
    path = rec.get("path")
    if path in PROBE_PATHS:
        return
    REQ_TOTAL.labels(
        str(rec.get("status")),
        rec.get("key") or "-",
        path if path in LOGGED_PATHS else "other",
    ).inc()


def record_upstream(backend, who, usage, charged, secs, stream):
    """Tokens and latency for one finished upstream call."""
    for kind, value in (
        ("prompt", usage.get("prompt_tokens")),
        ("cached", usage.get("cached_tokens")),
        ("completion", usage.get("completion_tokens")),
        ("charged", charged),
    ):
        if value:
            TOKEN_TOTAL.labels(kind, who or "-", backend).inc(value)
    UPSTREAM_SECONDS.labels(backend, str(bool(stream)).lower()).observe(secs)


def log_usage(rec):
    if LOG_STDOUT:
        print(json.dumps({"log": "usage", **rec}), flush=True)
    with _lock:
        with open(USAGE_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "gateway"
    sys_version = ""
    timeout = 300  # socket timeout: blunts slowloris

    def log_message(self, fmt, *args):
        pass

    def handle(self):
        global _inflight
        with _lock:
            _inflight += 1
        try:
            super().handle()
        finally:
            with _lock:
                _inflight -= 1

    # ------------------------------------------------------------------ helpers
    def _auth_name(self):
        h = self.headers.get("Authorization", "")
        tok = h[7:].strip() if h.lower().startswith("bearer ") else ""
        tok = tok or self.headers.get("X-API-Key", "").strip()
        if NO_AUTH:
            return "no-auth"
        return key_name(tok)

    def _cors(self):
        if not CORS_ORIGIN:
            return
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, x-api-key, anthropic-version",
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")

    def do_OPTIONS(self):
        if not CORS_ORIGIN:
            return self._send_json(501, {"error": "CORS disabled"})
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _client(self):
        return self.headers.get("CF-Connecting-IP") or self.client_address[0]

    def _json(self, code, obj, *, model=None, keyname=None, reason=None, path=None):
        h = self.headers.get("Authorization", "")
        tok = h[7:].strip() if h.lower().startswith("bearer ") else ""
        tok = tok or (self.headers.get("X-API-Key") or "").strip()
        log_access(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "client": self._client(),
                "method": self.command,
                "path": path or urllib.parse.urlparse(self.path).path,
                "model": model,
                "key": keyname or "-",
                "status": code,
                "key_prefix": (tok[:6] + "…") if tok and not keyname else None,
                "reason": reason,
                "ms": None,
                "ua": (self.headers.get("User-Agent") or "")[:80],
            }
        )
        return self._send_json(code, obj)

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        try:
            # Every POST reply from here skipped reading the request body (401/413/
            # 429) or the body was junk (400/404). With HTTP/1.1 keep-alive the
            # leftovers would be parsed as the client's *next* request, so end the
            # connection instead - what http.server's own send_error does. One
            # reconnect per rejected call, instead of an answer for the wrong one.
            end_connection = self.command == "POST"
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if end_connection:
                self.send_header("Connection", "close")
                self.close_connection = True
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return  # client gone (kube-probe timeout, CF 524, user cancel)

    def _pick_backend(self, model):
        """(needle, url) of the first matching backend, or None.

        The needle travels with the url because it is the metrics label for the
        engine: a fixed configured string, never whatever the client typed in `model`.
        """
        m = (model or "").lower()
        for needle, url in self.server.backends:
            if needle in m:
                return needle, url
        return None

    # ------------------------------------------------------------------ routing
    def _ready(self):
        # unauthenticated so a probe needs no key (k8s readiness cannot hold one safely)
        now = time.time()
        if (
            now - _readyz["ts"] >= 10
        ):  # 10 s cache; no single-flight guard (low traffic)
            hdrs = {"Authorization": f"Bearer {BACKEND_KEY}"} if BACKEND_KEY else {}
            results = {}

            def probe(url):
                try:
                    r = requests.get(f"{url}/v1/models", timeout=2, headers=hdrs)
                    results[url] = r.status_code == 200
                except Exception:
                    results[url] = False

            threads = [
                threading.Thread(target=probe, args=(url,), daemon=True)
                for _, url in self.server.backends
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(2.5)  # concurrent: worst case ~2 s total, not 2 s × N
            # ready if ANY backend serves — stopping one engine must not drain the gateway
            ok = bool(threads) and any(
                results.get(u, False) for _, u in self.server.backends
            )
            _readyz["ts"] = now
            _readyz["ok"] = ok
        if _readyz["ok"]:
            return self._json(200, {"ready": True}, reason="ready")
        return self._json(
            503,
            {"error": {"message": "upstream engine unreachable"}},
            reason="backend_unreachable",
        )

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/health", "/healthz"):
            return self._json(200, {"ok": True}, reason="health")
        if path == "/readyz":
            return self._ready()
        who = self._auth_name()
        if not who and path != "/health":
            return self._json(
                401, {"error": {"message": "invalid API key"}}, reason="invalid_key"
            )
        if path == "/v1/models":
            seen, data = set(), []
            for _, url in self.server.backends:
                try:
                    hdrs = (
                        {"Authorization": f"Bearer {BACKEND_KEY}"}
                        if BACKEND_KEY
                        else {}
                    )
                    r = requests.get(f"{url}/v1/models", timeout=5, headers=hdrs)
                    if r.status_code != 200:
                        # a wrong GATEWAY_BACKEND_KEY lands here as 401 — do not hide it
                        print(f"models: {url} HTTP {r.status_code}", flush=True)
                        continue
                    for m in r.json().get("data", []):
                        if m.get("id") not in seen:
                            seen.add(m.get("id"))
                            data.append(m)
                except Exception as e:
                    print(f"models: {url} unreachable: {e}", flush=True)
                    continue
            return self._json(200, {"object": "list", "data": data}, keyname=who)
        if path == "/usage/summary":
            return self._json(200, self._usage_summary(), keyname=who)
        return self._json(
            404, {"error": "not found"}, keyname=who, reason="unknown_path"
        )

    def _usage_summary(self):
        agg = {}
        try:
            for line in open(USAGE_LOG):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                k = (r.get("key", "?"), r.get("model", "?"))
                a = agg.setdefault(
                    k,
                    {
                        "requests": 0,
                        "prompt_tokens": 0,
                        "cached_tokens": 0,
                        "charged_tokens": 0,
                        "completion_tokens": 0,
                        "seconds": 0.0,
                    },
                )
                a["requests"] += 1
                a["prompt_tokens"] += r.get("prompt_tokens") or 0
                a["cached_tokens"] += r.get("cached_tokens") or 0
                a["charged_tokens"] += r.get("charged_tokens") or 0
                a["completion_tokens"] += r.get("completion_tokens") or 0
                a["seconds"] += r.get("elapsed_s") or 0
        except FileNotFoundError:
            pass
        return {
            "by_key_model": [{"key": k[0], "model": k[1], **v} for k, v in agg.items()]
        }

    # ------------------------------------------------------------------ chat
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in (
            "/v1/chat/completions",
            "/v1/completions",
            "/v1/messages",
            "/v1/responses",
        ):
            return self._json(404, {"error": "not found"}, reason="unknown_path")
        who = self._auth_name()
        if not who:
            return self._json(
                401,
                {
                    "error": {
                        "message": "invalid API key",
                        "type": "invalid_request_error",
                    }
                },
                reason="invalid_key",
            )
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            return self._json(
                413,
                {
                    "error": {
                        "message": f"request body too large ({length} > {MAX_BODY_BYTES} bytes)"
                    }
                },
                keyname=who,
                reason=f"body_too_large:{length}",
            )
        ok, why = check_quota(who)
        if not ok:
            return self._json(
                429,
                {"error": {"message": why}, "type": "rate_limit"},
                keyname=who,
                reason=why,
            )
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except Exception:
            return self._json(
                400,
                {"error": {"message": "invalid JSON"}},
                keyname=who,
                reason="bad_json",
            )
        if isinstance(body.get("input"), str):
            chars = len(body["input"])
        elif isinstance(body.get("input"), list):
            chars = sum(len(str(x)) for x in body["input"])
        else:
            chars = sum(
                len(str(m.get("content") or "")) for m in body.get("messages", [])
            )
        if chars > MAX_PROMPT_CHARS:
            return self._json(
                413,
                {
                    "error": {
                        "message": f"prompt too large ({chars} chars > {MAX_PROMPT_CHARS} = ~300k tokens). "
                        f"The model's context window is the practical limit; over a Cloudflare "
                        f"tunnel a *cold* prefill above ~70k tokens can also hit CF's 100s "
                        f"origin timeout (cached turns are fast)."
                    }
                },
                keyname=who,
                model=body.get("model"),
                reason=f"prompt_too_large:{chars}",
            )
        if body.get("max_tokens") and int(body["max_tokens"]) > MAX_COMPLETION_TOKENS:
            body["max_tokens"] = MAX_COMPLETION_TOKENS  # cost/latency guard
        if (
            body.get("max_output_tokens")
            and int(body["max_output_tokens"]) > MAX_COMPLETION_TOKENS
        ):
            body["max_output_tokens"] = MAX_COMPLETION_TOKENS  # Responses API spelling
        picked = self._pick_backend(body.get("model"))
        if not picked:
            return self._json(
                404,
                {
                    "error": {
                        "message": f"unknown model {body.get('model')!r}; see /v1/models"
                    }
                },
                keyname=who,
                model=body.get("model"),
                reason="unknown_model",
            )
        engine, backend = picked

        stream = bool(body.get("stream"))
        if stream:
            opts = body.get("stream_options") or {}
            opts["include_usage"] = True
            body["stream_options"] = opts
        started = time.time()
        UPSTREAM_INFLIGHT.labels(engine).inc()
        try:
            self._proxy(path, body, backend, engine, stream, started, who)
        finally:
            # the gauge must not stick when the client dies mid-stream
            UPSTREAM_INFLIGHT.labels(engine).dec()

    # ------------------------------------------------------------------ upstream
    def _proxy(self, path, body, backend, engine, stream, started, who):
        """One upstream call: forward, then copy the answer back to the client.

        Split out of do_POST so the in-flight gauge there wraps every way out
        (engine unreachable, normal return, BrokenPipe mid-stream).
        """
        payload = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if BACKEND_KEY:
            headers["Authorization"] = f"Bearer {BACKEND_KEY}"
            headers["x-api-key"] = BACKEND_KEY
        if self.headers.get("anthropic-version"):
            headers["anthropic-version"] = self.headers["anthropic-version"]
        try:
            r = requests.post(
                f"{backend}{path}",
                data=payload,
                headers=headers,
                stream=stream,
                timeout=1800,
            )
        except Exception as e:
            return self._json(
                502,
                {"error": {"message": f"backend unreachable: {e}"}},
                keyname=who,
                model=body.get("model"),
                reason="backend_unreachable",
            )

        if not stream:
            data = r.content
            usage = {}
            try:
                u = json.loads(data).get("usage") or {}
                usage = {
                    "prompt_tokens": u.get("prompt_tokens", u.get("input_tokens")),
                    "completion_tokens": u.get(
                        "completion_tokens", u.get("output_tokens")
                    ),
                    "cached_tokens": (
                        (u.get("prompt_tokens_details") or {}).get("cached_tokens")
                        or u.get("cache_read_input_tokens")
                        or 0
                    ),
                }
            except Exception:
                pass
            self.send_response(r.status_code)
            self.send_header(
                "Content-Type", r.headers.get("Content-Type", "application/json")
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            elapsed = time.time() - started
            charged = max(
                (usage.get("prompt_tokens") or 0) - (usage.get("cached_tokens") or 0), 0
            ) + (usage.get("completion_tokens") or 0)
            add_tokens(who, charged)
            record_upstream(engine, who, usage, charged, elapsed, False)
            log_usage(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "key": who,
                    "cached_tokens": usage.get("cached_tokens"),
                    "charged_tokens": charged,
                    "model": body.get("model"),
                    "status": r.status_code,
                    "client": self.headers.get("CF-Connecting-IP")
                    or self.client_address[0],
                    "stream": False,
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "elapsed_s": round(elapsed, 3),
                    "key_hash": hashlib.sha256(str(who).encode()).hexdigest()[:8],
                }
            )
            log_access(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "client": self._client(),
                    "method": self.command,
                    "path": path,
                    "model": body.get("model"),
                    "key": who,
                    "status": r.status_code,
                    "reason": None,
                    "ms": round(elapsed * 1000),
                    "stream": False,
                    "bytes": len(data),
                    "ua": (self.headers.get("User-Agent") or "")[:80],
                }
            )
            return

        # streaming pass-through, capturing the usage chunk
        self.send_response(r.status_code)
        self.send_header(
            "Content-Type", r.headers.get("Content-Type", "text/event-stream")
        )
        self._cors()
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        usage = {}
        buf = ""
        try:
            for raw in r.iter_content(chunk_size=None):
                if not raw:
                    continue
                self.wfile.write(raw)
                self.wfile.flush()
                try:
                    buf += raw.decode("utf-8", "ignore")
                except Exception:
                    continue
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if '"usage"' not in line:
                        continue
                    # `chunk` is the SSE line without its "data:" prefix
                    chunk = (
                        line[5:].strip() if line.startswith("data:") else line.strip()
                    )
                    try:
                        u = json.loads(chunk).get("usage") or {}
                    except Exception:
                        continue
                    pt = u.get("prompt_tokens", u.get("input_tokens"))
                    ct = u.get("completion_tokens", u.get("output_tokens"))
                    ca = (u.get("prompt_tokens_details") or {}).get(
                        "cached_tokens"
                    ) or u.get("cache_read_input_tokens")
                    if pt:
                        usage["prompt_tokens"] = pt
                    if ct:
                        usage["completion_tokens"] = ct
                    if ca is not None:
                        usage["cached_tokens"] = ca
                if len(buf) > 65536:  # keep the tail only
                    buf = buf[-4096:]
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            elapsed = time.time() - started
            charged = max(
                (usage.get("prompt_tokens") or 0) - (usage.get("cached_tokens") or 0), 0
            ) + (usage.get("completion_tokens") or 0)
            add_tokens(who, charged)
            record_upstream(engine, who, usage, charged, elapsed, True)
            log_usage(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "key": who,
                    "cached_tokens": usage.get("cached_tokens"),
                    "charged_tokens": charged,
                    "model": body.get("model"),
                    "status": r.status_code,
                    "stream": True,
                    "client": self.headers.get("CF-Connecting-IP")
                    or self.client_address[0],
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "elapsed_s": round(elapsed, 3),
                    "key_hash": hashlib.sha256(str(who).encode()).hexdigest()[:8],
                }
            )
            log_access(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "client": self._client(),
                    "method": self.command,
                    "path": path,
                    "model": body.get("model"),
                    "key": who,
                    "status": r.status_code,
                    "reason": None,
                    "ms": round(elapsed * 1000),
                    "stream": True,
                    "ua": (self.headers.get("User-Agent") or "")[:80],
                }
            )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(_env("PORT", "8000")))
    ap.add_argument("--host", default=_env("HOST", "127.0.0.1"))
    ap.add_argument(
        "--backends",
        default=_env("BACKENDS", "27b=http://127.0.0.1:8100,35b=http://127.0.0.1:8101"),
        help="comma separated needle=url pairs; the needle is matched against the model id",
    )
    args = ap.parse_args()
    ensure_state()
    backends = []
    for part in args.backends.split(","):
        needle, _, url = part.partition("=")
        backends.append((needle.strip().lower(), url.strip().rstrip("/")))
    ThreadingHTTPServer.allow_reuse_address = True
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.backends = backends
    srv.daemon_threads = True
    srv.block_on_close = (
        False  # server_close must not join threads; drain below is bounded
    )
    metrics = None
    if METRICS_PORT:
        # own port, own (thread-per-request) server; not part of the app listener
        metrics, _ = start_wsgi_server(METRICS_PORT, addr=METRICS_ADDR)

    def _stop(signum, frame):
        # shutdown() blocks until serve_forever exits; calling it from the signal
        # handler (same thread) would deadlock
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)

    print(f"proxy on http://{args.host}:{args.port} -> {backends}", flush=True)
    if metrics:
        print(f"metrics on http://{METRICS_ADDR}:{METRICS_PORT}/metrics", flush=True)
    print(f"keys: {KEYS_FILE}  usage: {USAGE_LOG}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()
    deadline = time.time() + 20  # stay under k8s default 30 s termination grace
    while time.time() < deadline:
        with _lock:
            if _inflight == 0:
                break
        time.sleep(0.5)
    print(f"shutdown: {_inflight} request(s) still in flight", flush=True)


if __name__ == "__main__":
    sys.exit(main())
