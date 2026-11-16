#!/usr/bin/env python3
"""quinn-bridge: OpenAI-compatible HTTP bridge to Quinn (Muse) via A2A.

Listens on 127.0.0.1:8769 and exposes a small OpenAI-compatible surface:

  GET  /health                 -> {"status": "ok", "brain": ..., "fallback": ...}
  GET  /v1/models              -> OpenAI model list, single model "quinn"
  POST /v1/chat/completions    -> OpenAI chat completion; stream=true is
                                  answered as a streamed fake (SSE chunks
                                  built from the completed reply)

Request flow for POST /v1/chat/completions:
  1. Ask Quinn over A2A (JSON-RPC to the registry): send one task carrying
     the chat messages compacted (latest turn in full, tool names only,
     system prompt capped — the verbatim ~52KB full turn tripped the fetch
     safety check, qh-03bfab49-001), then poll tasks/get until Quinn replies.
  2. On A2A timeout, error, or no reply, fall back to local Ollama at
     OLLAMA_BASE_URL (default http://127.0.0.1:11434/v1), proxying the same
     request (tools included) and normalising its OpenAI-format reply.
  3. If both backends fail, answer 502 with an OpenAI-style error object.

Tool calls: the bridge does not execute tools. A client's "tools" list is
logged (names only) and passed through to Quinn in the A2A message and to
Ollama in the proxied payload; the reply is plain assistant text. If a
client needs real tool execution it must do the tool call itself from the
reply text.

Python standard library only, no extra dependencies.

Configuration (environment variables, no secrets in this file):
  QUINN_BRIDGE_HOST   bind host, default 127.0.0.1
  QUINN_BRIDGE_PORT   bind port, default 8769
  A2A_URL             A2A registry URL, default https://YOUR-A2A-HOST/
  A2A_KEY_FILE        file holding the shared A2A bearer key,
                      default /home/ubuntu/.a2a/key (read at startup, and
                      re-read if missing at first use; never logged)
  A2A_TIMEOUT         seconds to wait for Quinn's reply, default 600
  A2A_POLL_SECONDS    seconds between polls, default 2
  A2A_RPC_TIMEOUT     seconds for one registry call, default 15
  OLLAMA_BASE_URL     Ollama OpenAI-compatible base, default
                      http://127.0.0.1:11434/v1
  OLLAMA_MODEL        model name to request from Ollama, default qwen2.5:7b-64k
  OLLAMA_TIMEOUT      seconds for one Ollama call, default 120
  QUINN_MODEL         public model id, default quinn

Dorian's A2A library: when it lands, drop a module named ``a2a_lib`` next
to this file (or on sys.path) exposing ``ask(messages, timeout) -> str`` and
this bridge will prefer it over the built-in minimal client below. The
built-in client speaks the same message/send + tasks/get JSON-RPC the
``a2a_client.py`` CLI uses, so the bridge works standalone until then.

Part of the quinn-brain workspace: /home/ubuntu/a2a/quinn-brain/
  dex/quinn_bridge.py   (this file)
  quinn-bridge.service  (systemd unit)
"""

import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("quinn-bridge")

MODEL_ID = os.environ.get("QUINN_MODEL", "quinn")
HOST = os.environ.get("QUINN_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("QUINN_BRIDGE_PORT", "8769"))
A2A_URL = os.environ.get("A2A_URL", "https://YOUR-A2A-HOST/")
A2A_KEY_FILE = os.environ.get("A2A_KEY_FILE", "/home/ubuntu/.a2a/key")
A2A_TIMEOUT = float(os.environ.get("A2A_TIMEOUT", "600"))
A2A_POLL_SECONDS = float(os.environ.get("A2A_POLL_SECONDS", "2"))
A2A_RPC_TIMEOUT = float(os.environ.get("A2A_RPC_TIMEOUT", "15"))
#: Max chars of the turn forwarded to Quinn over A2A (env-overridable).
#: Bridge-generated full turns (~52KB: the Hermes system prompt repeated
#: verbatim plus full tool schemas) tripped the fetch safety check and sat
#: unanswered past the 600s window (qh-03bfab49-001, qh-4ae05c49-001,
#: 2026-11-15/16). The forward stays small: latest turn in full, tool
#: names only, system prompt capped.
MAX_FORWARD_CHARS = int(os.environ.get("QUINN_BRIDGE_MAX_FORWARD_CHARS", "8000"))
#: System messages are capped to this many chars in the forward: the Hermes
#: system prompt repeats verbatim every turn and Quinn needs the turn, not
#: the helper's standing prompt.
MAX_SYSTEM_CHARS = int(os.environ.get("QUINN_BRIDGE_MAX_SYSTEM_CHARS", "500"))
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-64k")
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "120"))

STARTED_AT = time.time()
STARTED_ISO = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(STARTED_AT))


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _estimate_tokens(text):
    """Rough token estimate (no tokenizer in stdlib); ~4 chars per token."""
    # Fix (Option 2, 2026-11-14): Ollama can return content=None, and
    # len(None) crashed the handler instead of a clean 502. Guard it.
    if text is None:
        return 1
    return max(1, len(text) // 4)


def _chat_completion_response(content, backend):
    """Build an OpenAI-format chat.completion object."""
    if content is None:
        content = ""
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": _estimate_tokens(content),
            "total_tokens": _estimate_tokens(content),
        },
        "system_fingerprint": "quinn-bridge-" + backend,
    }


def _stream_chunk(content, finish_reason=None, first=False):
    """One OpenAI-format chat.completion.chunk object."""
    delta = {}
    if first:
        delta["role"] = "assistant"
    if content:
        delta["content"] = content
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


def _streamed_fake_body(content):
    """SSE body that replays a completed reply as a stream.

    Quinn and Ollama are both asked non-streaming, so the full text is
    already in hand; clients that asked for stream=true still get valid
    OpenAI SSE (content chunks, a stop chunk, then [DONE]).
    """
    lines = []
    # Emit in word-sized pieces so the stream looks like a stream.
    words = content.split(" ")
    piece = ""
    first = True
    for word in words:
        piece += word + " "
        if len(piece) >= 24:
            lines.append("data: " + json.dumps(
                _stream_chunk(piece, first=first)) + "\n\n")
            piece = ""
            first = False
    if piece:
        lines.append("data: " + json.dumps(
            _stream_chunk(piece, first=first)) + "\n\n")
    lines.append("data: " + json.dumps(
        _stream_chunk("", finish_reason="stop")) + "\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


def _error_response(message, code):
    return {
        "error": {
            "message": message,
            "type": "server_error" if code >= 500 else "invalid_request_error",
            "code": code,
        }
    }


def _post_json(url, payload, headers=None, timeout=30):
    """POST a JSON body, return (status, parsed_json). Raises on failure."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error": {"message": raw[:500]}}


# --------------------------------------------------------------------------
# Backend 1: Quinn over A2A
# --------------------------------------------------------------------------

class A2AError(Exception):
    """Raised when the A2A ask fails (timeout, transport, bad reply)."""


def _read_a2a_key():
    try:
        with open(A2A_KEY_FILE, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
        if not key:
            raise A2AError("A2A key file is empty: %s" % A2A_KEY_FILE)
        return key
    except OSError as exc:
        raise A2AError("cannot read A2A key file %s: %s" % (A2A_KEY_FILE, exc))


def _a2a_rpc(method, params, key, rpc_id=1):
    body = json.dumps(
        {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
    ).encode("utf-8")
    req = urllib.request.Request(A2A_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=A2A_RPC_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise A2AError("registry HTTP %s: %s" % (exc.code, exc.read()[:200]))
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise A2AError("registry unreachable: %s" % exc)
    if "error" in data:
        raise A2AError("registry error: %s" % data["error"])
    return data.get("result", {})


def _extract_quinn_reply(task):
    """Return Quinn's reply text from a fetched task, or None.

    Accepts the reply wherever Quinn puts it: payload.message,
    payload.result, payload.text, or a bare string payload.
    """
    history = task.get("history") or []
    for msg in reversed(history):
        if msg.get("from") != "quinn":
            continue
        payload = msg.get("payload") or {}
        if isinstance(payload, str):
            return payload
        for key in ("message", "result", "text", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        if isinstance(payload.get("choices"), list) and payload["choices"]:
            content = payload["choices"][0].get("message", {}).get("content")
            if content:
                return content
    return None


def _slugify(text, max_len=30):
    """Turn free text into a URL-ish slug for A2A task ids."""
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (slug or "chat")[:max_len].strip("-") or "chat"


# Per-slug sequence so repeat requests with the same opening line still get
# distinct task ids: qh-<slug>-001, qh-<slug>-002, ...
_TASK_SEQ = {}


def _next_task_id(messages):
    first_user = next(
        (m.get("content", "") for m in messages if m.get("role") == "user"),
        "",
    )
    slug = _slugify(first_user if isinstance(first_user, str) else "")
    _TASK_SEQ[slug] = _TASK_SEQ.get(slug, 0) + 1
    return "qh-%s-%03d" % (slug, _TASK_SEQ[slug])


def _tool_names(tools):
    """Short names for logging, e.g. ['get_weather', 'search']."""
    names = []
    for tool in tools or []:
        if isinstance(tool, dict):
            fn = tool.get("function") or {}
            names.append(fn.get("name") or tool.get("type") or "?")
    return names


def _message_text(message):
    """Plain text of one OpenAI chat message (list content joined)."""
    if not isinstance(message, dict):
        return str(message)
    content = message.get("content", "")
    if isinstance(content, list):
        content = " ".join(
            c.get("text", "") for c in content if isinstance(c, dict))
    return content if isinstance(content, str) else str(content)


def _compact_forward(messages, tools=None, max_chars=MAX_FORWARD_CHARS):
    """Build the small prompt forwarded to Quinn over A2A.

    The bridge used to forward the whole OpenAI request verbatim: the
    Hermes system prompt (~11KB, repeated every turn) plus the full tool
    schemas as JSON (~35KB). Those ~52KB forwards tripped the fetch
    safety check and sat unanswered past the 600s window. Compact instead:
    system messages capped to a short head, tools as names only, and the
    whole forward capped — oldest turns dropped first, and a single
    over-long newest turn middle-truncated so its head and tail survive.
    (Twin copy in a2a_lib.py, which the bridge prefers when present;
    keep the two in sync.)
    """
    lines = []
    for message in messages or []:
        role = message.get("role", "user") if isinstance(message, dict) else "user"
        text = _message_text(message)
        if role == "system" and len(text) > MAX_SYSTEM_CHARS:
            text = text[:MAX_SYSTEM_CHARS] + " [...system prompt truncated...]"
        lines.append("%s: %s" % (role, text))
    tools_line = ""
    if tools:
        names = _tool_names(tools)
        if names:
            tools_line = "Available tools: " + ", ".join(names)
    while True:
        parts = list(lines)
        if tools_line:
            parts.append(tools_line)
        forward = "\n\n".join(parts)
        if len(forward) <= max_chars or len(lines) <= 1:
            break
        lines.pop(0)
    if len(forward) > max_chars:
        # One line alone exceeds the cap: middle-truncate it, keeping
        # head and tail (the ask sits at one end or the other).
        marker = " [...] "
        keep = max(0, (max_chars - len(marker)) // 2)
        forward = (forward[:keep] + marker + forward[-keep:]
                   if keep else forward[:max_chars])
    return forward


def _ask_quinn_builtin(messages, tools=None, timeout=A2A_TIMEOUT):
    """Minimal stdlib A2A client: send one task, poll until Quinn replies."""
    key = _read_a2a_key()
    task_id = _next_task_id(messages)
    # Compact forward: the verbatim full turn (~52KB) tripped the fetch
    # safety check; see _compact_forward.
    text = _compact_forward(messages, tools)
    send_msg = {
        "id": "msg-%d" % int(time.time()),
        "from": "quinn-helper",
        "to": "quinn",
        "type": "request",
        "task_id": task_id,
        "payload": {"message": text},
    }
    _a2a_rpc("message/send", {"message": send_msg}, key)
    LOG.info("asked Quinn via A2A task %s (forward %d chars)",
             task_id, len(text))

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(A2A_POLL_SECONDS)
        # tasks/get answers {"task": {...}}; unwrap so status/history read.
        fetched = _a2a_rpc("tasks/get", {"id": task_id}, key)
        task = fetched.get("task", fetched) if isinstance(fetched, dict) else {}
        reply = _extract_quinn_reply(task)
        if reply is not None:
            LOG.info("Quinn replied on task %s (%d chars)", task_id, len(reply))
            return reply
        if task.get("status") in ("completed", "cancelled", "failed"):
            raise A2AError(
                "task %s ended (%s) with no reply from Quinn"
                % (task_id, task.get("status"))
            )
    raise A2AError("timed out after %.0fs waiting for Quinn" % timeout)


def ask_quinn(messages, tools=None, timeout=A2A_TIMEOUT):
    """Ask Quinn. Prefer Dorian's a2a_lib when present, else built-in client."""
    if tools:
        LOG.info("request carries %d tool(s): %s",
                 len(tools), ", ".join(_tool_names(tools)))
    try:
        import a2a_lib  # type: ignore  # Dorian's library, when it lands.
    except ImportError:
        return _ask_quinn_builtin(messages, tools, timeout)
    ask = getattr(a2a_lib, "ask", None)
    if not callable(ask):
        LOG.warning("a2a_lib present but has no ask(); using built-in client")
        return _ask_quinn_builtin(messages, tools, timeout)
    LOG.info("asking Quinn via Dorian's a2a_lib")
    try:
        return ask(messages, timeout=timeout, tools=tools)
    except TypeError:
        # Older a2a_lib.ask(messages, timeout) without tools support.
        return ask(messages, timeout)


# --------------------------------------------------------------------------
# Backend 2: Ollama fallback (OpenAI-compatible endpoint)
# --------------------------------------------------------------------------

class OllamaError(Exception):
    """Raised when the Ollama fallback fails."""


def ask_ollama(messages, temperature=None, max_tokens=None, tools=None):
    """Proxy the chat request to Ollama; return the assistant text."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if tools:
        payload["tools"] = tools
    url = OLLAMA_BASE_URL.rstrip("/") + "/chat/completions"
    try:
        status, data = _post_json(url, payload, timeout=OLLAMA_TIMEOUT)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise OllamaError("Ollama unreachable at %s: %s" % (url, exc))
    if status != 200:
        err = (data.get("error") or {}).get("message", data)
        raise OllamaError("Ollama HTTP %s: %s" % (status, err))
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise OllamaError("unexpected Ollama reply shape: %s" % str(data)[:300])
    # Fix (Option 2, 2026-11-14): Ollama can return content=None (valid
    # JSON, null content). Returning None crashed the handler in
    # _estimate_tokens; treat it as a failed fallback instead.
    if content is None:
        raise OllamaError("Ollama returned empty content")
    return content


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "quinn-bridge/1.0"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status, obj, backend=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if backend:
            self.send_header("X-Quinn-Bridge-Backend", backend)
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, body, backend=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        if backend:
            self.send_header("X-Quinn-Bridge-Backend", backend)
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("empty request body")
        if length > 1_000_000:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {
                "status": "ok",
                "service": "quinn-bridge",
                "model": MODEL_ID,
                "brain": MODEL_ID,
                "fallback": {
                    "backend": "ollama",
                    "model": OLLAMA_MODEL,
                    "base_url": OLLAMA_BASE_URL,
                },
                "started_at": STARTED_ISO,
                "uptime_seconds": int(time.time() - STARTED_AT),
                "a2a_url": A2A_URL,
                "a2a_key_file_present": os.path.exists(A2A_KEY_FILE),
                "ollama_base_url": OLLAMA_BASE_URL,
                "ollama_model": OLLAMA_MODEL,
            })
        elif self.path == "/v1/models":
            self._send_json(200, {
                "object": "list",
                "data": [{
                    "id": MODEL_ID,
                    "object": "model",
                    "created": int(STARTED_AT),
                    "owned_by": "quinn",
                }],
            })
        else:
            self._send_json(404, _error_response(
                "unknown path: %s" % self.path, 404))

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._send_json(404, _error_response(
                "unknown path: %s" % self.path, 404))
            return
        try:
            req = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, _error_response(
                "invalid JSON body: %s" % exc, 400))
            return
        messages = req.get("messages")
        if (not isinstance(messages, list) or not messages
                or any(not isinstance(m, dict) or "content" not in m
                       for m in messages)):
            self._send_json(400, _error_response(
                "messages must be a non-empty list of {role, content}", 400))
            return
        tools = req.get("tools")
        if tools is not None and not isinstance(tools, list):
            self._send_json(400, _error_response(
                "tools must be a list", 400))
            return

        backend = "quinn"
        try:
            content = ask_quinn(messages, tools=tools)
        except A2AError as exc:
            # Option 2: clear fallback log line — grep for this phrase.
            LOG.warning("quinn timeout, falling back to ollama (%s)", exc)
            backend = "ollama"
            try:
                content = ask_ollama(
                    messages,
                    temperature=req.get("temperature"),
                    max_tokens=req.get("max_tokens"),
                    tools=tools,
                )
                LOG.info("quinn timeout, falling back to ollama — ollama replied")
            except OllamaError as oexc:
                LOG.error("Ollama fallback also failed: %s", oexc)
                self._send_json(502, _error_response(
                    "Quinn unavailable (%s) and Ollama fallback failed (%s)"
                    % (exc, oexc), 502))
                return
        # Option 2 defense in depth: never build a response from None.
        if content is None:
            LOG.error("quinn timeout, falling back to ollama — both returned no content")
            self._send_json(502, _error_response(
                "Quinn unavailable and Ollama fallback returned no content", 502))
            return
        if req.get("stream"):
            # Streamed fake: the reply is complete; replay it as SSE.
            self._send_sse(_streamed_fake_body(content), backend=backend)
            return
        self._send_json(200, _chat_completion_response(content, backend),
                        backend=backend)

    # Any other method on a known path: 405 with a JSON body.
    def do_PUT(self):
        self._send_json(405, _error_response("method not allowed", 405))

    do_DELETE = do_PUT
    do_PATCH = do_PUT


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    host, port = HOST, PORT
    i = 0
    while i < len(argv):
        if argv[i] in ("--host",) and i + 1 < len(argv):
            host = argv[i + 1]
            i += 2
        elif argv[i] in ("--port",) and i + 1 < len(argv):
            port = int(argv[i + 1])
            i += 2
        elif argv[i] in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            print("unknown argument: %s" % argv[i], file=sys.stderr)
            return 2
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True

    def _shutdown(signum, _frame):
        # shutdown() must not run on the serve_forever thread (it would
        # deadlock waiting for itself); hand it to a short-lived thread.
        LOG.info("signal %s, shutting down", signum)
        import threading
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    LOG.info("quinn-bridge listening on %s:%d (model %s, A2A %s, Ollama %s)",
             host, port, MODEL_ID, A2A_URL, OLLAMA_BASE_URL)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
