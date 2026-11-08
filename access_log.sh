#!/bin/bash
# Tail the proxy access log (every request, accepted or rejected).
#   scripts/access_log.sh            follow, one compact line per request
#   scripts/access_log.sh --errors   only rejected requests (status >= 400)
#   scripts/access_log.sh --stats    per-status / per-key counters for the last 500
LOG="$HOME/.bench-usage/access.jsonl"
case "${1:-}" in
  --errors)
    tail -f "$LOG" | jq -c 'select(.status >= 400) | {ts,client,status,reason,model,key,key_prefix}' ;;
  --stats)
    tail -500 "$LOG" | jq -r '[.status, .key] | @tsv' | sort | uniq -c | sort -rn | head -20 ;;
  *)
    tail -f "$LOG" | jq -c '{ts,client,status,reason,key,model,ms,stream}' ;;
esac
