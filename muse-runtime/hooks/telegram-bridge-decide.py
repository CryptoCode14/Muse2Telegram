#!/usr/bin/env python3
"""Decide what the Telegram bridge hook does with one intake batch.

Reads the intake payload JSON on stdin and prints one line:
  wake            - some item needs this Muse; the hook wakes an agent
  hold            - paused and no command; the hook saves the payload
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
While paused, only owner/admin commands wake the agent; everything
else is held for the resume turn to answer or close.

Rate limiting is NOT done here. Throttling is a delay at claim time
(in the bridge core): throttled items get a wake_after and stay
pending, so no message is ever lost to throttling and the decider
never closes an item for rate limiting.
"""
import json
import os
import re
import sys

MY_BOT = os.environ.get("TELEGRAM_BRIDGE_BOT_USERNAME", "Westinmusebot").lower()
PAUSED_PATH = os.environ.get(
    "TELEGRAM_BRIDGE_PAUSED_PATH", os.path.expanduser("~/workspace/telegram-bridge/PAUSED")
)

#: An "@username" mention anywhere in the text. Same pattern as the
#: adapter's MENTION_RE ({3,}) so the two layers agree on what counts
#: as addressing someone.
MENTION_RE = re.compile(r"@[A-Za-z0-9_]{3,}")

#: A command is the command word at the start, optionally followed by
#: @BotName (Telegram appends it when a command is tapped in a group)
#: and then whitespace or end of text. "/pauseXYZ" is not /pause.
COMMAND_RE = re.compile(
    r"^/(pause|resume|status|help|who|active|menu)(?:@[A-Za-z0-9_]+)?(?:\s|$)",
    re.IGNORECASE,
)

#: This bot's @mention as a standalone token. The lookarounds keep
#: "@westinmusebot2", email-like "user@westinmusebot.com", and
#: "https://example.com/@westinmusebot" from matching.
MENTION_ME_RE = re.compile(
    r"(?<![A-Za-z0-9_/.])@" + re.escape(MY_BOT) + r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Wake-prompt tiers.
#
# The decider picks one tier per batch, rule-based, from the payload
# alone (no model call, no reply text). The tier only changes how
# much context the woken agent loads; it never changes whether it
# replies or what it may share. The hook injects the tier into the
# payload and the wake prompt reads the tier's prompt file.
#
#   quick    - clearly simple chat: a standalone greeting / small
#              talk, or /who / /help / /menu. Tiny prompt, no
#              context.md, no MEMORY.md, no typing, no reaction,
#              no claim-more. Fastest turn.
#   standard - everything else that is not clearly quick or deep.
#              The current default prompt (context.md, typing,
#              reaction, claim-more).
#   deep     - complex work: a lookup / task / long message, a
#              reference to past work, a media item that needs real
#              analysis, or a follow-up to unfinished work.
#              Full context (context.md + MEMORY.md) and tools.
#
# Rule: err toward a HIGHER tier when uncertain. Tier 1 is only for
# clearly simple messages. A batch with mixed complexity uses the
# HIGHEST tier any item in it needs.
# ---------------------------------------------------------------------------

TIER_QUICK = "quick"
TIER_STANDARD = "standard"
TIER_DEEP = "deep"
_TIER_RANK = {TIER_QUICK: 0, TIER_STANDARD: 1, TIER_DEEP: 2}

#: A quick message is short. Roughly under ~120 chars.
QUICK_MAX_CHARS = 120
#: A long message is deep work.
DEEP_MIN_CHARS = 500

#: Commands with fixed answers and no state to read: quick.
QUICK_COMMANDS = frozenset({"who", "help", "menu"})
#: Commands that need state.json: standard.
STATE_COMMANDS = frozenset({"status", "active", "pause", "resume"})

#: Optional trailing address word in a greeting ("hey bro",
#: "how's it going bud"). Westin's own vocatives, used to match
#: his speech, not to generate it.
_ADDRESS = r"(?:\s+(?:bud|bro|man|dude|my\s+guy|fam|nigga))?"

#: Standalone greetings / farewells / casual check-ins. Each pattern
#: must match the WHOLE message (after stripping), so a greeting
#: with a task attached ("hey, can you check my flight") does not
#: match and falls through to a higher tier.
GREETING_RES = [
    # bare greeting + optional address: hey, yo bro, hello man
    re.compile(r"^(?:hi|hey|hello|yo|howdy|hiya|heya)" + _ADDRESS + r"[.!?]*$",
               re.IGNORECASE),
    # good morning / afternoon / evening / night
    re.compile(r"^good\s+(?:morning|afternoon|evening|night)" + _ADDRESS + r"[.!?]*$",
               re.IGNORECASE),
    # how's it going / how's your day / how's things
    re.compile(r"^how'?s\s+(?:it\s+going|going|your\s+day|things|life|it)"
               + _ADDRESS + r"[.!?]*$", re.IGNORECASE),
    # how are you / how you doing
    re.compile(r"^how\s+(?:are\s+you|are\s+u|you\s+doing|u\s+doing|are\s+ya|ya\s+doing)"
               + _ADDRESS + r"[.!?]*$", re.IGNORECASE),
    # what's up / wassup / sup
    re.compile(r"^(?:what'?s\s+up|wassup|whatsup|sup)" + _ADDRESS + r"[.!?]*$",
               re.IGNORECASE),
    # greeting + greeting: hey how's it going, hey what's up
    re.compile(r"^(?:hey|hi|hello|yo)\s*,?\s+"
               r"(?:how'?s\s+(?:it\s+going|going|your\s+day)"
               r"|what'?s\s+up|wassup|sup"
               r"|how\s+(?:are\s+you|you\s+doing))"
               + _ADDRESS + r"[.!?]*$", re.IGNORECASE),
    # casual check-in: you good? / all good? / what's good?
    re.compile(r"^(?:you\s+good|are\s+you\s+good|u\s+good|everything\s+good"
               r"|all\s+good|what'?s\s+good|whats\s+good)"
               + _ADDRESS + r"[.!?]*$", re.IGNORECASE),
    # farewells: bye, goodbye, good night, see you, later
    re.compile(r"^(?:bye|goodbye|good\s?night|see\s+you|see\s+ya|later|gn)"
               + _ADDRESS + r"[.!?]*$", re.IGNORECASE),
]

#: Task / lookup signals. A message carrying one is deep work: it
#: needs a lookup, a tool, or a fact, not just a chat reply. Word
#: boundaries keep "book" the noun ("read a good book") and "order"
#: the phrase ("in order to") from matching; only the verb forms
#: (book a flight, order a pizza) count.
TASK_RES = [
    re.compile(r"\blook\s+up\b", re.IGNORECASE),
    re.compile(r"\blookup\b", re.IGNORECASE),
    re.compile(r"\bsearch\b", re.IGNORECASE),
    re.compile(r"\bgoogle\b", re.IGNORECASE),
    re.compile(r"\bfind\b", re.IGNORECASE),
    re.compile(r"\bbook\s+(?:a|an|the|me|my|us|it|that|this|for)\b", re.IGNORECASE),
    re.compile(r"\border\s+(?:a|an|the|me|my|us|it|that|this|for|one|two|some)\b",
               re.IGNORECASE),
    re.compile(r"\bbuy\b", re.IGNORECASE),
    re.compile(r"\bpurchase\b", re.IGNORECASE),
    re.compile(r"\breserve\b", re.IGNORECASE),
    re.compile(r"\bcancel\b", re.IGNORECASE),
    re.compile(r"\bschedule\b", re.IGNORECASE),
    re.compile(r"\bremind\b", re.IGNORECASE),
    re.compile(r"\bplan\b", re.IGNORECASE),
    re.compile(r"\bcheck\s+(?:my|the|on|if|whether|for)\b", re.IGNORECASE),
    re.compile(r"\bhow\s+much\b", re.IGNORECASE),
    re.compile(r"\bhow\s+many\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+time\b", re.IGNORECASE),
    re.compile(r"\bwhen\s+(?:is|does|do|are|will|can)\b", re.IGNORECASE),
    re.compile(r"\bwhere\s+(?:is|are|can|do)\b", re.IGNORECASE),
    re.compile(r"\bwho\s+is\b", re.IGNORECASE),
    re.compile(r"\bwhat'?s\s+the\s+(?:weather|time|price|cost|score|temperature|forecast)\b",
               re.IGNORECASE),
    re.compile(r"\bweather\b", re.IGNORECASE),
    re.compile(r"\bhow\s+do\s+i\b", re.IGNORECASE),
    re.compile(r"\bhow\s+to\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+does\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+do\b", re.IGNORECASE),
    re.compile(r"\bemail\b", re.IGNORECASE),
    re.compile(r"\bsend\s+(?:an?|the|me|him|her|it|them)\b", re.IGNORECASE),
]

#: References to past work / decisions. Answering one needs
#: MEMORY.md or a goal file, so it is deep work.
PAST_WORK_RES = [
    re.compile(r"\byou\s+said\b", re.IGNORECASE),
    re.compile(r"\byou\s+told\s+me\b", re.IGNORECASE),
    re.compile(r"\bwe\s+(?:decided|agreed|talked\s+about)\b", re.IGNORECASE),
    re.compile(r"\bthe\s+plan\b", re.IGNORECASE),
    re.compile(r"\blast\s+time\b", re.IGNORECASE),
    re.compile(r"\bthe\s+other\s+day\b", re.IGNORECASE),
    re.compile(r"\bremember\s+(?:the|when\s+we|that)\b", re.IGNORECASE),
    re.compile(r"\bmy\s+(?:flight|order|estimate|quote|ticket|hotel)\b", re.IGNORECASE),
    re.compile(r"\bthat\s+(?:flight|order|estimate|quote|plan|ticket|hotel)\b",
               re.IGNORECASE),
]


def _command_word(text):
    """The command word of a typed /command, or None."""
    m = COMMAND_RE.match((text or "").strip())
    return m.group(1).lower() if m else None


def _is_media(text):
    """True for a photo / document / voice marker."""
    t = text or ""
    return (
        t.startswith("[photo]")
        or t.startswith("[document")
        or t.startswith("[voice message")
    )


def _media_caption(text):
    """The user's caption from a media marker ("" when none). The
    marker boilerplate is system text, not his words, so the tier
    check reads only the caption."""
    if " Caption: " in (text or ""):
        return text.split(" Caption: ", 1)[1]
    return ""


def _is_quick_greeting(text):
    """True when the whole message is a standalone greeting /
    farewell / casual check-in (no task attached). An @mention is
    addressing, not content: "@Westinmusebot hey" is the greeting
    "hey", so mentions are stripped before matching."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return False
    t = re.sub(r"@[A-Za-z0-9_]{3,}", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return False
    return any(rx.search(t) for rx in GREETING_RES)


def _has_task_signal(text):
    return any(rx.search(text or "") for rx in TASK_RES)


def _has_past_work_signal(text):
    return any(rx.search(text or "") for rx in PAST_WORK_RES)


def _tier_for_item(item, has_active_work=False):
    """The tier for one payload item. Order matters: commands are
    decided first (a fixed-answer command is quick even while work
    is unfinished), then a follow-up to unfinished work, then media,
    then the reply / bot floors, then length, then the greeting
    test, then task / past-work signals. Anything ambiguous falls
    through to standard."""
    text = item.get("text") or ""

    # A command (typed, or a button tap whose text is "/<command>").
    # Fixed answers need no state: quick. State commands: standard.
    cmd = _command_word(text)
    if cmd is not None or item.get("source") == "callback_query":
        if cmd in QUICK_COMMANDS:
            return TIER_QUICK
        return TIER_STANDARD

    # A non-command message landing while work is unfinished is a
    # follow-up to that work: deep.
    if has_active_work:
        return TIER_DEEP

    # Media is at least standard (the agent must view / read /
    # transcribe it). A caption asking for real analysis is deep.
    if _is_media(text):
        caption = _media_caption(text)
        if caption and (
            _has_task_signal(caption) or _has_past_work_signal(caption)
        ):
            return TIER_DEEP
        return TIER_STANDARD

    # A message from another bot needs judgment: never quick.
    if item.get("is_bot"):
        return TIER_STANDARD

    # A reply is a follow-up to an earlier message: needs context.
    if item.get("is_reply"):
        return TIER_STANDARD

    # An open group message (free-for-all, no @mention of this bot)
    # needs the judgment call of whether to reply at all, so it is
    # at least standard. A greeting that @mentions the bot is
    # clearly addressed to it, so it can stay quick.
    if item.get("chat_type") in ("group", "supergroup") and not mentions_me(text):
        return TIER_STANDARD

    # A long message is deep work.
    if len(text) > DEEP_MIN_CHARS:
        return TIER_DEEP

    # A clear standalone greeting, short, needing no facts: quick.
    if len(text) <= QUICK_MAX_CHARS and _is_quick_greeting(text):
        return TIER_QUICK

    # A task / lookup, or a reference to past work: deep.
    if _has_task_signal(text) or _has_past_work_signal(text):
        return TIER_DEEP

    # Everything else, and anything ambiguous: standard.
    return TIER_STANDARD


def pick_tier(items, has_active_work=False):
    """The tier for a batch: the HIGHEST tier any item in it needs.
    ``has_active_work`` is the payload's lane flag (unfinished work
    outside this batch). Missing fields default to standard, never
    to quick, so an unfamiliar payload errs toward a higher tier."""
    tier = TIER_QUICK
    for it in items or []:
        t = _tier_for_item(it, has_active_work=bool(has_active_work))
        if _TIER_RANK[t] > _TIER_RANK[tier]:
            tier = t
            if tier == TIER_DEEP:
                break
    return tier



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

    # The tier travels with the decision: the hook injects it into
    # the payload so the woken agent knows which prompt file to
    # follow. It is rule-based, from the payload alone.
    tier = pick_tier(items, has_active_work=payload.get("has_active_work"))

    paused = os.path.exists(PAUSED_PATH)
    if paused:
        if any(_is_command_allowed(it) for it in items):
            print(f"wake {tier}")
        else:
            print(f"hold {tier}")
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
        print(f"wake {tier}")
        return 0

    ids = " ".join(
        str(it.get("update_id")) for it in items if it.get("update_id") is not None
    )
    print(f"done {ids}".strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
