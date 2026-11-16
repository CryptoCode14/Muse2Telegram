#!/usr/bin/env python3
"""
quinn-bridge v1 — Quinn (Muse) as the brain for Hermes, Ollama as backup.

Local HTTP service, stdlib only, localhost only.
- GET  /health
- GET  /v1/models
- POST /v1/chat/completions  (OpenAI format, non-streaming first)

Flow for chat completions:
  1. Build a self-contained prompt from the OpenAI messages.
  2. Send it to Quinn over A2A: from quinn-helper, to quinn,
     task qh-<slug>-<n>, payload {"message": <prompt>}.
  3. Poll /home/ubuntu/a2a/tasks.json every 5s, up to TIMEOUT seconds
     (default 150, env QUINN_BRIDGE_TIMEOUT), for a history entry
     from quinn. Reply text is payload.result / payload.message.
  4. Return an OpenAI chat-completion JSON with that text.
  5. On timeout / error / Quinn down: proxy the same request to
     Ollama at http://127.0.0.1:11434/v1/chat/completions and return
     that response, marking it in the log as fallback.

Key handling: the A2A key is read from /home/ubuntu/a2a/key on the VM
(or $A2A_KEY_FILE), never printed, never logged, never put in a message.
No money, orders, or ServiceTitan in this service.
"""
import http.client
import json
import os
import time
import uuid
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = int(os.environ.get("QUINN_BRIDGE_PORT", "8769"))
BUS_HOST = "YOUR-A2A-HOST"
TASKS_FILE = "/home/ubuntu/a2a/tasks.json"
KEY_FILE = os.environ.get("A2A_KEY_FILE", "/home/ubuntu/a2a/key")
OLLAMA_URL = "http://127.0.0.1:11434/v1/chat/completions"
TIMEOUT = int(os.environ.get("QUINN_BRIDGE_TIMEOUT", "150"))
POLL_SECONDS = 5
LOG_FILE = "/home/ubuntu/a2a/quinn-brain/quinn-bridge.log"


def log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except OSError:
        pass
    print(line, end="", flush=True)


def read_key():
    with open(KEY_FILE) as f:
        return f.read().strip()


def a2a_send(task_id, prompt):
    """Send one A2A request to Quinn. Returns (status, body_text)."""
    key = read_key()
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {
            "id": f"msg-{int(time.time())}",
            "from": "quinn-helper", "to": "quinn", "type": "request",
            "task_id": task_id,
            "payload": {"message": prompt},
        }},
    }).encode()
    conn = http.client.HTTPSConnection(BUS_HOST, 443, timeout=30)
    conn.request("POST", "/", body=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    })
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    return resp.status, data


def a2a_poll(task_id, timeout=TIMEOUT):
    """Poll tasks.json for a reply from quinn. Returns reply text or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(TASKS_FILE) as f:
                tasks = json.load(f)
            task = tasks.get(task_id)
            if task:
                for h in task.get("history", []):
                    if h.get("from") != "quinn":
                        continue
                    payload = h.get("payload", {})
                    if isinstance(payload, str):
                        return payload
                    if isinstance(payload, dict):
                        for k in ("result", "message", "text", "answer"):
                            if payload.get(k):
                                return str(payload[k])
                        return json.dumps(payload)
        except (OSError, json.JSONDecodeError) as e:
            log(f"poll read error: {e}")
        time.sleep(POLL_SECONDS)
    return None


def ollama_fallback(request_body):
    """Proxy the original OpenAI request to Ollama. Returns (status, body_bytes).
    Ollama has no model named 'quinn' — rewrite the model to the local
    Qwen model before forwarding, otherwise Ollama 404s (found 2026-11-13).
    """
    try:
        fb_req = json.loads(request_body.decode() or "{}")
        fb_req["model"] = "qwen2.5:7b-64k"
        request_body = json.dumps(fb_req).encode()
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    req = urllib.request.Request(
        OLLAMA_URL, data=request_body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 502, json.dumps({"error": f"ollama fallback failed: {e}"}).encode()


def build_prompt(messages):
    """Turn OpenAI messages into one self-contained prompt for Quinn."""
    parts = []
    for m in messages or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict))
        parts.append(f"{role}: {content}")
    tools = ""
    return (
        "You are Quinn, the Muse brain for Hermes. Answer the turn below "
        "in plain text only, no tool calls, no JSON wrapper, no preamble. "
        "If you cannot do it, say so plainly in one sentence.\n\n"
        + "\n".join(parts)
    )


def openai_response(text, model="quinn"):
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")

    def _send_json(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, status, data, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {
                "status": "ok", "brain": "quinn", "fallback": "ollama"})
        elif self.path == "/v1/models":
            self._send_json(200, {
                "object": "list",
                "data": [{"id": "quinn", "object": "model",
                          "created": int(time.time()), "owned_by": "quinn"}]})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON"})
            return
        if req.get("stream"):
            log("stream requested, returning non-streaming response")
        model = req.get("model", "quinn")
        prompt = build_prompt(req.get("messages", []))
        slug = uuid.uuid4().hex[:8]
        task_id = f"qh-{slug}-001"
        log(f"chat completion task={task_id} model={model}")
        try:
            status, _ = a2a_send(task_id, prompt)
            log(f"a2a send task={task_id} http={status}")
        except Exception as e:
            log(f"a2a send failed task={task_id}: {e}")
            status, body = ollama_fallback(raw)
            log(f"fallback ollama task={task_id} http={status}")
            self._send_bytes(status, body)
            return
        reply = a2a_poll(task_id)
        if reply is None:
            log(f"quinn timeout task={task_id}, falling back to ollama")
            status, body = ollama_fallback(raw)
            log(f"fallback ollama task={task_id} http={status}")
            self._send_bytes(status, body)
            return
        log(f"quinn replied task={task_id} chars={len(reply)}")
        self._send_json(200, openai_response(reply, model="quinn"))


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"quinn-bridge listening on {HOST}:{PORT}, timeout={TIMEOUT}s, fallback=ollama")
    server.serve_forever()
