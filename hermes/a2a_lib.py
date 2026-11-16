#!/usr/bin/env python3
"""
a2a_lib.py — adapter shim between Dex's bridge v2 and Dorian's library.

Dex's bridge (dex/quinn_bridge_v2.py) does:
    import a2a_lib
    a2a_lib.ask(messages, timeout=..., tools=...)
where messages is a list of {"role": ..., "content": ...}.

Dorian's library (dorian/a2a_client_lib.py) provides:
    ask_quinn(prompt: str, timeout=...) -> str | None

This shim translates messages (+ tools, as JSON) into one
self-contained prompt string and calls Dorian's ask_quinn.
Stdlib only. No keys in this file — Dorian's library reads the
A2A key from /home/ubuntu/a2a/key at call time.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_DORIAN_DIR = os.path.join(_HERE, "dorian")
if _DORIAN_DIR not in sys.path:
    sys.path.insert(0, _DORIAN_DIR)

from a2a_client_lib import ask_quinn as _ask_quinn  # noqa: E402


def _prompt_from_messages(messages, tools=None):
    parts = []
    for m in messages or []:
        role = m.get("role", "user") if isinstance(m, dict) else "user"
        content = m.get("content", "") if isinstance(m, dict) else str(m)
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict))
        parts.append(f"{role}: {content}")
    prompt = (
        "You are Quinn, the Muse brain for Hermes. Answer the turn below "
        "in plain text only, no tool calls, no JSON wrapper, no preamble. "
        "If you cannot do it, say so plainly in one sentence.\n\n"
        + "\n".join(parts)
    )
    if tools:
        import json
        prompt += "\n\nAvailable tools (JSON):\n" + json.dumps(tools)
    return prompt


def ask(messages, timeout=600, tools=None):
    """Ask Quinn via Dorian's library. Returns reply text or None."""
    prompt = _prompt_from_messages(messages, tools=tools)
    return _ask_quinn(prompt, timeout=timeout)


# Re-export Dorian's function for direct use.
ask_quinn = _ask_quinn
