# LLM gateway — context for a fresh agent

A small, dependency-light HTTP gateway that fronts one or more **MTPLX** model servers
(Apple-silicon inference engines) and gives OpenAI- and Anthropic-shaped clients a
single authenticated endpoint. It is deliberately boring: no database, no admin UI,
~470 lines of Python that you can read end to end.

This folder is self-contained. Take it to another machine and keep building.

---

## 1. Topology

```
              mobile app / pi / nanoclaw / curl
                          │  (TLS terminated by Cloudflare or the cluster ingress)
                          ▼
                 ┌──────────────────┐
                 │  gateway (:8000) │   auth · routing · quotas · logs
                 └────────┬─────────┘
              ┌───────────┴────────────┐
              ▼                        ▼
      ┌───────────────┐        ┌────────────────┐
      │ heavy engine  │        │ fast engine    │     MTPLX, one model per process
      │ :8100 serial  │        │ :8101 ar_batch │     (loopback / private net only)
      └───────────────┘        └────────────────┘
                          Apple-silicon host (128 GB unified memory)
```

The engines are **stateful and memory-bound**; the gateway is **stateless except for
in-memory quota counters** (see §7). That asymmetry is why the gateway is the part that
belongs in a cluster and the engines must stay on the metal.

---

## 2. Protocols

### 2.1 Client → gateway (this is what you must not break)

Base URL `http://<gateway>/v1`. All endpoints require a key:

```
Authorization: Bearer <client-key>      (OpenAI-style clients)
x-api-key: <client-key>                 (Anthropic-style clients)
```

| endpoint | method | notes |
|---|---|---|
| `/v1/chat/completions` | POST | primary; `stream: true` supported (SSE) |
| `/v1/messages` | POST | Anthropic shape (`max_tokens` required, `anthropic-version` header optional) |
| `/v1/responses` | POST | OpenAI Responses shape (`input`, `max_output_tokens`) |
| `/v1/completions` | POST | legacy prompt-style |
| `/v1/models` | GET | list of served model ids (auth required) |
| `/usage/summary` | GET | per-key × model token totals (auth required) |
| `/health` | GET | **unauthenticated** liveness |

Model selection is by the `model` field only. Routing is a **substring match on the model
id** (`next` → heavy, `35b` → fast), configured per deployment — see `--backends`.

Errors are JSON `{"error": {"message": ...}}` with these codes:

| code | meaning |
|---|---|
| 400 | malformed JSON, or upstream rejected the request (e.g. context too long) |
| 401 | unknown/absent client key (`reason: invalid_key`) |
| 404 | unknown path, or `unknown_model` |
| 413 | `body_too_large` (>16 MB) or `prompt_too_large` (see §4) |
| 429 | per-key rate limit or daily compute-token cap |
| 502 | engine unreachable (gateway cannot reach `:8100`/`:8101`) |

### 2.2 Gateway → engine (MTPLX)

OpenAI-compatible over plain HTTP on a private address (`http://127.0.0.1:8100/v1` by
default, but in a cluster you will point it at the Mac over WireGuard/Tailscale or a
firewalled LAN IP). The gateway forwards:

* `POST /v1/chat/completions`, `/v1/completions`, `/v1/messages`, `/v1/responses`
* request body **unchanged** except for the clamps in §4 and
  `stream_options.include_usage = true` when the client asked for streaming
* header `Authorization: Bearer <ENGINE_KEY>` (and `x-api-key` for the Anthropic path)
* `anthropic-version` if the client sent one

What the gateway reads from the engine response:

* non-streaming: `usage.prompt_tokens` / `completion_tokens` /
  `prompt_tokens_details.cached_tokens`; Anthropic variant `input_tokens` /
  `output_tokens` / `cache_read_input_tokens`
* streaming: forwards raw SSE bytes, and scrapes the final `usage` object
* status codes are propagated verbatim to the client

Engine-specific behaviours you must expect (they are not gateway bugs):

| engine signal | meaning | gateway behaviour |
|---|---|---|
| `400 ... maximum context length is N tokens` | prompt + generation exceeds the served window | passed through |
| `507 insufficient memory: ... engine held X GiB ... past its Y GiB limit` | MTPLX memory guard refused the prefill | passed through; retry or shrink the prompt |
| `401` | wrong engine key | surfaced as 502-ish failure; check `BENCH_BACKEND_KEY` |
| `usage.prompt_tokens_details.cached_tokens` | prefix-cache hits | **free** for quota purposes (§3) |

### 2.3 Engine configuration assumptions

The engines are started outside this repo's control, but these flags matter:

```
--host 127.0.0.1            # never 0.0.0.0 unless firewalled to the gateway only
--api-key <ENGINE_KEY>      # or --api-key-file; single key per file
--context-window <tokens>   # heavy 262144, fast 32768 on a 128 GB box
--memory-limit 96GiB        # MUST carry a unit; a bare number is BYTES -> 0 GiB budget
--allow-swap                # heavy model only: 83 GB weights exceed the Metal budget
--max-tokens 16384          # engine-side output ceiling (defence in depth)
--scheduler-mode ar_batch --batching-preset throughput --max-active-requests 6
```

Measured on a 128 GB M-series box: heavy 80–93 GB footprint at 256k ctx, fast 20 GB at
32k ctx, both resident ≈ 113 GB with ~20 GB headroom and no swap. When both are busy they
share memory bandwidth: heavy drops ~36 %, fast c4 aggregate ~32 %.

---

## 3. Quota semantics (deliberate, non-obvious)

`BENCH_DAILY_TOKEN_CAP` counts **compute tokens** = `(prompt_tokens − cached_tokens) +
completion_tokens`. Cached prefix tokens are **free**.

Reason: agentic coding clients resend a 100k-token context every turn with a 95 %+ cache
hit rate; charging the raw `prompt_tokens` burns a daily budget in ~200 turns (this really
happened). If you port this to an off-the-shelf gateway (litellm, etc.) reproduce this
rule with cache-aware pricing or a hook — a naive `tpm` limit is wrong for this workload.

Rate limiting is a simple in-process sliding minute per key (`BENCH_RATE_PER_MIN`, 120).

---

## 4. Request shaping (policy, not security theatre)

| knob | default | why |
|---|---|---|
| `BENCH_MAX_TOKENS` | 16384 | clamp `max_tokens` **and** `max_output_tokens`; a 32k-output request pins a serial engine for ~12 min |
| `BENCH_MAX_PROMPT_CHARS` | 1 200 000 (~300k tok) | anti-abuse only — the model's context window is the real limit |
| `BENCH_MAX_BODY_BYTES` | 16 MB | memory-exhaustion guard |
| `BENCH_CORS_ORIGIN` | off | set `*` or an origin for webview/BYOK apps |

Behind Cloudflare there is an extra hard constraint: the edge kills an origin that stays
silent >100 s, so a **cold** prefill above ~70k tokens can 524 (measured: 108k tokens =
55 s cold on the heavy model, 1.4 s cached). Streaming keeps the connection alive after
the first token; large cold prefills are the risk.

---

## 5. Observability contract

Two JSON-lines records per request, written to files (and to **stdout** when
`BENCH_LOG_STDOUT=1`, which is how you want it in a cluster):

* **access** — every request including rejections:
  `{ts, client, method, path, model, key, key_prefix, status, reason, ms, stream, bytes, ua}`
  * `client` prefers `CF-Connecting-IP`, else the peer address
  * `key_prefix` (first 6 chars) is recorded for **rejected** credentials so you can spot
    a revoked key still in use or credential stuffing
  * `reason` is a stable machine-readable tag: `invalid_key`, `unknown_model`,
    `prompt_too_large:<n>`, `body_too_large:<n>`, `rate limit: …`, `backend_unreachable`
* **usage** — accepted requests only: `{ts, key, model, prompt_tokens, cached_tokens,
  completion_tokens, charged_tokens, elapsed_s}`

Engine-side telemetry is **not** available over HTTP; MTPLX writes it to files on the
model host and it must be shipped separately if you want it in the same stack:

```
~/.mtplx/logs/request-log-<port>.jsonl    ~350 fields: ttft_s, decode_tok_s,
                                          cached_tokens, prefill_tok_s, thermal_pressure
~/.mtplx/metrics/flight-<port>.jsonl      in-flight curves
mtplx trace live | sessions | request <id> | report      CLI equivalents
```

---

## 6. Configuration reference

| env | default | meaning |
|---|---|---|
| `--port` / `--host` | 8000 / 127.0.0.1 | listen address |
| `--backends` | `27b=http://127.0.0.1:8100,35b=http://127.0.0.1:8101` | `needle=url` list; needle matched against model id |
| `BENCH_BACKEND_KEY` | – | engine key forwarded upstream (required) |
| `BENCH_KEYS_FILE` | `~/.bench-usage/keys.json` | client keys (re-read per request) |
| `BENCH_USAGE_DIR` | `~/.bench-usage` | state/log directory |
| `BENCH_LOG_STDOUT` | `0` | mirror records to stdout |
| `BENCH_MAX_TOKENS` | 16384 | output clamp |
| `BENCH_MAX_PROMPT_CHARS` | 1 200 000 | prompt ceiling |
| `BENCH_MAX_BODY_BYTES` | 16 MB | body ceiling |
| `BENCH_RATE_PER_MIN` | 120 | per-key rate limit |
| `BENCH_DAILY_TOKEN_CAP` | 200 000 000 | per-key compute tokens/day |
| `BENCH_CORS_ORIGIN` | off | CORS origin |
| `BENCH_PROXY_NO_AUTH` | `0` | **testing only** |

Keys: `python keys.py add <name>` / `list` / `revoke <name>` (writes 0600 JSON, no
restart needed).

---

## 7. Running it

```bash
pip install -r requirements.txt
BENCH_BACKEND_KEY=... python api_proxy.py --port 8000 --host 0.0.0.0 \
  --backends "next=http://<mac>:8100,35b=http://<mac>:8101"
./scripts/smoke_test.sh http://localhost:8000 <client-key> next
```

Docker/k8s: `Dockerfile` + `k8s/` give a Deployment, Service, ConfigMap, Secret example,
PDB and a Prometheus `ServiceMonitor` stub. `k8s/README.md` explains what still has to
change before it is production-correct (see below).

---

## 8. Known gaps — the work you are inheriting

Ordered by how much they matter for a cluster deployment:

1. **Quota state is in-process.** Two replicas = two independent counters, and a restart
   resets the daily cap. Move `check_quota`/`add_tokens` to Redis (or Postgres) with a
   UTC-day key. This is the single biggest correctness issue.
2. **No `/metrics`.** Add `prometheus_client`: request counter by `{status,model,key}`,
   token histograms (prompt/cached/completion/charged), upstream latency histogram,
   quota-denial counter, in-flight gauge. Then the ServiceMonitor actually does something.
3. **Keys in a Secret, hot-reload.** Today it is a file re-read per request — mount a
   k8s Secret at `BENCH_KEYS_FILE` (or add a control-plane endpoint for key CRUD).
   Never ship `keys.json` in an image.
4. **Stateless log shipping.** `BENCH_LOG_STDOUT=1` is wired; make the Deployment emit
   only stdout (no emptyDir needed) so promtail/fluent-bit picks it up.
5. **Per-key policy**: model allowlist (a "laptop" key probably should not spend the
   256k-context heavy model) and per-key max concurrency.
6. **Tests**: no unit tests. Worth adding with a fake upstream (`http.server` in a thread)
   covering: auth rejection, routing, clamps, quota accounting with cached tokens,
   SSE passthrough, CORS preflight.
7. **Upstream health**: `/health` only checks the gateway. Add a cheap upstream probe
   (`GET /v1/models` with a 2 s timeout, cached 10 s) and expose readiness separately from
   liveness so a dead Mac drains traffic instead of 502-ing.
8. **Timeouts**: `requests` calls use 1800 s. Behind an ingress you want connect/read
   timeouts, plus SSE-specific no-buffer settings (Traefik: disable compression/flush
   interval).
9. **Multi-engine routing**: needle matching is crude. If you add more engines, introduce
   real model→deployment mapping with weights/fallbacks.
10. **Retries**: no upstream retry. A 507 (memory guard) is sometimes transient; one
    bounded retry with backoff, only for idempotent non-streaming requests, is reasonable.

---

## 9. Design decisions worth keeping

* **One endpoint, model chosen by id.** Clients never learn about ports, and you can move
  engines freely.
* **Gateway owns auth; engines keep their own key too.** Defence in depth, and it means a
  leaked engine key alone does not let someone bypass quotas.
* **Rejections are first-class log records**, written before the response — that is the
  difference between "we saw 429s in the dashboard" and guessing.
* **Cache-aware quota.** Non-standard, and the reason this gateway exists at all.
* **No database, no admin UI on the public port.** The attack surface is one JSON handler.
