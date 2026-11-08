# Deploying the gateway in k3s

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

## Things that WILL bite you

1. **Reachability of the Mac.** `BACKENDS` must use an address the cluster can reach
   (WireGuard/Tailscale preferred). The MTPLX engines bind `127.0.0.1` by default — you
   must rebind them (`--host <overlay-ip>`) and restrict with `pf`/firewall to cluster
   nodes only. Keep `--api-key` on the engines as a second factor.
2. **readinessProbe cannot send a real key** in a manifest without leaking it. Solved:
   `/readyz` is unauthenticated and probes each upstream `GET /v1/models` (2 s timeout,
   result cached 10 s), so a dead Mac drains traffic instead of returning 502. `/health`
   stays liveness.
3. **SSE through the ingress.** Traefik: disable compression on `text/event-stream`, set
   `flushInterval`, and raise read timeouts — otherwise long generations buffer or 504.
4. **No shared quota state** until §8.1 is done: one replica only.
5. **State.** `usage.jsonl`/`access.jsonl` live on the `llm-gateway-state` PVC (k8s/pvc.yaml)
   and survive restarts; `usage_report.py` can run against it via `kubectl exec`.
   Keep `GATEWAY_LOG_STDOUT=1` anyway — the container log is what you ship to Loki.
