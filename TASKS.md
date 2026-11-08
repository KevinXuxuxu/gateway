# Handoff tasks (for the agent picking this up)

Everything here is optional-but-ordered. Each item has an acceptance check so you can
verify without asking anyone. Read `README.md` first; §8 is the rationale.

## P0 — required before running more than one replica

- [ ] **Move quota state out of process.** `check_quota()`/`add_tokens()` currently use a
      module dict. Replace with Redis (preferred, TTL = end of UTC day) or Postgres.
      *Accept:* two replicas share one counter; restarting a pod does not reset the day's
      usage; `GATEWAY_DAILY_TOKEN_CAP` is still enforced on `(prompt − cached) + completion`.

## P1 — observability (the reason this moved to k8s)

- [ ] **`/metrics` endpoint** (`prometheus_client`): counters
      `gateway_requests_total{status,model,key}`, `gateway_tokens_total{kind="prompt|cached|completion|charged",model}`,
      histogram `gateway_upstream_seconds{model}`, gauge `gateway_inflight`,
      counter `gateway_quota_denials_total{reason}`.
      *Accept:* `curl :8000/metrics` shows them; `k8s/servicemonitor.yaml` scrapes.
- [ ] **Log-shipping check:** run with `GATEWAY_LOG_STDOUT=1`, confirm access+usage lines
      appear on container stdout as `{"log":"access"|"usage",...}` and reach Loki.
- [ ] **Ship engine telemetry from the model host.** MTPLX writes
      `~/.mtplx/logs/request-log-<port>.jsonl` (TTFT, decode tok/s, cached_tokens,
      prefill_tok_s, thermal) and `~/.mtplx/metrics/flight-<port>.jsonl`. Push those to the
      cluster (promtail on the Mac, or a cron rsync + promtail) and add a dashboard with
      cache-hit-rate and TTFT p95 by model. *Without this you cannot explain latency.*

## P2 — correctness / operability

- [x] **`/readyz`** probes each upstream `GET /v1/models` with a 2 s timeout (cached
      10 s), so a dead Mac drains traffic instead of returning 502. `/health` stays
      liveness. Used by the readinessProbe in `k8s/deployment.yaml`.
- [x] **Keys from the Secret, hot-reload**: re-read per request; update = re-apply the
      Secret (see `k8s/README.md`), no restart, no rollout. `keys.py` removed — mint with
      `openssl rand -hex 20`. Optional future: `PUT /admin/keys` behind a separate admin
      key (never expose publicly).
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
4. `GATEWAY_BACKEND_KEY` is forwarded upstream; client keys never reach the engines.
5. Engine ports stay private; only the gateway is exposed.
