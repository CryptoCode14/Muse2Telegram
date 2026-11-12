"""Telegram adapter on the bridge core (successor to the standalone poll.py).

Keeps the operational interface the wake hook drives:

  --help, -h   print this usage and exit. Never polls, never touches
               state: a probe must not become a second getUpdates
               long-poll (Telegram answers that with HTTP 409).
  (no args)   fetch updates once, queue servable messages, claim one
              batch and print ONE wake payload JSON. Prints nothing
              when there is no work.
  --sending ID TOKEN       move a claimed item to "sending" — run this
                           BEFORE the Bot API send for the item, so a
                           crash mid-send leaves the item in "sending"
                           (never auto-resent) instead of recyclable.
                           Refused once a reply has been recorded.
                           (Operator/manual path; the fenced send
                           below is the normal turn path.)
  --send-fenced ID TOKEN CHAT_ID [REPLY_TO_MESSAGE_ID] [--keyboard]
                           fenced send of one queue reply: the reply
                           text is read from stdin. In one transaction
                           the item is verified (claimed, token matches,
                           no reply recorded) and moved to "sending";
                           only then is the deployment's send command
                           ($TELEGRAM_BRIDGE_SEND_CMD, invoked with
                           argv [chat id] and the text on stdin, and
                           expected to print the platform message id
                           as its last stdout line) executed. Prints
                           {"ok": true, "message_id": ...} on success
                           (item left in "sending" for --replied and
                           --done),
                           {"ok": false, "stage": "unknown"} when the
                           outcome is ambiguous (item stays "sending",
                           never resent), {"ok": false, "stage":
                           "fence"} when the fence refuses (nothing
                           is sent), and {"ok": false, "stage":
                           "config"} when the send command is not
                           configured (item stays claimed), and
                           {"ok": false, "stage": "input"} for empty or
                           oversized reply text, and {"ok": false,
                           "stage": "stale"} when the item is older
                           than the maximum reply age — the fence then
                           refuses without sending and leaves the item
                           claimed, preserved for reassessment; the
                           fence never closes claimed work. A
                           destination other than the chat recorded
                           at intake is refused without sending.
                           REPLY_TO_MESSAGE_ID threads the reply
                           under that message — and must be the
                           target item's own message_id (or omitted):
                           threading under any other message, in
                           particular the bot's own message, is
                           refused. A button tap's item has no
                           message_id, so its reply is standalone.
                           --keyboard attaches the inline command
                           keyboard, and is refused (stage
                           "keyboard") unless the reply answers a
                           /command: the keyboard goes only on
                           command replies, never on plain chat.
  --send-and-close ID TOKEN CHAT_ID [REPLY_TO_MESSAGE_ID] [--keyboard]
                           the normal turn path: --send-fenced, then
                           --replied, then --done, in one process.
                           Reads the reply text from stdin (file-based,
                           never pasted into a shell command). Prints
                           {"ok": true, "message_id": ...} on success,
                           or the --send-fenced failure result, or
                           {"ok": false, "stage": "record"} when the
                           send went out but the reply record could
                           not be written.
  --send-ack ID TOKEN CHAT_ID [REPLY_TO_MESSAGE_ID]
                           send the fixed acknowledgement for a
                           claimed item without closing it: "Got it,
                           looking into this, will reply when ready."
                           For work that will take 10+ minutes, so the
                           owner knows the turn is working. The ack
                           text is fixed; nothing is read from stdin.
                           The item stays claimed for the full reply.
                           Prints {"ok": true, "message_id": ...} on
                           success, {"ok": false, "stage":
                           "already-acked"} when an ack is already
                           recorded, or the fence/config/rejected/
                           retryable/unknown failure result.
  --claim-more CHAT_ID TOKEN [BATCH_SIZE]
                           pull messages that arrived during the turn
                           into this turn's batch (same lane, same
                           token), so they are answered by the same
                           reply instead of a second turn. Prints
                           {"ok": true, "items": [...]} (empty when
                           nothing new arrived). Only the token that
                           owns the lane's in-flight batch may claim
                           more; anyone else gets an empty list.
                           Claiming more also refreshes the batch
                           heartbeat (the turn is alive).
  --heartbeat CLAIM_TOKEN  refresh the heartbeat of every claimed
                           item under the token: the owning turn's
                           proof it is still alive and working.
                           Prints {"ok": true, "heartbeated": n}, or
                           {"ok": false, "stage": "stale-token"} when
                           the token owns no live batch. The turn calls
                           this first thing after waking and before
                           any step that may take over a minute; a
                           claim whose heartbeat goes stale for
                           600s (TELEGRAM_BRIDGE_HEARTBEAT_STALE_SECONDS)
                           is recycled on the next claim, so a turn
                           that died mid-work blocks its lane for
                           about ten minutes, not half an hour.
  --watchdog               one watchdog pass: recycle claimed items
                           whose heartbeat went stale (a turn that
                           died mid-work) back to pending, and print
                           the sweep report JSON. Never touches
                           sending / replied / live claims, never
                           sends anything. The watchdog hook calls
                           this every few minutes; the intake's next
                           poll re-claims and re-wakes anything
                           recycled here.
  --send-batch-and-close PRIMARY_ID TOKEN CHAT_ID
                           [REPLY_TO_MESSAGE_ID] [--keyboard]
                           [--cover UPDATE_ID ...]
                           one reply for a whole batch: sends once
                           for the primary item, records that reply
                           against every --cover item and closes them
                           all (done_reason "covered-by-batch-reply").
                           The reply target must be the message_id of
                           the primary or a covered item (or omitted);
                           --keyboard needs at least one covered item
                           to be a command. Prints {"ok": true,
                           "message_id": ..., "covered": [...]}, the
                           --send-fenced failure result, or
                           {"ok": false, "stage": "record"} when the
                           send went out but a reply record could not
                           be written for every item.
  --reply CLAIM_TOKEN [--react EMOJI] [--keyboard]
                           the single send path for a turn (v2): look
                           up every claimed item under CLAIM_TOKEN
                           (the caller passes no update/chat/reply
                           ids — they are derived from the batch),
                           heartbeat the batch, fire typing, react on
                           the latest user message when --react is
                           given, delete any recorded ack message,
                           then send one reply (the text is read from
                           stdin) threaded under the latest user
                           message and close every covered item.
                           Prints {"ok": true, "stage": "sent",
                           "message_id": ..., "chat_id": ...,
                           "replied_update_ids": [...],
                           "closed_update_ids": [...],
                           "ack_deleted": ..., "next_batch": null} on
                           success, or {"ok": false, "stage": ...}
                           with the same fields on failure. On stage
                           "unknown" the send outcome is ambiguous:
                           do NOT resend; check delivery first, and
                           nothing is closed as replied. On stage
                           "stale-token" the token owns no live batch
                           any more (recycled while the turn worked):
                           do NOT --done the items and do NOT resend;
                           the result also carries "item_status" and
                           "replied" for the item when it is known,
                           so the caller can tell whether the answer
                           went out. If "replied" is true, stop; else
                           the intake's re-wake owns the retry.
                           --reply is
                           NOT a substitute for --send-ack: ack first
                           for work that will take 10+ minutes, then
                           send the full reply with --reply. The
                           older --send-and-close and
                           --send-batch-and-close paths stay available
                           for backwards compatibility.
  --send-photo-and-close ID TOKEN CHAT_ID FILE_PATH
                           [REPLY_TO_MESSAGE_ID] [--keyboard]
                           [--cover UPDATE_ID ...]
                           one photo reply (sendPhoto), fenced like a
                           text send: the caption is read from stdin
                           (optional, may be empty, max 1024
                           characters). The file must live under the
                           home directory and fit Telegram's 10MB
                           photo cap. Prints {"ok": true,
                           "message_id": ..., "covered": [...]}, the
                           fence failure result, or {"ok": false,
                           "stage": "record"}.
  --send-document-and-close ID TOKEN CHAT_ID FILE_PATH
                           [REPLY_TO_MESSAGE_ID] [--keyboard]
                           [--cover UPDATE_ID ...]
                           one file reply (sendDocument), same shape
                           as --send-photo-and-close, with Telegram's
                           50MB document cap.
  --refresh-admins [CHAT_ID]
                           re-fetch a group's admin list
                           (getChatAdministrators) and print it.
                           With no chat id it uses the single adopted
                           group; a chat id is required when several
                           groups are adopted.
  --active [CHAT_ID] [WINDOW_SECONDS]
                           print the speakers active in a group
                           recently (from that group's buffer), as
                           JSON. A negative first argument is a group
                           chat id; otherwise it is the window in
                           seconds (default 3600). With no chat id it
                           uses the single adopted group.
  --done ID [TOKEN] [REASON] [EVIDENCE]
                           mark a claimed or sending item done; the
                           optional REASON (one short word, e.g.
                           "superseded" or "ack-only") records why an
                           item was closed without a reply. Closing a
                           "sending" item with no reply record REQUIRES
                           the EVIDENCE argument (a reconciliation
                           reference); a reason alone is refused
  --release ID [TOKEN]     return a claimed item to pending (refused
                           for replied or sending items)
  --replied ID MESSAGE_ID [TOKEN]
                           record the reply that went out for an item
                           (after the send, before --done; a replied
                           item is never recycled or released, so it
                           can never be answered twice). The token is
                           required in practice: without the batch's
                           token the record is rejected
  --send-failed ID TOKEN   return a "sending" item to pending — ONLY
                           when the Bot API definitively rejected the
                           send (the reply provably did not go out)
  --held                   list the held items (the resume turn's
                           list): items the paused bridge held in the
                           queue itself, with wake_after +24h
  --hold ID TOKEN          hold one claimed item for the resume turn:
                           wake_after = now + 24h, in the queue
                           itself (the intake hook calls this per
                           item of a held batch). The claim is left
                           intact; it recycles on the normal 600s
                           horizon, and the wake_after keeps the
                           recycled item unclaimable until --unhold
                           or the 24h bound
  --unhold ID [ID ...]     release held items and re-claim them in
                           the same transaction, printing a fresh
                           claim payload for the resume turn to
                           answer with --reply. Never --reply with a
                           held token: the old token is dead once
                           the 600s heartbeat horizon passes. Items
                           in another lane, or in a busy lane, stay
                           pending for the intake's next poll
  --purge-expired [--older-than SECONDS] [--dry-run]
                           operator purge: run the pending-expiry
                           sweep on demand (the same sweep claiming
                           performs) and print JSON counts plus the
                           expired items' ids. --older-than overrides
                           the group TTL for this run; private (owner
                           DM) items keep their configured TTL —
                           never, by default — so a purge cannot
                           silently drop the owner's own asks.
                           --dry-run reports what would expire and
                           changes nothing
  --adopt ID [GROUP_MODE]
                           bind a positive owner ID once (main agent/operator only).
               The same ID is idempotent; changing an established owner is
               refused and requires deliberate, offline state migration.
               GROUP_MODE (optional, "trusted" or "guarded") is the
               default mode the FIRST group adopted gets; every group
               adopted after that starts guarded (the safe default)
               until Muse sets its mode. When omitted the default
               stays as configured (guarded by default).
  --group-mode [CHAT_ID]
                           print the per-group sharing modes (one
                           record per adopted group: mode, allowlisted,
                           trip, effective mode, member count), the
                           trusted bots and the allowlist (read-only).
                           With a chat id, print only that group.
  --set-group-mode MODE [CHAT_ID]
                           operator / Muse only: set the sharing mode
               of one group to "trusted" or "guarded". This is the
               ONLY way a group's mode changes — no Telegram message
               or command can change it. With no chat id it uses the
               single adopted group; a chat id is required when
               several groups are adopted. Setting the mode also
               clears that group's safety trip (the owner is
               affirming the group's state).
  --set-trusted-bots "user1,user2,..."
                           operator / Muse only: replace the trusted-bot
               list (bot usernames, with or without "@"; a purely
               numeric entry is a Telegram user id). This bot itself
               is always kept in the list. Changing the list does NOT
               change the mode and does NOT clear a safety trip.
  --clear-group-safety-trip [CHAT_ID]
                           operator / Muse only: clear a group's safety
               trip (and its notice) after the owner decides the group
               is safe again. The stored mode is untouched. With no
               chat id it uses the single adopted group; a chat id is
               required when several groups are adopted.
  --allowlist-group CHAT_ID
                           operator / Muse only: add a group to the
               private-channel allowlist (trusted_group_ids). A
               listed group is always trusted and the safety trips
               never apply there, no matter who joins. No Telegram
               message or command can change the list.
  --unallowlist-group CHAT_ID
                           operator / Muse only: remove a group from
               the allowlist. The safety trips apply to it again.
  --list-allowlisted-groups
                           read-only: list the allowlisted group ids.

Turn protocol per replied item: --reply (the single send path: it
derives the batch from the claim token, heartbeats, types, reacts,
deletes any ack, sends once and closes every covered item, in one
process). The manual steps --send-fenced, --replied and --done, and
the older --send-and-close / --send-batch-and-close paths, remain
available for operator use and backwards compatibility.
Items closed without a reply only need --done.

Throttling is a DELAY, never a drop. A group item from a non-owner
that would exceed a limit gets a ``wake_after`` timestamp and stays
pending; a later poll claims it once its time comes (see
``apply_throttle``). Limits: a sender who sent more than 5 messages
in 60 seconds has their later items staggered +30s each (capped at
+300s); a chat that already woke 60 times in the last hour has its
items delayed +60s per re-check (capped at +300s). The owner, group
admins and DMs are never throttled. Per-sender counts come from the
queue itself; per-chat wake counts live in the rate file
(``$TELEGRAM_BRIDGE_RATE_PATH``, default
~/workspace/telegram-bridge/rate.json); trips are logged to rate.log.

Group admins: when the bridge adopts a group it fetches the admin
list (getChatAdministrators) and caches it in that group's record
(state["groups"][chat_id]["admins"]: creator id, admin ids,
fetched_at). Commands are allowed from the owner OR a group admin
of the group the command arrived in. When a command arrives from
someone not in the cached list, the list is re-fetched once (at
most once per 60s) in case they were just promoted.

The wake payload carries every claimed item under ``"items"`` plus the
batch ``claim_token``; for compatibility the first item's fields are
also present at the top level, exactly as the single-item payload did.
Each item carries ``message_id`` (the Telegram message id, for
reply threading and reactions) and ``is_group_admin``.

State lives in one JSON file managed by ``bridge_core.store``: the core
queue plus this adapter's keys (``offset``, ``owner_id``,
``group_ids`` (adopted groups, in adoption order), ``groups`` (a
per-group record per adopted group: ``mode``, ``admins``,
``member_count``, ``safety_tripped``, ``mode_notice``, ``buffer``),
``default_group_mode``, ``candidates``, ``candidate_groups``,
``trusted_bots``, ``trusted_group_ids``). The state path
is ``$TELEGRAM_BRIDGE_STATE_PATH`` or ``state.json`` beside this file.
A state written by an older single-group build is migrated on
first load: its ``group_id`` / ``group_mode`` / ``group_admins`` /
``group_member_count`` / ``group_safety_tripped`` /
``group_mode_notice`` / ``group_buffer`` become the one adopted
group's record, and the legacy keys are removed.

Group sharing mode (trusted / guarded) is PER GROUP, stored in
each group's record (``state["groups"][chat_id]["mode"]``, default
guarded). Trusted means the group holds only the owner + his Muse
bots (state["trusted_bots"], seeded with this bot itself): the owner
may ask for his personal details there, and a bot may ask another
Muse bot a clarifying question only to complete a specific task the
owner asked for — never an open-ended personal question, no fishing.
Guarded means any group with other people: the bot never shares
personal details about the owner. The mode of a group is changed
only by the owner asking Muse (--set-group-mode, with that group's
chat id); no Telegram message or command can change it. The
setup-time mode (--adopt's GROUP_MODE, stored as
``default_group_mode``) is the mode the FIRST group adopted gets;
every group adopted after that starts guarded (the safe default)
until Muse sets its mode. The safety trip is a runtime guard, not
a mode change, and it is per-group: in a trusted group, a message
from a sender who is neither the owner nor a trusted bot, or a
member count above the expected trusted count (1 owner + trusted
bots), stops personal-detail sharing (the group behaves as guarded)
and records a one-time notice for the next wake — the stored mode
stays "trusted" until Muse clears the trip or changes the mode. A
group on the allowlist (state["trusted_group_ids"], set only by
Muse on the owner's instruction) is exempt from the trips: it is
the owner's private channel, always trusted, no matter who joins.
The wake payload carries group_mode, group_safety_tripped,
group_mode_effective (what the agent must apply),
group_allowlisted, group_mode_notice, and adopted_group_ids (every
group the bot serves). A private DM is always trusted.
The bridge serves EVERY group the owner adds it to: it adopts a
group when the owner speaks (or taps a button) there, and ignores
messages from groups that are not adopted. Each adopted group has
its own mode, allowlist status, trip, admin list, member count,
and room buffer, so nothing leaks between groups.
Batch size and stale TTL can be tuned with ``$TELEGRAM_BRIDGE_BATCH_SIZE``
and ``$TELEGRAM_BRIDGE_STALE_TTL`` (seconds). The heartbeat horizon —
how long a claim may go without a heartbeat before it counts as
abandoned — is ``$TELEGRAM_BRIDGE_HEARTBEAT_STALE_SECONDS`` (default
600): the owning turn heartbeats while it works (``--heartbeat``,
plus automatic refresh on ack / claim-more), and a claim whose
heartbeat goes stale is recycled on the next claim instead of
blocking its lane for the full stale TTL. The settle window before
a freshly arrived batch is claimed is ``$TELEGRAM_BRIDGE_SETTLE_SECONDS``
(default 0, off; the hook sets 2): the intake waits out the window
and re-fetches once so a rapid follow-up joins the same batch.
and the fence's stale guard are tuned with
``$TELEGRAM_BRIDGE_GROUP_PENDING_TTL`` (default 600),
``$TELEGRAM_BRIDGE_PRIVATE_PENDING_TTL`` (unset: owner DMs never
expire) and ``$TELEGRAM_BRIDGE_MAX_REPLY_AGE`` (default 1800);
a negative value disables the limit it tunes.

At intake every message is classified (see ``classify_message``):
only complete greetings from ``ORDINARY_CONVERSATION_TEXTS`` with no
protection signal or active work in the same chat can expire. Unknown
expressions are protected "request" records, including unmentioned work
follow-ups. The marker lists are additional protection, not eligibility.

The wake payload carries every claimed item under ``"items"`` plus the
batch ``claim_token``; for compatibility the first item's fields are
also present at the top level, exactly as the single-item payload did.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from . import queue as queue_core
from .store import JsonStateStore

PLATFORM = "telegram"
CREDENTIAL = "custom.telegram-muse-bot"
BUFFER_MAX = 20
CONTEXT_TAIL = 8

#: Environment variable naming the deployment's send executable for
#: the fenced send (--send-fenced). The executable is invoked with
#: argv [chat_id] and the reply text on stdin, and must print the
#: platform message id as the last stdout line on success.
SEND_CMD_ENV = "TELEGRAM_BRIDGE_SEND_CMD"

#: Environment variable naming the muse-runtime checkout the bridge
#: runs from (the directory holding bridge_core/ and .venv). The
#: hook scripts set it at install time; when unset, the health
#: check derives it from this module's own location.
BRIDGE_HOME_ENV = "BRIDGE_HOME"

#: Environment variable naming the directory of hook definitions
#: (default ~/hooks/definitions). The health check reads the
#: intake hook definition from there.
HOOK_DEFINITIONS_ENV = "TELEGRAM_BRIDGE_HOOK_DEFINITIONS_DIR"

#: Seconds the fenced send waits for the send command before the
#: outcome is recorded as unknown (the send may have gone out).
#: Subprocess budget for one sender invocation. This must exceed the
#: sender's worst case, or a slow-but-successful send is reported as
#: "unknown" and strands the item in "sending": the sender fetches a
#: credential first, then makes up to three Bot API calls, each with
#: its own 30s timeout for text (60s for a media multipart call).
#: Worst case is roughly credential fetch + 3 x 60s, so 200s leaves
#: headroom. The normal path is still fast (a send usually returns
#: in a second or two); the ceiling only bounds a hung sender.
SEND_TIMEOUT_SECONDS = 200.0

#: Retryable-send delay (seconds): after a retryable failure
#: (429/5xx) the item returns to pending with ``wake_after`` =
#: now + this delay, so the next poll does not re-send into the
#: same throttle. The delay is min(Retry-After, 300) when the
#: sender reported a Retry-After header, and this 60 s default
#: when it did not (a 5xx carries no Retry-After). The sender's
#: own in-process 429 retry (one sleep of at most 60 s) is what
#: keeps the whole send inside the 200 s budget above.
RETRYABLE_DEFAULT_DELAY_SECONDS = 60.0
RETRYABLE_MAX_DELAY_SECONDS = 300.0

#: Fixed acknowledgement text for long work. A turn that will take
#: 10+ minutes sends this first, so the owner knows the turn is
#: working instead of wondering whether it errored. The text is
#: fixed — never customized per turn — and short. Comma, not a dash,
#: per the owner's style (no em dashes in replies).
ACK_TEXT = "Got it, looking into this, will reply when ready."

#: Environment variables tuning the pending-expiry sweep (claim-time
#: and --purge-expired) and the send fence's stale guard, in seconds.
#: The group TTL defaults to queue_core.DEFAULT_GROUP_PENDING_TTL;
#: the private TTL is unset (owner DMs never expire) by default; the
#: maximum reply age defaults to DEFAULT_MAX_REPLY_AGE. A negative
#: value disables the limit it tunes.
GROUP_PENDING_TTL_ENV = "TELEGRAM_BRIDGE_GROUP_PENDING_TTL"
PRIVATE_PENDING_TTL_ENV = "TELEGRAM_BRIDGE_PRIVATE_PENDING_TTL"
MAX_REPLY_AGE_ENV = "TELEGRAM_BRIDGE_MAX_REPLY_AGE"

#: Default maximum age (seconds since intake) at which the send
#: fence still sends a queue reply. Past it, the fence refuses with
#: stage "stale" and leaves the item claimed: the conversation has
#: moved on, so the reply would land off-context, but claimed work
#: is never closed by the fence — it is preserved for reassessment
#: (a fresh claim, an operator close, or a later send decision).
#: Operator posts that do not come from the queue never pass
#: through the fence and are unaffected.
DEFAULT_MAX_REPLY_AGE = 1800.0

#: Maximum reply age for work a turn already claimed
#: (``ever_claimed``), in seconds. A claimed item is unfinished
#: work the owner is waiting on, not stale chatter — it stays
#: answerable for a day, so a recovery after an outage is never
#: refused by the fence that exists to stop late replies to old
#: room chatter. This mirrors the pending-expiry sweep, which
#: likewise never expires ever-claimed work. Never-claimed items
#: keep DEFAULT_MAX_REPLY_AGE (30 minutes).
DEFAULT_MAX_REPLY_AGE_CLAIMED = 86400.0

#: Environment variable tuning the heartbeat horizon, in seconds:
#: a claim whose heartbeat is older than this counts as abandoned
#: and is recycled on the next claim (see
#: queue_core.HEARTBEAT_STALE_SECONDS for why 600s).
HEARTBEAT_STALE_ENV = "TELEGRAM_BRIDGE_HEARTBEAT_STALE_SECONDS"

#: Environment variable overriding the single-run poll lock path
#: (tests point it at a temp file so the suite never shares the
#: live lock — and never flakes against the live intake).
POLL_LOCK_PATH_ENV = "TELEGRAM_BRIDGE_POLL_LOCK_PATH"


def poll_lock_path() -> Path:
    """Path of the single-run poll lock for this bot.

    Keyed on the connector credential id, NOT on the state file:
    the Bot API allows exactly one getUpdates long-poll per bot,
    whatever state file the caller points at. A probe run from a
    checkout without TELEGRAM_BRIDGE_STATE_PATH used the default
    state path — and therefore a different lock file — and raced
    the intake's long poll, which Telegram answers with HTTP 409.
    One lock per credential id on this machine serializes every
    poller for the bot, including manual probes.
    """
    override = os.environ.get(POLL_LOCK_PATH_ENV)
    if override:
        return Path(override)
    return (
        Path(tempfile.gettempdir())
        / f"telegram-bridge-poll-{CREDENTIAL}.lock"
    )

#: Environment variable naming this deployment's bot username, used
#: by the intake classifier to recognise messages addressed to the
#: bot. Telegram usernames are case-insensitive; a leading "@" in
#: the value is ignored. There is no default: the variable must be
#: set, or the bridge refuses to run instead of silently
#: identifying as some other bot.
BOT_USERNAME_ENV = "TELEGRAM_BRIDGE_BOT_USERNAME"

#: Optional comma-separated short names that also @mention this bot
#: (e.g. "Example" for a bot whose username is ExampleBot).
#: Set with TELEGRAM_BRIDGE_BOT_ALIASES, no "@", case-insensitive.
#: Unset means only the bot's username counts as a mention of it.
#: Aliases are read by the intake classifier, the preclaim filter,
#: and the decider, so all three layers agree on what counts as
#: addressing this bot.
BOT_ALIASES_ENV = "TELEGRAM_BRIDGE_BOT_ALIASES"

#: An "@username" mention anywhere in the text. ANY mention — of
#: this bot, of another agent, of the owner — is a protection
#: signal for the intake classifier: a message that addresses
#: someone is work for a specific reader, not room chatter. The
#: pattern also matches the local part of an address-like string;
#: over-protecting is the safe direction here. No length floor:
#: the decider's MENTION_ME_RE has none either, so a 2-character
#: alias mention is visible to both layers — a floor here would
#: silently drop a short configured alias from the preclaim
#: filter while the decider still wakes on it. Short aliases are
#: never rejected; a short unaliased name is still noise.
MENTION_RE = re.compile(r"@[A-Za-z0-9_]+")

#: Question / request markers for the intake classifier. A text
#: carrying any of these is protected ("request" class): a
#: question or an ask must never be swept as chatter, even when it
#: mentions nobody. The list is deliberately small and lives ONLY
#: here; extend it in this one place.
QUESTION_REQUEST_MARKERS = (
    "?",
    "？",
    "ください",
    "お願い",
    "してほしい",
    "教えて",
)

#: Correction / cancellation markers for the intake classifier. A
#: text carrying any of these is protected ("request" class): a
#: correction or cancellation changes earlier work, so expiring it
#: would silently drop the change. Same rule as above: small, and
#: kept in this single list.
CORRECTION_MARKERS = (
    "やめる",
    "やめて",
    "中止",
    "取消",
    "キャンセル",
    "訂正",
    "間違",
    "取り消し",
    "やっぱりなし",
)

# Positive eligibility is deliberately narrow: exact standalone greetings,
# never arbitrary text with no known work markers. Do not add acknowledgements
# such as "OK" or "thanks": they can decide or close ongoing work.
ORDINARY_CONVERSATION_TEXTS = frozenset({
    "hello", "hi", "good morning", "good evening",
    "こんにちは", "おはよう", "おはようございます", "こんばんは",
})

HERE = Path(__file__).resolve().parent


def state_path() -> Path:
    override = os.environ.get("TELEGRAM_BRIDGE_STATE_PATH")
    return Path(override) if override else HERE / "state.json"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_seconds(name: str, default):
    """Float seconds from the environment; ``default`` when unset or
    invalid. A negative value resolves to None — the limit it tunes
    is disabled. A non-finite value (NaN, infinities — ``float()``
    parses "nan"/"inf" successfully) is invalid, not a limit: it
    resolves to ``default`` too, before it can reach any mutation.
    """
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value):
        return default
    return None if value < 0 else value


def _bot_username() -> str:
    raw = os.environ.get(BOT_USERNAME_ENV, "")
    if not raw.strip():
        raise RuntimeError(
            "TELEGRAM_BRIDGE_BOT_USERNAME is not set: set it to this "
            "bot's Telegram username (without '@') before running "
            "the bridge."
        )
    return raw.strip().lstrip("@").lower()


def _bot_aliases() -> list:
    raw = os.environ.get(BOT_ALIASES_ENV, "") or ""
    return [a.strip().lstrip("@").lower() for a in raw.split(",") if a.strip()]


def _bot_names() -> set:
    """This bot's username plus every configured alias, all
    lowercased, no "@". Used wherever a message is checked for
    addressing this bot."""
    names = {_bot_username()}
    names.update(_bot_aliases())
    return {n for n in names if n}


def classify_message(
    text: str,
    *,
    sender_is_owner: bool,
    bot_username: str = "",
    is_reply: bool = False,
    has_active_work: bool = False,
) -> str:
    """Protect unknown input; positively identify only standalone greetings.

    Owner messages, replies, addressed messages, known work signals and any
    unfinished work in the chat override greeting eligibility. Matching uses
    only whitespace trimming and case folding: extra clauses, punctuation or
    unfamiliar expressions remain protected rather than guessed to be chatter.
    Bot and human messages follow the same rules.
    """
    if has_active_work:
        return queue_core.CLASS_REQUEST
    if sender_is_owner:
        return queue_core.CLASS_REQUEST
    if text.lstrip().startswith("/"):
        return queue_core.CLASS_REQUEST
    if is_reply:
        return queue_core.CLASS_REQUEST
    if MENTION_RE.search(text):
        return queue_core.CLASS_REQUEST
    if bot_username and ("@" + bot_username) in text.lower():
        return queue_core.CLASS_REQUEST
    if any(marker in text for marker in QUESTION_REQUEST_MARKERS):
        return queue_core.CLASS_REQUEST
    if any(marker in text for marker in CORRECTION_MARKERS):
        return queue_core.CLASS_REQUEST
    if text.strip().casefold() in ORDINARY_CONVERSATION_TEXTS:
        return queue_core.CLASS_CONVERSATION
    return queue_core.CLASS_REQUEST


def _expiry_ttls() -> tuple:
    """(group_ttl, private_ttl) for the pending-expiry sweep."""
    return (
        _env_seconds(
            GROUP_PENDING_TTL_ENV, queue_core.DEFAULT_GROUP_PENDING_TTL
        ),
        _env_seconds(PRIVATE_PENDING_TTL_ENV, None),
    )


def _max_reply_age():
    """The send fence's maximum reply age, from the environment."""
    return _env_seconds(MAX_REPLY_AGE_ENV, DEFAULT_MAX_REPLY_AGE)


def _heartbeat_stale():
    """The heartbeat horizon, from the environment."""
    return _env_seconds(
        HEARTBEAT_STALE_ENV, queue_core.HEARTBEAT_STALE_SECONDS
    )


def _bridge_health():
    """The bridge-install health check for the watchdog sweep.

    Paths come from the environment the hook scripts already set:
    BRIDGE_HOME (the muse-runtime checkout),
    TELEGRAM_BRIDGE_SEND_CMD, and
    TELEGRAM_BRIDGE_HOOK_DEFINITIONS_DIR (default
    ~/hooks/definitions). When BRIDGE_HOME is unset, derive the
    checkout from this module's own location, so a bare CLI run
    still checks the install it was imported from. See
    ``queue_core.check_bridge_health`` for what is checked.
    """
    bridge_home = os.environ.get(BRIDGE_HOME_ENV)
    if not bridge_home:
        bridge_home = str(Path(__file__).resolve().parent.parent)
    definitions_dir = os.environ.get(HOOK_DEFINITIONS_ENV)
    if not definitions_dir:
        definitions_dir = str(Path.home() / "hooks" / "definitions")
    return queue_core.check_bridge_health(
        definitions_dir=definitions_dir,
        bridge_home=bridge_home,
        send_cmd=os.environ.get(SEND_CMD_ENV),
    )


#: Environment variable tuning the ACTIVE heartbeat horizon for
#: the two-phase rule (queue._recycle_stale): a claimed item whose
#: turn started is recycled once its heartbeat is older than this,
#: and a never-started claim once its claim is older than this.
#: Default 600 s (Argon, corrected) — the ONE heartbeat horizon:
#: the legacy heartbeat rule (HEARTBEAT_STALE_SECONDS) is 600 s
#: too, so the horizon is 600 s whichever rule applies. A negative
#: value disables the two-phase rule (returns None, and the legacy
#: rule applies); unset means the 600 s default, NOT None.
ACTIVE_HEARTBEAT_STALE_ENV = "TELEGRAM_BRIDGE_ACTIVE_HEARTBEAT_STALE_SECONDS"


def _active_heartbeat_stale():
    """The active (two-phase) heartbeat horizon, in seconds.

    Default 600 s; None only when the env value is negative
    (two-phase rule disabled — the legacy heartbeat rule, also
    600 s, applies instead). Unset is the default, not None.
    """
    return _env_seconds(
        ACTIVE_HEARTBEAT_STALE_ENV,
        queue_core.NEVER_STARTED_RECYCLE_SECONDS,
    )


#: Environment variable tuning the warm-baton timeout (seconds).
WARM_BATON_TIMEOUT_ENV = "TELEGRAM_BRIDGE_WARM_BATON_TIMEOUT_SECONDS"


def _warm_baton_timeout():
    """Warm-baton timeout from the environment (default 120 s)."""
    return _env_seconds(
        WARM_BATON_TIMEOUT_ENV, queue_core.WARM_BATON_TIMEOUT_SECONDS
    )


#: Settle / batching constants (OpenClaw bot-handlers + Hermes
#: adapter, retuned for the hook intake): default settle 300 ms,
#: max settle 7.5 s, batch cap 12 messages / 50,000 chars, album
#: (media-group) settle 1.5 s. The settle window is bypassed ONLY
#: when every item in the current batch is a /command.
SETTLE_DEFAULT_SECONDS = 0.3
SETTLE_MAX_SECONDS = 7.5
BATCH_MAX_MESSAGES = 12
BATCH_MAX_CHARS = 50000
ALBUM_SETTLE_SECONDS = 1.5

#: Warm-hold defaults (lane-scoped warm lease, v2 emulation of the
#: Hermes/OpenClaw warm pattern — our own design, not found in
#: either repo; see inventory §2). Default hold 25 s; the warm
#: agent polls for a follow-up in its own lane every 0.25 s and
#: re-fetches (short poll) at most every 1.5 s while holding.
WARM_HOLD_DEFAULT_SECONDS = 25.0
WARM_HOLD_SECONDS_ENV = "TELEGRAM_BRIDGE_WARM_HOLD_SECONDS"
WARM_HOLD_POLL_INTERVAL_SECONDS = 0.25
WARM_HOLD_REFETCH_MIN_SECONDS = 1.5
WARM_HOLD_FETCH_TIMEOUT_SECONDS = 2


def _warm_hold_seconds():
    """Warm-hold length from the environment (default 25 s)."""
    value = _env_seconds(WARM_HOLD_SECONDS_ENV, WARM_HOLD_DEFAULT_SECONDS)
    return WARM_HOLD_DEFAULT_SECONDS if value is None else value


def _reply_age_limit(item: dict, max_reply_age) -> float | None:
    """The stale-guard age limit (seconds since intake) for one item.

    Never-claimed items use ``max_reply_age`` (default 1800s): a
    late reply to hours-old room chatter lands off-context. An item
    a turn already claimed (``ever_claimed``) is unfinished work
    the owner is waiting on — it gets DEFAULT_MAX_REPLY_AGE_CLAIMED
    (a day), so a recovery after an outage is never refused by the
    fence. A negative or None ``max_reply_age`` disables the guard
    for both, matching the existing convention.
    """
    if max_reply_age is None or max_reply_age < 0:
        return None
    if item.get("ever_claimed"):
        return DEFAULT_MAX_REPLY_AGE_CLAIMED
    return max_reply_age


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects on authenticated calls: the request URL carries
    the credential surrogate, and a redirect would carry it to the
    redirect target. Same rule as the sender."""

    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def _urlopen_no_redirect(request, timeout):
    """Open a request with redirects disabled."""
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


def api(method: str, params: dict, timeout: float = 20.0) -> dict:
    """Call the Telegram Bot API through Muse's credential surrogate.

    Same mechanism the live poll.py uses: the surrogate path segment is
    swapped for the real token by the egress layer, so the token itself
    never exists on this machine and is never printed.
    """
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    from dynamic_credentials import dynamic_credential_entry, read_json_response

    entry = dynamic_credential_entry(CREDENTIAL)
    surrogate = str(entry["surrogate"]).strip()
    url = f"https://api.telegram.org/bot{surrogate}/{method}"
    request = urllib.request.Request(
        url,
        data=json.dumps(params).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _urlopen_no_redirect(request, timeout=timeout) as response:
        return read_json_response(response)


def fetch_updates(offset: int) -> dict:
    """Long-poll getUpdates: Telegram holds the request until a
    message arrives (or the timeout expires) and returns the moment
    one does, so a message is picked up in ~0-1s instead of waiting
    for the next poll tick. The HTTP timeout is the long-poll
    timeout plus margin, so the request is never cut short by the
    client. Set TELEGRAM_BRIDGE_LONG_POLL_SECONDS to 0 to go back
    to immediate (non-blocking) polling."""
    long_poll = _long_poll_seconds()
    return api(
        "getUpdates",
        {
            "offset": offset,
            "timeout": long_poll,
            "allowed_updates": [
                "message",
                "my_chat_member",
                "callback_query",
                "edited_message",
                "message_reaction",
            ],
        },
        timeout=long_poll + 15.0,
    )


#: Long-poll timeout (seconds) for getUpdates. Telegram allows 0-50;
#: 30 is the default. A persistent poller process is NOT used: a
#: poller cannot wake an agent (the only wake path is the hook
#: harness reading HATCH_HOOK_RESULT from a hook-script invocation),
#: and a poller-plus-hook hybrid would still wait for the hook's
#: 5s tick, so it would be no faster than the hook doing the long
#: poll itself. The hook script runs the intake once per tick and
#: the intake long-polls, so the message is picked up the moment it
#: arrives and the agent is woken in the same run. Expected wait:
#: ~0-1s when a long poll is active (most of the time), up to ~5s
#: when the message lands in the short gap between polls. Polling
#: costs no model tokens (just network). A single-run lock (see
#: run_once) keeps overlapping harness ticks from running two long
#: polls at once.
LONG_POLL_SECONDS_ENV = "TELEGRAM_BRIDGE_LONG_POLL_SECONDS"
DEFAULT_LONG_POLL_SECONDS = 30
MAX_LONG_POLL_SECONDS = 50


def _long_poll_seconds() -> int:
    """Long-poll timeout from the environment, clamped to 0-50."""
    try:
        value = int(os.environ.get(LONG_POLL_SECONDS_ENV, "") or DEFAULT_LONG_POLL_SECONDS)
    except ValueError:
        return DEFAULT_LONG_POLL_SECONDS
    return max(0, min(MAX_LONG_POLL_SECONDS, value))


#: Rate limits for group wakes from non-owner, non-admin senders.
#: The owner, group admins and DMs are never throttled. Throttling
#: is a DELAY (see apply_throttle), never a drop: a throttled item
#: keeps its place in the queue and is answered later, spaced out.
#: A sender flooding the group has their later items staggered +30s
#: each (capped at +300s); a chat that already woke 60 times in the
#: last hour has its items delayed +60s per re-check (capped at
#: +300s). One wake can cover up to 5 messages in a lane, so 60
#: turns/hour covers an active chat.
RATE_PATH_ENV = "TELEGRAM_BRIDGE_RATE_PATH"
MAX_PER_SENDER = 5
SENDER_WINDOW_SECONDS = 60
MAX_WAKES_PER_CHAT = 60
CHAT_WINDOW_SECONDS = 3600
THROTTLE_DELAY_STEP_SECONDS = 30
THROTTLE_CHAT_DELAY_SECONDS = 60
THROTTLE_MAX_DELAY_SECONDS = 300

#: Minimum seconds between re-fetches of the group admin list when
#: an unknown sender issues a command (so a stranger spamming
#: commands cannot turn every one into an API call).
ADMIN_REFRESH_MIN_SECONDS = 60

#: callback_data values for the inline command keyboard. The sender
#: builds the buttons; the bridge turns a tap into the matching
#: typed command ("/pause", "/resume", ...). Privileged buttons are
#: owner/group-admin gated exactly like typed commands.
CALLBACK_PREFIX = "cmd:"
CALLBACK_COMMANDS = ("pause", "resume", "status", "help", "who", "active", "menu")

#: Group sharing modes. The mode is PER GROUP and LOCKED: it is
#: stored in each group's record (state["groups"][chat_id]["mode"])
#: and changed only by the owner asking Muse (see --set-group-mode,
#: or --adopt with a mode for the first group adopted). No Telegram
#: message or command can change it — there is no code path from
#: intake to this field, and /trust, /guard, /mode, /trustbot,
#: /untrustbot are not commands. A private DM is always trusted
#: (it is just the owner); the mode only governs the group.
GROUP_MODE_TRUSTED = "trusted"
GROUP_MODE_GUARDED = "guarded"

#: Seconds between getChatMemberCount re-fetches for the safety
#: trip (same throttle as the admin list: at most once per 60s
#: unless forced). A re-fetch that fails keeps the cached count.
MEMBER_COUNT_REFRESH_MIN_SECONDS = 60


def _rate_path() -> Path:
    return Path(
        os.environ.get(
            RATE_PATH_ENV,
            os.path.expanduser("~/workspace/telegram-bridge/rate.json"),
        )
    )


def _rate_log_path() -> Path:
    return _rate_path().with_name("rate.log")


def _load_rate_state() -> dict:
    """Best-effort rate state. A broken or missing file means no
    throttling, never a broken intake."""
    try:
        with open(_rate_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {"senders": {}, "chats": {}}
        senders = data.get("senders")
        chats = data.get("chats")
        return {
            "senders": senders if isinstance(senders, dict) else {},
            "chats": chats if isinstance(chats, dict) else {},
        }
    except (OSError, ValueError):
        return {"senders": {}, "chats": {}}


def _save_rate_state(state: dict) -> None:
    """Atomic replace; a failure disables throttling, never routing.

    Mirrors ``store._write_atomic``: a unique mkstemp file (mode
    0600 regardless of umask — the rate file holds sender ids and
    wake timestamps), file fsync before the atomic replace, and a
    directory fsync after it, so the rename itself is durable.
    Best-effort throughout: any failure is swallowed, and the
    intake degrades to "no throttling", never a broken intake.

    No file locking here, deliberately: run_once is the only
    writer and its per-credential poll lock already serializes
    whole runs; load/save straddle the long poll, so a rate lock
    would mean restructuring run_once. A torn rate file only
    degrades to "no throttling", never a broken intake.
    """
    try:
        path = _rate_path()
        if path.parent:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(
            dir=path.parent, prefix=f"{path.name}.tmp."
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
            except OSError:
                dir_fd = None
            if dir_fd is not None:
                try:
                    os.fsync(dir_fd)
                except OSError:
                    pass
                finally:
                    os.close(dir_fd)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError:
        pass


def _prune_rate_state(state: dict, now: float) -> None:
    for sender_id, times in list(state["senders"].items()):
        kept = [t for t in times if now - t < SENDER_WINDOW_SECONDS]
        if kept:
            state["senders"][sender_id] = kept[-200:]
        else:
            del state["senders"][sender_id]
    for chat_id, times in list(state["chats"].items()):
        kept = [t for t in times if now - t < CHAT_WINDOW_SECONDS]
        if kept:
            state["chats"][chat_id] = kept
        else:
            del state["chats"][chat_id]


def _log_throttle(reason: str, sender_id, chat_id) -> None:
    try:
        line = (
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
            f"throttle reason={reason} sender={sender_id} chat={chat_id}\n"
        )
        with open(_rate_log_path(), "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def _count_in_window(times, now: float, window: float) -> int:
    return sum(1 for t in times if now - t < window)


def apply_throttle(state: dict, rate_state: dict, now: float) -> None:
    """Set wake_after on pending group items that would exceed a
    limit, so they are claimed later instead of being dropped.

    Runs inside the run_once transaction, after ingest and before
    claim_batch. Only non-owner, non-admin group items are delayed;
    the owner, group admins and DMs are never throttled. A delay is
    only ever extended, never shortened, and is capped at
    THROTTLE_MAX_DELAY_SECONDS. A trip is logged only when a delay
    is first set or extended, so rate.log does not spam on every poll.

    Per-sender: a sender gets 5 messages per 60 seconds answered
    promptly; later messages in the window are staggered +30s each
    (6th message +30s, 7th +60s, ...). The count comes from the
    queue itself (that sender's items received in the last 60s, in
    queue order), so the first 5 always go through and only the
    excess is delayed.
    Per-chat: a chat that already woke 60 times in the last hour has
    its pending items delayed +60s per re-check. Wake timestamps
    live in the rate file (rate_state["chats"]).
    """
    _prune_rate_state(rate_state, now)
    # Per-sender: walk the queue in order, counting each sender's
    # messages in the window. A pending item that is the sender's
    # 6th (or later) message in the window is delayed.
    sender_counts: dict = {}
    for item in state.get("queue", []):
        if item.get("platform") != PLATFORM:
            continue
        sender_id = item.get("sender_id")
        if sender_id is None:
            continue
        received_at = item.get("received_at")
        if received_at is None or now - received_at >= SENDER_WINDOW_SECONDS:
            continue
        sender_counts[sender_id] = sender_counts.get(sender_id, 0) + 1
        position = sender_counts[sender_id]
        if position <= MAX_PER_SENDER:
            continue
        if item.get("status") != queue_core.PENDING:
            continue
        if item.get("replied_at") is not None:
            continue
        if item.get("chat_type") not in ("group", "supergroup"):
            continue
        if sender_id == state.get("owner_id"):
            continue
        if _is_group_admin(state, sender_id, item.get("chat_id")):
            continue
        delay = THROTTLE_DELAY_STEP_SECONDS * (position - MAX_PER_SENDER)
        delay = min(delay, float(THROTTLE_MAX_DELAY_SECONDS))
        new_wake_after = now + delay
        existing = item.get("wake_after")
        if existing is not None and existing >= new_wake_after:
            continue
        item["wake_after"] = new_wake_after
        _log_throttle("sender-limit", sender_id, item.get("chat_id"))
    # Per-chat: a chat at the wake cap has its pending items delayed.
    for item in state.get("queue", []):
        if item.get("platform") != PLATFORM:
            continue
        if item.get("status") != queue_core.PENDING:
            continue
        if item.get("replied_at") is not None:
            continue
        if item.get("chat_type") not in ("group", "supergroup"):
            continue
        sender_id = item.get("sender_id")
        if sender_id is None:
            continue
        if sender_id == state.get("owner_id"):
            continue
        if _is_group_admin(state, sender_id, item.get("chat_id")):
            continue
        chat_times = rate_state["chats"].get(str(item.get("chat_id")), [])
        chat_count = _count_in_window(chat_times, now, CHAT_WINDOW_SECONDS)
        if chat_count < MAX_WAKES_PER_CHAT:
            continue
        new_wake_after = now + THROTTLE_CHAT_DELAY_SECONDS
        new_wake_after = min(new_wake_after, now + THROTTLE_MAX_DELAY_SECONDS)
        existing = item.get("wake_after")
        if existing is not None and existing >= new_wake_after:
            continue
        item["wake_after"] = new_wake_after
        _log_throttle("chat-limit", sender_id, item.get("chat_id"))


def fetch_group_admins(chat_id, api_call=api):
    """Return (creator_id, [admin_ids]) for a group chat.

    Calls getChatAdministrators. The creator counts as an admin.
    Any failure returns (None, []): a missing admin list means
    commands stay owner-only, never stranger-open.
    """
    try:
        result = api_call("getChatAdministrators", {"chat_id": chat_id})
    except Exception:
        return None, []
    members = (result.get("result") or []) if isinstance(result, dict) else []
    creator_id = None
    admin_ids = []
    for member in members:
        user = member.get("user") or {}
        user_id = user.get("id")
        if user_id is None:
            continue
        status = member.get("status")
        if status == "creator":
            creator_id = user_id
            admin_ids.append(user_id)
        elif status == "administrator":
            admin_ids.append(user_id)
    return creator_id, admin_ids


def refresh_group_admins(state: dict, chat_id, *, force: bool = False,
                         now: float | None = None, api_call=api) -> dict | None:
    """Fetch and cache the admin list for an adopted group, if it is
    missing, stale, or for a different chat. A re-fetch is at most
    once per ADMIN_REFRESH_MIN_SECONDS unless forced. Returns the
    cached record, or None when the group is not adopted or no list
    could be fetched. Never raises."""
    now = time.time() if now is None else now
    group = _group_record(state, chat_id)
    if group is None:
        return None
    record = group.get("admins")
    if (
        record is not None
        and record.get("chat_id") == chat_id
        and not force
        and now - float(record.get("fetched_at") or 0) < ADMIN_REFRESH_MIN_SECONDS
    ):
        return record
    creator_id, admin_ids = fetch_group_admins(chat_id, api_call=api_call)
    if creator_id is None and not admin_ids:
        # Fetch failed: keep whatever was cached (possibly None).
        return record
    record = {
        "chat_id": chat_id,
        "creator_id": creator_id,
        "admin_ids": admin_ids,
        "fetched_at": now,
    }
    group["admins"] = record
    return record


def _is_group_admin(state: dict, sender_id, chat_id=None) -> bool:
    """True when the sender is the creator or an admin of the given
    group, per that group's cached admin list. With no chat id, true
    when the sender is an admin of ANY adopted group (the fallback
    for callers that do not know the lane). No list means not an
    admin."""
    if sender_id is None:
        return False
    if chat_id is not None:
        group = _group_record(state, chat_id)
        if group is None:
            return False
        record = group.get("admins") or {}
        if sender_id == record.get("creator_id"):
            return True
        return sender_id in (record.get("admin_ids") or [])
    for group in (state.get("groups") or {}).values():
        record = (group or {}).get("admins") or {}
        if sender_id == record.get("creator_id"):
            return True
        if sender_id in (record.get("admin_ids") or []):
            return True
    return False


def _is_command_allowed(state: dict, sender_id, chat_type: str,
                        chat_id=None) -> bool:
    """A command may run when the sender is the owner (or it is a
    private DM, which intake only queues for the owner) or a group
    admin of the group the command arrived in."""
    if sender_id is not None and sender_id == state.get("owner_id"):
        return True
    if chat_type == "private":
        return True
    return _is_group_admin(state, sender_id, chat_id)


# ---------------------------------------------------------------------------
# Group sharing mode (trusted / guarded) and the safety trip.
#
# The mode is PER GROUP, stored in each group's record
# (state["groups"][chat_id]["mode"]). Only Muse, on the owner's
# instruction, changes a group's mode (--set-group-mode, with that
# group's chat id) or edits the trusted_bots list
# (--set-trusted-bots). There is no code path from any Telegram
# message or command to either field: /trust, /guard, /mode,
# /trustbot, /untrustbot are not commands and do nothing.
#
# The setup default (state["default_group_mode"], from --adopt) is
# the mode the FIRST group adopted gets; every group adopted after
# that starts guarded (the safe default) until Muse sets its mode.
#
# The safety trip is a runtime guard, NOT a mode change, and it is
# per-group. In a trusted group, if someone who should not be
# there is detected — a message from a sender who is neither the
# owner nor a trusted bot, or a member count above the expected
# trusted count — the trip fires: personal-detail sharing stops
# (the group behaves as guarded) and a one-time notice is recorded
# for the next wake. The stored mode stays "trusted"; only Muse
# clears the trip, on the owner's instruction, after the owner
# decides what to do. A trip in one group never affects another.
#
# The allowlist (state["trusted_group_ids"]) exempts one group at a
# time from the trips: the owner's private channel, always trusted,
# no matter who joins. It is set only by Muse (--allowlist-group),
# never from Telegram. A listed group skips both the stranger check
# and the member-count check, and its effective mode ignores any
# trip record (a trip fired before the listing is harmless).
# ---------------------------------------------------------------------------

def _trusted_bot_names(state: dict) -> set:
    """Lowercase bot usernames in the trusted list (this bot itself,
    plus the owner's work / business bots when added). Entries are
    usernames; a purely numeric entry is a Telegram user id."""
    names = set()
    for entry in state.get("trusted_bots") or []:
        if entry is None:
            continue
        names.add(str(entry).strip().lstrip("@").lower())
    return names


def _is_trusted_bot(state: dict, sender: dict) -> bool:
    """True when the sender is a bot in the trusted list, matched by
    username (case-insensitive) or by numeric user id. A sender with
    no username and no matching id is outside the trusted list: the safe default."""
    if not isinstance(sender, dict) or not sender.get("is_bot"):
        return False
    names = _trusted_bot_names(state)
    username = (sender.get("username") or "").strip().lower()
    if username and username in names:
        return True
    sender_id = sender.get("id")
    if sender_id is not None and str(sender_id) in names:
        return True
    return False


def _trusted_group_ids(state: dict) -> set:
    """Group chat ids on the owner's private-channel allowlist
    (ints; non-numeric entries are ignored)."""
    ids = set()
    for entry in state.get("trusted_group_ids") or []:
        try:
            ids.add(int(entry))
        except (TypeError, ValueError):
            continue
    return ids


def is_group_allowlisted(state: dict, chat_id) -> bool:
    """True when the group is on the allowlist: the owner's private
    channel, always trusted. The safety trips never apply there, no
    matter who joins. The list is set only by Muse, on the owner's
    instruction (--allowlist-group), never from Telegram."""
    if chat_id is None:
        return False
    try:
        return int(chat_id) in _trusted_group_ids(state)
    except (TypeError, ValueError):
        return False


def expected_trusted_count(state: dict) -> int:
    """Members a fully trusted group holds: the owner plus every
    trusted bot (the list includes this bot itself)."""
    return 1 + len(_trusted_bot_names(state))


def fetch_chat_member_count(chat_id, api_call=api):
    """Return the group's member count (getChatMemberCount), or None
    when the call fails. A missing count means the trip cannot use
    it; it never trips on its own."""
    try:
        result = api_call("getChatMemberCount", {"chat_id": chat_id})
    except Exception:
        return None
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    count = result.get("result")
    return count if isinstance(count, int) and count >= 0 else None


def refresh_group_member_count(state: dict, chat_id, *, force: bool = False,
                               now: float | None = None, api_call=api):
    """Fetch and cache the member count for an adopted group, if it
    is missing, stale, or for a different chat. A re-fetch is at
    most once per MEMBER_COUNT_REFRESH_MIN_SECONDS unless forced.
    Returns the cached record, or None when the group is not adopted
    or no count could be fetched. Never raises."""
    now = time.time() if now is None else now
    group = _group_record(state, chat_id)
    if group is None:
        return None
    record = group.get("member_count")
    if (
        record is not None
        and record.get("chat_id") == chat_id
        and not force
        and now - float(record.get("fetched_at") or 0) < MEMBER_COUNT_REFRESH_MIN_SECONDS
    ):
        return record
    count = fetch_chat_member_count(chat_id, api_call=api_call)
    if count is None:
        # Fetch failed: keep whatever was cached (possibly None).
        return record
    record = {"chat_id": chat_id, "count": count, "fetched_at": now}
    group["member_count"] = record
    return record


def _trip_group_safety(state: dict, chat_id, reason: str, detail: str,
                       now: float | None = None) -> bool:
    """Fire the safety trip for one group (trusted mode only).
    Records the trip and a one-time notice for the next wake, in
    that group's record. Returns True when it newly fired; when
    already tripped it keeps the original reason and returns False.
    The stored mode is NOT changed, and no other group is touched."""
    group = _group_record(state, chat_id)
    if group is None:
        return False
    if group.get("safety_tripped"):
        return False
    now = time.time() if now is None else now
    record = {"reason": reason, "detail": detail or "", "at": now}
    group["safety_tripped"] = record
    group["mode_notice"] = dict(record)
    return True


def _group_mode(state: dict, chat_id) -> str:
    """The configured sharing mode of one group (guarded when the
    group is not adopted or has no mode)."""
    group = _group_record(state, chat_id)
    if group is None:
        return GROUP_MODE_GUARDED
    return group.get("mode") or GROUP_MODE_GUARDED


def _group_tripped(state: dict, chat_id) -> bool:
    """True when the safety trip is active for one group."""
    group = _group_record(state, chat_id)
    return bool(group and group.get("safety_tripped"))


def check_group_member_count(state: dict, chat_id, *, now: float | None = None,
                             api_call=api) -> bool:
    """Trip the safety when the member count exceeds the expected
    trusted count (someone extra is in the group, silent or not).
    Only runs in trusted mode and only when not already tripped.
    A count at or below the expected count, or a failed fetch,
    never trips. An allowlisted group never trips here: the trips
    do not apply to the owner's private channel, no matter who
    joins. The trip is recorded in THIS group's record only; no
    other group is affected. Returns True when the trip is active
    afterwards."""
    if _group_mode(state, chat_id) != GROUP_MODE_TRUSTED:
        return _group_tripped(state, chat_id)
    if is_group_allowlisted(state, chat_id):
        return _group_tripped(state, chat_id)
    if _group_tripped(state, chat_id):
        return True
    record = refresh_group_member_count(
        state, chat_id, now=now, api_call=api_call
    )
    if record is None:
        return False
    count = record.get("count")
    if isinstance(count, int) and count > expected_trusted_count(state):
        _trip_group_safety(
            state, chat_id, "member_count",
            f"group has {count} members, expected {expected_trusted_count(state)}",
            now,
        )
        return True
    return False


def effective_group_mode(state: dict, chat_type: str | None,
                         chat_id=None) -> str:
    """The mode the agent must apply for a lane. A private DM is
    always trusted (it is just the owner). A group is guarded when
    its configured mode is guarded OR its safety trip is active;
    otherwise it is trusted. An allowlisted group is always trusted
    (its configured mode applies): the safety trips never apply
    there, no matter who joins, so an active trip record is ignored
    for that group. The mode and the trip are per-group: another
    group's mode or trip never changes this lane."""
    if chat_type == "private":
        return GROUP_MODE_TRUSTED
    if _group_mode(state, chat_id) != GROUP_MODE_TRUSTED:
        return GROUP_MODE_GUARDED
    if is_group_allowlisted(state, chat_id):
        return GROUP_MODE_TRUSTED
    if _group_tripped(state, chat_id):
        return GROUP_MODE_GUARDED
    return GROUP_MODE_TRUSTED


def answer_callback_query(callback_id, api_call=api) -> bool:
    """Dismiss a tapped button's loading spinner. Best effort: a
    failure must never break intake. Returns True when Telegram
    accepted it."""
    try:
        result = api_call(
            "answerCallbackQuery", {"callback_query_id": callback_id}
        )
        return bool(result.get("ok"))
    except Exception:
        return False


def recent_speakers(buffer: list, now: float, window_seconds: float = 3600) -> list:
    """Who spoke in one group recently, from that group's buffer.

    Returns [{"sender_name", "last_seen" (epoch), "messages": count}]
    sorted most-recent first, one entry per sender, limited to
    entries inside the window. Pure function, for /active."""
    by_sender: dict = {}
    for entry in buffer or []:
        date = entry.get("date")
        if date is None:
            continue
        if now - float(date) > window_seconds:
            continue
        name = entry.get("sender_name") or "?"
        record = by_sender.get(name)
        if record is None:
            by_sender[name] = {
                "sender_name": name,
                "last_seen": float(date),
                "messages": 1,
            }
        else:
            record["messages"] += 1
            record["last_seen"] = max(record["last_seen"], float(date))
    return sorted(
        by_sender.values(), key=lambda r: r["last_seen"], reverse=True
    )


def find_item_by_message_id(state: dict, platform: str, message_id):
    """The queue item for a Telegram message id, or None. Used by
    edited_message handling. Compares as strings so int/str
    mismatches cannot hide a match."""
    if message_id is None:
        return None
    wanted = str(message_id)
    for item in state.get("queue", []):
        if item.get("platform") != platform:
            continue
        if item.get("message_id") is None:
            continue
        if str(item["message_id"]) == wanted:
            return item
    return None


#: Environment variable naming the directory where downloaded voice
#: messages are saved, so the woken agent can transcribe them.
VOICE_DIR_ENV = "TELEGRAM_BRIDGE_VOICE_DIR"

#: Maximum voice note duration (seconds) the bridge downloads. A
#: longer note is queued with a marker asking for a shorter one
#: instead of being downloaded.
MAX_VOICE_DURATION_SECONDS = 300

#: Maximum voice file size (bytes) the bridge downloads. Telegram caps
#: Bot API downloads at 20MB; this is a tighter backstop for when the
#: duration is missing or the file is unusually large.
MAX_VOICE_BYTES = 10 * 1024 * 1024

#: Days after which downloaded voice files and transcript outputs are pruned.
VOICE_RETENTION_DAYS = 30
VOICE_RETENTION_SECONDS = VOICE_RETENTION_DAYS * 86400

#: Safe characters for a Telegram file_path (getFile result). Anything
#: else — including ".." — is rejected before it reaches a URL.
_FILE_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")


def _valid_file_path(file_path) -> bool:
    """A Telegram file_path is `<type>/file_<n>.<ext>`; reject anything
    with "..", a leading slash, or characters outside the safe set."""
    if not isinstance(file_path, str) or not file_path:
        return False
    if ".." in file_path or file_path.startswith("/"):
        return False
    return bool(_FILE_PATH_RE.match(file_path))


def prune_old_voice_files() -> int:
    """Delete voice files and transcript outputs older than 30 days, and
    tighten modes (0700 on the voice/ and transcripts/ directories,
    0600 on surviving transcript files). Best effort; never raises.
    Returns the number of entries removed."""
    removed = 0
    cutoff = time.time() - VOICE_RETENTION_SECONDS
    try:
        vdir = Path(
            os.environ.get(
                VOICE_DIR_ENV,
                os.path.expanduser("~/workspace/telegram-bridge/voice"),
            )
        )
        base = vdir.parent
        for top in (vdir, base / "transcripts"):
            if not top.is_dir():
                continue
            try:
                os.chmod(top, 0o700)
            except OSError:
                pass
            for child in list(top.iterdir()):
                try:
                    if child.stat().st_mtime < cutoff:
                        if child.is_dir():
                            shutil.rmtree(child, ignore_errors=True)
                        else:
                            child.unlink()
                        removed += 1
                    elif child.is_dir():
                        # Surviving transcript dir: tighten its mode and
                        # the modes inside it.
                        try:
                            os.chmod(child, 0o700)
                        except OSError:
                            pass
                        for root, dirs, files in os.walk(child):
                            for d in dirs:
                                try:
                                    os.chmod(Path(root) / d, 0o700)
                                except OSError:
                                    pass
                            for f in files:
                                try:
                                    os.chmod(Path(root) / f, 0o600)
                                except OSError:
                                    pass
                    elif child.is_file():
                        # Surviving voice file: tighten its mode.
                        try:
                            os.chmod(child, 0o600)
                        except OSError:
                            pass
                except OSError:
                    continue
    except Exception:
        pass
    return removed


def voice_dir() -> Path | None:
    """Directory for downloaded voice messages; created on demand.

    Returns None when the directory cannot be created (disk full,
    permissions): the caller treats that as a failed download and
    queues the marker instead of aborting the whole ingest.
    The directory is mode 0700. Old voice files are NO LONGER
    pruned here: pruning walks the filesystem, and this function
    runs inside ingest, inside the state transaction — a prune
    walk must never hold the state lock (see _maybe_prune_files,
    run once per 24 h after the transaction commits).
    """
    path = Path(
        os.environ.get(
            VOICE_DIR_ENV, os.path.expanduser("~/workspace/telegram-bridge/voice")
        )
    )
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError:
        return None
    return path


def download_voice_file(file_id: str, update_id) -> Path | None:
    """Download a Telegram voice/audio file by its file_id.

    Calls getFile for the file path, then downloads the file from
    api.telegram.org with the raw surrogate, exactly like the intake
    and the sender build their URLs. Returns the local path, or None
    when the file cannot be fetched. Never prints the surrogate.
    The download is capped at MAX_VOICE_BYTES with a streaming read,
    so a huge file is refused without ever holding it all in memory,
    and the saved file is mode 0600.
    """
    try:
        result = api("getFile", {"file_id": file_id})
    except Exception:
        return None
    file_path = (result.get("result") or {}).get("file_path")
    if not _valid_file_path(file_path):
        return None
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    from dynamic_credentials import dynamic_credential_entry

    try:
        entry = dynamic_credential_entry(CREDENTIAL)
        surrogate = str(entry["surrogate"]).strip()
    except Exception:
        return None
    url = f"https://api.telegram.org/file/bot{surrogate}/{file_path}"
    ext = Path(file_path).suffix or ".ogg"
    vdir = voice_dir()
    if vdir is None:
        return None
    dest = vdir / f"{update_id}{ext}"
    try:
        request = urllib.request.Request(url, method="GET")
        with _urlopen_no_redirect(request, 30) as response:
            total = 0
            chunks = []
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_VOICE_BYTES:
                    return None
                chunks.append(chunk)
            dest.write_bytes(b"".join(chunks))
    except Exception:
        return None
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return dest


def voice_marker(update: dict, message: dict, voice: dict) -> str:
    """Marker text for a voice/audio message, after downloading it.

    The agent transcribes the saved audio and replies to what was
    said. When the download fails the marker says so, so the agent
    can ask the sender to resend instead of the message vanishing.
    A note longer than MAX_VOICE_DURATION_SECONDS is not downloaded
    at all: the marker asks for a shorter one instead, so a 1-2 hour
    recording cannot tie up an agent turn. The download runs inside
    ingest (and therefore inside the state transaction): the file must
    be local before the agent wakes, and a voice note is small enough
    that the fetch is quick.
    """
    duration = voice.get("duration")
    if isinstance(duration, (int, float)) and duration > MAX_VOICE_DURATION_SECONDS:
        return (
            f"[voice message, {duration}s] This voice note is longer than "
            f"{MAX_VOICE_DURATION_SECONDS // 60} minutes, so it was not "
            "downloaded. Reply asking the sender to send a shorter voice "
            "note (under 5 minutes)."
        )
    dest = download_voice_file(str(voice.get("file_id", "")), update.get("update_id"))
    if dest is None:
        return "[voice message] I could not download the audio. Ask the sender to send it again."
    if duration is not None:
        return (
            f"[voice message, {duration}s] Audio saved to {dest}. "
            "Transcribe it with the local Whisper helper and reply to what was said."
        )
    return (
        f"[voice message] Audio saved to {dest}. "
        "Transcribe it with the local Whisper helper and reply to what was said."
    )


#: Environment variable naming the directory where downloaded
#: photos and documents are saved, so the woken agent can view them.
MEDIA_DIR_ENV = "TELEGRAM_BRIDGE_MEDIA_DIR"

#: Maximum inbound file size (bytes) the bridge downloads. Telegram
#: caps Bot API downloads at 20MB; a file larger than that is queued
#: with a "too big" note instead of being downloaded.
MAX_MEDIA_BYTES = 20 * 1024 * 1024

#: Days after which downloaded media files are pruned.
MEDIA_RETENTION_DAYS = 30
MEDIA_RETENTION_SECONDS = MEDIA_RETENTION_DAYS * 86400

#: Safe characters for a saved media file name. Anything else is
#: replaced with "_", so a sender-supplied file name can never
#: escape the media directory or smuggle in shell metacharacters.
_MEDIA_NAME_RE = re.compile(r"[^A-Za-z0-9_.\-]+")


def _safe_media_filename(name, file_id: str, fallback_ext: str = "") -> str:
    """A sender-supplied file name made safe to save.

    Keeps only the base name (no directories), replaces unsafe
    characters, and falls back to ``<file_id><ext>`` when nothing
    usable remains. Never returns a dot-file or an empty name."""
    base = Path(str(name or "")).name if name else ""
    base = _MEDIA_NAME_RE.sub("_", base).strip(".")
    if not base or base in (".", ".."):
        safe_id = _MEDIA_NAME_RE.sub("_", str(file_id or "file"))
        base = f"{safe_id}{fallback_ext}"
    return base[:100]


def prune_old_media_files() -> int:
    """Delete media files older than 30 days, and tighten modes
    (0700 on the media directory and per-update subdirectories,
    0600 on surviving files). Best effort; never raises. Returns
    the number of entries removed."""
    removed = 0
    cutoff = time.time() - MEDIA_RETENTION_SECONDS
    try:
        mdir = Path(
            os.environ.get(
                MEDIA_DIR_ENV,
                os.path.expanduser("~/workspace/telegram-bridge/media"),
            )
        )
        if not mdir.is_dir():
            return 0
        try:
            os.chmod(mdir, 0o700)
        except OSError:
            pass
        for child in list(mdir.iterdir()):
            try:
                if child.stat().st_mtime < cutoff:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink()
                    removed += 1
                elif child.is_dir():
                    try:
                        os.chmod(child, 0o700)
                    except OSError:
                        pass
                    for root, dirs, files in os.walk(child):
                        for d in dirs:
                            try:
                                os.chmod(Path(root) / d, 0o700)
                            except OSError:
                                pass
                        for f in files:
                            try:
                                os.chmod(Path(root) / f, 0o600)
                            except OSError:
                                pass
                elif child.is_file():
                    try:
                        os.chmod(child, 0o600)
                    except OSError:
                        pass
            except OSError:
                continue
    except Exception:
        pass
    return removed


def media_dir() -> Path | None:
    """Directory for downloaded photos and documents; created on demand.

    Returns None when the directory cannot be created: the caller
    treats that as a failed download and queues the marker instead
    of aborting the whole ingest. The directory is mode 0700. Old
    media files are NO LONGER pruned here (see voice_dir): pruning
    walks the filesystem and must never run inside the state
    transaction — see _maybe_prune_files.
    """
    path = Path(
        os.environ.get(
            MEDIA_DIR_ENV,
            os.path.expanduser("~/workspace/telegram-bridge/media"),
        )
    )
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError:
        return None
    return path


def _maybe_prune_files(store: JsonStateStore) -> int:
    """Prune old voice/media files, throttled to once per 24 h.

    Runs OUTSIDE any state transaction (run_once calls this after
    the ingest transaction commits): pruning walks the filesystem
    and must never hold the state lock. A marker file
    ``.last_prune`` next to the state file records the last run;
    a missing or unreadable marker means "prune now". Best
    effort; never raises. Returns files removed (0 when throttled).

    Remaining limitation (documented, not fixed here): voice/media
    DOWNLOADS still happen inside ingest, inside the transaction
    (``voice_marker`` / ``media_marker`` call the downloaders from
    ``_ingest_update``). Moving them out needs a prefetch step in
    run_once that downloads before opening the transaction and
    hands the local paths to ingest — a bigger restructure than
    this pass; the prune walks, which were the unbounded
    filesystem work inside the lock, are out.
    """
    try:
        marker = Path(store.path).parent / ".last_prune"
        now = time.time()
        try:
            last = float(marker.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            last = 0.0
        if now - last < 86400.0:
            return 0
        removed = prune_old_voice_files() + prune_old_media_files()
        try:
            marker.write_text(str(now), encoding="utf-8")
        except OSError:
            pass
        return removed
    except Exception:
        return 0


def download_media_file(file_id: str, update_id, filename_hint=None):
    """Download a Telegram photo/document file by its file_id.

    Same pattern as the voice download: getFile for the file path
    (validated), then download from api.telegram.org with the raw
    surrogate and NoRedirect. The file is saved under
    ``media/<update_id>/<safe name>`` (mode 0600, dirs 0700).

    Returns ``(path, error)``: ``(Path, None)`` on success,
    ``(None, "too-big")`` when the file exceeds MAX_MEDIA_BYTES
    (checked from getFile's file_size first, and again while
    streaming, so a huge file is refused without ever holding it
    all in memory), and ``(None, "failed")`` for any other failure.
    A partial download is removed, never left behind.
    """
    try:
        result = api("getFile", {"file_id": file_id})
    except Exception:
        return None, "failed"
    info = (result.get("result") or {}) if isinstance(result, dict) else {}
    file_path = info.get("file_path")
    if not _valid_file_path(file_path):
        return None, "failed"
    file_size = info.get("file_size")
    if isinstance(file_size, (int, float)) and file_size > MAX_MEDIA_BYTES:
        return None, "too-big"
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    from dynamic_credentials import dynamic_credential_entry

    try:
        entry = dynamic_credential_entry(CREDENTIAL)
        surrogate = str(entry["surrogate"]).strip()
    except Exception:
        return None, "failed"
    url = f"https://api.telegram.org/file/bot{surrogate}/{file_path}"
    mdir = media_dir()
    if mdir is None:
        return None, "failed"
    subdir = mdir / str(update_id)
    try:
        subdir.mkdir(parents=True, exist_ok=True)
        os.chmod(subdir, 0o700)
    except OSError:
        return None, "failed"
    hint = filename_hint or Path(file_path).name
    ext = Path(file_path).suffix
    dest = subdir / _safe_media_filename(hint, file_id, fallback_ext=ext)
    try:
        request = urllib.request.Request(url, method="GET")
        with _urlopen_no_redirect(request, 30) as response:
            total = 0
            chunks = []
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:
                    try:
                        dest.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return None, "too-big"
                chunks.append(chunk)
            dest.write_bytes(b"".join(chunks))
    except Exception:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return None, "failed"
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return dest, None


def _largest_photo_size(photo: list):
    """The largest PhotoSize in a message's photo array, by file_size
    (falling back to pixel area, then to the last entry — Telegram
    lists sizes ascending). Returns None for an empty list."""
    if not photo:
        return None
    def _area(size):
        try:
            return int(size.get("width") or 0) * int(size.get("height") or 0)
        except (TypeError, ValueError):
            return 0
    def _key(size):
        file_size = size.get("file_size")
        if isinstance(file_size, (int, float)):
            return (file_size, _area(size))
        return (0, _area(size))
    return max(photo, key=_key)


def media_marker(update: dict, message: dict, kind: str, file_obj: dict) -> str:
    """Marker text for a photo or document message, after downloading it.

    The agent views the saved image (photo) or reads the saved file
    (document) and replies to what it shows or says. The caption, if
    any, is carried in the marker. When the download fails or the
    file is too big, the marker says so instead of the message
    vanishing: a photo with no caption is still queued, never
    silently dropped.
    """
    caption = message.get("caption") or ""
    if kind == "photo":
        file_id = str(file_obj.get("file_id", ""))
        dest, error = download_media_file(file_id, update.get("update_id"))
        if error == "too-big":
            return (
                "[photo] This photo is larger than 20MB, so it was not "
                "downloaded. Reply asking the sender to send a smaller file."
            )
        if dest is None:
            return "[photo] I could not download the image. Ask the sender to send it again."
        text = f"[photo] Image saved to {dest}. View the image and reply to what it shows."
    else:
        file_name = file_obj.get("file_name") or "file"
        file_id = str(file_obj.get("file_id", ""))
        dest, error = download_media_file(
            file_id, update.get("update_id"), filename_hint=file_name
        )
        if error == "too-big":
            return (
                f"[document {file_name}] This file is larger than 20MB, so "
                "it was not downloaded. Reply asking the sender to send a "
                "smaller file."
            )
        if dest is None:
            return (
                f"[document {file_name}] I could not download the file. "
                "Ask the sender to send it again."
            )
        text = (
            f"[document {file_name}] File saved to {dest}. "
            "Read the file and reply to what it says."
        )
    if caption:
        text += f" Caption: {caption}"
    return text


def sender_label(user: dict) -> str:
    if user.get("username"):
        return "@" + user["username"]
    name = " ".join(p for p in [user.get("first_name"), user.get("last_name")] if p)
    return name or str(user.get("id"))


def _group_key(chat_id):
    """The groups-dict key for a chat id (JSON keys are strings), or
    None when the id is not an integer chat id."""
    try:
        return str(int(chat_id))
    except (TypeError, ValueError):
        return None


def _group_record(state: dict, chat_id, *, create: bool = False):
    """The per-group record for an adopted group, or None when the
    group is not adopted. With create=True, adopt it: the record is
    created and the group id registered in group_ids (adoption
    order). The FIRST group adopted gets the setup default mode
    (state["default_group_mode"]); every group after that starts
    guarded (the safe default) until Muse sets its mode."""
    key = _group_key(chat_id)
    if key is None:
        return None
    groups = state.get("groups")
    if not isinstance(groups, dict):
        groups = state["groups"] = {}
    record = groups.get(key)
    if record is None and create:
        ids = state.get("group_ids")
        if not isinstance(ids, list):
            ids = state["group_ids"] = []
        if not ids:
            mode = state.get("default_group_mode") or GROUP_MODE_GUARDED
        else:
            mode = GROUP_MODE_GUARDED
        record = {
            "mode": mode,
            "admins": None,
            "member_count": None,
            "safety_tripped": None,
            "mode_notice": None,
            "buffer": [],
        }
        groups[key] = record
        if int(chat_id) not in ids:
            ids.append(int(chat_id))
    if record is not None:
        # Normalize a record that predates a field (or was built by
        # an older version): every reader assumes these keys exist,
        # so a missing one must not raise. A record without a buffer
        # used to crash intake with KeyError on the next group
        # message; now it just starts empty.
        if not isinstance(record, dict):
            return None
        record.setdefault("mode", GROUP_MODE_GUARDED)
        record.setdefault("admins", None)
        record.setdefault("member_count", None)
        record.setdefault("safety_tripped", None)
        record.setdefault("mode_notice", None)
        record.setdefault("buffer", [])
    return record


def _adopt_group(state: dict, chat_id, *, now: float | None = None,
                 api_call=api):
    """Adopt a group the owner just acted in: create its record and
    fetch its admin list. Returns the record, or None when the chat
    id is not usable. Adoption is what makes the bridge serve a
    group; messages from groups that are not adopted are ignored."""
    record = _group_record(state, chat_id, create=True)
    if record is not None:
        refresh_group_admins(state, chat_id, force=True, now=now,
                             api_call=api_call)
    return record


def _migrate_legacy_group_state(state: dict) -> None:
    """One-time migration from the single-group state shape
    (group_id / group_mode / group_admins / group_member_count /
    group_safety_tripped / group_mode_notice / group_buffer) to the
    per-group shape (groups + group_ids + default_group_mode).

    The adopted group's record takes the legacy mode, admin list,
    member count, trip, notice and buffer. The setup default
    (default_group_mode) takes the legacy mode when no group was
    adopted yet (the setup choice is still unspent); when a group
    was adopted, the setup choice was spent on it, so the default
    for later groups is guarded. The legacy keys are removed: the
    per-group record is the single source of truth afterwards.
    Idempotent: a state with no legacy keys is untouched."""
    legacy_keys = (
        "group_id", "group_mode", "group_admins", "group_member_count",
        "group_safety_tripped", "group_mode_notice", "group_buffer",
    )
    if not any(key in state for key in legacy_keys):
        return
    group_id = state.get("group_id")
    legacy_mode = state.get("group_mode") or GROUP_MODE_GUARDED
    if group_id is not None:
        record = _group_record(state, group_id, create=True)
        if record is not None:
            record["mode"] = legacy_mode
            record["admins"] = state.get("group_admins")
            record["member_count"] = state.get("group_member_count")
            record["safety_tripped"] = state.get("group_safety_tripped")
            record["mode_notice"] = state.get("group_mode_notice")
            buffer = state.get("group_buffer")
            record["buffer"] = list(buffer) if isinstance(buffer, list) else []
    if "default_group_mode" not in state:
        # No group adopted yet: the setup choice is still unspent.
        # A group was adopted: the choice was spent on it, and
        # later groups start guarded.
        state["default_group_mode"] = (
            legacy_mode if group_id is None else GROUP_MODE_GUARDED
        )
    for key in legacy_keys:
        state.pop(key, None)


def _ensure_adapter_state(state: dict) -> dict:
    queue_core.ensure_state(state)
    state.setdefault("offset", 0)
    state.setdefault("owner_id", None)
    # Migrate a single-group state FIRST: it fills in groups /
    # group_ids / default_group_mode from the legacy keys, and the
    # setdefaults below must not run before it (they would make
    # the migration think the defaults are already set).
    _migrate_legacy_group_state(state)
    # Per-group state: "groups" maps str(chat_id) to one record per
    # adopted group (mode, admins, member_count, safety_tripped,
    # mode_notice, buffer); "group_ids" is the adopted groups in
    # adoption order. The bridge serves every adopted group.
    state.setdefault("groups", {})
    state.setdefault("group_ids", [])
    # Setup default mode (--adopt's GROUP_MODE, default guarded):
    # the FIRST group adopted gets this mode; every group after
    # that starts guarded until Muse sets its mode.
    state.setdefault("default_group_mode", GROUP_MODE_GUARDED)
    # Bot usernames the owner trusts in the group (his own Muse
    # bots). Seeded with this bot itself; the owner adds work /
    # business bots when they exist, via Muse (--set-trusted-bots),
    # never from Telegram.
    state.setdefault("trusted_bots", [_bot_username()])
    # Group chat ids on the private-channel allowlist: a listed
    # group is always trusted and the safety trips never apply
    # there, no matter who joins. Set only by Muse, on the owner's
    # instruction (--allowlist-group), never from Telegram.
    state.setdefault("trusted_group_ids", [])
    state.setdefault("candidates", [])
    state.setdefault("candidate_groups", [])
    # Messages that could not be enqueued at all, surfaced loudly
    # with their update id and text (see _record_unanswered). A
    # message is never silently dropped; this is the last-resort
    # record when even a minimal hold in pending is impossible.
    state.setdefault("intake_unanswered", [])
    return state


def _has_active_work(state: dict, chat_id, exclude_external_id=None,
                     exclude_external_ids=None) -> bool:
    """True when the chat lane has unfinished work (a non-done item
    that is a request, was ever claimed, or is in flight). The
    classifier uses it so a greeting never overrides open work.
    ``exclude_external_ids`` (a set) skips a whole batch, so a batch
    does not count as active work for itself when the payload is
    built."""
    excluded = set(exclude_external_ids or ())
    if exclude_external_id is not None:
        excluded.add(str(exclude_external_id))
    for item in state.get("queue", []):
        if str(item.get("external_id")) in excluded:
            continue
        if item.get("platform") != PLATFORM:
            continue
        if item.get("chat_id") != chat_id:
            continue
        if item.get("status") == queue_core.DONE:
            continue
        if (
            item.get("item_class") != queue_core.CLASS_CONVERSATION
            or item.get("ever_claimed")
            or item.get("status") in queue_core.IN_FLIGHT
        ):
            return True
    return False


def _handle_callback_query(state: dict, update: dict, callback: dict,
                           api_call=api) -> bool:
    """A button tap: dismiss the spinner promptly, then queue the
    matching command so the agent handles it like a typed command.
    Returns True when an item was queued.

    A tap has no user message to thread under: callback["message"]
    is the BOT's own message (the one the keyboard is attached to).
    The item therefore carries message_id None and source
    "callback_query", so the reply goes out standalone — threading
    it under the bot's own message made it look like the bot was
    replying to itself. The send fence enforces this too: a
    reply_to that is not the target item's own message_id is
    refused (see send_fenced)."""
    cb_id = callback.get("id")
    if cb_id is not None:
        # Dismiss the loading spinner now, not after the agent turn:
        # Telegram expects an answer within a few seconds.
        answer_callback_query(cb_id, api_call=api_call)
    data = callback.get("data") or ""
    if not isinstance(data, str) or not data.startswith(CALLBACK_PREFIX):
        return False
    command = data[len(CALLBACK_PREFIX):]
    if command not in CALLBACK_COMMANDS:
        return False
    from_user = callback.get("from") or {}
    sender_id = from_user.get("id")
    msg = callback.get("message") or {}
    chat = msg.get("chat") or {}
    chat_type = chat.get("type")
    if sender_id is None:
        return False
    if chat_type not in ("private", "group", "supergroup"):
        return False
    if state["owner_id"] is None:
        if sender_id not in state["candidates"]:
            state["candidates"].append(sender_id)
        return False
    if chat_type in ("group", "supergroup"):
        # Adopt every group where the owner acts (a button tap
        # counts as the owner acting), and fetch its admin list.
        # A group that is not adopted is ignored.
        if sender_id == state["owner_id"]:
            _adopt_group(state, chat["id"], api_call=api_call)
        if _group_record(state, chat["id"]) is None:
            return False
    queue_core.enqueue(
        state,
        platform=PLATFORM,
        external_id=update["update_id"],
        chat_id=chat["id"],
        chat_type=chat_type,
        sender_id=sender_id,
        sender_name=sender_label(from_user),
        is_bot=False,
        text="/" + command,
        date=msg.get("date"),
        item_class=queue_core.CLASS_REQUEST,
        # Deliberately None: the button's message is the bot's own
        # message, not the user's — see the docstring above.
        message_id=None,
        source="callback_query",
    )
    return True


def _handle_edited_message(state: dict, edited: dict) -> bool:
    """Update a still-pending queued item's text when its message is
    edited. Claimed, answered or done items are untouched, and no new
    item is created: an edit never wakes a turn on its own. The
    classification is re-computed but never downgraded (an edit
    cannot turn protected work into expirable chatter)."""
    message_id = edited.get("message_id")
    item = find_item_by_message_id(state, PLATFORM, message_id)
    if item is None:
        return False
    if item.get("status") != queue_core.PENDING or item.get("replied_at") is not None:
        return False
    new_text = edited.get("text")
    if new_text is None:
        return False
    sender = edited.get("from") or {}
    sender_id = sender.get("id")
    new_class = classify_message(
        new_text,
        sender_is_owner=sender_id == state.get("owner_id"),
        bot_username=_bot_username(),
        is_reply=bool(
            edited.get("reply_to_message") or edited.get("reply_to_message_id")
        ),
        has_active_work=_has_active_work(
            state, item.get("chat_id"), exclude_external_id=item.get("external_id")
        ),
    )
    item["text"] = new_text
    # Never downgrade protection on an edit.
    if not (
        new_class == queue_core.CLASS_CONVERSATION
        and item.get("item_class") == queue_core.CLASS_REQUEST
    ):
        item["item_class"] = new_class
    group = _group_record(state, item.get("chat_id"))
    if group is not None:
        for entry in group.get("buffer", []):
            if entry.get("message_id") is not None and str(entry["message_id"]) == str(message_id):
                entry["text"] = new_text
    return True


#: Maximum unanswered-message records kept in the state (see
#: _record_unanswered). A message that cannot be enqueued even
#: minimally is surfaced here, loudly, with its update id and text
#: -- never silently dropped.
INTAKE_UNANSWERED_MAX = 20

#: Poison-update handling: an update that can neither be processed
#: nor held minimally is re-delivered on every poll and recorded as
#: unanswered each time. After this many consecutive failures the
#: offset advances past it anyway and a receipt is recorded, so a
#: later re-delivery is deduped instead of re-processed. The
#: unanswered records stay visible. A poison update pins the
#: offset only when it is the newest — an older poison update is
#: already passed by newer updates advancing the offset. An update
#: with NO update_id at all still cannot be skipped (there is
#: nothing to key the receipt on) and keeps being recorded loudly:
#: that is a deliberate limit, not an oversight.
INTAKE_POISON_SKIP_AFTER = 3
#: Bound on the poison counter dict (one counter per update id):
#: the oldest counters are dropped past this, so a stream of
#: distinct poison updates cannot grow state without bound.
INTAKE_POISON_MAX = 100


def _record_unanswered(state: dict, update_id, error, text, now: float) -> None:
    """Record a message that could not be enqueued at all.

    This is the last resort: the message cannot be answered (there
    is nothing to hold in pending), so it is surfaced loudly -- a
    bounded record in the state (visible to the operator) and a
    stderr line -- never silently closed. Never raises.
    """
    try:
        entry = {
            "update_id": update_id,
            "error": str(error),
            "text": text if isinstance(text, str) else "",
            "at": now,
        }
        unanswered = state.setdefault("intake_unanswered", [])
        unanswered.append(entry)
        del unanswered[:-INTAKE_UNANSWERED_MAX]
        # Watchdog v2 health counts intake errors from here.
        state["intake_errors"] = int(state.get("intake_errors") or 0) + 1
    except Exception:
        pass
    try:
        print(
            f"telegram intake: UNANSWERED update {update_id}: {error} "
            f"(text={text!r})",
            file=sys.stderr,
        )
    except Exception:
        pass


def _update_text(update) -> str:
    """Best-effort text of an update, for the unanswered log."""
    try:
        message = (update or {}).get("message") or {}
        text = message.get("text")
        if isinstance(text, str):
            return text
        if message.get("voice") or message.get("audio"):
            return "[voice message]"
        if message.get("photo") or message.get("document"):
            return "[photo/document]"
    except Exception:
        pass
    return ""


def _enqueue_minimal(state: dict, update: dict, now: float) -> bool:
    """Hold a message in pending when full processing failed.

    Enqueues the message with just its essentials (update id, chat,
    sender, text) so it stays pending and the next poll retries it
    instead of the message being lost. A voice/media message with no
    text gets a placeholder so the agent can still answer it (asking
    the sender to resend) rather than staying silent. Returns True
    when the message is now held (enqueued, or already enqueued),
    False when it cannot be held at all (missing essentials).
    Never raises.
    """
    try:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return False
        if queue_core.find_item(state, PLATFORM, update_id) is not None:
            return True
        message = update.get("message") or {}
        sender = message.get("from") or {}
        sender_id = sender.get("id")
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if sender_id is None or chat_id is None:
            return False
        text = message.get("text")
        if not isinstance(text, str):
            if message.get("voice") or message.get("audio"):
                text = "[voice message -- could not be processed, please send it again]"
            elif message.get("photo") or message.get("document"):
                text = "[photo/document -- could not be processed, please send it again]"
            else:
                text = "[message could not be processed]"
        queue_core.enqueue(
            state,
            platform=PLATFORM,
            external_id=update_id,
            chat_id=chat_id,
            chat_type=chat.get("type"),
            sender_id=sender_id,
            sender_name=sender_label(sender),
            is_bot=bool(sender.get("is_bot")),
            text=text,
            date=message.get("date"),
            item_class=queue_core.CLASS_REQUEST,
            message_id=message.get("message_id"),
            is_reply=bool(
                message.get("reply_to_message")
                or message.get("reply_to_message_id")
            ),
            now=now,
        )
        return True
    except Exception:
        return False


def _ingest_update(state: dict, update: dict, now: float, api_call=api) -> str:
    """Process one update (message, callback, edit, reaction, member).

    Raises on failure; the caller (ingest) holds the message in
    pending via a minimal enqueue instead of losing it. The update
    id is already validated by the caller.

    Returns an outcome string: "enqueued" when a queue item was
    created (or already existed), "duplicate" when the item was
    already queued, "handled:<what>" when the update was applied
    without a new item (e.g. an edit), or "closed:<reason>" when
    the update was deliberately closed as not-for-me (a reaction,
    a message in an unadopted group, a non-owner DM, ...). The
    caller (ingest) advances the offset ONLY on a returned
    outcome — never before the enqueue — so a failed update is
    re-delivered instead of lost (OpenClaw offset-after-spool-ack).
    """
    member = update.get("my_chat_member")
    if member:
        chat = member.get("chat") or {}
        if chat.get("type") in ("group", "supergroup"):
            if chat["id"] not in state["candidate_groups"]:
                state["candidate_groups"].append(chat["id"])
        return "closed:my-chat-member"

    callback = update.get("callback_query")
    if callback:
        queued = _handle_callback_query(state, update, callback, api_call=api_call)
        return "enqueued" if queued else "closed:callback-not-queued"

    edited = update.get("edited_message")
    if edited:
        applied = _handle_edited_message(state, edited)
        return "handled:edited" if applied else "closed:edit-no-pending-item"

    if update.get("message_reaction"):
        # A reaction is not work: it never wakes a turn and
        # never creates a queue item.
        return "closed:reaction"

    message = update.get("message") or {}
    sender = message.get("from") or {}
    sender_id = sender.get("id")
    chat = message.get("chat") or {}
    chat_type = chat.get("type")
    text = message.get("text")
    voice = message.get("voice") or message.get("audio")
    is_voice = bool(voice and not text)
    photo = message.get("photo")
    document = message.get("document")
    is_media = bool((photo or document) and not text)
    if sender_id is None or (not text and not voice and not is_media):
        return "closed:missing-sender-or-content"
    if is_voice:
        # A voice message has no text; download it and queue a
        # marker the agent transcribes. Without this the message
        # is consumed by the offset and silently lost.
        text = voice_marker(update, message, voice)
    if is_media:
        # A photo/document has no text (only a caption, if any);
        # download it and queue a marker carrying the saved file
        # path. A photo with no caption is still queued, never
        # silently dropped.
        if photo:
            largest = _largest_photo_size(photo)
            text = (
                media_marker(update, message, "photo", largest)
                if largest is not None
                else "[photo] I could not download the image. Ask the sender to send it again."
            )
        else:
            text = media_marker(update, message, "document", document)

    if state["owner_id"] is None:
        if sender_id not in state["candidates"]:
            state["candidates"].append(sender_id)
        return "closed:owner-not-adopted"

    # A reply is a follow-up to earlier work: protected,
    # whoever sent it (see classify_message). It is also stored
    # on the item so the tier classifier can see it.
    is_reply = bool(
        message.get("reply_to_message")
        or message.get("reply_to_message_id")
    )
    item_class = classify_message(
        text,
        sender_is_owner=sender_id == state["owner_id"],
        bot_username=_bot_username(),
        # Even a greeting can be a follow-up when work is unfinished.
        # Legacy/unclassified records are work too. Completed records
        # and never-claimed ordinary talk do not make a lane active.
        has_active_work=_has_active_work(state, chat.get("id")),
        is_reply=is_reply,
    )
    # A voice message is always a request: the agent must listen
    # and answer it, never sweep it as chatter.
    if is_voice:
        item_class = queue_core.CLASS_REQUEST
    # A photo or document is always a request too: the agent must
    # look at it and answer it, never sweep it as chatter.
    if is_media:
        item_class = queue_core.CLASS_REQUEST

    if chat_type == "private":
        if sender_id == state["owner_id"]:
            queued = queue_core.enqueue(
                state,
                platform=PLATFORM,
                external_id=update["update_id"],
                chat_id=chat["id"],
                chat_type="private",
                sender_id=sender_id,
                sender_name=sender_label(sender),
                is_bot=bool(sender.get("is_bot")),
                text=text,
                date=message.get("date"),
                item_class=item_class,
                message_id=message.get("message_id"),
                is_reply=is_reply,
            )
            return "enqueued" if queued is not None else "duplicate"
        return "closed:non-owner-dm"

    if chat_type in ("group", "supergroup"):
        # Adopt every group where the owner speaks, and fetch
        # its admin list so group admins can run commands too.
        # A group that is not adopted is ignored.
        if sender_id == state["owner_id"]:
            _adopt_group(state, chat["id"], now=now, api_call=api_call)
        if _group_record(state, chat["id"]) is None:
            return "closed:group-not-adopted"
        # Safety trip (trusted mode only, and never for an
        # allowlisted group): the group must still be the owner
        # + known Muse bots. A message from anyone else — a
        # human stranger, or a bot not in the trusted list —
        # trips it immediately. Otherwise the member count is
        # re-checked (throttled): a silent extra member trips
        # it too. The trip stops personal-detail sharing; it
        # does NOT change the stored mode, and it is recorded
        # in THIS group's record only — no other group is
        # affected. An allowlisted group is the owner's private
        # channel: the trips never apply there, no matter who
        # joins.
        if (_group_mode(state, chat["id"]) == GROUP_MODE_TRUSTED
                and not is_group_allowlisted(state, chat["id"])):
            if sender_id != state.get("owner_id") and not _is_trusted_bot(state, sender):
                _trip_group_safety(
                    state, chat["id"], "stranger", sender_label(sender), now
                )
            else:
                check_group_member_count(
                    state, chat["id"], now=now, api_call=api_call
                )
        # A command from someone not in the cached admin list
        # re-fetches the list once (at most once per 60s), in
        # case they were just promoted.
        if (
            isinstance(text, str)
            and text.lstrip().startswith("/")
            and sender_id != state["owner_id"]
            and not _is_group_admin(state, sender_id, chat["id"])
        ):
            refresh_group_admins(state, chat["id"], now=now, api_call=api_call)
        group = _group_record(state, chat["id"])
        group["buffer"].append(
            {
                "external_id": str(update["update_id"]),
                "message_id": message.get("message_id"),
                "sender_name": sender_label(sender),
                "is_bot": bool(sender.get("is_bot")),
                "text": text,
                "date": message.get("date"),
            }
        )
        group["buffer"] = group["buffer"][-BUFFER_MAX:]
        queued = queue_core.enqueue(
            state,
            platform=PLATFORM,
            external_id=update["update_id"],
            chat_id=chat["id"],
            chat_type=chat_type,
            sender_id=sender_id,
            sender_name=sender_label(sender),
            is_bot=bool(sender.get("is_bot")),
            text=text,
            date=message.get("date"),
            item_class=item_class,
            message_id=message.get("message_id"),
            is_reply=is_reply,
        )
        return "enqueued" if queued is not None else "duplicate"
    # Any other chat type (e.g. channel_post, which arrives under
    # "message" with an unknown type) is deliberately closed.
    return "closed:unsupported-chat-type"


def ingest(state: dict, result: dict, now: float | None = None, api_call=api) -> None:
    """Fold one getUpdates response into the state (queue + buffers).

    Handles message, callback_query (button taps), edited_message
    (updates a still-pending item's text) and message_reaction
    (ignored: a reaction never wakes a turn and never creates an
    item). api_call is the Bot API function (injectable for tests).

    Offset advances ONLY after durable commit (OpenClaw
    telegram-ingress-worker: offset is set only after the spool
    ack; Hermes receipt file): the offset for an update is
    advanced inside the same transaction as that update's enqueue
    (or its deliberate close as not-for-me), never before
    ``_ingest_update`` runs. If the enqueue fails, the offset
    does not advance for that update, so Telegram re-delivers it
    (at-least-once) instead of it being silently lost.

    Receipt dedupe (Hermes update_admission.py, 24 h TTL): a
    receipt ``"<bot>:<update_id>"`` is recorded for every update
    whose offset advanced. A re-delivered update whose receipt
    already exists is skipped without re-enqueueing, and its
    offset may advance (it was already durably recorded). Receipts
    older than 24 h are pruned at the start of each ingest.

    A single bad update never aborts the poll and never loses its
    message. Each update is processed in isolation: when full
    processing fails, the message is held in pending with a minimal
    enqueue so the next poll retries it. Only when even that is
    impossible (the update has no usable id, chat, or sender) is it
    recorded as unanswered, loudly, with its update id and text --
    never silently dropped. In that last case the offset does NOT
    advance, so the update is re-delivered and retried — until the
    3rd consecutive failure (see INTAKE_POISON_SKIP_AFTER), when
    the offset advances past it and a receipt is recorded, so a
    later re-delivery is deduped. The unanswered records stay
    visible either way.
    """
    if not result.get("ok"):
        raise RuntimeError(f"getUpdates failed: {result.get('description')}")
    _ensure_adapter_state(state)
    now = time.time() if now is None else now
    bot_id = state.get("bot_id") or _bot_username()
    queue_core.prune_receipts(state, now)
    receipts = state.setdefault("receipts", {})
    for update in result.get("result", []):
        update_id = update.get("update_id") if isinstance(update, dict) else None
        if not isinstance(update_id, int):
            # No id: cannot enqueue it and cannot retry it by id.
            # Surface it loudly; continue with the rest.
            _record_unanswered(
                state, update_id, "missing update_id",
                _update_text(update), now,
            )
            continue
        key = queue_core.receipt_key(bot_id, update_id)
        if key in receipts:
            # Duplicate delivery of an update already durably
            # recorded: skip it, but the offset may advance — the
            # work was committed when the receipt was written.
            state["offset"] = max(state["offset"], update_id + 1)
            continue
        try:
            _ingest_update(state, update, now=now, api_call=api_call)
        except Exception as exc:
            # Full processing failed. Do not lose the message: hold
            # it in pending so the next poll retries it. Only when
            # even a minimal hold is impossible is it unanswered —
            # and then the offset does NOT advance, so Telegram
            # re-delivers the update instead of losing it.
            if _enqueue_minimal(state, update, now):
                receipts[key] = {"received_at": now}
                state["offset"] = max(state["offset"], update_id + 1)
                try:
                    print(
                        f"telegram intake: held update {update_id} in "
                        f"pending after {exc!r}",
                        file=sys.stderr,
                    )
                except Exception:
                    pass
            else:
                _record_unanswered(
                    state, update_id, exc, _update_text(update), now
                )
                # Poison update: unprocessable, so it would be
                # re-delivered and re-recorded on every poll
                # forever. Count consecutive failures per update
                # id; on the 3rd, advance the offset and record
                # the receipt, so a later re-delivery is deduped
                # instead of re-processed. The unanswered records
                # stay visible — skipping is not silencing.
                poison = state.setdefault("intake_poison", {})
                try:
                    failures = int(poison.get(str(update_id)) or 0) + 1
                except (TypeError, ValueError):
                    failures = 1
                poison[str(update_id)] = failures
                while len(poison) > INTAKE_POISON_MAX:
                    poison.pop(next(iter(poison)))
                if failures >= INTAKE_POISON_SKIP_AFTER:
                    poison.pop(str(update_id), None)
                    receipts[key] = {"received_at": now}
                    state["offset"] = max(state["offset"], update_id + 1)
            continue
        # Success: enqueued, or deliberately closed as not-for-me
        # (see _ingest_update's "closed:<reason>" outcomes). Only
        # now — in the same transaction as the enqueue/close —
        # does the offset advance, with the receipt recorded.
        # Success also clears a stale poison counter for this
        # update id: an earlier failure that later succeeded was
        # transient, so a later failure (if any) counts from 1.
        poison = state.get("intake_poison")
        if isinstance(poison, dict):
            poison.pop(str(update_id), None)
        receipts[key] = {"received_at": now}
        state["offset"] = max(state["offset"], update_id + 1)


def _expired_external_ids(state: dict) -> set:
    """External ids of this platform's expired queue records."""
    return {
        item.get("external_id")
        for item in state.get("queue", [])
        if item.get("platform") == PLATFORM
        and item.get("status") == queue_core.DONE
        and item.get("done_reason") == queue_core.EXPIRED_REASON
    }


def _payload_item_minimal(item: dict) -> dict:
    """Minimal payload view of one item, using only its own fields.

    No state access and no group/context lookups, so it cannot fail
    on group state: the fallback when the full view raises. The
    agent still gets the message itself and can answer it.
    """
    raw_id = item.get("external_id") if isinstance(item, dict) else None
    try:
        update_id = int(raw_id)
    except (TypeError, ValueError):
        update_id = 0
    return {
        "update_id": update_id,
        "chat_id": item.get("chat_id"),
        "chat_type": item.get("chat_type"),
        "sender_id": item.get("sender_id"),
        "is_owner": False,
        "is_group_admin": False,
        "message_id": item.get("message_id"),
        "source": item.get("source"),
        "item_class": item.get("item_class"),
        "is_reply": bool(item.get("is_reply")),
        "sender_name": item.get("sender_name") or "",
        "is_bot": bool(item.get("is_bot")),
        "text": item.get("text") or "",
    }


def _payload_item(state: dict, item: dict, expired_ids: set) -> dict:
    # A queue item missing a display field (e.g. written by an older
    # version) must not abort the poll: coerce with defaults so the
    # message still reaches the agent and gets answered.
    raw_id = item.get("external_id")
    try:
        update_id = int(raw_id)
    except (TypeError, ValueError):
        update_id = 0
    view = {
        "update_id": update_id,
        "chat_id": item.get("chat_id"),
        "chat_type": item.get("chat_type"),
        "sender_id": item.get("sender_id"),
        # Verified owner identity (sender id matches the adopted owner).
        # The decider and the wake prompt gate commands on this, never
        # on the display name, which anyone can spoof.
        "is_owner": (
            item.get("sender_id") is not None
            and state.get("owner_id") is not None
            and item.get("sender_id") == state.get("owner_id")
        ),
        # Group admin identity (creator or admin of the adopted
        # group, per the cached admin list). Commands are allowed
        # from the owner OR a group admin.
        "is_group_admin": _is_group_admin(
            state, item.get("sender_id"), item.get("chat_id")
        ),
        # Telegram message id: the reply threads under this message
        # and a reaction is set on it. None for legacy items and for
        # button taps (source "callback_query"): a tap has no user
        # message, so its reply is standalone and gets no reaction.
        "message_id": item.get("message_id"),
        # Where the item came from: "message" (typed/sent) or
        # "callback_query" (inline-keyboard button tap). Legacy
        # items carry None, which behaves like "message".
        "source": item.get("source"),
        # Intake classification ("conversation" or "request") and
        # whether the message is a reply to an earlier message. The
        # decider's tier classifier uses both: a reply is a
        # follow-up, so it needs context and is never quick.
        "item_class": item.get("item_class"),
        "is_reply": bool(item.get("is_reply")),
        "sender_name": item.get("sender_name") or "",
        "is_bot": bool(item.get("is_bot")),
        "text": item.get("text") or "",
    }
    if item.get("chat_type") in ("group", "supergroup"):
        # Expired work is closed precisely so that no turn acts on
        # it: buffer entries tied to an expired record are excluded
        # from the actionable context of later wakes, so an expired
        # request cannot re-enter through a fresh item's context
        # and bypass that item's own age guard. Entries with no
        # record link (legacy buffer entries) are kept — they
        # cannot be tied to expired work. The context is THIS
        # group's buffer only: another group's room history never
        # leaks into this lane.
        group = _group_record(state, item["chat_id"])
        buffer = group.get("buffer", []) if group else []
        context = []
        for entry in buffer:
            if str(entry.get("external_id")) in expired_ids:
                continue
            view_entry = dict(entry)
            if entry.get("is_bot"):
                # Mark other bots' messages as third-party content so
                # the woken agent never mistakes them for the owner's
                # words or for instructions.
                view_entry["note"] = "third-party bot message"
            context.append(view_entry)
        view["context"] = context[-CONTEXT_TAIL:]
    return view


def build_payload(state: dict, batch: dict) -> dict | None:
    # The batch must contain only Telegram items of one chat lane
    # (claim_batch is called with platform=PLATFORM). Refuse loudly
    # rather than shaping a foreign item into a Telegram payload --
    # raising inside the transaction leaves every item untouched.
    # A missing platform is a malformed item, not a foreign one: it
    # flows to the per-item handling below instead of aborting the
    # poll, so its message is still answered.
    for item in batch.get("items") or []:
        if isinstance(item, dict) and item.get("platform") not in (None, PLATFORM):
            raise ValueError(
                f"refusing to shape a {item.get('platform')!r} item "
                "into a Telegram payload"
            )
    expired_ids = _expired_external_ids(state)
    items = []
    for item in batch.get("items") or []:
        try:
            items.append(_payload_item(state, item, expired_ids))
        except Exception as exc:
            # One bad item must not abort the poll or lose its
            # message: fall back to a minimal view (raw text, no
            # context) so the agent can still answer it. Only when
            # even that is impossible is it recorded as unanswered,
            # loudly, and skipped -- never silently closed.
            try:
                items.append(_payload_item_minimal(item))
            except Exception:
                _record_unanswered(
                    state,
                    item.get("external_id") if isinstance(item, dict) else None,
                    exc,
                    item.get("text") if isinstance(item, dict) else "",
                    time.time(),
                )
                continue
    if not items:
        # Nothing viewable in this batch: no wake. The items stay
        # claimed and are recycled as stale for a later retry.
        return None
    payload = {
        "claim_token": batch["claim_token"],
        "count": len(items),
        "items": items,
        # Whether the lane has unfinished work OUTSIDE this batch
        # (a request still pending, a stale in-flight batch, a
        # throttled item). The decider's tier classifier uses it: a
        # batch that lands while work is unfinished is a follow-up
        # to that work, so it gets the deep tier. The batch's own
        # items are excluded, so a batch never counts as active
        # work for itself.
        "has_active_work": _has_active_work(
            state,
            batch["items"][0].get("chat_id"),
            exclude_external_ids={
                str(item.get("external_id")) for item in batch["items"]
            },
        ),
    }
    # Compatibility: the first item's fields at the top level, as the
    # old single-item payload presented them.
    payload.update(items[0])
    # Group sharing mode, per group. The agent applies
    # group_mode_effective: the configured mode of THIS group,
    # except a private DM is always trusted and an active safety
    # trip makes a trusted group behave as guarded. group_mode is
    # the locked per-group choice (it never changes from Telegram);
    # group_safety_tripped says whether THIS group's trip is
    # active; group_allowlisted says whether this group is on the
    # owner's private-channel allowlist (the trips never apply
    # there, no matter who joins). adopted_group_ids lists every
    # group the bot serves, so the agent can tell its groups apart
    # and say how many it is in. A one-time notice, when the trip
    # newly fired, is included once and then cleared so the agent
    # mentions it (and only once) in its reply.
    chat_id = items[0].get("chat_id")
    chat_type = items[0].get("chat_type")
    group = _group_record(state, chat_id)
    payload["group_mode"] = _group_mode(state, chat_id)
    payload["group_safety_tripped"] = _group_tripped(state, chat_id)
    payload["group_allowlisted"] = is_group_allowlisted(state, chat_id)
    payload["group_mode_effective"] = effective_group_mode(
        state, chat_type, chat_id
    )
    payload["adopted_group_ids"] = sorted(
        int(g) for g in (state.get("group_ids") or [])
    )
    notice = group.pop("mode_notice", None) if group is not None else None
    if notice is not None:
        payload["group_mode_notice"] = notice
    return payload


#: Settle window (seconds) before a freshly arrived batch is
#: claimed: when a message lands, the intake waits this long and
#: re-fetches once (non-blocking) so a rapid follow-up typed a
#: second later joins the same batch instead of becoming a second
#: turn. Default SETTLE_DEFAULT_SECONDS (0.3 s, OpenClaw debounce
#: retune — the old 2.0 s default over-waited); set
#: TELEGRAM_BRIDGE_SETTLE_SECONDS to 0 to disable, or up to
#: SETTLE_MAX_SECONDS (7.5 s, the OpenClaw long-paste max wait).
#: The wait is at most the window, and only when a pending item
#: arrived inside it; a quiet poll claims nothing and waits for
#: nothing. The settle is bypassed entirely when every item in
#: the current batch is a /command (see _has_immediately_claimable_work
#: / run_once): commands never wait for a follow-up.
SETTLE_SECONDS_ENV = "TELEGRAM_BRIDGE_SETTLE_SECONDS"


def _settle_seconds() -> float:
    """Settle window from the environment; SETTLE_DEFAULT_SECONDS
    when unset or invalid, 0 when explicitly disabled, clamped to
    SETTLE_MAX_SECONDS."""
    raw = os.environ.get(SETTLE_SECONDS_ENV, "")
    if not raw:
        return SETTLE_DEFAULT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return SETTLE_DEFAULT_SECONDS
    if not math.isfinite(value) or value <= 0:
        return 0.0
    return min(value, SETTLE_MAX_SECONDS)


def _newest_pending_received_at(state: dict):
    """received_at of the newest pending item of this platform, or
    None when the queue holds nothing claimable."""
    newest = None
    for item in state.get("queue", []):
        if item.get("platform") != PLATFORM:
            continue
        if item.get("status") != queue_core.PENDING:
            continue
        if item.get("replied_at") is not None:
            continue
        received_at = item.get("received_at")
        if received_at is None:
            continue
        if newest is None or received_at > newest:
            newest = received_at
    return newest


def _is_command(text) -> bool:
    """True when the text is a /command.

    A small local mirror of decide.py's ``is_command`` logic
    (do NOT import decide here — the adapter stays dependency-
    free); the settle bypass and the tier hint use this, not the
    decider. Identical to ``_is_command_text``.
    """
    return _is_command_text(text)


def preclaim_filter(state: dict, now: float | None = None) -> list:
    """Close PENDING group noise before any claim wakes a turn.

    Group items only, closed as DONE with done_reason /
    close_reason "not-for-me" (replied_at stays None — nothing
    was ever answered). An item is noise when:

    - it is from another bot and does not @mention this bot, or
    - it @mentions a different bot (and not this bot), or
    - it is a command from someone not allowed to run commands
      (see ``_is_command_allowed``: owner, or a group admin).

    DMs are never closed here, owner commands are never closed,
    and a message with no @mention in a free-for-all group is
    never closed — only addressed-to-someone-else and
    unauthorized-command noise is. Group buffer context was
    appended before enqueue, so closing the item loses no
    context for later wakes. Returns the closed update ids
    (external_ids), in queue order.
    """
    now = time.time() if now is None else now
    names = _bot_names()
    closed = []
    for item in state.get("queue", []):
        if item.get("status") != queue_core.PENDING:
            continue
        if item.get("chat_type") not in ("group", "supergroup"):
            continue
        if item.get("replied_at") is not None:
            continue
        text = item.get("text") or ""
        mentions = {
            m.group(0)[1:].lower() for m in MENTION_RE.finditer(text)
        }
        mentions_this_bot = bool(mentions & names)
        sender_name = (item.get("sender_name") or "").strip().lstrip("@").lower()
        is_this_bot = bool(sender_name) and sender_name in names
        noise = False
        if item.get("is_bot") and not is_this_bot and not mentions_this_bot:
            # Another bot talking, not to us.
            noise = True
        elif mentions and not mentions_this_bot:
            # Addresses a different bot, never us.
            noise = True
        elif _is_command_text(text) and not _is_command_allowed(
            state,
            item.get("sender_id"),
            item.get("chat_type"),
            item.get("chat_id"),
        ):
            # A command from someone who may not run commands.
            noise = True
        if not noise:
            continue
        item["status"] = queue_core.DONE
        item["done_at"] = now
        item["claim_token"] = None
        item["done_reason"] = "not-for-me"
        item["close_reason"] = "not-for-me"
        closed.append(item.get("external_id"))
    return closed


def _has_immediately_claimable_work(
    state: dict,
    *,
    now: float | None = None,
    active_heartbeat_stale: float | None = None,
    warm_baton_timeout: float | None = None,
) -> bool:
    """True when a poll can skip the long wait: some PENDING item
    is claimable right now (its lane is free, it is not throttled,
    and no warm lease holds its lane), or some CLAIMED item is
    recyclable as stale under the two-phase rules (a dead claim
    the next claim would free). run_once uses this to fetch with
    a zero long-poll instead of waiting up to 30 s for work that
    is already here."""
    now = time.time() if now is None else now
    if active_heartbeat_stale is None:
        active_heartbeat_stale = _active_heartbeat_stale()
    if warm_baton_timeout is None:
        warm_baton_timeout = _warm_baton_timeout()
    if queue_core.stale_claims(
        state,
        now=now,
        heartbeat_stale=_heartbeat_stale(),
        active_heartbeat_stale=active_heartbeat_stale,
        warm_baton_timeout=warm_baton_timeout,
    ):
        return True
    busy_lanes = {
        (item["platform"], item["chat_id"])
        for item in state.get("queue", [])
        if item["status"] in queue_core.IN_FLIGHT
    }
    for item in state.get("queue", []):
        if item.get("platform") != PLATFORM:
            continue
        if item.get("status") != queue_core.PENDING:
            continue
        if item.get("replied_at") is not None:
            continue
        wake_after = item.get("wake_after")
        if wake_after is not None and wake_after > now:
            continue
        if (item["platform"], item["chat_id"]) in busy_lanes:
            continue
        if queue_core.warm_lease_active(state, item.get("chat_id"), now):
            continue
        return True
    return False


def run_once(store: JsonStateStore, fetch=fetch_updates, api_call=api) -> dict | None:
    """Fetch, ingest, claim one batch; return the wake payload or None.

    Long-poll: fetch holds the request until a message arrives (see
    fetch_updates), so a message is picked up in ~0-1s. A single-run
    lock (a flock on a per-credential lock file, held for the whole
    run — see poll_lock_path) keeps overlapping harness ticks from
    running two long polls at once: a second concurrent run returns
    None immediately and the first one handles the poll. The lock
    is released when this process exits, so a killed run never
    leaves a stale lock.

    Throttling is applied at claim time (apply_throttle): throttled
    items get a wake_after and stay pending instead of being dropped.
    A claimed group batch from non-owner senders records a wake in
    the rate state (for the per-chat cap). The rate state is saved
    after the transaction, best effort.
    """
    # Single-run lock: only one intake may long-poll at a time.
    # The lock is per credential id (see poll_lock_path), not per
    # state file, so a second poller for the same bot — whatever
    # state path it was started with — bows out instead of racing
    # the live long poll into a Telegram 409.
    lock_path = poll_lock_path()
    lock_handle = open(lock_path, "a+")
    try:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None
        snapshot = _ensure_adapter_state(store.load())
        # Fast-claim: work is already claimable (or a dead claim is
        # recyclable), so there is nothing to wait for — fetch with
        # a zero long-poll instead of holding the request up to
        # 30 s. Otherwise the long poll waits for new arrivals.
        fast_claim = _has_immediately_claimable_work(snapshot)
        previous_poll = os.environ.get(LONG_POLL_SECONDS_ENV)
        if fast_claim:
            os.environ[LONG_POLL_SECONDS_ENV] = "0"
        try:
            result = fetch(snapshot["offset"])
        finally:
            if fast_claim:
                if previous_poll is None:
                    os.environ.pop(LONG_POLL_SECONDS_ENV, None)
                else:
                    os.environ[LONG_POLL_SECONDS_ENV] = previous_poll
        if not result.get("ok"):
            raise RuntimeError(f"getUpdates failed: {result.get('description')}")
        # "Now" is measured AFTER the fetch returns: a long poll can
        # block for up to 30s, and the throttle math (sender windows,
        # wake_after delays) must use the time the updates actually
        # arrived, not the time the poll started.
        now = time.time()
        batch_size = min(
            _env_int("TELEGRAM_BRIDGE_BATCH_SIZE", queue_core.DEFAULT_BATCH_SIZE),
            BATCH_MAX_MESSAGES,
        )
        stale_ttl = float(_env_int("TELEGRAM_BRIDGE_STALE_TTL", int(queue_core.DEFAULT_STALE_TTL)))
        heartbeat_stale = _heartbeat_stale()
        active_heartbeat_stale = _active_heartbeat_stale()
        warm_baton_timeout = _warm_baton_timeout()
        group_ttl, private_ttl = _expiry_ttls()
        rate_state = _load_rate_state()
        with store.transaction() as state:
            _ensure_adapter_state(state)
            ingest(state, result, now=now, api_call=api_call)
            apply_throttle(state, rate_state, now)
            state["last_successful_poll"] = now
        # Prune old voice/media files OUTSIDE the transaction (see
        # _maybe_prune_files): walking the filesystem must never
        # hold the state lock. Throttled to once per 24 h.
        _maybe_prune_files(store)
        # Settle: when a message arrived inside the window, wait out
        # the rest of it and re-fetch once (non-blocking) so a rapid
        # follow-up joins this batch instead of a second turn. The
        # re-fetch is deduped by ingest, so a repeated delivery
        # cannot double-queue. No pending item, or no window
        # configured, means no wait at all. Settle is bypassed
        # when every pending item is a /command (OpenClaw: no
        # debounce for bot commands) — commands never wait for a
        # follow-up.
        settle = _settle_seconds()
        if settle > 0:
            loaded = _ensure_adapter_state(store.load())
            pending_now = [
                item for item in loaded.get("queue", [])
                if item.get("platform") == PLATFORM
                and item.get("status") == queue_core.PENDING
                and item.get("replied_at") is None
            ]
            all_commands = bool(pending_now) and all(
                _is_command(item.get("text")) for item in pending_now
            )
            newest = _newest_pending_received_at(loaded)
            if newest is not None and now - newest < settle and not all_commands:
                time.sleep(settle - (now - newest))
                previous_poll = os.environ.get(LONG_POLL_SECONDS_ENV)
                os.environ[LONG_POLL_SECONDS_ENV] = "0"
                try:
                    again = fetch(_ensure_adapter_state(store.load())["offset"])
                finally:
                    if previous_poll is None:
                        os.environ.pop(LONG_POLL_SECONDS_ENV, None)
                    else:
                        os.environ[LONG_POLL_SECONDS_ENV] = previous_poll
                if not again.get("ok"):
                    raise RuntimeError(
                        f"getUpdates failed: {again.get('description')}"
                    )
                now = time.time()
                with store.transaction() as state:
                    _ensure_adapter_state(state)
                    ingest(state, again, now=now, api_call=api_call)
                    apply_throttle(state, rate_state, now)
                    state["last_successful_poll"] = now
        payload = None
        with store.transaction() as state:
            _ensure_adapter_state(state)
            # Pre-claim noise filter: close other-bot chatter and
            # unauthorized commands before any claim wakes a turn.
            preclaim_filter(state, now=now)
            batch = queue_core.claim_batch(
                state,
                platform=PLATFORM,
                batch_size=batch_size,
                stale_ttl=stale_ttl,
                heartbeat_stale=heartbeat_stale,
                active_heartbeat_stale=active_heartbeat_stale,
                warm_baton_timeout=warm_baton_timeout,
                group_pending_ttl=group_ttl,
                private_pending_ttl=private_ttl,
                now=now,
            )
            payload = build_payload(state, batch) if batch else None
            if batch:
                # Record the wake for the per-chat cap, but only for
                # a group batch triggered by non-owner senders (the
                # owner and admins are never counted).
                items = batch["items"]
                if items and items[0].get("chat_type") in ("group", "supergroup"):
                    if not any(
                        it.get("sender_id") == state.get("owner_id")
                        or _is_group_admin(state, it.get("sender_id"), it.get("chat_id"))
                        for it in items
                    ):
                        chat_key = str(items[0].get("chat_id"))
                        rate_state["chats"].setdefault(chat_key, []).append(now)
        _save_rate_state(rate_state)
        return payload
    finally:
        lock_handle.close()


#: Sender exit codes (see bin/telegram-sender): 0 = sent (message
#: id on stdout), 2 = definitive rejection (400/403/404 — provably
#: not delivered), 3 = retryable (429/5xx — not delivered, may
#: succeed later). Any other nonzero exit, a timeout, or exit 0
#: without a message id stays "unknown": the send may or may not
#: have gone out, so it is never resent blindly.
SENDER_EXIT_REJECTED = 2
SENDER_EXIT_RETRYABLE = 3

#: Retry-After value the sender prints to stderr on a retryable
#: exit (``telegram HTTP 429 retry-after=N``), in seconds. The
#: bridge clamps it to 300 s when it sets wake_after (see
#: send_fenced); a missing value means the sender had no header
#: (5xx), and the bridge uses its own default delay instead.
_RETRY_AFTER_RE = re.compile(r"retry-after=(\d+)")


class _SendResult(tuple):
    """The ``(outcome, message_id)`` pair _run_send_command
    returns, plus ``.retry_after`` (seconds, or None) for a
    retryable outcome. A tuple subclass so existing two-value
    unpacking keeps working; callers that need the delay read
    the attribute (``getattr(result, "retry_after", None)`` is
    safe for plain tuples from mocked senders)."""

    def __new__(cls, outcome, message_id, retry_after=None):
        self = super().__new__(cls, (outcome, message_id))
        self.retry_after = retry_after
        return self


def _run_send_command(send_cmd: str, chat_id, text: str, reply_to=None, keyboard: bool = False, media=None):
    """Invoke the deployment's send executable once.

    Returns ``(outcome, message_id)`` (a ``_SendResult``; see
    above for ``.retry_after``):

    - ``("sent", id)`` — exit 0 and a message id as the last
      non-empty stdout line.
    - ``("rejected", None)`` — exit 2: the sender's definitive
      rejection (400/403/404), so the send provably did not go
      out. The caller returns the item to pending with
      ``mark_send_failed``; it may be re-claimed and sent again.
    - ``("retryable", None)`` — exit 3: the sender was throttled
      or Telegram errored (429/5xx), so the send did not go out
      but may succeed later. ``.retry_after`` carries the
      sender's Retry-After seconds when it had one; the caller
      returns the item to pending with a ``wake_after`` delay
      (see ``send_fenced``).
    - ``("unknown", None)`` — the runner timed out or could not be
      started, exited nonzero with any other code (including a
      signal), produced undecodable output, or exited 0 without a
      message id: the send may or may not have been delivered, so
      the item must stay in "sending" for reconciliation and never
      be resent blindly.

    The executable contract has no verifiable no-send result
    beyond exits 2 and 3: an exit code alone cannot prove
    rejection in general (remote acceptance may precede a lost
    response or a local crash), but the sender only exits 2 for
    HTTP 400/403/404 responses it received — Telegram answered,
    and the answer was no — and only exits 3 for 429/5xx. A
    timeout or a lost response after server acceptance usually
    means the message went out. Confirmed non-delivery after an
    "unknown" still uses the separate operator --send-failed path
    after reconciliation.

    reply_to threads the reply under that Telegram message id.
    keyboard attaches the inline command keyboard to the reply.
    media is ("photo"|"document", path) for a media send: the
    executable is invoked as [send_cmd, --photo|--document, chat_id,
    path] with the caption on stdin, instead of the text form.
    """
    argv = [send_cmd, str(chat_id)]
    if media is not None:
        kind, path = media
        argv = [send_cmd, f"--{kind}", str(chat_id), str(path)]
    if reply_to is not None:
        argv += ["--reply-to", str(reply_to)]
    if keyboard:
        argv += ["--keyboard"]
    try:
        proc = subprocess.run(
            argv,
            input=text,
            capture_output=True,
            text=True,
            timeout=SEND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(
            f"send-unknown: sender timed out after {SEND_TIMEOUT_SECONDS}s",
            file=sys.stderr,
        )
        return _SendResult("unknown", None)
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        print(f"send-unknown: sender failed to run: {exc}", file=sys.stderr)
        return _SendResult("unknown", None)
    if proc.returncode == SENDER_EXIT_REJECTED:
        # Definitive rejection: Telegram answered 400/403/404, so
        # the send provably did not go out.
        err = (proc.stderr or "").strip()
        print(
            f"send-rejected: sender exit 2: {err[:500]}",
            file=sys.stderr,
        )
        return _SendResult("rejected", None)
    if proc.returncode == SENDER_EXIT_RETRYABLE:
        # Retryable: 429/5xx, not delivered. Carry the Retry-After
        # the sender printed (if any) for the wake_after delay.
        err = (proc.stderr or "").strip()
        match = _RETRY_AFTER_RE.search(err)
        retry_after = int(match.group(1)) if match else None
        print(
            f"send-retryable: sender exit 3: {err[:500]}",
            file=sys.stderr,
        )
        return _SendResult("retryable", None, retry_after)
    if proc.returncode != 0:
        # The sender prints its own diagnostic to stderr (HTTP code,
        # transport error, or "all attempts rejected"). Log it so an
        # "unknown" outcome is diagnosable instead of silent: a
        # timeout or a lost response after server acceptance usually
        # means the message went out, while a 4xx rejection means it
        # did not. The sender never prints credentials.
        err = (proc.stderr or "").strip()
        print(
            f"send-unknown: sender exit {proc.returncode}: {err[:500]}",
            file=sys.stderr,
        )
        return _SendResult("unknown", None)
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        err = (proc.stderr or "").strip()
        print(
            f"send-unknown: sender exit 0 without message_id: {err[:500]}",
            file=sys.stderr,
        )
        return _SendResult("unknown", None)
    return _SendResult("sent", lines[-1])


def _is_command_text(text) -> bool:
    """True when the queued text is a /command (typed or tapped).

    The inline keyboard is attached only to command replies, so
    the send fence uses this to refuse --keyboard on a plain
    conversation reply."""
    return isinstance(text, str) and text.lstrip().startswith("/")


def _retryable_delay_seconds(retry_after) -> float:
    """Delay (seconds) for a retryable send: min(Retry-After, 300)
    when the sender reported a Retry-After header, or the 60 s
    default when it did not (a 5xx carries no Retry-After)."""
    if retry_after is None:
        return RETRYABLE_DEFAULT_DELAY_SECONDS
    return min(float(retry_after), RETRYABLE_MAX_DELAY_SECONDS)


def send_fenced(
    store: JsonStateStore,
    target: int,
    token,
    chat_id,
    text: str,
    *,
    reply_to=None,
    keyboard: bool = False,
    extra_targets=(),
    max_reply_age: float | None = DEFAULT_MAX_REPLY_AGE,
    now: float | None = None,
) -> dict:
    """Send one queue reply under the send fence.

    Order is the whole point: configuration is checked first (an
    unconfigured deployment leaves the item claimed), then the fence
    — verify the item is claimed, the token matches and no reply is
    recorded, and move it to "sending" — is persisted in one
    transaction. Only after the fence is durable does the send
    command run, exactly once. Its outcome decides the result:

    - sent → the item stays in "sending"; the caller records the
      reply with --replied and closes with --done, as usual.
    - rejected → the send provably did not go out (sender exit 2:
      400/403/404). The item returns to pending with
      ``mark_send_failed`` and may be re-claimed and sent again.
    - retryable → the send was throttled or Telegram errored
      (sender exit 3: 429/5xx) and did not go out. The item
      returns to pending with a ``wake_after`` delay of
      min(Retry-After, 300 s) — 60 s when the sender had no
      Retry-After header — so the next poll does not re-send
      into the same throttle.
    - unknown → the item stays in "sending" (send-unknown) for
      operator reconciliation; it is never resent automatically.
      Only independently verified non-delivery permits an operator
      to return it to pending with --send-failed.
    - stale → the item is older than ``max_reply_age`` (seconds
      since intake; None disables the guard) and has no recorded
      reply. The fence refuses BEFORE marking "sending" or running
      the send command — and changes nothing: the item stays
      claimed, with its claim token intact, preserved for
      reassessment. The fence never closes claimed work, whatever
      its class. Re-claiming does not change intake time or bypass
      the age guard. A caller that does not own the claim gets the
      plain fence refusal instead, stale or not.
    - fence → the destination, the reply target or the keyboard
      flag does not match the queued item (see below): nothing is
      sent and the item stays claimed.
    - keyboard → --keyboard was requested for a reply that does not
      answer a command: the keyboard goes only on command replies
      (and /menu). Nothing is sent; the caller retries without it.

    ``extra_targets`` are the other batch items this one reply
    covers (see ``send_batch_and_close``): they are reserved —
    moved to "sending" — atomically with the primary in the same
    transaction, BEFORE the network send, so a turn that dies
    mid-send never leaves covered siblings claimed while the
    primary is sending. One message per item is rejected: a batch
    costs one send, and the extras are recorded against that
    send by ``_finish_send``. On rejected / retryable the extras
    return to pending with the primary (with the same wake_after
    on retryable).

    Reply threading is fenced too: ``reply_to`` must be None or the
    target item's own ``message_id``. A button tap's item carries
    message_id None (it has no user message), so any reply_to for
    it is refused — the reply goes out standalone and can never be
    threaded under the bot's own message.
    """
    # Refuse invalid text before reserving the send. Telegram sendMessage
    # accepts 1-4096 characters, and a rejected empty/oversized reply must
    # never strand an otherwise valid claim in an ambiguous "sending" state.
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        return {"ok": False, "stage": "input"}
    # A non-finite age limit is a configuration error, not a limit:
    # refuse before any state is touched (NaN would disable the
    # guard silently; an infinite limit likewise; a negative
    # infinity would refuse every send). The environment layer
    # already maps such values back to the default; this guards
    # direct callers.
    if max_reply_age is not None and not math.isfinite(max_reply_age):
        return {"ok": False, "stage": "input"}
    send_cmd = os.environ.get(SEND_CMD_ENV)
    if not send_cmd:
        return {"ok": False, "stage": "config"}
    extras = list(dict.fromkeys(int(t) for t in extra_targets))
    if target in extras:
        extras.remove(target)
    error, queued_chat_id = _reserve_send(
        store, target, token, chat_id,
        reply_to=reply_to, keyboard=keyboard,
        extra_targets=extras,
        max_reply_age=max_reply_age, now=now,
    )
    if error is not None:
        return error
    # Never trust the caller's destination, even after comparison. Only
    # the queue's persisted chat ID may reach the sender subprocess.
    send_result = _run_send_command(
        send_cmd, queued_chat_id, text, reply_to=reply_to, keyboard=keyboard
    )
    outcome, message_id = send_result
    if outcome == "sent":
        return {"ok": True, "message_id": message_id}
    if outcome == "rejected":
        # Definitive: the send provably did not go out. Return
        # every reserved item to pending so it can be re-claimed
        # and sent again.
        with store.transaction() as state:
            _ensure_adapter_state(state)
            for external_id in [target] + extras:
                queue_core.mark_send_failed(
                    state, PLATFORM, external_id, token
                )
        return {"ok": False, "stage": "rejected"}
    if outcome == "retryable":
        # Throttled / errored, not delivered: return every
        # reserved item to pending with a delay, so the next poll
        # does not re-send into the same throttle.
        retry_after = getattr(send_result, "retry_after", None)
        send_now = time.time() if now is None else now
        delay = _retryable_delay_seconds(retry_after)
        wake_after = send_now + delay
        with store.transaction() as state:
            _ensure_adapter_state(state)
            for external_id in [target] + extras:
                queue_core.mark_send_retryable(
                    state, PLATFORM, external_id, token, wake_after
                )
        return {
            "ok": False, "stage": "retryable", "retry_after": delay,
        }
    return {"ok": False, "stage": "unknown"}


def _reserve_send(
    store: JsonStateStore,
    target: int,
    token,
    chat_id,
    *,
    reply_to=None,
    keyboard: bool = False,
    extra_targets=(),
    max_reply_age: float | None = DEFAULT_MAX_REPLY_AGE,
    now: float | None = None,
) -> tuple:
    """The shared fence reservation for text and media sends.

    Checks the send command is configured, then, in one
    transaction, binds the destination to the queued item's chat,
    checks the reply target (None or the target item's own
    message_id — never the bot's own message), checks the keyboard
    rule (only on command replies), checks the stale guard, and
    moves the item to "sending". Returns ``(None, queued_chat_id)``
    when reserved, or ``(error_dict, None)`` when refused — with
    the item left claimed and nothing sent. The text and media
    send paths both reserve through here, so neither can bypass
    the fence.

    ``extra_targets`` are the other batch items the one reply
    covers: each must be claimed under the same token with no
    reply recorded (and not stale), and all of them — primary
    plus extras — are moved to "sending" in this same
    transaction, BEFORE the network send. Every check runs
    before any item is marked, so a refused extra leaves the
    primary claimed too: the reservation is all-or-nothing.
    """
    send_cmd = os.environ.get(SEND_CMD_ENV)
    if not send_cmd:
        return {"ok": False, "stage": "config"}, None
    extras = list(dict.fromkeys(int(t) for t in extra_targets))
    if target in extras:
        extras.remove(target)
    check_now = time.time() if now is None else now

    def _stale_owned(it):
        # The stale verdict belongs to the claim's owner alone: it
        # requires the item claimed, the token matching, and no
        # reply recorded — the same ownership the fence itself
        # checks. A caller that does not own the claim falls
        # through to mark_sending and gets the plain fence refusal.
        # The age limit is per item: work a turn already claimed
        # gets the longer claimed limit (see _reply_age_limit), so
        # a recovery after an outage is never refused as stale.
        limit = _reply_age_limit(it, max_reply_age) if it is not None else None
        return (
            limit is not None
            and it is not None
            and it["status"] == queue_core.CLAIMED
            and token is not None
            and it["claim_token"] == token
            and it["replied_at"] is None
            and check_now - it["received_at"] > limit
        )

    with store.transaction() as state:
        _ensure_adapter_state(state)
        item = queue_core.find_item(state, PLATFORM, target)
        # The model/dispatcher controls argv, but not the intended recipient.
        # Bind the requested chat to the durable intake record *inside* the
        # same transaction as the claim fence, before any external send.
        queued_chat_id = item.get("chat_id") if item is not None else None
        if type(queued_chat_id) is not int or str(queued_chat_id) != str(chat_id):
            return {"ok": False, "stage": "fence"}, None
        # Threading is fenced: reply_to must be this item's own
        # message_id, or None for a standalone reply. A callback
        # item has message_id None, so any reply_to for it is
        # refused here — its reply can never thread under the bot's
        # own message (the message the button was attached to).
        if reply_to is not None:
            queued_message_id = item.get("message_id")
            if queued_message_id is None or str(reply_to) != str(queued_message_id):
                return {"ok": False, "stage": "fence"}, None
        # The keyboard goes only on command replies (typed /command
        # or button tap). Refuse it on a plain conversation reply
        # before anything is sent; the caller retries without it.
        if keyboard and not _is_command_text(item.get("text")):
            return {"ok": False, "stage": "keyboard"}, None
        if _stale_owned(item):
            # Stale: refuse the send, preserve the work. No state
            # changes in this transaction — the item stays claimed
            # under the caller's own token for reassessment.
            return {"ok": False, "stage": "stale"}, None
        # Extras: same ownership, reply and staleness rules as the
        # primary, checked BEFORE anything is marked — a refused
        # extra refuses the whole reservation.
        extra_items = []
        for external_id in extras:
            extra = queue_core.find_item(state, PLATFORM, external_id)
            if (
                extra is None
                or extra["status"] != queue_core.CLAIMED
                or token is None
                or extra["claim_token"] != token
                or extra["replied_at"] is not None
            ):
                return {"ok": False, "stage": "fence"}, None
            if _stale_owned(extra):
                return {"ok": False, "stage": "stale"}, None
            extra_items.append(extra)
        fenced = queue_core.mark_sending(state, PLATFORM, target, token)
        if fenced:
            for external_id in extras:
                if not queue_core.mark_sending(
                    state, PLATFORM, external_id, token
                ):
                    fenced = False
                    break
        if not fenced:
            # Ownership lost (2026-11-12 turns-dying bug): the item
            # exists but this token no longer owns it. Report the
            # distinct "stale-token" stage with the item's
            # disposition, so the caller knows whether the answer
            # went out and whether a re-wake owns the retry:
            # - the reply already went out (replied_at set): the
            #   answer is sent, stop, never resend;
            # - the claim was recycled and the item is pending again
            #   (claim_token None, ever_claimed): the re-wake owns
            #   the retry, exit quietly without closing anything.
            # A live claim under a different token, an item never
            # claimed at all, an item already in "sending" (a send
            # in flight, outcome unknown), and item-missing /
            # chat-mismatch above still report plain "fence" — the
            # caller never owned that claim, so this is not its
            # stale token.
            if item is not None and item.get("status") != queue_core.SENDING:
                if item.get("replied_at") is not None:
                    stale_disposition = {
                        "item_status": item.get("status"),
                        "replied": True,
                    }
                elif (
                    item.get("claim_token") is None
                    and item.get("ever_claimed")
                ):
                    stale_disposition = {
                        "item_status": item.get("status"),
                        "replied": False,
                    }
                else:
                    stale_disposition = None
            else:
                stale_disposition = None
        else:
            stale_disposition = None
    if not fenced:
        if stale_disposition is not None:
            return {
                "ok": False, "stage": "stale-token", **stale_disposition
            }, None
        return {"ok": False, "stage": "fence"}, None
    return None, queued_chat_id


def _finish_send(
    store: JsonStateStore,
    target: int,
    token,
    message_id,
    extras=(),
) -> tuple:
    """Record one sent reply against the primary item and every
    extra item, and close them all, in one transaction. Extras are
    closed with done_reason "covered-by-batch-reply". Returns
    ``(all_ok, covered)``: ``all_ok`` is False when any record
    could not be written — the send already went out, so the
    caller reports stage "record" and never resends."""
    extras = list(dict.fromkeys(int(t) for t in extras))
    if target in extras:
        extras.remove(target)
    with store.transaction() as state:
        _ensure_adapter_state(state)
        ok_reply = queue_core.mark_replied(
            state, PLATFORM, target, message_id, token
        )
        ok_done = False
        if ok_reply:
            # The reply is recorded, so mark_done needs no evidence.
            ok_done = queue_core.mark_done(state, PLATFORM, target, token)
        covered = []
        all_ok = ok_reply and ok_done
        for external_id in extras:
            ok_r = queue_core.mark_replied(
                state, PLATFORM, external_id, message_id, token
            )
            ok_d = False
            if ok_r:
                ok_d = queue_core.mark_done(
                    state, PLATFORM, external_id, token,
                    reason="covered-by-batch-reply",
                )
            if ok_r and ok_d:
                covered.append(external_id)
            else:
                all_ok = False
    return all_ok, covered


#: Maximum caption length (characters) for a photo/document send.
#: Telegram caps captions at 1024 characters.
MAX_MEDIA_CAPTION_CHARS = 1024

#: Maximum outbound file size (bytes) per kind: Telegram's Bot API
#: caps sendPhoto at 10MB and sendDocument at 50MB.
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024


def _validate_media_path(media_kind: str, media_path) -> tuple:
    """Validate an outbound media file before anything is reserved.

    The kind must be "photo" or "document"; the file must exist and
    be within the home directory (so a steered agent can never be
    made to exfiltrate a system file); and it must fit Telegram's
    per-kind size cap. Returns ``(None, resolved_path)`` when valid,
    or ``(error_dict, None)`` when the input is refused — the item
    stays claimed and nothing is sent.
    """
    if media_kind not in ("photo", "document"):
        return {"ok": False, "stage": "input"}, None
    try:
        path = Path(media_path)
    except TypeError:
        return {"ok": False, "stage": "input"}, None
    try:
        resolved = path.resolve()
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return {"ok": False, "stage": "input"}, None
    if resolved != home and home not in resolved.parents:
        return {"ok": False, "stage": "input"}, None
    if not resolved.is_file():
        return {"ok": False, "stage": "input"}, None
    cap = MAX_PHOTO_BYTES if media_kind == "photo" else MAX_DOCUMENT_BYTES
    try:
        if resolved.stat().st_size > cap:
            return {"ok": False, "stage": "input"}, None
    except OSError:
        return {"ok": False, "stage": "input"}, None
    return None, resolved


def send_media_fenced(
    store: JsonStateStore,
    target: int,
    token,
    chat_id,
    media_kind: str,
    media_path,
    caption: str,
    *,
    reply_to=None,
    keyboard: bool = False,
    max_reply_age: float | None = DEFAULT_MAX_REPLY_AGE,
    now: float | None = None,
) -> dict:
    """Fenced send of one photo/document reply (sendPhoto /
    sendDocument), mirroring send_fenced.

    The caption (optional, max 1024 characters) is the reply text;
    the file must live under the home directory and fit Telegram's
    per-kind cap (see _validate_media_path). Reservation goes
    through the same fence as a text send (destination, reply
    target, keyboard, stale), and the outcome rules are the same:
    sent leaves the item in "sending" for --replied/--done,
    rejected returns it to pending (definitive non-delivery),
    retryable returns it to pending with a wake_after delay, and
    unknown stays in "sending" for reconciliation, never resent.
    """
    if not isinstance(caption, str) or len(caption) > MAX_MEDIA_CAPTION_CHARS:
        return {"ok": False, "stage": "input"}
    if max_reply_age is not None and not math.isfinite(max_reply_age):
        return {"ok": False, "stage": "input"}
    error, resolved = _validate_media_path(media_kind, media_path)
    if error is not None:
        return error
    send_cmd = os.environ.get(SEND_CMD_ENV)
    if not send_cmd:
        return {"ok": False, "stage": "config"}
    error, queued_chat_id = _reserve_send(
        store, target, token, chat_id,
        reply_to=reply_to, keyboard=keyboard,
        max_reply_age=max_reply_age, now=now,
    )
    if error is not None:
        return error
    send_result = _run_send_command(
        send_cmd, queued_chat_id, caption,
        reply_to=reply_to, keyboard=keyboard,
        media=(media_kind, str(resolved)),
    )
    outcome, message_id = send_result
    if outcome == "sent":
        return {"ok": True, "message_id": message_id}
    if outcome == "rejected":
        with store.transaction() as state:
            _ensure_adapter_state(state)
            queue_core.mark_send_failed(state, PLATFORM, target, token)
        return {"ok": False, "stage": "rejected"}
    if outcome == "retryable":
        retry_after = getattr(send_result, "retry_after", None)
        send_now = time.time() if now is None else now
        delay = _retryable_delay_seconds(retry_after)
        with store.transaction() as state:
            _ensure_adapter_state(state)
            queue_core.mark_send_retryable(
                state, PLATFORM, target, token, send_now + delay
            )
        return {
            "ok": False, "stage": "retryable", "retry_after": delay,
        }
    return {"ok": False, "stage": "unknown"}


def send_media_and_close(
    store: JsonStateStore,
    target: int,
    token,
    chat_id,
    media_kind: str,
    media_path,
    caption: str,
    *,
    reply_to=None,
    keyboard: bool = False,
    extra_targets=(),
    max_reply_age: float | None = DEFAULT_MAX_REPLY_AGE,
    now: float | None = None,
) -> dict:
    """One media reply for one item (plus optional --cover items),
    in one process: the fenced media send, then the reply record
    and close for every covered item. Returns {"ok": true,
    "message_id": ..., "covered": [...]}, the fenced failure
    result, or {"ok": false, "stage": "record"} when the send went
    out but a reply record could not be written (never resent)."""
    result = send_media_fenced(
        store,
        target,
        token,
        chat_id,
        media_kind,
        media_path,
        caption,
        reply_to=reply_to,
        keyboard=keyboard,
        max_reply_age=max_reply_age,
        now=now,
    )
    if not result.get("ok"):
        return result
    all_ok, covered = _finish_send(
        store, target, token, result["message_id"], extras=extra_targets
    )
    if all_ok:
        return {"ok": True, "message_id": result["message_id"], "covered": covered}
    return {"ok": False, "stage": "record"}


def send_and_close(
    store: JsonStateStore,
    target: int,
    token,
    chat_id,
    text: str,
    *,
    reply_to=None,
    keyboard: bool = False,
    max_reply_age: float | None = DEFAULT_MAX_REPLY_AGE,
    now: float | None = None,
) -> dict:
    """The normal turn path, in one process: --send-fenced, then
    --replied, then --done. Reads the reply text from stdin (the
    caller passes it as ``text``). Returns {"ok": true, "message_id"}
    on success, the --send-fenced failure result when the send did
    not go out, or {"ok": false, "stage": "record"} when the send
    went out but the reply record could not be written (the item
    stays in "sending" for reconciliation; it is never resent)."""
    result = send_fenced(
        store,
        target,
        token,
        chat_id,
        text,
        reply_to=reply_to,
        keyboard=keyboard,
        max_reply_age=max_reply_age,
        now=now,
    )
    if not result.get("ok"):
        return result
    all_ok, _covered = _finish_send(
        store, target, token, result["message_id"]
    )
    if all_ok:
        return {"ok": True, "message_id": result["message_id"]}
    return {"ok": False, "stage": "record"}


def send_ack(
    store: JsonStateStore,
    target: int,
    token,
    chat_id,
    *,
    reply_to=None,
    now: float | None = None,
) -> dict:
    """Send the fixed acknowledgement for a claimed item, without
    closing it. The turn uses this when the work will take 10+
    minutes (deep research, a long build, anything estimated at 10
    minutes or more), so the owner knows the turn is working instead
    of wondering whether it errored.

    The ack is the fixed ACK_TEXT, threaded under the item's own
    message when reply_to names it. The fence is the same ownership
    check as a full send (item claimed under the caller's token, the
    destination is the queued item's chat, reply_to is the item's own
    message_id or None), plus two ack-only guards: no full reply may
    be recorded yet, and no ack may be recorded yet — one ack per
    item, never a second. Unlike a full send, the ack does NOT move
    the item to "sending": the item stays claimed so the full reply
    can still close it with --send-and-close as usual.

    The ack is RESERVED before sending: the first transaction sets
    ``ack_sent_at`` with no message id, so a second concurrent ack
    sees the reservation and returns "already-acked" without
    sending — two threads can never send the ack twice. The
    reservation is completed by ``mark_ack`` after a successful
    send, and cleared on rejected / retryable / unknown, so a
    retry after a failed send is safe.

    Returns {"ok": true, "message_id": ...} on success,
    {"ok": false, "stage": "already-acked"} when an ack is already
    recorded (or reserved by a concurrent ack), {"ok": false,
    "stage": "fence"} when the item is not claimed under the token
    or the destination/threading does not match, {"ok": false,
    "stage": "config"} when no send command is configured,
    {"ok": false, "stage": "rejected"} when the send was
    definitively rejected (provably not delivered — a retry is
    safe), {"ok": false, "stage": "retryable"} when the send was
    throttled or Telegram errored (not delivered — a retry is
    safe, after the reported delay), and {"ok": false, "stage":
    "unknown"} when the send outcome is unknown. On "unknown"
    nothing is recorded — the item stays claimed and the ack may
    be tried again, or the turn may simply proceed to the full
    reply (a duplicate short ack is harmless, a lost ack just
    means no ack). The ack is never resent blindly after a
    recorded success.
    """
    send_cmd = os.environ.get(SEND_CMD_ENV)
    if not send_cmd:
        return {"ok": False, "stage": "config"}
    with store.transaction() as state:
        _ensure_adapter_state(state)
        item = queue_core.find_item(state, PLATFORM, target)
        queued_chat_id = item.get("chat_id") if item is not None else None
        if type(queued_chat_id) is not int or str(queued_chat_id) != str(chat_id):
            return {"ok": False, "stage": "fence"}
        if (
            item is None
            or item["status"] != queue_core.CLAIMED
            or token is None
            or item["claim_token"] != token
            or item["replied_at"] is not None
        ):
            return {"ok": False, "stage": "fence"}
        if reply_to is not None:
            queued_message_id = item.get("message_id")
            if queued_message_id is None or str(reply_to) != str(queued_message_id):
                return {"ok": False, "stage": "fence"}
        # Reserve the ack inside this transaction, before sending:
        # ack_sent_at is set with no message id yet, so a second
        # concurrent ack is refused as already-acked without
        # sending. (The reply_to check above runs before the
        # reservation, so a bad thread target never reserves.)
        if not queue_core.reserve_ack(state, PLATFORM, target, token, now=now):
            return {"ok": False, "stage": "already-acked"}
    send_result = _run_send_command(
        send_cmd, queued_chat_id, ACK_TEXT, reply_to=reply_to
    )
    outcome, message_id = send_result
    if outcome != "sent":
        # The ack did not go out (rejected / retryable / unknown):
        # clear the reservation so a retry is safe. A completed
        # ack would refuse the clear, but none can exist yet —
        # this turn holds the only reservation.
        with store.transaction() as state:
            _ensure_adapter_state(state)
            queue_core.clear_ack(state, PLATFORM, target, token)
        if outcome == "rejected":
            return {"ok": False, "stage": "rejected"}
        if outcome == "retryable":
            return {
                "ok": False,
                "stage": "retryable",
                "retry_after": _retryable_delay_seconds(
                    getattr(send_result, "retry_after", None)
                ),
            }
        return {"ok": False, "stage": "unknown"}
    with store.transaction() as state:
        _ensure_adapter_state(state)
        # Another turn may have recorded an ack meanwhile (only the
        # claim holder can ack, so this is near-impossible, but be
        # safe): the ack went out either way, so return success
        # without overwriting the existing record.
        queue_core.mark_ack(state, PLATFORM, target, message_id, token, now=now)
        # The turn is alive and working (it just sent the ack), so
        # refresh the whole batch heartbeat: a long turn that acks
        # early never looks dead to the watchdog while it works.
        queue_core.heartbeat_batch(state, token, now=now)
    return {"ok": True, "message_id": message_id}


def heartbeat_batch(store: JsonStateStore, token, *, now: float | None = None) -> dict:
    """Refresh the heartbeat of every claimed item under one batch
    token: the owning turn's proof it is still alive and working.

    Returns ``{"ok": true, "heartbeated": n}`` when n > 0, and
    ``{"ok": false, "stage": "stale-token"}`` when the token owns
    no live batch (stale token, or every item already replied /
    closed). The turn calls this first thing after waking, and
    again before any step that may take over a minute. The ack and
    claim-more paths refresh it automatically, so a turn that acks
    or claims more never needs an explicit heartbeat for those
    steps.
    """
    with store.transaction() as state:
        _ensure_adapter_state(state)
        refreshed = queue_core.heartbeat_batch(state, token, now=now)
    if refreshed == 0:
        return {"ok": False, "stage": "stale-token"}
    return {"ok": True, "heartbeated": refreshed}


def watchdog_sweep(store: JsonStateStore, *, now: float | None = None) -> dict:
    """Run one watchdog pass over the queue and report it.

    Recycles claimed items whose heartbeat went stale (a turn that
    died mid-work) back to pending, so the intake's next poll
    re-claims and re-wakes them. Never touches ``sending`` items,
    replied items, or live-heartbeat claims, and never sends
    anything. Also verifies the bridge install (intake hook
    definition, checkout, venv, sender — see
    ``queue_core.check_bridge_health``). Returns the sweep report
    (see ``queue_core.watchdog_sweep``).
    """
    with store.transaction() as state:
        _ensure_adapter_state(state)
        return queue_core.watchdog_sweep(
            state,
            heartbeat_stale=_heartbeat_stale(),
            active_heartbeat_stale=_active_heartbeat_stale(),
            health=_bridge_health(),
            now=now,
        )


def send_batch_and_close(
    store: JsonStateStore,
    target: int,
    token,
    chat_id,
    text: str,
    *,
    reply_to=None,
    keyboard: bool = False,
    extra_targets=(),
    max_reply_age: float | None = DEFAULT_MAX_REPLY_AGE,
    now: float | None = None,
) -> dict:
    """One reply for a whole batch, in one process.

    Sends once for the primary item (the usual fenced send, with
    its reply_to/keyboard/stale guards) and then records that same
    reply against every extra target and closes them all with
    done_reason "covered-by-batch-reply". The batch therefore
    costs one Telegram send and one agent reply instead of one
    send per message. The primary AND the extras are reserved
    (moved to "sending") atomically in the fence, before the
    network call — a turn that dies mid-send never leaves covered
    siblings claimed while the primary is sending. The fence rules
    still apply end to end:

    - reply_to must be None or the message_id of the primary or
      one of the extra items — never the bot's own message.
    - --keyboard is allowed only when at least one covered item
      answers a command.
    - Any covered item older than ``max_reply_age`` refuses the
      whole send (stage "stale") before anything goes out, with
      every item left claimed for reassessment.
    - If the send went out but a reply record could not be written
      for every item, the result is stage "record": the item(s)
      stay in "sending"/claimed for reconciliation and the reply
      is never resent.
    """
    extras = list(dict.fromkeys(int(t) for t in extra_targets))
    if target in extras:
        extras.remove(target)
    # Pre-flight checks that need the queued items: reply target,
    # keyboard rule and staleness for every covered item. All are
    # read-only; the fenced send re-checks the primary itself.
    check_now = time.time() if now is None else now
    with store.transaction() as state:
        _ensure_adapter_state(state)
        items = {}
        for external_id in [target] + extras:
            item = queue_core.find_item(state, PLATFORM, external_id)
            if item is None:
                return {"ok": False, "stage": "fence"}
            items[external_id] = item
        if reply_to is not None:
            allowed = {
                str(it.get("message_id"))
                for it in items.values()
                if it.get("message_id") is not None
            }
            if str(reply_to) not in allowed:
                return {"ok": False, "stage": "fence"}
        if keyboard and not any(
            _is_command_text(it.get("text")) for it in items.values()
        ):
            return {"ok": False, "stage": "keyboard"}
        if max_reply_age is not None:
            for external_id, item in items.items():
                limit = _reply_age_limit(item, max_reply_age)
                if (
                    limit is not None
                    and item.get("status") == queue_core.CLAIMED
                    and token is not None
                    and item.get("claim_token") == token
                    and item.get("replied_at") is None
                    and check_now - item["received_at"] > limit
                ):
                    return {"ok": False, "stage": "stale"}
    result = send_fenced(
        store,
        target,
        token,
        chat_id,
        text,
        reply_to=reply_to,
        keyboard=keyboard,
        extra_targets=extras,
        max_reply_age=max_reply_age,
        now=now,
    )
    if not result.get("ok"):
        return result
    all_ok, covered = _finish_send(
        store, target, token, result["message_id"], extras=extras
    )
    if all_ok:
        return {"ok": True, "message_id": result["message_id"], "covered": covered}
    return {"ok": False, "stage": "record"}


#: Timeout (seconds) for a best-effort sender subcommand (typing,
#: reaction, delete): a slow one must never stall the reply it
#: precedes. Typing itself expires after 5 s, so 10 s is ample.
BEST_EFFORT_TIMEOUT_SECONDS = 10.0


def _run_sender_action(send_cmd, argv_parts) -> bool:
    """Run one best-effort sender subcommand ([send_cmd, *argv_parts]).

    Returns True only when the sender exits 0. Any failure — the
    command is unconfigured, cannot start, times out, or exits
    nonzero — returns False and is logged to stderr; the caller
    proceeds anyway. Never reads stdin and never returns a message
    id: typing, reactions and deletes have no reply to record.
    """
    if not send_cmd:
        return False
    try:
        proc = subprocess.run(
            [send_cmd, *[str(part) for part in argv_parts]],
            input=None,
            capture_output=True,
            text=True,
            timeout=BEST_EFFORT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        print(f"sender-action failed: {exc}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        print(
            f"sender-action exit {proc.returncode}: {err[:200]}",
            file=sys.stderr,
        )
        return False
    return True


def delete_message(send_cmd, chat_id, message_id) -> bool:
    """Delete one Telegram message via the sender's --delete
    subcommand ([send_cmd, --delete, CHAT_ID, MESSAGE_ID]).

    Best-effort: returns True only when the sender exits 0
    (Telegram accepted the deleteMessage). Any failure returns
    False; the caller sends its reply anyway and reports
    "ack_deleted": false. ``send_cmd`` is the deployment's send
    executable, or None when unconfigured (a plain failure, never
    a crash). Non-integer ids are refused without calling the
    sender.
    """
    try:
        chat_id = int(chat_id)
        message_id = int(message_id)
    except (TypeError, ValueError):
        return False
    return _run_sender_action(send_cmd, ["--delete", chat_id, message_id])


def _warm_hold(
    store: JsonStateStore,
    chat_id,
    token,
    *,
    seconds: float,
    fetch=None,
    api_call=None,
    sleep=None,
) -> dict | None:
    """Lane-scoped warm hold after a --reply send (v2).

    Blocks up to ``seconds`` waiting for a follow-up in the SAME
    chat_id lane, WITHOUT holding poll.lock. The lane is marked
    busy by a warm lease in state.json (see
    queue_core.set_warm_lease), so intake's claim_batch leaves a
    follow-up pending for us instead of waking a cold turn; other
    lanes are never touched and stay claimable.

    When a follow-up arrives in this lane, it is claimed with
    claim_lane_batch (this lane only), marked warm_claimed, and
    its wake payload (build_payload, the same shape run_once
    returns) is returned as the caller's ``next_batch`` — the
    already-warm agent answers it with --reply again, zero cold
    start. Typing is fired on the claim so the user sees activity
    immediately.

    When nothing arrives, the lease expires (or is cleared here)
    and intake resumes normally; pending items in other lanes are
    left exactly as they were. If this process is killed -9 mid
    hold, the lease simply expires at its expires_at and the lane
    re-opens on its own, with any pending items still PENDING —
    nothing is lost and nothing is double-claimed.

    The baton guard lives in queue_core: a warm claim whose turn
    never started (turn_started_at None) is recycled only once
    its heartbeat is older than the 120 s warm-baton timeout, so a
    warm agent reading MEMORY before its first heartbeat is never
    recycled while thinking.

    Polling: the state file is re-read every 0.25 s; a
    non-blocking getUpdates fetch (long-poll 0) runs at most
    every 1.5 s, and only when poll.lock is free — if intake
    owns the lock the fetch is skipped and intake ingests the
    message instead. ``fetch``/``api_call``/``sleep`` are
    injectable for tests; defaults are the live fetch_updates /
    api / time.sleep.

    NOTE (unverified live): a 25 s blocking --reply has not been
    proven against a real Muse tool-call timeout. The hold length
    is env-tunable (TELEGRAM_BRIDGE_WARM_HOLD_SECONDS, default
    25) and 0 disables the hold entirely.
    """
    if seconds is None or seconds <= 0:
        return None
    fetch = fetch_updates if fetch is None else fetch
    api_call = api if api_call is None else api_call
    sleep = time.sleep if sleep is None else sleep
    start = time.time()
    deadline = start + float(seconds)
    # Register the lease: intake treats this lane as busy until
    # the lease expires, so a follow-up stays pending for us.
    with store.transaction() as state:
        _ensure_adapter_state(state)
        queue_core.set_warm_lease(
            state, chat_id, token, "unified", float(seconds), now=start
        )
    last_fetch = start - WARM_HOLD_REFETCH_MIN_SECONDS - 1.0
    send_cmd = os.environ.get(SEND_CMD_ENV)
    try:
        while True:
            now_t = time.time()
            if now_t >= deadline:
                return None
            loaded = _ensure_adapter_state(store.load())
            pending = [
                item
                for item in loaded.get("queue", [])
                if item.get("platform") == PLATFORM
                and item.get("status") == queue_core.PENDING
                and item.get("replied_at") is None
                and item.get("chat_id") == chat_id
                and (
                    item.get("wake_after") is None
                    or item.get("wake_after") <= now_t
                )
            ]
            if pending:
                with store.transaction() as state:
                    _ensure_adapter_state(state)
                    lease = (state.get("warm_leases") or {}).get(str(chat_id))
                    if isinstance(lease, dict) and lease.get("token") == token:
                        queue_core.clear_warm_lease(state, chat_id)
                    preclaim_filter(state, now=now_t)
                    batch = queue_core.claim_lane_batch(
                        state,
                        chat_id=chat_id,
                        platform=PLATFORM,
                        batch_size=BATCH_MAX_MESSAGES,
                        stale_ttl=float(
                            _env_int(
                                "TELEGRAM_BRIDGE_STALE_TTL",
                                int(queue_core.DEFAULT_STALE_TTL),
                            )
                        ),
                        heartbeat_stale=_heartbeat_stale(),
                        active_heartbeat_stale=_active_heartbeat_stale(),
                        warm_baton_timeout=_warm_baton_timeout(),
                        now=now_t,
                    )
                    if not batch:
                        # The pending items were noise closed by the
                        # pre-claim filter (or another claimer took
                        # them): re-check the lane, do not end the
                        # hold early.
                        continue
                    for item in batch["items"]:
                        item["warm_claimed"] = True
                    payload = build_payload(state, batch)
                if payload is None:
                    # Nothing viewable: the items stay claimed and
                    # recycle as stale for a later retry.
                    return None
                if send_cmd:
                    _run_sender_action(send_cmd, ["--typing", chat_id])
                return payload
            # No follow-up yet: re-fetch (non-blocking, poll lock
            # permitting) so a message that arrived after intake's
            # last poll is ingested without waiting for the next
            # hook tick. The lock is never held: if intake owns
            # it, the fetch is skipped.
            if now_t - last_fetch >= WARM_HOLD_REFETCH_MIN_SECONDS:
                last_fetch = now_t
                try:
                    lock_handle = open(poll_lock_path(), "a+")
                except OSError:
                    lock_handle = None
                if lock_handle is not None:
                    try:
                        try:
                            fcntl.flock(
                                lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB
                            )
                        except OSError:
                            pass
                        else:
                            previous_poll = os.environ.get(
                                LONG_POLL_SECONDS_ENV
                            )
                            os.environ[LONG_POLL_SECONDS_ENV] = "0"
                            try:
                                offset = _ensure_adapter_state(
                                    store.load()
                                )["offset"]
                                result = fetch(offset)
                            finally:
                                if previous_poll is None:
                                    os.environ.pop(
                                        LONG_POLL_SECONDS_ENV, None
                                    )
                                else:
                                    os.environ[LONG_POLL_SECONDS_ENV] = (
                                        previous_poll
                                    )
                            if isinstance(result, dict) and result.get("ok"):
                                fetched_at = time.time()
                                with store.transaction() as state:
                                    _ensure_adapter_state(state)
                                    ingest(
                                        state,
                                        result,
                                        now=fetched_at,
                                        api_call=api_call,
                                    )
                                    state["last_successful_poll"] = fetched_at
                    finally:
                        lock_handle.close()
            sleep(
                min(
                    WARM_HOLD_POLL_INTERVAL_SECONDS,
                    max(0.0, deadline - time.time()),
                )
            )
    finally:
        # Clear our lease if it is still there (timeout path). If
        # we claimed a follow-up it is already gone; if this
        # process is killed -9 the lease simply expires at its
        # expires_at and the lane re-opens on its own.
        try:
            with store.transaction() as state:
                _ensure_adapter_state(state)
                lease = (state.get("warm_leases") or {}).get(str(chat_id))
                if isinstance(lease, dict) and lease.get("token") == token:
                    queue_core.clear_warm_lease(state, chat_id)
        except Exception:
            pass


def send_reply(
    store: JsonStateStore,
    token,
    text: str,
    *,
    react=None,
    keyboard: bool = False,
    max_reply_age: float | None = DEFAULT_MAX_REPLY_AGE,
    now: float | None = None,
    warm_hold_seconds: float | None = None,
    fetch=None,
    api_call=None,
    sleep=None,
) -> dict:
    """The single send path for a turn (v2 --reply).

    The caller passes only the claim token (plus the reply text);
    everything else is derived from the claimed batch, in order:

    (a) Look up ALL claimed, unreplied items under the token. No
        items, or items spanning more than one chat_id, fails
        closed with nothing sent: one batch is one
        lane, so a split batch is corrupt state, never a reply to
        half of it. No items at all means the token owns no live
        batch any more — the claim was recycled while the turn was
        working — so that case reports the distinct stage
        "stale-token" instead of the generic "fence", and the
        split-batch case keeps "fence". Any item under the token
        that is SENDING with no recorded reply also fails closed
        with "fence": a previous send under this token went out
        (or did not) and its outcome is unknown, so silently
        replying for only the still-claimed siblings would drop
        it. A SENDING item WITH a reply recorded is not a fence:
        its answer went out, so it falls through to the existing
        stale-token path like any other replied item.
    (b) Heartbeat the whole batch first (proof the turn is alive).
    (c) Fire typing in the chat (best-effort, never blocks the
        send).
    (d) When --react is given, react on the latest user message in
        the batch — the most recent claimed item with a real
        message_id (best-effort, never blocks the send). A batch
        with no user message (e.g. button taps only) is never
        reacted to: there is no user message to react to.
    (e) Delete any temporary ack_message_id recorded on the
        claimed items (see queue_core.mark_ack / send_ack),
        best-effort only: a failed delete still sends, and the
        result reports "ack_deleted": false.
    (f) Send the reply once, threaded under the latest user
        message_id (standalone when no item has one), through
        send_batch_and_close — so the fence, the max-reply-age
        guard, the keyboard rule and the unknown-send handling are
        exactly the batch send's. On stage "unknown" nothing is
        resent and nothing is closed as replied: the primary item
        stays in "sending" for reconciliation, exactly as the
        batch send leaves it, so the caller can check delivery
        first.
    (g) On success, record the reply against and close ALL items
        in the batch as DONE (no orphaned siblings), with the same
        close semantics as send_batch_and_close: the primary item
        closes plainly, the rest with done_reason
        "covered-by-batch-reply", and replied_at is recorded on
        every covered item, as the batch send does today.

    Returns one dict with at least {"ok", "stage", "message_id",
    "chat_id", "replied_update_ids", "closed_update_ids",
    "ack_deleted", "next_batch"}: "message_id" is the Telegram
    message id as an int on success, else None; "chat_id" is the
    batch's chat, or None when no batch could be derived;
    "replied_update_ids" / "closed_update_ids" list the covered
    update ids on success and are empty on failure; "ack_deleted"
    is True only when at least one ack message existed and every
    delete succeeded; "next_batch" is the wake payload of a
    follow-up that arrived in the same lane during the warm hold
    (see _warm_hold), or None when nothing arrived before the
    hold expired. When "next_batch" is present, the caller answers
    it the same way — --reply again with next_batch's
    claim_token — with zero cold start.

    The warm hold runs after a successful send only, for
    ``warm_hold_seconds`` seconds (None = the
    TELEGRAM_BRIDGE_WARM_HOLD_SECONDS env, default 25; 0 =
    disabled; --no-warm-hold on the CLI). ``fetch`` /
    ``api_call`` / ``sleep`` are injectable for tests. A 25 s
    blocking --reply has NOT been proven against a real Muse
    tool-call timeout — see _warm_hold.

    --reply is NOT a substitute for --send-ack: ack first for work
    that will take 10+ minutes (send_ack sends the fixed ack and
    leaves the item claimed), then send the full reply with
    --reply. --reply itself never acks.
    """
    def _result(ok, stage, message_id=None, chat_id=None,
                replied=(), closed=(), ack_deleted=False,
                next_batch=None, item_status=None, replied_flag=None):
        result = {
            "ok": ok,
            "stage": stage,
            "message_id": message_id,
            "chat_id": chat_id,
            "replied_update_ids": list(replied),
            "closed_update_ids": list(closed),
            "ack_deleted": ack_deleted,
            "next_batch": next_batch,
        }
        # Disposition on stage "stale-token" only (see (a)/(b)):
        # what the batch's items look like now, so the caller knows
        # whether the answer went out and whether a re-wake owns
        # the retry — there is no turn-side state-read CLI, so the
        # result itself must carry it.
        if item_status is not None:
            result["item_status"] = item_status
        if replied_flag is not None:
            result["replied"] = replied_flag
        return result

    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        return _result(False, "input")
    # (a) The whole batch, derived from the token alone.
    with store.transaction() as state:
        _ensure_adapter_state(state)
        items = [
            item
            for item in state.get("queue", [])
            if item.get("platform") == PLATFORM
            and item.get("status") == queue_core.CLAIMED
            and item.get("claim_token") == token
            and item.get("replied_at") is None
        ]
        # A SENDING sibling with no recorded reply fails closed:
        # its send outcome is unknown, so replying for only the
        # still-claimed items would silently drop it. A SENDING
        # item WITH a reply recorded is not fenced here — its
        # answer went out, so it falls through below.
        sending_siblings = [
            item
            for item in state.get("queue", [])
            if item.get("platform") == PLATFORM
            and item.get("status") == queue_core.SENDING
            and item.get("claim_token") == token
            and item.get("replied_at") is None
        ]
    if sending_siblings:
        return _result(False, "fence")
    if not items:
        return _result(False, "stale-token")
    chat_ids = {item.get("chat_id") for item in items}
    if len(chat_ids) != 1:
        return _result(False, "fence")
    chat_id = next(iter(chat_ids))
    if type(chat_id) is not int:
        return _result(False, "fence")
    # (b) Heartbeat first: the turn is alive and working.
    with store.transaction() as state:
        _ensure_adapter_state(state)
        refreshed = queue_core.heartbeat_batch(state, token, now=now)
    if refreshed == 0:
        # The batch vanished between the lookup and the heartbeat
        # (recycled mid-send): same stale-token story as (a), not
        # a generic fence failure. The items were found in (a), so
        # report one item's disposition now (the first found item;
        # the whole batch shares one token and one fate).
        with store.transaction() as state:
            _ensure_adapter_state(state)
            current = queue_core.find_item(
                state, PLATFORM, int(items[0]["external_id"])
            )
        if current is not None:
            return _result(
                False, "stale-token", chat_id=chat_id,
                item_status=current.get("status"),
                replied_flag=current.get("replied_at") is not None,
            )
        return _result(False, "stale-token", chat_id=chat_id)
    ordered = sorted(items, key=lambda it: it.get("received_at") or 0)

    def _message_id(item):
        try:
            return int(item.get("message_id"))
        except (TypeError, ValueError):
            return None

    with_message = [
        item for item in ordered if _message_id(item) is not None
    ]
    if with_message:
        primary = with_message[-1]
        reply_to = _message_id(primary)
    else:
        primary = ordered[0]
        reply_to = None
    primary_id = int(primary["external_id"])
    extras = [
        int(item["external_id"]) for item in ordered if item is not primary
    ]
    send_cmd = os.environ.get(SEND_CMD_ENV)
    # (c) Typing, best-effort: refresh the indicator right before
    # sending; a failed typing call never blocks the reply.
    if send_cmd:
        _run_sender_action(send_cmd, ["--typing", chat_id])
        # (d) Reaction on the latest user message, best-effort and
        # only when there is a user message to react to.
        if react and reply_to is not None:
            _run_sender_action(
                send_cmd, ["--react", chat_id, reply_to, react]
            )
    # (e) Delete any recorded ack message(s), best-effort only: a
    # failed delete still sends, and ack_deleted reports it.
    ack_ids = []
    for item in ordered:
        ack_id = item.get("ack_message_id")
        if ack_id is not None and ack_id not in ack_ids:
            ack_ids.append(ack_id)
    ack_deleted = bool(ack_ids) and all(
        delete_message(send_cmd, chat_id, ack_id) for ack_id in ack_ids
    )
    # (f) One fenced batch send, threaded under the latest user
    # message (or standalone). (g) is inside send_batch_and_close:
    # it records the reply and closes every covered item.
    result = send_batch_and_close(
        store,
        primary_id,
        token,
        chat_id,
        text,
        reply_to=reply_to,
        keyboard=keyboard,
        extra_targets=extras,
        max_reply_age=max_reply_age,
        now=now,
    )
    if not result.get("ok"):
        return _result(
            False, result.get("stage", "unknown"),
            chat_id=chat_id, ack_deleted=ack_deleted,
        )
    message_id = result.get("message_id")
    try:
        message_id = int(message_id)
    except (TypeError, ValueError):
        message_id = None
    covered_ids = [primary_id] + extras
    # Warm hold: after a successful send only. The hold length
    # comes from the caller, else the env (default 25 s); 0
    # disables it. A follow-up in this lane comes back as
    # next_batch for the already-warm agent.
    hold_seconds = (
        warm_hold_seconds
        if warm_hold_seconds is not None
        else _warm_hold_seconds()
    )
    next_batch = None
    if hold_seconds and hold_seconds > 0:
        next_batch = _warm_hold(
            store,
            chat_id,
            token,
            seconds=hold_seconds,
            fetch=fetch,
            api_call=api_call,
            sleep=sleep,
        )
    return _result(
        True, "sent", message_id=message_id, chat_id=chat_id,
        replied=covered_ids, closed=covered_ids,
        ack_deleted=ack_deleted, next_batch=next_batch,
    )


def _parse_send_args(extra: list) -> tuple:
    """Optional args after UPDATE_ID CLAIM_TOKEN CHAT_ID for the send
    commands: a bare integer is the REPLY_TO_MESSAGE_ID, and
    --keyboard attaches the inline command keyboard. Order does not
    matter. Anything else is rejected (returns None, None, False)."""
    reply_to = None
    keyboard = False
    for arg in extra:
        if arg == "--keyboard":
            keyboard = True
        else:
            try:
                reply_to = int(arg)
            except ValueError:
                return None, None, False
    return reply_to, keyboard, True


def _parse_batch_send_args(extra: list) -> tuple:
    """Optional args after PRIMARY_ID CLAIM_TOKEN CHAT_ID for
    --send-batch-and-close: a bare integer is the REPLY_TO_MESSAGE_ID,
    --keyboard attaches the inline command keyboard, and --cover
    takes one or more UPDATE_IDs (the other batch items the single
    reply covers). Order does not matter. Anything else is rejected
    (returns None, None, [], False)."""
    reply_to = None
    keyboard = False
    cover: list = []
    index = 0
    while index < len(extra):
        arg = extra[index]
        if arg == "--keyboard":
            keyboard = True
            index += 1
        elif arg == "--cover":
            index += 1
            if index >= len(extra):
                return None, None, [], False
            while index < len(extra) and not extra[index].startswith("--"):
                try:
                    cover.append(int(extra[index]))
                except ValueError:
                    return None, None, [], False
                index += 1
        else:
            try:
                if reply_to is not None:
                    return None, None, [], False
                reply_to = int(arg)
            except ValueError:
                return None, None, [], False
            index += 1
    return reply_to, keyboard, cover, True


def _resolve_group_target(state: dict, chat_id_arg):
    """Resolve a per-group CLI target: (chat_id, error).

    A chat id names the group directly (it must be adopted). With
    no chat id, the single adopted group is used when there is
    exactly one; with none adopted the error is "no-group-adopted",
    and with several it is "multiple-groups-require-chat-id" (the
    caller must name the group, so a per-group change can never
    land on the wrong group)."""
    ids = list(state.get("group_ids") or [])
    if chat_id_arg is not None:
        try:
            chat_id = int(chat_id_arg)
        except (TypeError, ValueError):
            return None, "invalid-chat-id"
        if _group_record(state, chat_id) is None:
            return None, "group-not-adopted"
        return chat_id, None
    if not ids:
        return None, "no-group-adopted"
    if len(ids) > 1:
        return None, "multiple-groups-require-chat-id"
    return ids[0], None


def _group_mode_view(state: dict, chat_id) -> dict:
    """The read-only view of one adopted group for --group-mode."""
    group = _group_record(state, chat_id)
    if group is None:
        return {"chat_id": chat_id, "adopted": False}
    return {
        "chat_id": chat_id,
        "mode": _group_mode(state, chat_id),
        "allowlisted": is_group_allowlisted(state, chat_id),
        "safety_tripped": group.get("safety_tripped"),
        "mode_effective": effective_group_mode(state, "group", chat_id),
        "member_count": group.get("member_count"),
    }


def main(argv=None, *, fetch=fetch_updates, path: Path | None = None, api_call=api) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] in (["--help"], ["-h"]):
        # Help is read-only: print the module usage and exit before
        # any state is opened, so a probe can never fall through
        # to the poller and race the intake's long poll (HTTP 409).
        print(__doc__.strip())
        return 0
    store = JsonStateStore(path or state_path())

    if args[:1] == ["--adopt"] and len(args) in (2, 3):
        # Bootstrap is a one-time operator action, not a mutable preference.
        # Silently replacing an established owner can transfer all future DM
        # access to the wrong Telegram account.
        try:
            requested = int(args[1])
        except ValueError:
            requested = 0
        if requested <= 0:
            print(json.dumps({"ok": False, "error": "invalid-owner-id"}))
            return 2
        mode = None
        if len(args) == 3:
            # Optional group sharing mode, chosen at setup: the
            # owner picks trusted (a private group with only the
            # owner + his Muse bots) or guarded (any group with
            # other people). It becomes the default mode the FIRST
            # group adopted gets; every group adopted after that
            # starts guarded until Muse sets its mode
            # (--set-group-mode). Locked after setup; only Muse
            # changes a group's mode later, never Telegram.
            if args[2] not in (GROUP_MODE_TRUSTED, GROUP_MODE_GUARDED):
                print(json.dumps({"ok": False, "error": "invalid-group-mode"}))
                return 2
            mode = args[2]
        with store.transaction() as state:
            _ensure_adapter_state(state)
            existing = state.get("owner_id")
            if existing is not None and (type(existing) is not int or existing != requested):
                print(json.dumps({"ok": False, "error": "owner-already-adopted"}))
                return 2
            state["owner_id"] = requested
            state["candidates"] = []
            if mode is not None:
                state["default_group_mode"] = mode
        result = {"owner_id": requested}
        if mode is not None:
            result["default_group_mode"] = mode
        print(json.dumps(result))
        return 0

    if args[:1] in (["--done"], ["--release"]) and len(args) in (2, 3, 4, 5):
        target = int(args[1])
        token = args[2] if len(args) >= 3 else None
        reason = args[3] if len(args) >= 4 and args[0] == "--done" else None
        evidence = args[4] if len(args) == 5 and args[0] == "--done" else None
        with store.transaction() as state:
            _ensure_adapter_state(state)
            if args[0] == "--done":
                if reason is None:
                    # Close-path audit (2026-11-12 turns-dying bug):
                    # the owning turn closed unanswered @mentions
                    # with no reason recorded (items 344786942-945:
                    # done, replied_at None, done_reason None).
                    # Default the reason so every silent close is
                    # auditable; request items get their own marker.
                    # queue_core.mark_done itself stays reason-optional
                    # (its tests pin that); the default lives here, on
                    # the operator path the agent actually uses.
                    item = queue_core.find_item(state, PLATFORM, target)
                    if item is not None and item.get("replied_at") is None:
                        if item.get("item_class") == queue_core.CLASS_REQUEST:
                            reason = "closed-no-reply-request"
                        else:
                            reason = "closed-no-reply"
                ok = queue_core.mark_done(
                    state, PLATFORM, target, token,
                    reason=reason, evidence=evidence,
                )
            else:
                ok = queue_core.release(state, PLATFORM, target, token)
        print(json.dumps({"ok": ok}))
        return 0

    if args[:1] == ["--send-fenced"]:
        if len(args) < 4:
            print(json.dumps({
                "ok": False,
                "error": "usage: --send-fenced UPDATE_ID CLAIM_TOKEN CHAT_ID "
                         "[REPLY_TO_MESSAGE_ID] [--keyboard]",
            }))
            return 2
        reply_to, keyboard, valid = _parse_send_args(args[4:])
        if not valid:
            print(json.dumps({
                "ok": False,
                "error": "usage: --send-fenced UPDATE_ID CLAIM_TOKEN CHAT_ID "
                         "[REPLY_TO_MESSAGE_ID] [--keyboard]",
            }))
            return 2
        result = send_fenced(
            store,
            int(args[1]),
            args[2],
            args[3],
            sys.stdin.read(),
            reply_to=reply_to,
            keyboard=keyboard,
            max_reply_age=_max_reply_age(),
        )
        print(json.dumps(result))
        return 0

    if args[:1] == ["--send-and-close"]:
        if len(args) < 4:
            print(json.dumps({
                "ok": False,
                "error": "usage: --send-and-close UPDATE_ID CLAIM_TOKEN CHAT_ID "
                         "[REPLY_TO_MESSAGE_ID] [--keyboard]",
            }))
            return 2
        reply_to, keyboard, valid = _parse_send_args(args[4:])
        if not valid:
            print(json.dumps({
                "ok": False,
                "error": "usage: --send-and-close UPDATE_ID CLAIM_TOKEN CHAT_ID "
                         "[REPLY_TO_MESSAGE_ID] [--keyboard]",
            }))
            return 2
        result = send_and_close(
            store,
            int(args[1]),
            args[2],
            args[3],
            sys.stdin.read(),
            reply_to=reply_to,
            keyboard=keyboard,
            max_reply_age=_max_reply_age(),
        )
        print(json.dumps(result))
        return 0

    if args[:1] == ["--send-ack"]:
        # --send-ack UPDATE_ID CLAIM_TOKEN CHAT_ID [REPLY_TO_MESSAGE_ID]:
        # send the fixed acknowledgement for a claimed item without
        # closing it. The ack text is fixed (ACK_TEXT); nothing is
        # read from stdin. Prints {"ok": true, "message_id": ...} on
        # success, {"ok": false, "stage": "already-acked"} when an
        # ack is already recorded, or the fence/config/unknown
        # failure result.
        if len(args) < 4:
            print(json.dumps({
                "ok": False,
                "error": "usage: --send-ack UPDATE_ID CLAIM_TOKEN CHAT_ID "
                         "[REPLY_TO_MESSAGE_ID]",
            }))
            return 2
        reply_to, _keyboard, valid = _parse_send_args(args[4:])
        if not valid:
            print(json.dumps({
                "ok": False,
                "error": "usage: --send-ack UPDATE_ID CLAIM_TOKEN CHAT_ID "
                         "[REPLY_TO_MESSAGE_ID]",
            }))
            return 2
        result = send_ack(
            store,
            int(args[1]),
            args[2],
            args[3],
            reply_to=reply_to,
        )
        print(json.dumps(result))
        return 0

    if args[:1] == ["--heartbeat"]:
        # --heartbeat CLAIM_TOKEN: refresh the heartbeat of every
        # claimed item under the token — the owning turn's proof it
        # is still alive. Prints {"ok": true, "heartbeated": n}, or
        # {"ok": false, "stage": "fence"} when the token owns no
        # live batch. The turn calls this first thing after waking
        # and before any step that may take over a minute.
        if len(args) != 2:
            print(json.dumps({
                "ok": False,
                "error": "usage: --heartbeat CLAIM_TOKEN",
            }))
            return 2
        print(json.dumps(heartbeat_batch(store, args[1])))
        return 0

    if args[:1] == ["--watchdog"]:
        # --watchdog: one watchdog pass — recycle claimed items
        # whose heartbeat went stale (a turn that died mid-work)
        # back to pending, verify the bridge install (intake hook
        # definition, checkout, venv, sender), and report queue
        # health. Never touches sending / replied / live claims,
        # never sends anything.
        # Prints the sweep report JSON. The hook script calls this
        # every few minutes; the intake's next poll re-claims and
        # re-wakes anything recycled here.
        if len(args) != 1:
            print(json.dumps({
                "ok": False,
                "error": "usage: --watchdog",
            }))
            return 2
        try:
            print(json.dumps(watchdog_sweep(store)))
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)[:200]}))
            return 1
        return 0

    if args[:1] == ["--claim-more"]:
        # --claim-more CHAT_ID CLAIM_TOKEN [BATCH_SIZE]: pull messages
        # that arrived during the turn into this turn's batch, so
        # they are answered by the same reply instead of a second
        # turn. Only the token that owns the lane's in-flight batch
        # may claim more.
        if len(args) not in (3, 4):
            print(json.dumps({
                "ok": False,
                "error": "usage: --claim-more CHAT_ID CLAIM_TOKEN "
                         "[BATCH_SIZE]",
            }))
            return 2
        try:
            chat_id = int(args[1])
        except ValueError:
            print(json.dumps({"ok": False, "error": "invalid-chat-id"}))
            return 2
        batch_size = queue_core.DEFAULT_BATCH_SIZE
        if len(args) == 4:
            try:
                batch_size = int(args[3])
            except ValueError:
                print(json.dumps({"ok": False, "error": "invalid-batch-size"}))
                return 2
        with store.transaction() as state:
            _ensure_adapter_state(state)
            batch = queue_core.claim_more(
                state,
                platform=PLATFORM,
                chat_id=chat_id,
                token=args[2],
                batch_size=batch_size,
            )
            if batch is not None:
                # The turn is alive (it just claimed more), so
                # refresh the whole batch heartbeat: claiming more
                # proves the turn is working, and the pre-existing
                # items get the same fresh heartbeat as the new ones.
                queue_core.heartbeat_batch(state, args[2])
            if batch is None:
                claimed = None
            else:
                expired_ids = _expired_external_ids(state)
                claimed = {
                    "claim_token": batch["claim_token"],
                    "items": [
                        _payload_item(state, item, expired_ids)
                        for item in batch["items"]
                    ],
                }
        if claimed is None:
            print(json.dumps({"ok": True, "items": []}))
            return 0
        print(json.dumps({"ok": True, **claimed}))
        return 0

    if args[:1] == ["--reply"]:
        # --reply CLAIM_TOKEN [--react EMOJI] [--keyboard]: the
        # single send path for a turn (v2). The reply text is read
        # from stdin (heredoc, never pasted into a shell command);
        # the batch, chat, reply target and cover list are all
        # derived from the claim token — the caller passes no
        # update/chat/reply ids. Prints the send_reply result JSON.
        # --reply sends the full reply and closes the batch; it is
        # NOT a substitute for --send-ack (ack first for long work,
        # then send the full reply with --reply).
        _reply_usage = (
            "usage: --reply CLAIM_TOKEN [--react EMOJI] [--keyboard] "
            "[--no-warm-hold] — reply text on stdin; --reply sends the "
            "full reply and closes the batch, it is NOT a substitute "
            "for --send-ack (ack first for long work, then --reply)."
        )
        if len(args) < 2:
            print(json.dumps({"ok": False, "error": _reply_usage}))
            return 2
        react = None
        keyboard = False
        no_warm_hold = False
        valid = True
        index = 2
        while index < len(args):
            arg = args[index]
            if arg == "--keyboard":
                keyboard = True
                index += 1
            elif arg == "--no-warm-hold":
                no_warm_hold = True
                index += 1
            elif arg == "--react":
                if (
                    index + 1 >= len(args)
                    or not args[index + 1].strip()
                    or react is not None
                ):
                    valid = False
                    break
                react = args[index + 1]
                index += 2
            else:
                valid = False
                break
        if not valid:
            print(json.dumps({"ok": False, "error": _reply_usage}))
            return 2
        result = send_reply(
            store,
            args[1],
            sys.stdin.read(),
            react=react,
            keyboard=keyboard,
            max_reply_age=_max_reply_age(),
            warm_hold_seconds=0.0 if no_warm_hold else None,
        )
        print(json.dumps(result))
        return 0

    if args[:1] == ["--send-batch-and-close"]:
        # --send-batch-and-close PRIMARY_ID CLAIM_TOKEN CHAT_ID
        # [REPLY_TO_MESSAGE_ID] [--keyboard] [--cover ID ...]:
        # one reply for the whole batch. Sends once for the
        # primary item, records that reply against every --cover
        # item and closes them all. The reply text is read from
        # stdin (file-based, never pasted into a shell command).
        if len(args) < 4:
            print(json.dumps({
                "ok": False,
                "error": "usage: --send-batch-and-close PRIMARY_ID "
                         "CLAIM_TOKEN CHAT_ID [REPLY_TO_MESSAGE_ID] "
                         "[--keyboard] [--cover UPDATE_ID ...]",
            }))
            return 2
        reply_to, keyboard, cover, valid = _parse_batch_send_args(args[4:])
        if not valid:
            print(json.dumps({
                "ok": False,
                "error": "usage: --send-batch-and-close PRIMARY_ID "
                         "CLAIM_TOKEN CHAT_ID [REPLY_TO_MESSAGE_ID] "
                         "[--keyboard] [--cover UPDATE_ID ...]",
            }))
            return 2
        result = send_batch_and_close(
            store,
            int(args[1]),
            args[2],
            args[3],
            sys.stdin.read(),
            reply_to=reply_to,
            keyboard=keyboard,
            extra_targets=cover,
            max_reply_age=_max_reply_age(),
        )
        print(json.dumps(result))
        return 0

    if args[:1] in (["--send-photo-and-close"], ["--send-document-and-close"]):
        # --send-photo-and-close ID TOKEN CHAT_ID FILE_PATH
        # [REPLY_TO_MESSAGE_ID] [--keyboard] [--cover UPDATE_ID ...]
        # (same shape for --send-document-and-close): one photo/file
        # reply, fenced like a text send. The caption is read from
        # stdin (optional, may be empty, max 1024 characters). The
        # file must live under the home directory.
        if len(args) < 5:
            print(json.dumps({
                "ok": False,
                "error": f"usage: {args[0]} UPDATE_ID CLAIM_TOKEN CHAT_ID "
                         "FILE_PATH [REPLY_TO_MESSAGE_ID] [--keyboard] "
                         "[--cover UPDATE_ID ...]",
            }))
            return 2
        reply_to, keyboard, cover, valid = _parse_batch_send_args(args[5:])
        if not valid:
            print(json.dumps({
                "ok": False,
                "error": f"usage: {args[0]} UPDATE_ID CLAIM_TOKEN CHAT_ID "
                         "FILE_PATH [REPLY_TO_MESSAGE_ID] [--keyboard] "
                         "[--cover UPDATE_ID ...]",
            }))
            return 2
        result = send_media_and_close(
            store,
            int(args[1]),
            args[2],
            args[3],
            "photo" if args[0] == "--send-photo-and-close" else "document",
            args[4],
            sys.stdin.read().strip(),
            reply_to=reply_to,
            keyboard=keyboard,
            extra_targets=cover,
            max_reply_age=_max_reply_age(),
        )
        print(json.dumps(result))
        return 0

    if args[:1] == ["--refresh-admins"]:
        chat_id = None
        if len(args) == 2:
            try:
                chat_id = int(args[1])
            except ValueError:
                print(json.dumps({"ok": False, "error": "invalid-chat-id"}))
                return 2
        elif len(args) != 1:
            print(json.dumps({
                "ok": False,
                "error": "usage: --refresh-admins [CHAT_ID]",
            }))
            return 2
        with store.transaction() as state:
            _ensure_adapter_state(state)
            chat_id, error = _resolve_group_target(state, args[1] if len(args) == 2 else None)
            if error is not None:
                print(json.dumps({"ok": False, "error": error}))
                return 2
            record = refresh_group_admins(
                state, chat_id, force=True, api_call=api_call
            )
        print(json.dumps({"ok": record is not None, "group_admins": record}))
        return 0

    if args[:1] == ["--group-mode"] and len(args) in (1, 2):
        # Read-only inspection: the per-group modes, the trusted
        # bots, the allowlist, and the default mode the first
        # group adopted gets. With a chat id, only that group.
        state = _ensure_adapter_state(store.load())
        if len(args) == 2:
            chat_id, error = _resolve_group_target(state, args[1])
            if error is not None:
                print(json.dumps({"ok": False, "error": error}))
                return 2
            print(json.dumps({
                "ok": True,
                "default_group_mode": (
                    state.get("default_group_mode") or GROUP_MODE_GUARDED
                ),
                "trusted_bots": sorted(_trusted_bot_names(state)),
                "trusted_group_ids": sorted(_trusted_group_ids(state)),
                "groups": [_group_mode_view(state, chat_id)],
            }))
            return 0
        print(json.dumps({
            "ok": True,
            "default_group_mode": (
                state.get("default_group_mode") or GROUP_MODE_GUARDED
            ),
            "trusted_bots": sorted(_trusted_bot_names(state)),
            "trusted_group_ids": sorted(_trusted_group_ids(state)),
            "groups": [
                _group_mode_view(state, chat_id)
                for chat_id in (state.get("group_ids") or [])
            ],
        }))
        return 0

    if args[:1] == ["--set-group-mode"] and len(args) in (2, 3):
        # Operator / Muse only: change one group's locked sharing
        # mode. There is deliberately no Telegram path to this —
        # the owner changes a group's mode by asking Muse, never
        # from a chat. Setting the mode is an explicit owner
        # decision, so it also clears that group's safety trip
        # (the owner is affirming the group's state). An invalid
        # mode changes nothing. With no chat id it uses the single
        # adopted group; a chat id is required when several groups
        # are adopted.
        if args[1] not in (GROUP_MODE_TRUSTED, GROUP_MODE_GUARDED):
            print(json.dumps({"ok": False, "error": "invalid-group-mode"}))
            return 2
        with store.transaction() as state:
            _ensure_adapter_state(state)
            chat_id, error = _resolve_group_target(
                state, args[2] if len(args) == 3 else None
            )
            if error is not None:
                print(json.dumps({"ok": False, "error": error}))
                return 2
            group = _group_record(state, chat_id)
            group["mode"] = args[1]
            group["safety_tripped"] = None
            group["mode_notice"] = None
        print(json.dumps({
            "ok": True, "chat_id": chat_id, "group_mode": args[1],
        }))
        return 0

    if args[:1] == ["--set-trusted-bots"] and len(args) == 2:
        # Operator / Muse only: replace the trusted-bot list
        # (comma-separated usernames, with or without "@"; a purely
        # numeric entry is a Telegram user id). This bot itself is
        # always kept in the list. Changing the list does NOT change
        # the mode and does NOT clear a safety trip: adding a bot
        # never silently re-enables sharing.
        names = set()
        for entry in args[1].split(","):
            entry = entry.strip().lstrip("@").lower()
            if entry:
                names.add(entry)
        names.add(_bot_username())
        with store.transaction() as state:
            _ensure_adapter_state(state)
            state["trusted_bots"] = sorted(names)
        print(json.dumps({"ok": True, "trusted_bots": sorted(names)}))
        return 0

    if args[:1] == ["--clear-group-safety-trip"] and len(args) in (1, 2):
        # Operator / Muse only: clear one group's safety trip (and
        # its notice) after the owner decides the group is safe
        # again — e.g. the stranger left. The stored mode is
        # untouched; this only re-enables trusted sharing under
        # the mode already set. With no chat id it uses the single
        # adopted group; a chat id is required when several groups
        # are adopted.
        with store.transaction() as state:
            _ensure_adapter_state(state)
            chat_id, error = _resolve_group_target(
                state, args[1] if len(args) == 2 else None
            )
            if error is not None:
                print(json.dumps({"ok": False, "error": error}))
                return 2
            group = _group_record(state, chat_id)
            was_tripped = group.get("safety_tripped") is not None
            group["safety_tripped"] = None
            group["mode_notice"] = None
        print(json.dumps({
            "ok": True, "chat_id": chat_id, "was_tripped": was_tripped,
        }))
        return 0

    if args[:1] == ["--allowlist-group"] and len(args) == 2:
        # Operator / Muse only: add a group to the private-channel
        # allowlist. A listed group is always trusted and the
        # safety trips never apply there, no matter who joins.
        # There is deliberately no Telegram path to this — the
        # owner allowlists a group by asking Muse, never from a
        # chat. Adding a group does NOT clear a safety trip: use
        # --clear-group-safety-trip for that (the effective mode
        # is trusted anyway while the group is listed).
        try:
            chat_id = int(args[1])
        except ValueError:
            print(json.dumps({"ok": False, "error": "invalid-chat-id"}))
            return 2
        with store.transaction() as state:
            _ensure_adapter_state(state)
            ids = _trusted_group_ids(state)
            ids.add(chat_id)
            state["trusted_group_ids"] = sorted(ids)
        print(json.dumps({"ok": True, "trusted_group_ids": sorted(ids)}))
        return 0

    if args[:1] == ["--unallowlist-group"] and len(args) == 2:
        # Operator / Muse only: remove a group from the allowlist.
        # The safety trips apply to it again from now on (a trip
        # does not retroactively fire for members already there;
        # the next member-count check or stranger message trips it
        # if the group no longer qualifies as trusted).
        try:
            chat_id = int(args[1])
        except ValueError:
            print(json.dumps({"ok": False, "error": "invalid-chat-id"}))
            return 2
        with store.transaction() as state:
            _ensure_adapter_state(state)
            ids = _trusted_group_ids(state)
            ids.discard(chat_id)
            state["trusted_group_ids"] = sorted(ids)
        print(json.dumps({"ok": True, "trusted_group_ids": sorted(ids)}))
        return 0

    if args[:1] == ["--list-allowlisted-groups"] and len(args) == 1:
        # Read-only inspection of the allowlist.
        state = _ensure_adapter_state(store.load())
        print(json.dumps({
            "ok": True,
            "trusted_group_ids": sorted(_trusted_group_ids(state)),
        }))
        return 0

    if args[:1] == ["--active"]:
        # --active [CHAT_ID] [WINDOW_SECONDS]: who spoke in one
        # group recently, from that group's buffer. A negative
        # first argument is a group chat id (group ids are
        # negative); otherwise it is the window in seconds
        # (default 3600). With no chat id it uses the single
        # adopted group; a chat id is required when several groups
        # are adopted.
        window = 3600
        chat_arg = None
        rest = args[1:]
        if rest:
            try:
                first = int(rest[0])
            except ValueError:
                print(json.dumps({"ok": False, "error": "invalid-window"}))
                return 2
            if first < 0:
                chat_arg = rest[0]
                rest = rest[1:]
                if rest:
                    try:
                        window = int(rest[0])
                    except ValueError:
                        print(json.dumps({"ok": False, "error": "invalid-window"}))
                        return 2
                    rest = rest[1:]
            else:
                window = first
                rest = rest[1:]
        if rest:
            print(json.dumps({
                "ok": False,
                "error": "usage: --active [CHAT_ID] [WINDOW_SECONDS]",
            }))
            return 2
        state = _ensure_adapter_state(store.load())
        chat_id, error = _resolve_group_target(state, chat_arg)
        if error is not None:
            print(json.dumps({"ok": False, "error": error}))
            return 2
        group = _group_record(state, chat_id)
        speakers = recent_speakers(
            group.get("buffer", []) if group else [], time.time(), window
        )
        print(json.dumps({
            "ok": True, "chat_id": chat_id, "speakers": speakers,
        }))
        return 0

    if args[:1] == ["--purge-expired"]:
        older_than = None
        dry_run = False
        rest = args[1:]
        index = 0
        valid = True
        while index < len(rest):
            if rest[index] == "--dry-run":
                dry_run = True
                index += 1
            elif rest[index] == "--older-than" and index + 1 < len(rest):
                try:
                    older_than = float(rest[index + 1])
                except ValueError:
                    valid = False
                    break
                # float() accepts "nan"/"inf"; a NaN threshold would
                # expire every eligible item and an infinite one
                # none. Both are input errors — reject them here,
                # before any state is loaded or mutated.
                if not math.isfinite(older_than):
                    valid = False
                    break
                index += 2
            else:
                valid = False
                break
        if not valid:
            print(json.dumps({
                "ok": False,
                "error": "usage: --purge-expired "
                         "[--older-than SECONDS] [--dry-run]",
            }))
            return 2
        group_ttl, private_ttl = _expiry_ttls()
        if older_than is not None:
            group_ttl = older_than
        if dry_run:
            # A dry run only reads: no transaction, no save.
            state = _ensure_adapter_state(store.load())
            expired = queue_core.expire_stale_pending(
                state,
                platform=PLATFORM,
                group_ttl=group_ttl,
                private_ttl=private_ttl,
                dry_run=True,
            )
        else:
            with store.transaction() as state:
                _ensure_adapter_state(state)
                expired = queue_core.expire_stale_pending(
                    state,
                    platform=PLATFORM,
                    group_ttl=group_ttl,
                    private_ttl=private_ttl,
                )
        print(json.dumps({
            "ok": True,
            "dry_run": dry_run,
            "expired": len(expired),
            "external_ids": [item["external_id"] for item in expired],
        }))
        return 0

    if args[:1] == ["--replied"] and len(args) in (3, 4):
        target = int(args[1])
        token = args[3] if len(args) == 4 else None
        with store.transaction() as state:
            _ensure_adapter_state(state)
            ok = queue_core.mark_replied(
                state, PLATFORM, target, args[2], token
            )
        print(json.dumps({"ok": ok}))
        return 0

    if args[:1] in (["--sending"], ["--send-failed"]) and len(args) in (2, 3):
        target = int(args[1])
        token = args[2] if len(args) == 3 else None
        with store.transaction() as state:
            _ensure_adapter_state(state)
            if args[0] == "--sending":
                ok = queue_core.mark_sending(state, PLATFORM, target, token)
            else:
                ok = queue_core.mark_send_failed(state, PLATFORM, target, token)
        print(json.dumps({"ok": ok}))
        return 0

    if args[:1] == ["--held"]:
        # --held: read-only list of the held items (the resume
        # turn's list). Held items live in the queue itself
        # (wake_after +24h); there is no held.jsonl any more.
        if len(args) != 1:
            print(json.dumps({
                "ok": False, "error": "usage: --held",
            }))
            return 2
        state = _ensure_adapter_state(store.load())
        held = queue_core.held_items(state, platform=PLATFORM)
        print(json.dumps({
            "ok": True,
            "held": [
                {
                    "update_id": int(item["external_id"]),
                    "chat_id": item.get("chat_id"),
                    "chat_type": item.get("chat_type"),
                    "sender_name": item.get("sender_name"),
                    "text": item.get("text"),
                    "status": item.get("status"),
                }
                for item in held
            ],
        }))
        return 0

    if args[:1] == ["--hold"]:
        # --hold UPDATE_ID CLAIM_TOKEN: hold one claimed item for
        # the resume turn — wake_after = now + 24h, in the queue
        # itself. The intake hook calls this per item of a held
        # batch, with the payload's claim token. The claim is left
        # intact (it recycles on the normal 600s heartbeat
        # horizon); the wake_after keeps the recycled item
        # unclaimable until --unhold or the 24h bound.
        if len(args) != 3:
            print(json.dumps({
                "ok": False,
                "error": "usage: --hold UPDATE_ID CLAIM_TOKEN",
            }))
            return 2
        try:
            target = int(args[1])
        except ValueError:
            print(json.dumps({"ok": False, "error": "invalid-update-id"}))
            return 2
        with store.transaction() as state:
            _ensure_adapter_state(state)
            ok = queue_core.hold_item(state, PLATFORM, target, args[2])
        print(json.dumps({"ok": ok}))
        return 0

    if args[:1] == ["--unhold"]:
        # --unhold UPDATE_ID [UPDATE_ID ...]: release held items
        # and re-claim them in the same transaction, so the intake
        # hook cannot claim them first. Prints a fresh claim
        # payload for the resume turn to answer with --reply —
        # never with a held token (the old token is dead once the
        # 600s heartbeat horizon passes). Items in another lane,
        # or in a busy lane, stay pending for the intake's next
        # poll.
        if len(args) < 2:
            print(json.dumps({
                "ok": False,
                "error": "usage: --unhold UPDATE_ID [UPDATE_ID ...]",
            }))
            return 2
        try:
            ids = [int(a) for a in args[1:]]
        except ValueError:
            print(json.dumps({"ok": False, "error": "invalid-update-id"}))
            return 2
        with store.transaction() as state:
            _ensure_adapter_state(state)
            unheld = []
            for external_id in ids:
                if queue_core.unhold_item(state, PLATFORM, external_id):
                    unheld.append(external_id)
            batch = queue_core.claim_ids(
                state,
                platform=PLATFORM,
                external_ids=unheld,
                batch_size=BATCH_MAX_MESSAGES,
            )
            if batch is not None:
                expired_ids = _expired_external_ids(state)
                claimed = {
                    "claim_token": batch["claim_token"],
                    "items": [
                        _payload_item(state, item, expired_ids)
                        for item in batch["items"]
                    ],
                }
            else:
                claimed = None
        print(json.dumps({
            "ok": True,
            "unheld": unheld,
            "claim_token": claimed["claim_token"] if claimed else None,
            "items": claimed["items"] if claimed else [],
        }))
        return 0

    if args and args[0].startswith("-"):
        # No command matched: refuse instead of falling through to
        # the poller. A mistyped flag used to silently start a
        # second getUpdates long-poll (Telegram 409s the race).
        print(json.dumps({
            "ok": False,
            "error": f"unknown command: {args[0]}",
        }))
        return 2

    payload = run_once(store, fetch=fetch, api_call=api_call)
    if payload:
        print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
