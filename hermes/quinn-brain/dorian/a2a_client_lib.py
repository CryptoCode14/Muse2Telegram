#!/usr/bin/env python3
"""
a2a_client_lib.py — A2A client library for the Quinn bridge (Hermes brain).

Lets the quinn-bridge HTTP server talk to Quinn (Muse, A2A `quinn`) as
its brain, with Ollama as fallback (the fallback itself lives in the
bridge server, not here — this library only talks to Quinn).

Contract (DESIGN.md, /home/ubuntu/a2a/quinn-brain/DESIGN.md):
- Send: A2A request from `quinn-helper` to `quinn`, task id
  `qh-<slug>-001` (unique, never reused), payload {"message": <prompt>}.
  `--from` is always `quinn-helper`. Never send as `quinn`.
- Reply: poll /home/ubuntu/a2a/tasks.json every 5s for a history
  entry where `from` is `quinn`. Reply text is in the payload
  (result / message / text / answer, or a plain string payload).
- Wait up to 10 min (600s) by default, then return None (timeout)
  so the caller can fall back to Ollama.

Stdlib only. The A2A key is read from /home/ubuntu/a2a/key (or
$A2A_KEY_FILE) at call time, never printed, never logged, never
put in a message. No money, orders, or ServiceTitan.

Usage from the bridge server:
    import sys
    sys.path.insert(0, "/home/ubuntu/a2a/quinn-brain/dorian")
    from a2a_client_lib import ask_quinn

    reply = ask_quinn("Reply with exactly: HELLO QUINN")
    if reply is None:
        # timeout / Quinn down — fall back to Ollama
        ...

Or lower level:
    from a2a_client_lib import make_task_id, send_task, poll_reply
    task_id = make_task_id("hello")
    send_task(task_id, "Reply with exactly: HELLO QUINN")
    reply = poll_reply(task_id, timeout=600)
"""
import http.client
import json
import os
import time
import uuid

# --- Config (env-overridable, sane defaults per DESIGN.md) ---------------

BUS_HOST = os.environ.get("A2A_BUS_HOST", "YOUR-A2A-HOST")
BUS_PORT = int(os.environ.get("A2A_BUS_PORT", "443"))
TASKS_FILE = os.environ.get(
    "A2A_TASKS_FILE", "/home/ubuntu/a2a/tasks.json")
KEY_FILE = os.environ.get("A2A_KEY_FILE", "/home/ubuntu/a2a/key")

FROM_BOT = "quinn-helper"
TO_BOT = "quinn"

POLL_SECONDS = 5          # poll tasks.json every 5s (DESIGN.md)
DEFAULT_TIMEOUT = 600     # wait up to 10 min, then timeout/fallback
SEND_TIMEOUT = 30         # HTTP timeout for the A2A send call

# Payload keys to try, in order, when extracting Quinn's reply text.
REPLY_KEYS = ("result", "message", "text", "answer")


# --- Task ids -------------------------------------------------------------

def make_task_id(slug=None):
    """Unique task id: qh-<slug>-001. A short random suffix is always
    appended, so two calls with the same slug still get different
    ids — task ids are never reused. Without a slug the slug is 8
    hex chars from uuid (same shape as Quinn's bridge)."""
    rand = uuid.uuid4().hex[:4]
    if slug:
        clean = "".join(c for c in str(slug) if c.isalnum() or c == "-")
        clean = clean.strip("-")[:12] or uuid.uuid4().hex[:8]
        return f"qh-{clean}-{rand}-001"
    return f"qh-{uuid.uuid4().hex[:8]}-001"


# --- Key ------------------------------------------------------------------

def read_key(key_file=None):
    """Read the shared A2A key from disk. The key is never printed,
    never logged, and never returned in a message — callers must not
    log the return value."""
    path = key_file or KEY_FILE
    with open(path) as f:
        return f.read().strip()


# --- Send -----------------------------------------------------------------

def send_task(task_id, prompt, key_file=None):
    """Send one A2A request to Quinn.

    Args:
        task_id: unique id from make_task_id(), e.g. "qh-11436dbc-001".
        prompt: self-contained plain-text prompt for Quinn.
        key_file: optional override for the A2A key file.

    Returns:
        (http_status, body_text) from the A2A bus.

    Raises:
        OSError / http.client.HTTPException on transport failure.
        The caller decides whether to fall back to Ollama.
    """
    key = read_key(key_file)
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {
            "id": f"msg-{int(time.time())}",
            "from": FROM_BOT, "to": TO_BOT, "type": "request",
            "task_id": task_id,
            "payload": {"message": prompt},
        }},
    }).encode()
    conn = http.client.HTTPSConnection(BUS_HOST, BUS_PORT, timeout=SEND_TIMEOUT)
    try:
        conn.request("POST", "/", body=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        })
        resp = conn.getresponse()
        return resp.status, resp.read().decode()
    finally:
        conn.close()


# --- Poll -----------------------------------------------------------------

def extract_reply(task):
    """Extract Quinn's reply text from a tasks.json task dict, or None
    if Quinn has not replied yet. Only history entries where `from`
    is `quinn` count. A plain-string payload is the reply; a dict
    payload is searched for result/message/text/answer (in that
    order); anything else is returned as JSON text."""
    if not isinstance(task, dict):
        return None
    for h in task.get("history", []) or []:
        if h.get("from") != TO_BOT:
            continue
        payload = h.get("payload", {})
        if isinstance(payload, str):
            if payload:
                return payload
            continue
        if isinstance(payload, dict):
            for k in REPLY_KEYS:
                if payload.get(k):
                    return str(payload[k])
            if payload:
                return json.dumps(payload)
    return None


def poll_reply(task_id, timeout=DEFAULT_TIMEOUT, poll_seconds=POLL_SECONDS,
              tasks_file=None):
    """Poll tasks.json every `poll_seconds` for Quinn's reply to
    `task_id`. Returns the reply text, or None on timeout (caller
    falls back to Ollama). A missing or corrupt tasks.json is logged
    by the caller if needed — here it is treated as 'no reply yet'
    and polling continues until the deadline."""
    path = tasks_file or TASKS_FILE
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(path) as f:
                tasks = json.load(f)
            reply = extract_reply(tasks.get(task_id))
            if reply is not None:
                return reply
        except (OSError, json.JSONDecodeError):
            # tasks.json mid-write or temporarily unreadable — retry
            # on the next tick, do not fail the turn for it.
            pass
        time.sleep(poll_seconds)
    return None


# --- High level -----------------------------------------------------------

def ask_quinn(prompt, slug=None, timeout=DEFAULT_TIMEOUT,
              poll_seconds=POLL_SECONDS, key_file=None, tasks_file=None):
    """Send `prompt` to Quinn and wait for the reply.

    Returns the reply text, or None on timeout / send failure
    (caller falls back to Ollama). Raises nothing for a timeout;
    a transport failure on send returns None as well (logged by
    the caller if it wants the detail — use send_task + poll_reply
    directly when the send error itself matters).

    The returned task id is not exposed here; callers that need it
    (for logging / correlation) should use make_task_id + send_task
    + poll_reply instead.
    """
    task_id = make_task_id(slug)
    try:
        status, _body = send_task(task_id, prompt, key_file=key_file)
        if status != 200:
            return None
    except Exception:
        return None
    return poll_reply(task_id, timeout=timeout, poll_seconds=poll_seconds,
                      tasks_file=tasks_file)


def ask_quinn_with_task_id(prompt, slug=None, timeout=DEFAULT_TIMEOUT,
                           poll_seconds=POLL_SECONDS, key_file=None,
                           tasks_file=None):
    """Same as ask_quinn, but also returns the task id:
    (task_id, reply_or_None). Useful for logging / correlation."""
    task_id = make_task_id(slug)
    try:
        status, _body = send_task(task_id, prompt, key_file=key_file)
        if status != 200:
            return task_id, None
    except Exception:
        return task_id, None
    reply = poll_reply(task_id, timeout=timeout, poll_seconds=poll_seconds,
                       tasks_file=tasks_file)
    return task_id, reply


# --- Self-test ------------------------------------------------------------

if __name__ == "__main__":
    # Smoke test: task-id format + reply extraction (no network).
    tid = make_task_id("hello")
    assert tid.startswith("qh-") and tid.endswith("-001"), tid
    assert tid != make_task_id("hello"), "task ids must be unique"
    fake = {"history": [
        {"from": "quinn-helper", "payload": {"message": "hi"}},
        {"from": "quinn", "payload": {"result": "HELLO QUINN"}},
    ]}
    assert extract_reply(fake) == "HELLO QUINN"
    assert extract_reply({"history": []}) is None
    print("a2a_client_lib self-test OK")
    print("task id example:", tid)
