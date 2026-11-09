# Handoff tasks (for the agent picking this up)

Everything here is optional-but-ordered. Each item has an acceptance check so you can
verify without asking anyone. Read `README.md` first; §8 is the rationale.

## P0 — required before running more than one replica

- [ ] **Move quota state out of process.** `check_quota()`/`add_tokens()` currently use a
      module dict. Replace with Redis (preferred, TTL = end of UTC day) or Postgres.
      *Accept:* two replicas share one counter; restarting a pod does not reset the day's
      usage; `GATEWAY_DAILY_TOKEN_CAP` is still enforced on `(prompt − cached) + completion`.

## P1 — observability (the reason this moved to k8s)

- [x] **`/metrics` endpoint** (`prometheus_client`): `gateway_requests_total{status,key,path}`,
      `gateway_tokens_total{kind="prompt|cached|completion|charged",key,backend}`,
      histogram `gateway_upstream_seconds{backend,stream}`, gauge
      `gateway_upstream_inflight{backend}`, counter `gateway_quota_denials_total{reason}`.
      Served on a **second listener** (`GATEWAY_METRICS_PORT`, default 0 = off, and
      `GATEWAY_METRICS_ADDR` default 127.0.0.1), never on the app port: that one is public
      and the series name every key. Labels are gateway-chosen (engine needle, not the client's
      model id) so cardinality stays finite; `/health` and `/readyz` are not counted.
      *Accept:* `curl :9091/metrics` shows them (`tests/test_metrics.py` asserts it); the
      deployment's `prometheus.io/{scrape,port,path}` annotations do the scraping, and
      `k8s/servicemonitor.yaml` is the operator-only alternative.
- [ ] **Log-shipping check:** run with `GATEWAY_LOG_STDOUT=1`, confirm access+usage lines
      appear on container stdout as `{"log":"access"|"usage",...}` and reach Loki.
- [ ] **Ship engine telemetry from the model host.** MTPLX writes
      `~/.mtplx/logs/request-log-<port>.jsonl` (TTFT, decode tok/s, cached_tokens,
      prefill_tok_s, thermal) and `~/.mtplx/metrics/flight-<port>.jsonl`. Push those to the
      cluster (promtail on the Mac, or a cron rsync + promtail) and add a dashboard with
      cache-hit-rate and TTFT p95 by model. *Without this you cannot explain latency.*

## P2 — correctness / operability

- [x] **`/readyz`** probes each upstream `GET /v1/models` with a 2 s timeout (cached
      10 s, concurrent). **Ready if at least one backend answers** — stopping one engine
      keeps the gateway serving the other. `/health` stays liveness.
- [x] **Keys from the Secret, hot-reload**: re-read per request; update = re-apply the
      Secret (see `k8s/README.md`), no restart, no rollout. `keys.py` removed — mint with
      `openssl rand -hex 20`. Optional future: `PUT /admin/keys` behind a separate admin
      key (never expose publicly).
- [ ] **Per-key model allowlist + max concurrency** (e.g. the "laptop" key must not use the
      256k heavy model; cap in-flight per key at 2).
- [ ] **Upstream retry**: one bounded retry with backoff for non-streaming requests on
      502/507 only.

## P3 — quality

- [ ] **Tests** (`pytest`, fake upstream in a thread): `tests/test_metrics.py` already runs a
      fake MTPLX upstream and asserts the metrics side (non-stream + SSE usage, 401/429/500,
      probes excluded, no `/metrics` on the app port). Add the rest to that file: auth matrix,
      routing, clamps, cache-aware quota, CORS preflight, oversized body.
      *Accept:* `uv run pytest -q` green; the smoke test alone is not enough.
- [ ] **Ingress tuning doc** for Traefik: disable compression for `text/event-stream`,
      flush interval, read/send timeouts ≥ longest generation (1800 s).
- [ ] **Idempotent model map**: replace needle substring routing with an explicit
      model→deployment map (+ weights/fallback) once there are >2 engines.

## Invariants — do not break these

1. `Authorization: Bearer` **and** `x-api-key` both work (OpenAI and Anthropic clients).
2. Rejections are logged *before* the response, with a stable machine-readable `reason`.
3. Cached prompt tokens are **free** for quota (README §3).
4. `GATEWAY_BACKEND_KEY` is forwarded upstream; client keys never reach the engines.
5. Engine ports stay private; only the gateway is exposed.
