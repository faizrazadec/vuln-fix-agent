#!/bin/sh
# Ask the agent something and wait for the answer.
#
#   ./ask.sh "fix vulns in git@github.com:Ember-AI-Engineering/snapdev-backend.git"
#   ./ask.sh -f request.txt          # long prompt from a file
#   A2A_URL=https://agent.example.com/ ./ask.sh "..."   # if fronted by a proxy
#
# Sends returnImmediately so the run survives any HTTP timeout, then polls GetTask
# until it finishes. Prints progress as the agent works.
set -e
# scripts/ live one level below the repo root; operate from the root.
cd "$(dirname "$0")/.."
. ./.env

URL="${A2A_URL:-http://127.0.0.1:9999/}"
: "${A2A_TOKEN:?A2A_TOKEN missing from .env}"

if [ "$1" = "-f" ]; then
  [ -f "$2" ] || { echo "no such file: $2" >&2; exit 1; }
  PROMPT=$(cat "$2")
else
  PROMPT="$*"
fi
[ -n "$PROMPT" ] || { echo "usage: $0 \"your request\"   |   $0 -f file" >&2; exit 1; }

# python3 does the JSON escaping — a prompt with quotes or newlines breaks naive sed.
REQ=$(PROMPT="$PROMPT" python3 -c '
import json, os, uuid
print(json.dumps({"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{
  "message":{"messageId":str(uuid.uuid4()),"role":"ROLE_USER",
             "parts":[{"text":os.environ["PROMPT"]}]},
  "configuration":{"returnImmediately":True}}}))')

RESP=$(curl -sS -X POST "$URL" -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" -H "Authorization: Bearer $A2A_TOKEN" -d "$REQ")

TASK=$(printf '%s' "$RESP" | python3 -c 'import json,sys
d=json.load(sys.stdin)
if "error" in d: print("ERROR:", d["error"].get("message")); raise SystemExit(1)
print(d["result"]["task"]["id"])')

echo "task $TASK — polling (ctrl-c is safe, the run continues)"
SEEN=0
FAILS=0
while :; do
  sleep 10
  # A transient curl/parse blip must NOT kill the poll — the task keeps running in the
  # container regardless. Retry; give up only after many consecutive failures.
  GOT=$(curl -sS -X POST "$URL" -H "Content-Type: application/json" \
    -H "A2A-Version: 1.0" -H "Authorization: Bearer $A2A_TOKEN" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":\"2\",\"method\":\"GetTask\",\"params\":{\"id\":\"$TASK\"}}") || GOT=""

  if printf '%s' "$GOT" | SEEN=$SEEN python3 -c '
import json, os, sys
d = json.load(sys.stdin)
t = d["result"]; st = t["status"]["state"]
hist = t.get("history", [])
seen = int(os.environ["SEEN"])
for m in hist[seen:]:                       # only new progress lines
    print("  " + "".join(p.get("text","") for p in m["parts"])[:400])
print("__SEEN__", len(hist))
print("__STATE__", st)
if st in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED"):
    print("\n--- " + st.replace("TASK_STATE_","") + " ---")
    print("".join(p.get("text","") for p in t["status"]["message"]["parts"]))
' > /tmp/a2a.$$ 2>/dev/null; then
    FAILS=0
  else
    FAILS=$((FAILS + 1))
    rm -f /tmp/a2a.$$
    if [ "$FAILS" -ge 30 ]; then
      echo "poll: 30 consecutive failures (~5 min) — giving up on polling; task may still be running" >&2
      exit 1
    fi
    continue
  fi

  grep -v '^__' /tmp/a2a.$$ || true
  SEEN=$(sed -n 's/^__SEEN__ //p' /tmp/a2a.$$)
  STATE=$(sed -n 's/^__STATE__ //p' /tmp/a2a.$$)
  rm -f /tmp/a2a.$$
  case "$STATE" in *COMPLETED|*FAILED) exit 0 ;; esac
done
