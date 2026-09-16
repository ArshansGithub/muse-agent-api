#!/usr/bin/env python3
"""Local CONNECT relay: lets cloudflared reach Cloudflare edge IPs through the
sandbox's authenticating egress HTTP proxy (which cloudflared won't use itself).

Listens on 127.0.0.1:17844. Each inbound connection is forwarded via
`CONNECT <edge-ip>:7844` through $HTTPS_PROXY, round-robin across edge IPs.
cloudflared is pointed at it with `--edge 127.0.0.1:17844`.
"""
import base64
import itertools
import os
import socket
import threading
import urllib.parse

EDGE_IPS = [
    "198.41.192.67", "198.41.192.57", "198.41.192.227", "198.41.192.77",
    "198.41.192.27", "198.41.192.167", "198.41.192.107", "198.41.192.47",
    "198.41.192.37", "198.41.192.7",
    "198.41.200.233", "198.41.200.13", "198.41.200.43", "198.41.200.113",
    "198.41.200.73", "198.41.200.53", "198.41.200.63", "198.41.200.23",
    "198.41.200.33", "198.41.200.193",
]
EDGE_PORT = 7844
LISTEN = ("127.0.0.1", 17844)

proxy = urllib.parse.urlparse(os.environ["HTTPS_PROXY"])
PROXY_HOST, PROXY_PORT = proxy.hostname, proxy.port or 8080
PROXY_AUTH = base64.b64encode(
    f"{proxy.username}:{proxy.password}".encode()).decode() if proxy.username else None

picker = itertools.cycle(EDGE_IPS)


def splice(a, b):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client):
    edge_ip = next(picker)
    try:
        up = socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=15)
        req = (f"CONNECT {edge_ip}:{EDGE_PORT} HTTP/1.1\r\n"
               f"Host: {edge_ip}:{EDGE_PORT}\r\n")
        if PROXY_AUTH:
            req += f"Proxy-Authorization: Basic {PROXY_AUTH}\r\n"
        req += "\r\n"
        up.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = up.recv(4096)
            if not chunk:
                raise OSError("proxy closed during CONNECT")
            resp += chunk
        if b" 200 " not in resp.split(b"\r\n", 1)[0]:
            raise OSError(f"proxy refused CONNECT: {resp[:80]!r}")
        t1 = threading.Thread(target=splice, args=(client, up), daemon=True)
        t2 = threading.Thread(target=splice, args=(up, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except OSError:
        pass
    finally:
        client.close()


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN)
    srv.listen(64)
    print(f"relay listening on {LISTEN[0]}:{LISTEN[1]} -> edge :{EDGE_PORT} via proxy",
          flush=True)
    while True:
        client, _ = srv.accept()
        threading.Thread(target=handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
