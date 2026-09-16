#!/usr/bin/env python3
"""OpenAI-compatible shim backed by muse agent workers.

Endpoints:
  POST /v1/responses        (OpenAI Responses API)
  POST /v1/chat/completions (OpenAI Chat Completions API — what CLIProxyAPI's
                             openai-compatibility channel speaks to upstreams;
                             translated to/from the Responses shape internally)
  GET  /v1/models
  GET  /health

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
        if path == "/v1/responses":
            self._handle_responses(req)
        elif path == "/v1/chat/completions":
            self._handle_chat_completions(req)
        else:
            self._send_json(*_err("unknown endpoint", "not_found", 404))

    # ---- shared dispatch: enqueue one worker turn, long-poll the response ----
    def _dispatch(self, req):
        """Returns (response_obj, None) or (None, (status, error_obj))."""
        rid = "resp_" + uuid.uuid4().hex[:24]
        req["_request_id"] = rid
        try:
            with open(os.path.join(QUEUE, rid + ".json"), "w") as f:
                json.dump(req, f)
        except OSError:
            return None, _err("could not enqueue request", "server_error", 500)
        rpath = os.path.join(RESP, rid + ".json")
        deadline = time.time() + TIMEOUT
        while time.time() < deadline:
            if os.path.exists(rpath):
                try:
                    with open(rpath) as f:
                        return json.load(f), None
                except (json.JSONDecodeError, OSError):
                    time.sleep(0.5)
                    continue
            time.sleep(0.5)
        return None, _err("worker did not respond in time", "timeout", 504)

    def _handle_responses(self, req):
        if "input" not in req:
            self._send_json(*_err("missing required field: input", "invalid_request", 400))
            return
        resp, err = self._dispatch(req)
        if err:
            self._send_json(*err)
            return
        if req.get("stream"):
            self._send_sse(resp)
        else:
            self._send_json(200, resp)

    # ---- Chat Completions API (what CLIProxyAPI openai-compatibility speaks) ----
    @staticmethod
    def _cc_text(content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(p.get("text", "") for p in content
                            if isinstance(p, dict) and p.get("type") == "text")
        return ""

    def _handle_chat_completions(self, req):
        messages = req.get("messages")
        if not isinstance(messages, list) or not messages:
            self._send_json(*_err("missing required field: messages", "invalid_request", 400))
            return
        instructions, input_items = [], []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role == "system":
                instructions.append(self._cc_text(m.get("content")))
            elif role in ("user", "assistant"):
                input_items.append({"role": role,
                                    "content": self._cc_text(m.get("content"))})
            elif role == "tool":
                input_items.append({"type": "function_call_output",
                                    "call_id": m.get("tool_call_id", ""),
                                    "output": self._cc_text(m.get("content"))})
        tools = []
        for t in req.get("tools") or []:
            fn = (t or {}).get("function", {}) if isinstance(t, dict) else {}
            tools.append({"type": "function", "name": fn.get("name", ""),
                          "description": fn.get("description", ""),
                          "parameters": fn.get("parameters", {})})
        tc = req.get("tool_choice", "auto")
        if isinstance(tc, dict) and tc.get("type") == "function":
            tool_choice = {"type": "function",
                           "name": (tc.get("function") or {}).get("name", "")}
        elif tc in ("auto", "none", "required"):
            tool_choice = tc
        else:
            tool_choice = "auto"
        internal = {
            "input": input_items,
            "instructions": "\n\n".join(instructions) or None,
            "tools": tools or None,
            "tool_choice": tool_choice,
            "max_output_tokens": req.get("max_tokens"),
            "temperature": req.get("temperature"),
            "top_p": req.get("top_p"),
            "model": req.get("model", MODEL_ID),
        }
        internal = {k: v for k, v in internal.items() if v is not None}
        resp, err = self._dispatch(internal)
        if err:
            self._send_json(*err)
            return
        if req.get("stream"):
            self._send_cc_sse(resp, req.get("model", MODEL_ID))
        else:
            self._send_json(200, self._cc_response(resp, req.get("model", MODEL_ID)))

    @staticmethod
    def _cc_tool_calls(resp):
        calls = []
        for item in resp.get("output", []):
            if item.get("type") == "function_call":
                args = item.get("arguments", "")
                if not isinstance(args, str):
                    args = json.dumps(args)
                calls.append({"id": item.get("call_id") or item.get("id", ""),
                              "type": "function",
                              "function": {"name": item.get("name", ""),
                                           "arguments": args}})
        return calls

    @staticmethod
    def _cc_text_out(resp):
        parts = []
        for item in resp.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        parts.append(c.get("text", ""))
        return "".join(parts)

    def _cc_response(self, resp, model):
        text = self._cc_text_out(resp)
        calls = self._cc_tool_calls(resp)
        message = {"role": "assistant", "content": text or None}
        reasoning = resp.get("reasoning")
        if reasoning:
            message["reasoning_content"] = reasoning
        if calls:
            message["tool_calls"] = calls
        if not text and not calls:
            message["content"] = ""
        return {
            "id": "chatcmpl-" + uuid.uuid4().hex[:24],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if calls else "stop",
            }],
            "usage": resp.get("usage") or {},
        }

    def _send_cc_sse(self, resp, model):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())

        def chunk(delta, finish=None):
            return ("data: %s\n\n" % json.dumps({
                "id": cid, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta,
                             "finish_reason": finish}],
            })).encode()

        w = self.wfile.write
        w(chunk({"role": "assistant"}))
        reasoning = resp.get("reasoning")
        if reasoning:
            w(chunk({"reasoning_content": reasoning}))
        text = self._cc_text_out(resp)
        for j in range(0, len(text), 60):
            w(chunk({"content": text[j:j + 60]}))
        for i, tc in enumerate(self._cc_tool_calls(resp)):
            w(chunk({"tool_calls": [{
                "index": i, "id": tc["id"], "type": "function",
                "function": {"name": tc["function"]["name"], "arguments": ""}}]}))
            args = tc["function"]["arguments"]
            for j in range(0, len(args), 60):
                w(chunk({"tool_calls": [{
                    "index": i,
                    "function": {"arguments": args[j:j + 60]}}]}))
        w(chunk({}, "tool_calls" if self._cc_tool_calls(resp) else "stop"))
        w(b"data: [DONE]\n\n")

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
