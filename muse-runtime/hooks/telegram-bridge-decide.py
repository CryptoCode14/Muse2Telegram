#!/usr/bin/env python3
"""Decide what the Telegram bridge hook does with one intake batch.

Reads the intake payload JSON on stdin and prints one line:
  wake            - some item needs this Muse; the hook wakes an agent
  hold            - paused and no command; the hook holds the batch
                    in the queue itself (wake_after +24h)
  done <ids...>   - nothing needs this Muse; the hook closes the batch
  empty           - no items in the payload

Routing (free-for-all + mention):
  - A private DM is always for this Muse.
  - A command (/pause, /resume, /status, /help, /who, /active, /menu)
    is for it ONLY when it comes from the owner or a group admin,
    verified by the payload's is_owner / is_group_admin fields
    (sender id matches the adopted owner, or the sender is the
    creator or an admin of the adopted group), never by display
    name. A stranger's /pause in the group does nothing.
  - A message that @mentions this bot is for it.
  - An open message in a group from a human with no @mention is a
    free-for-all: wake, and the agent decides whether it has anything
    worth adding.
  - A message @mentioning someone else, or a message from another bot
    that does not mention this bot, is not for it.
  - A configured alias also counts as a mention of this bot:
    TELEGRAM_BRIDGE_BOT_ALIASES is a comma-separated list of short
    names (no "@", e.g. "Example" for a bot whose username is
    ExampleBot), and "@<alias>" wakes it just like
    "@<username>". Unset, only the username counts.
While paused, only owner/admin commands wake the agent; everything
else is held for the resume turn to answer or close.

The decision prints exactly one line with NO prompt-selection word:
"wake", "hold", "done <ids...>", or "empty". The woken agent always
follows the single unified prompt (prompts/unified.md); the hook
pre-loads the agent's context into the wake payload instead (see
hydrate_payload below), so no per-batch prompt choice exists any
more.

Rate limiting is NOT done here. Throttling is a delay at claim time
(in the bridge core): throttled items get a wake_after and stay
pending, so no message is ever lost to throttling and the decider
never closes an item for rate limiting.
"""
import json
import os
import re
import sys
from pathlib import Path

#: This bot's Telegram username (no "@", case-insensitive). There is
#: no default: TELEGRAM_BRIDGE_BOT_USERNAME must be set, or the
#: decider refuses to run instead of silently answering as some
#: other bot.
_raw_bot_username = os.environ.get("TELEGRAM_BRIDGE_BOT_USERNAME", "")
if not _raw_bot_username.strip():
    raise RuntimeError(
        "TELEGRAM_BRIDGE_BOT_USERNAME is not set: set it to this bot's "
        "Telegram username (without '@') before running the bridge."
    )
MY_BOT = _raw_bot_username.strip().lstrip("@").lower()

#: Optional comma-separated short names that also @mention this bot
#: (e.g. "Example" for a bot whose username is ExampleBot).
#: Set with TELEGRAM_BRIDGE_BOT_ALIASES, no "@", case-insensitive.
#: Unset means only the bot's username counts as a mention of it.
BOT_ALIASES_ENV = "TELEGRAM_BRIDGE_BOT_ALIASES"


def _bot_aliases():
    raw = os.environ.get(BOT_ALIASES_ENV, "") or ""
    return [a.strip().lstrip("@").lower() for a in raw.split(",") if a.strip()]


ALIASES = _bot_aliases()
PAUSED_PATH = os.environ.get(
    "TELEGRAM_BRIDGE_PAUSED_PATH", os.path.expanduser("~/workspace/telegram-bridge/PAUSED")
)

#: An "@username" mention anywhere in the text. Same pattern as the
#: adapter's MENTION_RE (no length floor in either) so the two layers
#: agree on what counts as addressing someone — a short alias mention
#: (e.g. "@jo") is visible to both layers, never silently dropped.
MENTION_RE = re.compile(r"@[A-Za-z0-9_]+")

#: A command is the command word at the start, optionally followed by
#: @BotName (Telegram appends it when a command is tapped in a group)
#: and then whitespace or end of text. "/pauseXYZ" is not /pause.
COMMAND_RE = re.compile(
    r"^/(pause|resume|status|help|who|active|menu)(?:@[A-Za-z0-9_]+)?(?:\s|$)",
    re.IGNORECASE,
)

#: This bot's @mention as a standalone token — its username or any
#: configured alias. The lookarounds keep "@examplebot2",
#: email-like "user@examplebot.com", and
#: "https://example.com/@examplebot" from matching.
MENTION_ME_RE = re.compile(
    r"(?<![A-Za-z0-9_/.])@(?:" + "|".join(
        re.escape(n) for n in [MY_BOT] + ALIASES) + r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def is_command(text):
    return bool(COMMAND_RE.match((text or "").strip()))


def mentions_me(text):
    return MENTION_ME_RE.search(text or "") is not None


def has_any_mention(text):
    return MENTION_RE.search(text or "") is not None


def _is_owner(item):
    """True only when the payload says so. Private items are the
    owner's by intake (non-owner DMs are never queued), so a private
    item counts as the owner even if the field is missing."""
    if item.get("is_owner") is True:
        return True
    return item.get("chat_type") == "private"


def _is_group_admin(item):
    """True when the payload says the sender is the creator or an
    admin of the adopted group."""
    return item.get("is_group_admin") is True


def _is_command_allowed(item):
    """A command that may wake the agent: it must be a command AND
    from the owner or a group admin (by sender id, never by display
    name)."""
    return is_command(item.get("text", "")) and (
        _is_owner(item) or _is_group_admin(item)
    )


# ---------------------------------------------------------------------------
# Wake-payload hydration (hook layer, v2).
#
# The unified prompt is static, so the per-batch context the old
# tiers used to select must arrive in the payload instead. The
# intake hook calls hydrate_payload() on a wake payload before
# waking the agent.
#
# There is deliberately NO pre-wake typing here any more (P1
# latency, 2026-11-12): typing means "I am responding", and a
# turn that never starts must never show it — the pre-wake
# typing was the defect. Typing fires only while a turn is
# actually running: the agent fires it after deciding to reply
# (see prompts/unified.md), and --reply fires it right before
# sending. The hook sends no typing at all.
# ---------------------------------------------------------------------------

#: context.md is pre-loaded into the wake payload, bounded to this
#: many characters. The bound is on the TOTAL value (content +
#: truncation marker), so the payload never exceeds it.
CONTEXT_MD_MAX_CHARS = 6000

#: Appended (with a leading newline) when context.md is longer than
#: the bound; the kept prefix is shortened so the total stays within
#: the bound.
_CONTEXT_TRUNCATED_MARKER = "\n[context truncated]"

#: Default state path, same default as the bridge core.
_DEFAULT_STATE_PATH = os.path.expanduser(
    "~/workspace/telegram-bridge/state.json"
)


def context_md_path(state_path=None):
    """Path of context.md: it lives next to state.json, i.e.
    <state-dir>/context.md. Not in the repo; it is written at
    install time."""
    state = state_path or os.environ.get(
        "TELEGRAM_BRIDGE_STATE_PATH", _DEFAULT_STATE_PATH
    )
    return Path(state).expanduser().parent / "context.md"


def load_context_md(state_path=None):
    """Contents of context.md, bounded to CONTEXT_MD_MAX_CHARS,
    or None when the file is missing or unreadable. Never raises:
    a wake must never fail because the context file is absent."""
    try:
        text = context_md_path(state_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if len(text) > CONTEXT_MD_MAX_CHARS:
        keep = CONTEXT_MD_MAX_CHARS - len(_CONTEXT_TRUNCATED_MARKER)
        text = text[:keep] + _CONTEXT_TRUNCATED_MARKER
    return text


def hydrate_payload(payload, state_path=None):
    """A copy of the wake payload with "context_md" added (the
    bounded contents of context.md, or None when it is missing or
    unreadable) and any stale "tier" key removed. Never raises for
    a missing context file; never mutates the input."""
    out = dict(payload or {})
    out.pop("tier", None)
    try:
        out["context_md"] = load_context_md(state_path)
    except Exception:
        out["context_md"] = None
    return out


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print("empty")
        return 0
    items = payload.get("items") or []
    if not items:
        print("empty")
        return 0

    paused = os.path.exists(PAUSED_PATH)
    if paused:
        if any(_is_command_allowed(it) for it in items):
            print("wake")
        else:
            print("hold")
        return 0

    wake_item = None
    for it in items:
        text = it.get("text", "") or ""
        chat_type = it.get("chat_type", "")
        is_bot = bool(it.get("is_bot"))
        if chat_type == "private":
            wake_item = it
            break
        if _is_command_allowed(it):
            wake_item = it
            break
        # A command from anyone but the owner or a group admin is
        # never a wake: it is not a free-for-all message and not an
        # @mention. Skip it so a stranger's /pause costs no turn at
        # all. (The installing Muse's local routing note also checks
        # is_owner/is_group_admin before acting on a command, as a
        # backstop in case one ever reaches an agent.)
        if is_command(text):
            continue
        if mentions_me(text):
            wake_item = it
            break
        if chat_type in ("group", "supergroup"):
            if is_bot:
                # Another bot's message only wakes me if it mentions me.
                continue
            # Human message in a group: free-for-all unless it
            # @mentions someone else.
            if not has_any_mention(text):
                wake_item = it
                break
            continue

    if wake_item is not None:
        print("wake")
        return 0

    ids = " ".join(
        str(it.get("update_id")) for it in items if it.get("update_id") is not None
    )
    print(f"done {ids}".strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
