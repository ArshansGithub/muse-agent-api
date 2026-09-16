#!/bin/bash
# (Re)start the Cloudflare tunnel stack for agent-api:
#   1. Refresh Cloudflare edge IPs via DNS-over-HTTPS (the sandbox resolver
#      cannot do the SRV lookup cloudflared needs) and rewrite relay.py's list.
#   2. Start the local CONNECT relay (lets cloudflared's data plane traverse
#      the egress HTTP proxy, which cloudflared won't use on its own).
#   3. Start cloudflared pointed at the relay via --edge 127.0.0.1:17844.
set -u
DIR=/home/hatch/.cloudflared
RELAY=$DIR/relay.py

# 1. Refresh edge IPs via DoH
IPS=$(curl -s -m 15 "https://cloudflare-dns.com/dns-query?name=_v2-origintunneld._tcp.argotunnel.com&type=SRV" \
  -H "accept: application/dns-json" | python3 -c "
import json,sys,urllib.request
d=json.load(sys.stdin)
hosts=[a['data'].split()[-1] for a in d.get('Answer',[])]
ips=[]
for h in hosts:
    q=urllib.request.Request(f'https://cloudflare-dns.com/dns-query?name={h}&type=A', headers={'accept':'application/dns-json'})
    dd=json.load(urllib.request.urlopen(q, timeout=15))
    ips += [a['data'] for a in dd.get('Answer',[])]
print(' '.join(ips))")
if [ -z "$IPS" ]; then echo "edge IP refresh failed, keeping old list"; else
  python3 - "$RELAY" "$IPS" <<'EOF'
import re,sys
path, ips = sys.argv[1], sys.argv[2].split()
src = open(path).read()
new = "EDGE_IPS = [\n" + "\n".join(f'    "{ip}",' for ip in ips) + "\n]"
open(path,'w').write(re.sub(r"EDGE_IPS = \[.*?\]", new, src, flags=re.S))
print(f"edge IPs refreshed: {len(ips)}")
EOF
fi

# 2+3. Restart relay and tunnel
pkill -f "cloudflared tunnel" 2>/dev/null
pkill -f "cloudflared/relay.py" 2>/dev/null
sleep 1
echo "--- restart $(date -u +%FT%TZ) ---" >> $DIR/relay.log
nohup python3 $RELAY >> $DIR/relay.log 2>&1 &
sleep 1
echo "--- restart $(date -u +%FT%TZ) ---" >> $DIR/tunnel.log
nohup cloudflared tunnel --edge 127.0.0.1:17844 run agent-api >> $DIR/tunnel.log 2>&1 &

# Wait for a fresh registration (after our restart marker)
for i in $(seq 1 24); do
  if awk '/--- restart/{found=1} found && /Registered tunnel connection/{ok=1} END{exit !ok}' $DIR/tunnel.log; then
    echo "tunnel registered OK"
    exit 0
  fi
  sleep 5
done
echo "tunnel did not register within 120s"
tail -5 $DIR/tunnel.log
exit 1
