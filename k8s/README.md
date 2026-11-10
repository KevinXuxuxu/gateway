# Deploying the gateway in k3s

> The `k8s/*.yaml` files this page applies are **not tracked by git** (`.gitignore` excludes
> `*.yaml`), so a clone has this README only. The manifests actually applied to the cluster are
> versioned in the turingpi repo under `kube/public/llm-gateway/`; the ones here are the local
> working copies. Keep any re-created manifest sanitised: no real hostnames, tunnel addresses
> or keys.

```bash
kubectl create namespace llm
kubectl -n llm apply -f configmap.yaml
# create the secret out-of-band (never commit keys):
KEY=$(openssl rand -hex 20)                    # the client key; hand it to your client
printf '{"keys": [{"name": "laptop", "key": "%s", "created": "%s"}]}\n' \
  "$KEY" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > keys.json
kubectl -n llm create secret generic llm-gateway-keys \
  --from-literal=engine-key="$(cat ~/.llm-gateway/engine.key)" \
  --from-file=keys.json=keys.json
rm keys.json
kubectl -n llm apply -f deployment.yaml -f service.yaml -f pdb.yaml
```

Then point your existing ingress/cloudflared at `llm-gateway.llm.svc:80`.

## Updating keys (no redeploy)

The gateway re-reads `keys.json` on every request, and a mounted Secret (no `subPath`!)
propagates into the pod within ~a minute. Edit your keys.json (add or remove entries),
then re-apply the Secret in place:

```bash
kubectl -n llm create secret generic llm-gateway-keys \
  --from-literal=engine-key="$(cat ~/.llm-gateway/engine.key)" \
  --from-file=keys.json=keys.json \
  --dry-run=client -o yaml | kubectl apply -f -
```

No pod restart, no rollout. Do NOT mount the Secret with `subPath:` — that freezes the
file at pod start and updates never arrive.

## Metrics

`GET /metrics` lives on its **own port**: 9091, enabled with `GATEWAY_METRICS_PORT` (default 0 =
off) and bound to `GATEWAY_METRICS_ADDR` (default 127.0.0.1). It is deliberately absent from
the app port: that one is fronted by your ingress or tunnel, and the series name every key
and its token counts.

Two scrape paths — pick one, or you collect every sample twice:

* plain Prometheus: keep the `prometheus.io/{scrape,port,path}` pod annotations
  (`k8s/deployment.yaml`); a `kubernetes-pods` job that honours them needs no other config.
* prometheus-operator: delete those annotations and apply `k8s/servicemonitor.yaml`.

Check it before trusting any panel:

```bash
kubectl -n llm port-forward deploy/llm-gateway 9091:9091 &
curl -s localhost:9091/metrics | grep '^gateway_'      # counters appear after the first request
```

If the target never appears in Prometheus → Status → Targets, the annotations are not being read
(no CNI/Prometheus honouring them, or an image without `/metrics` so the scrape errors instead).
The fallback is a `static_configs` job for the pod port 9091 — one scrape path per deployment,
never both.

The Grafana dashboard (Prometheus + Loki panels) is in the turingpi repo at
`kube/monitoring/grafana/dashboards/llm-gateway.json`: Dashboards → Import → paste, then pick
your Prometheus and Loki datasources. It needs `GATEWAY_LOG_STDOUT=1` for the four log panels.

## Things that WILL bite you

1. **Reachability of the Mac.** `BACKENDS` must use an address the cluster can reach
   (WireGuard/Tailscale preferred). The MTPLX engines bind `127.0.0.1` by default — you
   must rebind them (`--host <overlay-ip>`) and restrict with `pf`/firewall to cluster
   nodes only. Keep `--api-key` on the engines as a second factor.
2. **readinessProbe cannot send a real key** in a manifest without leaking it. Solved:
   `/readyz` is unauthenticated and probes each upstream `GET /v1/models` (2 s timeout,
   result cached 10 s) — **ready if at least one backend answers**, so stopping one
   engine does not drain the gateway. `/health` stays liveness.
3. **SSE through the ingress.** Traefik: disable compression on `text/event-stream`, set
   `flushInterval`, and raise read timeouts — otherwise long generations buffer or 504.
4. **No shared quota state** until §8.1 is done: one replica only.
5. **State.** `usage.jsonl`/`access.jsonl` live on the `llm-gateway-state` PVC (k8s/pvc.yaml)
   and survive restarts; `usage_report.py` can run against it via `kubectl exec`.
   Keep `GATEWAY_LOG_STDOUT=1` anyway — the container log is what you ship to Loki.
