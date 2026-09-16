#!/usr/bin/env bash
# (Re)starts the agent-api server. Canonical restart path — the watchdog cron
# calls this rather than inlining its own recipe.
# The dispatcher subagent is supervised by the agent-api-watchdog cron job and
# will be respawned automatically within ~5 minutes if it is missing.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDFILE="$DIR/server.pid"

# Stop any previous server: pidfile first, pattern as backup.
# The [a] trick keeps pkill from matching this script's own command line.
if [ -f "$PIDFILE" ]; then
  kill "$(cat "$PIDFILE")" 2>/dev/null || true
  rm -f "$PIDFILE"
fi
pkill -f "[a]gent-api/server.py" 2>/dev/null || true
# Also catch servers started with a relative path (cwd inside the project).
for pid in $(pgrep -f "^python3 server.py$" 2>/dev/null); do
  if [ "$(readlink -f /proc/$pid/cwd 2>/dev/null)" = "$DIR" ]; then
    kill "$pid" 2>/dev/null || true
  fi
done
sleep 1

cd "$DIR"
nohup python3 server.py > server.log 2>&1 &
echo $! > "$PIDFILE"
echo "server started (pid $(cat "$PIDFILE")), waiting for health check..."

for _ in $(seq 1 20); do
  if curl -s -m 2 http://127.0.0.1:8787/health | grep -q '"ok": true'; then
    echo "OK: agent-api healthy on 127.0.0.1:8787"
    exit 0
  fi
  sleep 1
done

echo "FAIL: server did not become healthy. Last log lines:"
tail -20 "$DIR/server.log"
rm -f "$PIDFILE"
exit 1
