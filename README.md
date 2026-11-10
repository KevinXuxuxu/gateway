# LLM gateway — context for a fresh agent

A small, dependency-light HTTP gateway that fronts one or more **MTPLX** model servers
(Apple-silicon inference engines) and gives OpenAI- and Anthropic-shaped clients a
single authenticated endpoint. It is deliberately boring: no database, no admin UI, no
framework — one Python file you can read end to end.

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

`/metrics` is **not** part of this API. It is served by a second listener on
`GATEWAY_METRICS_PORT` (9091 in the cluster), so nothing on the public port exposes key
names or per-key usage. There is no `/metrics` route on the app port at all.

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
| `401` | wrong engine key | surfaced as 502-ish failure; check `GATEWAY_BACKEND_KEY` |
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

`GATEWAY_DAILY_TOKEN_CAP` counts **compute tokens** = `(prompt_tokens − cached_tokens) +
completion_tokens`. Cached prefix tokens are **free**.

Reason: agentic coding clients resend a 100k-token context every turn with a 95 %+ cache
hit rate; charging the raw `prompt_tokens` burns a daily budget in ~200 turns (this really
happened). If you port this to an off-the-shelf gateway (litellm, etc.) reproduce this
rule with cache-aware pricing or a hook — a naive `tpm` limit is wrong for this workload.

Rate limiting is a per-key count of `GATEWAY_RATE_PER_MIN` (120) requests in a 60-second
window, held in process memory. The window opens at the first request after a quiet
minute, so it is **fixed**, not rolling: a client sitting on the limit can spend ~2x
across a boundary, and the engine's own queue absorbs the difference. `GATEWAY_DAILY_TOKEN_CAP`
is counted the same way (§3 above). Both are per-process — see §8.1 before adding a
second replica.

---

## 4. Request shaping (policy, not security theatre)

| knob | default | why |
|---|---|---|
| `GATEWAY_MAX_TOKENS` | 8192 | clamp `max_tokens` **and** `max_output_tokens`; a 32k-output request pins a serial engine for ~12 min |
| `GATEWAY_MAX_PROMPT_CHARS` | 1 200 000 (~300k tok) | anti-abuse only — the model's context window is the real limit |
| `GATEWAY_MAX_BODY_BYTES` | 16 MB | memory-exhaustion guard |
| `GATEWAY_CORS_ORIGIN` | off | set `*` or an origin for webview/BYOK apps |

Behind Cloudflare there is an extra hard constraint: the edge kills an origin that stays
silent >100 s, so a **cold** prefill above ~70k tokens can 524 (measured: 108k tokens =
55 s cold on the heavy model, 1.4 s cached). Streaming keeps the connection alive after
the first token; large cold prefills are the risk.

---

## 5. Observability contract

Two JSON-lines records per request, written to files (and to **stdout** when
`GATEWAY_LOG_STDOUT=1`, which is how you want it in a cluster):

* **access** — every request including rejections, except a `/health` or `/readyz`
  that answered 200 (two probes every 15 s is ~11.5k lines a day, and
  `access_log.sh --stats` reads only the last 500):
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

### Metrics (Prometheus, on `GATEWAY_METRICS_PORT` only)

| metric | labels | what it answers |
|---|---|---|
| `gateway_requests_total` | `status`, `key`, `path` | traffic, error mix, who is being refused |
| `gateway_tokens_total` | `kind`, `key`, `backend` | spend per client; `kind` = prompt \| cached \| completion \| charged |
| `gateway_upstream_seconds` | `backend`, `stream` | latency: queue + prefill + decode + client drain |
| `gateway_upstream_inflight` | `backend` | how many requests are pinned to an engine right now |
| `gateway_quota_denials_total` | `reason` | rate_limit / daily_token_cap, kept separate from other 429s |

Three rules that keep the cardinality finite — every label above is chosen by the gateway,
never taken from client input:

* `/health` and `/readyz` are not counted at all (the kubelet would out-produce your traffic).
  A probe that answers 503 *is* logged, so a dead engine is still visible in both systems.
* Unknown paths collapse to `path="other"`, and the model id never becomes a label. The exact
  model, client IP and user agent are in the log records above if you need them.
* `backend` is the routing needle from `GATEWAY_BACKENDS`, which today is 1:1 with a model.

Token kinds overlap on purpose (`prompt` includes `cached`; `charged` is the §3 quota formula):
**never sum `gateway_tokens_total` across kinds.**

The Grafana dashboard built on these metrics — plus the log panels that read the records above
out of Loki — is `kube/monitoring/grafana/dashboards/llm-gateway.json` in the turingpi repo.

---

## 6. Configuration reference

| env | default | meaning |
|---|---|---|
| `GATEWAY_PORT` / `GATEWAY_HOST` (or `--port` / `--host`) | 8000 / 127.0.0.1 | listen address |
| `GATEWAY_BACKENDS` (or `--backends`) | `27b=http://127.0.0.1:8100,35b=http://127.0.0.1:8101` | `needle=url` list; needle matched against model id |
| `GATEWAY_BACKEND_KEY` | – | engine key forwarded upstream (required) |
| `GATEWAY_KEYS_FILE` | `~/.llm-gateway/keys.json` | client keys (re-read per request) |
| `GATEWAY_USAGE_DIR` | `~/.llm-gateway` | state/log directory |
| `GATEWAY_LOG_STDOUT` | `0` | mirror records to stdout |
| `GATEWAY_METRICS_PORT` | `0` | Prometheus listener port (9091 in the cluster); `0` = no metrics |
| `GATEWAY_METRICS_ADDR` | `127.0.0.1` | only raise it to `0.0.0.0` where the scraper reaches the pod IP |
| `GATEWAY_MAX_TOKENS` | 8192 | output clamp (`k8s/configmap.yaml` raises it to 16384 for the cluster) |
| `GATEWAY_MAX_PROMPT_CHARS` | 1 200 000 | prompt ceiling |
| `GATEWAY_MAX_BODY_BYTES` | 16 MB | body ceiling |
| `GATEWAY_RATE_PER_MIN` | 120 | per-key rate limit |
| `GATEWAY_DAILY_TOKEN_CAP` | 200 000 000 | per-key compute tokens/day |
| `GATEWAY_CORS_ORIGIN` | off | CORS origin |
| `GATEWAY_PROXY_NO_AUTH` | `0` | **testing only** |

Keys are a plain JSON file, re-read on **every request** — revoke/add takes effect on the
next request, no restart:

```json
{"keys": [{"name": "laptop", "key": "<openssl rand -hex 20>", "created": "2025-01-01T00:00:00Z"}]}
```

In a cluster, keys live in the k8s Secret (see `k8s/README.md`): edit the JSON, re-apply
the Secret, done — no redeploy. `keys.py` (local CLI minter) was removed; mint with
`openssl rand -hex 20`.

A file that fails to parse fails **closed**: every request gets a 401 and nothing is
cached, which is what you want, but it looks like every client has the wrong key. One
line a minute on the log says `keys file unreadable …` so you can tell the two apart.

> **Renamed in this release:** the env prefix is now `GATEWAY_*` (was `BENCH_*`) and the
> state dir is `~/.llm-gateway` (was `~/.bench-usage`). Old `BENCH_*` names still work for
> one release; to keep existing keys/history run `mv ~/.bench-usage ~/.llm-gateway`.

---

## 7. Running it

```bash
uv sync --group dev
GATEWAY_BACKEND_KEY=... GATEWAY_METRICS_PORT=9091 uv run api_proxy.py --port 8000 --host 0.0.0.0 \
  --backends "next=http://<mac>:8100,35b=http://<mac>:8101"
./scripts/smoke_test.sh http://localhost:8000 <client-key> next   # behaviour
uv run pytest -q                                                   # /metrics + quota accounting
uv run ruff format --check . && uv run ruff check .                # style
```

`.github/workflows/ci.yml` runs the last three lines on every push to `main` and on every PR.
The dev group (`pytest`, `ruff`) is what `uv sync --group dev` installs; `uv sync` alone gives
you the runtime only.

The smoke test needs a live engine; `tests/test_metrics.py` ships its own fake one, so it runs
anywhere. With `GATEWAY_METRICS_PORT` set, `curl localhost:9091/metrics` shows the counters after
the first request.

Docker/k8s: `Dockerfile` + `k8s/` give a Deployment, Service, ConfigMap, Secret example,
PDB and a Prometheus `ServiceMonitor` (for operator installs only — the checked-in
deployments scrape with pod annotations). `k8s/README.md` explains what still has to
change before it is production-correct (see below).

---

## 8. Known gaps — the work you are inheriting

Ordered by how much they matter for a cluster deployment:

1. **Quota state is in-process.** Two replicas = two independent counters, and a restart
   resets the daily cap. Move `check_quota`/`add_tokens` to Redis (or Postgres) with a
   UTC-day key. This is the single biggest correctness issue.
2. **`/metrics` — done, and it stays off the app port.** `prometheus_client` counters, a latency
   histogram, an in-flight gauge and a quota-denial counter, served by a second listener on
   `GATEWAY_METRICS_PORT` (off by default): the app port is public and the series name every
   key. Labels are chosen by the gateway only — a label built from a client-supplied model id
   or URL is unbounded cardinality. See §5 for the list and §7 for how to scrape it.
3. **Keys in a Secret, hot-reload.** Today it is a file re-read per request — mount a
   k8s Secret at `GATEWAY_KEYS_FILE` (or add a control-plane endpoint for key CRUD).
   Never ship `keys.json` in an image.
4. **Stateless log shipping.** `GATEWAY_LOG_STDOUT=1` is wired; make the Deployment emit
   only stdout (no emptyDir needed) so promtail/fluent-bit picks it up.
5. **Per-key policy**: model allowlist (a "laptop" key probably should not spend the
   256k-context heavy model) and per-key max concurrency.
6. **Tests**: `tests/test_metrics.py` covers the metrics path end to end against a fake
   upstream (non-stream + SSE usage, 401/429/500, probes not counted, metrics absent from
   the app port). Still missing: auth matrix, routing, clamps, quota accounting with cached
   tokens, CORS preflight.
7. **No failover between engines.** `/readyz` already probes each backend
   (`GET /v1/models`, 2 s timeout, cached 10 s) and reports ready if at least one answers,
   so stopping one engine does not drain the gateway — but a request for a model on the
   engine that is down still arrives, gets pinned, and answers 502. Two engines on one
   host is a single point of failure with extra steps: the missing piece is refusing or
   rerouting those calls (per-model readiness, or one bounded retry on the other engine),
   not a better probe.
8. **Timeouts**: the upstream call now waits 5 s to connect and 1800 s to read. The
   open question is a *total* deadline: an engine that trickles bytes can still hold a
   request (and its thread) for 30 minutes. Behind an ingress you also want SSE no-buffer
   settings (Traefik: disable compression/flush interval) and read timeouts ≥ the longest
   generation you allow.
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
* **Metrics on a side port, never the public one.** The series name every key and its token
  counts, and the ingress forwards every path on the app port — so there is no `/metrics`
  there at all. Prometheus scrapes the pod directly.
