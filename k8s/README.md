# Deploying the gateway in k3s

```bash
kubectl create namespace llm
kubectl -n llm apply -f configmap.yaml
# create the secret out-of-band (never commit keys):
python ../keys.py add laptop          # prints the key; put the JSON file in the secret
kubectl -n llm create secret generic llm-gateway-keys \
  --from-literal=engine-key="$(cat ~/.llm-gateway/engine.key)" \
  --from-file=keys.json="$HOME/.llm-gateway/keys.json"
kubectl -n llm apply -f deployment.yaml -f service.yaml -f pdb.yaml
```

Then point your existing ingress/cloudflared at `llm-gateway.llm.svc:80`.

## Things that WILL bite you

1. **Reachability of the Mac.** `BACKENDS` must use an address the cluster can reach
   (WireGuard/Tailscale preferred). The MTPLX engines bind `127.0.0.1` by default — you
   must rebind them (`--host <overlay-ip>`) and restrict with `pf`/firewall to cluster
   nodes only. Keep `--api-key` on the engines as a second factor.
2. **readinessProbe cannot send a real key** in a manifest without leaking it. Either
   (a) accept `/health` as readiness (simplest, does not detect a dead Mac), or
   (b) add an unauthenticated `/readyz` that probes upstreams with a 2s timeout (README
   §8.7). Option (b) is the right answer.
3. **SSE through the ingress.** Traefik: disable compression on `text/event-stream`, set
   `flushInterval`, and raise read timeouts — otherwise long generations buffer or 504.
4. **No shared quota state** until §8.1 is done: one replica only.
5. **State.** `usage.jsonl`/`access.jsonl` live on the `llm-gateway-state` PVC (k8s/pvc.yaml)
   and survive restarts; `usage_report.py` can run against it via `kubectl exec`.
   Keep `GATEWAY_LOG_STDOUT=1` anyway — the container log is what you ship to Loki.
