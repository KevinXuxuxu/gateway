# Handoff tasks (for the agent picking this up)

Everything here is optional-but-ordered. Each item has an acceptance check so you can
verify without asking anyone. Read `README.md` first; §8 is the rationale.

## P0 — required before running more than one replica

- [ ] **Move quota state out of process.** `check_quota()`/`add_tokens()` currently use a
      module dict. Replace with Redis (preferred, TTL = end of UTC day) or Postgres.
      *Accept:* two replicas share one counter; restarting a pod does not reset the day's
      usage; `BENCH_DAILY_TOKEN_CAP` is still enforced on `(prompt − cached) + completion`.

## P1 — observability (the reason this moved to k8s)

- [ ] **`/metrics` endpoint** (`prometheus_client`): counters
      `gateway_requests_total{status,model,key}`, `gateway_tokens_total{kind="prompt|cached|completion|charged",model}`,
      histogram `gateway_upstream_seconds{model}`, gauge `gateway_inflight`,
      counter `gateway_quota_denials_total{reason}`.
      *Accept:* `curl :8000/metrics` shows them; `k8s/servicemonitor.yaml` scrapes.
- [ ] **Log-shipping check:** run with `BENCH_LOG_STDOUT=1`, confirm access+usage lines
      appear on container stdout as `{"log":"access"|"usage",...}` and reach Loki.
- [ ] **Ship engine telemetry from the model host.** MTPLX writes
      `~/.mtplx/logs/request-log-<port>.jsonl` (TTFT, decode tok/s, cached_tokens,
      prefill_tok_s, thermal) and `~/.mtplx/metrics/flight-<port>.jsonl`. Push those to the
      cluster (promtail on the Mac, or a cron rsync + promtail) and add a dashboard with
      cache-hit-rate and TTFT p95 by model. *Without this you cannot explain latency.*

## P2 — correctness / operability

- [ ] **`/readyz`** that probes each upstream `GET /v1/models` with a 2 s timeout (cached
      10 s), so a dead Mac drains traffic instead of returning 502. Keep `/health` as
      liveness. See `k8s/README.md` item 2.
- [ ] **Keys from the Secret, hot-reload**: already re-read per request — verify a
      `kubectl create secret` + pod restart works, and add an optional
      `PUT /admin/keys` behind a separate admin key (never expose publicly).
- [ ] **Per-key model allowlist + max concurrency** (e.g. the "laptop" key must not use the
      256k heavy model; cap in-flight per key at 2).
- [ ] **Upstream retry**: one bounded retry with backoff for non-streaming requests on
      502/507 only.

## P3 — quality

- [ ] **Tests** (`pytest`, fake upstream in a thread): auth matrix, routing, clamps,
      cache-aware quota, SSE passthrough, CORS preflight, oversized body.
      *Accept:* `pytest -q` green; the smoke test alone is not enough.
- [ ] **Ingress tuning doc** for Traefik: disable compression for `text/event-stream`,
      flush interval, read/send timeouts ≥ longest generation (1800 s).
- [ ] **Idempotent model map**: replace needle substring routing with an explicit
      model→deployment map (+ weights/fallback) once there are >2 engines.

## Invariants — do not break these

1. `Authorization: Bearer` **and** `x-api-key` both work (OpenAI and Anthropic clients).
2. Rejections are logged *before* the response, with a stable machine-readable `reason`.
3. Cached prompt tokens are **free** for quota (README §3).
4. `BENCH_BACKEND_KEY` is forwarded upstream; client keys never reach the engines.
5. Engine ports stay private; only the gateway is exposed.
