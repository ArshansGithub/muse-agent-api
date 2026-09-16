#!/usr/bin/env python3
"""OpenAI Responses-API-compatible shim backed by muse agent workers.

Flow:
  POST /v1/responses -> request JSON written to queue/{id}.json
  A dispatcher subagent notices the new file and spawns a stateless worker
  subagent, which writes an OpenAI response object to responses/{id}.json.
  This server long-polls for that file (up to TIMEOUT) and returns it.
  With "stream": true, the response is delivered as SSE using the Responses
  API event names (chunked server-side; token streaming is faked in chunks).

The client (e.g. Claude Code via CLIProxyAPI) owns the agentic loop: each
HTTP request is one model turn. Stateless workers are the correct semantics.
"""
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.expanduser("~/workspace/agent-api")
QUEUE = os.path.join(BASE, "queue")
RESP = os.path.join(BASE, "responses")
TIMEOUT = 900  # seconds to long-poll for a worker response
PORT = 8787
MODEL_ID = "muse-spark"

for d in (QUEUE, RESP):
    os.makedirs(d, exist_ok=True)


def _load_token():
    tok = os.environ.get("AGENT_API_TOKEN")
    if tok and tok.strip():
        return tok.strip()
    p = os.path.join(BASE, ".token")
    if os.path.exists(p):
        with open(p) as f:
            return f.read().strip()
    return None


API_TOKEN = _load_token()


def _err(message, code="server_error", status=500):
    return status, {"error": {"message": message, "type": code, "param": None, "code": code}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, code, obj):
        self._send(code, json.dumps(obj))

    def _authorized(self):
        if not API_TOKEN:
            return False
        return self.headers.get("Authorization", "") == "Bearer " + API_TOKEN

    def _require_auth(self):
        if not self._authorized():
            self._send_json(401, {"error": {
                "message": "invalid or missing API key",
                "type": "invalid_request_error", "param": None,
                "code": "invalid_api_key"}})
            return False
        return True

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            self._send_json(200, {"ok": True})
        elif path == "/v1/models":
            if not self._require_auth():
                return
            self._send_json(200, {
                "object": "list",
                "data": [{"id": MODEL_ID, "object": "model",
                          "created": 1757980000, "owned_by": "muse"}],
            })
        else:
            self._send_json(*_err("unknown endpoint", "not_found", 404))

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/v1/responses":
            self._send_json(*_err("unknown endpoint", "not_found", 404))
            return
        if not self._require_auth():
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json(*_err("request body is not valid JSON", "invalid_request", 400))
            return
        if "input" not in req:
            self._send_json(*_err("missing required field: input", "invalid_request", 400))
            return

        rid = "resp_" + uuid.uuid4().hex[:24]
        req["_request_id"] = rid
        with open(os.path.join(QUEUE, rid + ".json"), "w") as f:
            json.dump(req, f)

        rpath = os.path.join(RESP, rid + ".json")
        deadline = time.time() + TIMEOUT
        while time.time() < deadline:
            if os.path.exists(rpath):
                try:
                    with open(rpath) as f:
                        resp = json.load(f)
                except (json.JSONDecodeError, OSError):
                    time.sleep(0.5)
                    continue
                if req.get("stream"):
                    self._send_sse(resp)
                else:
                    self._send_json(200, resp)
                return
            time.sleep(0.5)

        self._send_json(*_err("worker did not respond in time", "timeout", 504))

    # ---- SSE (Responses API event names, chunked server-side) ----
    def _send_sse(self, resp):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def ev(name, data):
            return ("event: %s\ndata: %s\n\n" % (name, json.dumps(data))).encode()

        w = self.wfile.write
        rid = resp.get("id", "resp_unknown")
        w(ev("response.created", {
            "type": "response.created",
            "response": {"id": rid, "object": "response", "status": "in_progress",
                         "model": resp.get("model", MODEL_ID)},
        }))
        out_idx = 0
        for item in resp.get("output", []):
            itype = item.get("type")
            w(ev("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": out_idx,
                "item": {"id": item.get("id"), "type": itype, "status": "in_progress"},
            }))
            if itype == "message":
                for ci, part in enumerate(item.get("content", [])):
                    if part.get("type") != "output_text":
                        continue
                    w(ev("response.content_part.added", {
                        "type": "response.content_part.added",
                        "item_id": item.get("id"), "output_index": out_idx,
                        "content_index": ci,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    }))
                    text = part.get("text", "")
                    for j in range(0, len(text), 60):
                        w(ev("response.output_text.delta", {
                            "type": "response.output_text.delta",
                            "item_id": item.get("id"), "output_index": out_idx,
                            "content_index": ci, "delta": text[j:j + 60],
                        }))
                    w(ev("response.output_text.done", {
                        "type": "response.output_text.done",
                        "item_id": item.get("id"), "output_index": out_idx,
                        "content_index": ci,
                        "text": text,
                    }))
                    w(ev("response.content_part.done", {
                        "type": "response.content_part.done",
                        "item_id": item.get("id"), "output_index": out_idx,
                        "content_index": ci,
                        "part": {"type": "output_text", "text": text, "annotations": []},
                    }))
            elif itype == "function_call":
                args = item.get("arguments", "")
                if not isinstance(args, str):
                    args = json.dumps(args)
                for j in range(0, len(args), 60):
                    w(ev("response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "item_id": item.get("id"), "output_index": out_idx,
                        "delta": args[j:j + 60],
                    }))
            w(ev("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": out_idx, "item": item,
            }))
            out_idx += 1
        w(ev("response.completed", {"type": "response.completed", "response": resp}))


if __name__ == "__main__":
    if not API_TOKEN:
        raise SystemExit("no API token: set AGENT_API_TOKEN or write it to .token in " + BASE)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("agent-api (responses format) listening on 127.0.0.1:%d" % PORT, flush=True)
    server.serve_forever()
