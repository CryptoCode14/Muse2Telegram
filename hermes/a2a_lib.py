#!/usr/bin/env python3
"""
a2a_lib.py — adapter shim between Dex's bridge v2 and Dorian's library.

Dex's bridge (dex/quinn_bridge_v2.py) does:
    import a2a_lib
    a2a_lib.ask(messages, timeout=..., tools=...)
where messages is a list of {"role": ..., "content": ...}.

Dorian's library (dorian/a2a_client_lib.py) provides:
    ask_quinn(prompt: str, timeout=...) -> str | None

This shim translates messages (+ tool names) into one
self-contained prompt string and calls Dorian's ask_quinn.
Stdlib only. No keys in this file — Dorian's library reads the
A2A key from /home/ubuntu/a2a/key at call time.

The prompt is compacted, not verbatim: bridge-generated full turns
(~52KB: the Hermes system prompt repeated verbatim plus full tool
schemas) tripped the fetch safety check and sat unanswered past the
600s window (qh-03bfab49-001, qh-4ae05c49-001, 2026-11-15/16).
System messages are capped to a short head, tools go as names only,
and the whole forward is capped — oldest turns dropped first.
(Twin copy in quinn-bridge.py, which also has a built-in client
without this shim; keep the two in sync.)
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_DORIAN_DIR = os.path.join(_HERE, "dorian")
if _DORIAN_DIR not in sys.path:
    sys.path.insert(0, _DORIAN_DIR)

from a2a_client_lib import ask_quinn as _ask_quinn  # noqa: E402

#: Max chars of the turn forwarded to Quinn over A2A (env-overridable).
MAX_FORWARD_CHARS = int(os.environ.get("QUINN_BRIDGE_MAX_FORWARD_CHARS", "8000"))
#: System messages are capped to this many chars in the forward.
MAX_SYSTEM_CHARS = int(os.environ.get("QUINN_BRIDGE_MAX_SYSTEM_CHARS", "500"))


def _tool_names(tools):
    """Short names, e.g. ['get_weather', 'search']."""
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


def _compact_forward(messages, tools=None, max_chars=MAX_FORWARD_CHARS,
                     separator="\n"):
    """Build the small prompt forwarded to Quinn over A2A.

    System messages capped to a short head, tools as names only, whole
    forward capped — oldest turns dropped first, and a single over-long
    newest turn middle-truncated so its head and tail survive.
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
        forward = separator.join(parts)
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


def _prompt_from_messages(messages, tools=None):
    prompt = (
        "You are Quinn, the Muse brain for Hermes. Answer the turn below "
        "in plain text only, no tool calls, no JSON wrapper, no preamble. "
        "If you cannot do it, say so plainly in one sentence.\n\n"
        + _compact_forward(messages, tools)
    )
    return prompt


def ask(messages, timeout=600, tools=None):
    """Ask Quinn via Dorian's library. Returns reply text or None."""
    prompt = _prompt_from_messages(messages, tools=tools)
    return _ask_quinn(prompt, timeout=timeout)


# Re-export Dorian's function for direct use.
ask_quinn = _ask_quinn
