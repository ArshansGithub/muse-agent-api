#!/usr/bin/env bash
# Health overview for the agent-api bridge.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== server =="
if curl -s -m 5 http://127.0.0.1:8787/health | grep -q '"ok": true'; then
  echo "healthy on 127.0.0.1:8787"
else
  echo "DOWN (run ./scripts/bootstrap.sh)"
fi

echo
echo "== dispatcher agent =="
if [ -f "$DIR/dispatcher.scanlog" ]; then
  age=$(( $(date +%s) - $(tail -1 "$DIR/dispatcher.scanlog") ))
  if [ "$age" -gt 600 ]; then
    echo "STALE: last proof-of-life ${age}s ago (watchdog should respawn within 5m)"
  else
    echo "alive (last scan ${age}s ago)"
  fi
else
  echo "scanlog MISSING: dispatcher agent is down (watchdog should respawn within 5m)"
fi

echo
echo "== queues =="
echo "queue:        $(ls "$DIR/queue" 2>/dev/null | wc -l) waiting"
echo "processing:   $(ls "$DIR/processing" 2>/dev/null | wc -l) in flight"
echo "responses:    $(ls "$DIR/responses" 2>/dev/null | wc -l) total served"
echo "dead_letters: $(ls "$DIR/dead_letters" 2>/dev/null | wc -l) archived"

echo
echo "== latest responses =="
for f in $(ls -t "$DIR/responses" 2>/dev/null | head -3); do
  python3 -c "import json;d=json.load(open('$DIR/responses/$f'));print(' ', d.get('id'), '->', d.get('status'))" 2>/dev/null
done
