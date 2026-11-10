#!/bin/bash
# Tail the proxy access log (every request, accepted or rejected). Needs jq, and
# reads the log file - so run it where the file is (a workstation, or next to the
# PVC copy), not inside the pod: the image ships neither jq nor this script.
#   ./access_log.sh            follow, one compact line per request
#   ./access_log.sh --errors   only rejected requests (status >= 400)
#   ./access_log.sh --stats    per-status / per-key counters for the last 500
LOG="${GATEWAY_USAGE_DIR:-${BENCH_USAGE_DIR:-$HOME/.llm-gateway}}/access.jsonl"
case "${1:-}" in
  --errors)
    tail -f "$LOG" | jq -c 'select(.status >= 400) | {ts,client,status,reason,model,key,key_prefix}' ;;
  --stats)
    tail -500 "$LOG" | jq -r '[.status, .key] | @tsv' | sort | uniq -c | sort -rn | head -20 ;;
  *)
    tail -f "$LOG" | jq -c '{ts,client,status,reason,key,model,ms,stream}' ;;
esac
