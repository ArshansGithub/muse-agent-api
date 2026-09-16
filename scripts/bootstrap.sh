#!/usr/bin/env bash
# (Re)starts the agent-api server.
# The dispatcher subagent is supervised by the agent-api-watchdog cron job and
# will be respawned automatically within ~5 minutes if it is missing.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Kill stale servers. The [a] trick keeps pkill from matching this script itself.
pkill -f "[a]gent-api/server.py" 2>/dev/null || true
sleep 1

cd "$DIR"
nohup python3 server.py > server.log 2>&1 &
echo "server started, waiting for health check..."

for _ in $(seq 1 20); do
  if curl -s -m 2 http://127.0.0.1:8787/health | grep -q '"ok": true'; then
    echo "OK: agent-api healthy on 127.0.0.1:8787"
    exit 0
  fi
  sleep 1
done

echo "FAIL: server did not become healthy. Last log lines:"
tail -20 "$DIR/server.log"
exit 1
