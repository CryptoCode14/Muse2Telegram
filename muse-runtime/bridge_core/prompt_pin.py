"""Prompt pin: byte-stable system-prompt snapshot on the session record.

Pattern source: Hermes ``gateway/session_prompt_pin.py`` (a durable
snapshot of the system-prompt inputs internal turns reuse so the system
prompt stays byte-stable across a gateway restart; CAS-guarded by
``expected_session_id`` so a stale turn cannot write the old
conversation's system bytes onto a new one). This is our own Python
implementation; it does not import Hermes code.

The pin lives as a plain dict on ``session_record["prompt_pin"]`` so it
survives in the JSON state store with no schema change. Pure functions,
no I/O.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

PROMPT_PIN_VERSION = 1


@dataclass
class PromptPin:
    version: int = PROMPT_PIN_VERSION
    context_key: str = ""
    context_prompt: str = ""
    redact_pii: bool = False
    channel_prompt: str | None = None
    parent_chat_id: str | None = None


def _sanitize(pin) -> dict | None:
    """Validate a pin (dataclass or dict) into its canonical dict form."""
    if isinstance(pin, PromptPin):
        raw = asdict(pin)
    elif isinstance(pin, dict):
        raw = pin
    else:
        return None
    if raw.get("version") != PROMPT_PIN_VERSION:
        return None
    context_key = raw.get("context_key")
    context_prompt = raw.get("context_prompt")
    redact_pii = raw.get("redact_pii")
    channel_prompt = raw.get("channel_prompt")
    parent_chat_id = raw.get("parent_chat_id")
    if not isinstance(context_key, str) or not context_key:
        return None
    if not isinstance(context_prompt, str):
        return None
    if not isinstance(redact_pii, bool):
        return None
    if channel_prompt is not None and not isinstance(channel_prompt, str):
        return None
    if parent_chat_id is not None and not isinstance(parent_chat_id, str):
        return None
    return {
        "version": PROMPT_PIN_VERSION,
        "context_key": context_key,
        "context_prompt": context_prompt,
        "redact_pii": redact_pii,
        "channel_prompt": channel_prompt,
        "parent_chat_id": parent_chat_id,
    }


def pin_prompt(session_record: dict, pin: PromptPin, *,
               expected_session_id: str | None = None) -> bool:
    """CAS-guarded store of ``pin`` on ``session_record["prompt_pin"]``.

    Returns False (and stores nothing) when the pin is invalid or when
    ``expected_session_id`` is given and does not match
    ``session_record.get("session_id")`` -- a stale turn must not pin
    the old conversation's prompt bytes onto a new session.
    """
    if not isinstance(session_record, dict):
        return False
    if (expected_session_id is not None
            and session_record.get("session_id") != expected_session_id):
        return False
    cleaned = _sanitize(pin)
    if cleaned is None:
        return False
    session_record["prompt_pin"] = cleaned
    return True


def get_prompt_pin(session_record: dict, *,
                   expected_session_id: str | None = None) -> dict | None:
    """Return a copy of the stored pin, honouring the same CAS guard."""
    if not isinstance(session_record, dict):
        return None
    if (expected_session_id is not None
            and session_record.get("session_id") != expected_session_id):
        return None
    pin = session_record.get("prompt_pin")
    return dict(pin) if isinstance(pin, dict) else None


def render_prompt_pin(session_record: dict) -> str:
    """Return the stored ``context_prompt`` verbatim (byte-stable)."""
    pin = get_prompt_pin(session_record)
    if pin is None:
        return ""
    prompt = pin.get("context_prompt")
    return prompt if isinstance(prompt, str) else ""


def build_session_context_prompt(*, platform: str = "telegram",
                                 chat_id=None, chat_type: str = "private",
                                 user_id=None, redact_pii: bool = False) -> str:
    """Render the "Current Session Context" system-prompt section.

    Mirrors Hermes ``gateway/session.py::build_session_context_prompt``
    (source/platform/chat, user or hashed id when ``redact_pii``).
    Telegram is a PII-safe platform in Hermes' sense, so callers
    typically pass ``redact_pii=False``; when True the user id is
    replaced by a truncated SHA-256 so the prompt never carries the
    raw id.
    """
    if chat_id is None:
        chat_line = f"Chat: {chat_type}"
    else:
        chat_line = f"Chat: {chat_type} {chat_id}"
    if user_id is None:
        user_line = "User: unknown"
    elif redact_pii:
        digest = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:16]
        user_line = f"User: redacted:{digest}"
    else:
        user_line = f"User: {user_id}"
    return "\n".join([
        "Current Session Context",
        f"Source: {platform}",
        chat_line,
        user_line,
    ])
