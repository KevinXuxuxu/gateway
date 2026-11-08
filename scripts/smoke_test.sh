#!/bin/bash
# End-to-end check of a deployed gateway.
#   scripts/smoke_test.sh <base-url> <client-key> [model-needle]
# Exits non-zero on the first failure; prints one line per check.
#
# Note: JSON bodies are built with printf and passed as a single variable, never as
# nested double-quoted strings — `-d "{\"a\":\"$X\"}"` inside $(...) gets re-split by
# bash and the gateway (correctly) answers 400 bad_json.
set -u
BASE=${1:-http://localhost:8000}
KEY=${2:-}
NEEDLE=${3:-}
fail=0

chk() { # chk <label> <expected> <actual>
  if [ "$2" = "$3" ]; then printf "  ok    %-34s %s\n" "$1" "$3"
  else printf "  FAIL  %-34s expected %s got %s\n" "$1" "$2" "$3"; fail=1; fi
}

chk "health (no auth)"            200 "$(curl -s -o /dev/null -w '%{http_code}' -m 5  "$BASE/health")"
chk "models without key rejected" 401 "$(curl -s -o /dev/null -w '%{http_code}' -m 5  "$BASE/v1/models")"
chk "models with key"             200 "$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H "Authorization: Bearer $KEY" "$BASE/v1/models")"
chk "bad key rejected"            401 "$(curl -s -o /dev/null -w '%{http_code}' -m 5  -H 'Authorization: Bearer nope' "$BASE/v1/models")"
chk "unknown path"                404 "$(curl -s -o /dev/null -w '%{http_code}' -m 5  -H "Authorization: Bearer $KEY" "$BASE/nope")"

MODELS=$(curl -s -m 10 -H "Authorization: Bearer $KEY" "$BASE/v1/models")
MODEL=$(printf '%s' "$MODELS" | python3 -c "
import json,sys
d=json.load(sys.stdin).get('data',[])
n='${NEEDLE}'
picked=[m['id'] for m in d if n and n in m['id']]
ids=picked or [m['id'] for m in d]
print(ids[0] if ids else '')")
if [ -z "$MODEL" ]; then echo "  FAIL  no model advertised"; exit 1; fi
echo "  info  using model: $MODEL"

post() { # post <path> <payload> [extra curl args...]
  local path=$1 payload=$2; shift 2
  curl -s -o /dev/null -w '%{http_code}' -m 300 -H "Authorization: Bearer $KEY" \
       -H 'Content-Type: application/json' "$@" -d "$payload" "$BASE$path"
}

PAYLOAD=$(printf '{"model":"%s","messages":[{"role":"user","content":"say ok"}],"max_tokens":32}' "$MODEL")
chk "chat completion"            200 "$(post /v1/chat/completions "$PAYLOAD")"

PAYLOAD_S=$(printf '{"model":"%s","messages":[{"role":"user","content":"say ok"}],"max_tokens":16,"stream":true}' "$MODEL")
chk "streaming (SSE)"            200 "$(post /v1/chat/completions "$PAYLOAD_S" -N)"

PAYLOAD_A=$(printf '{"model":"%s","max_tokens":16,"messages":[{"role":"user","content":"say ok"}]}' "$MODEL")
chk "anthropic /v1/messages"     200 "$(post /v1/messages "$PAYLOAD_A" -H 'anthropic-version: 2023-06-01')"

chk "unknown model"              404 "$(post /v1/chat/completions '{"model":"definitely-not-a-model","messages":[{"role":"user","content":"hi"}]}')"

# large bodies must go via a file: a 1.3 MB argv exceeds ARG_MAX
BIGF=$(mktemp)
python3 -c "import json;print(json.dumps({'model':'$MODEL','messages':[{'role':'user','content':'x'*1300000}]}))" > "$BIGF"
chk "oversized prompt rejected"  413 "$(curl -s -o /dev/null -w '%{http_code}' -m 120 \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  --data-binary "@$BIGF" "$BASE/v1/chat/completions")"
rm -f "$BIGF"

if [ $fail -eq 0 ]; then echo "SMOKE OK"; else echo "SMOKE FAILED"; fi
exit $fail
