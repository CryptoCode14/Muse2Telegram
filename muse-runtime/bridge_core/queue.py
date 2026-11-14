"""Platform-neutral intake queue with batch-claim discipline.

The queue lives in the ``"queue"`` list of the shared state document
(see ``store.JsonStateStore``); every function here mutates that state
in place and is meant to be called inside ``store.transaction()`` so
the whole load -> modify -> save cycle is serialized.

Claim discipline (the rules that keep wake turns from double-answering):

- At most ONE batch is in flight PER conversation lane. While an
  item of a lane is ``claimed`` or ``sending`` and not stale,
  ``claim_batch`` will not claim that lane again — but other lanes
  (and other platforms) stay claimable, so one conversation's
  backlog never stalls the rest.
- Each claim issues one token shared by the batch; every transition
  (``mark_sending``, ``mark_replied``, ``mark_done``, ``release``,
  ``mark_send_failed``) only applies when the caller's token matches
  the item's current claim.
- Sending is an explicit lifecycle, not a side effect: a turn moves an
  item to ``sending`` (``mark_sending``) BEFORE calling the platform
  send API, then records the outcome — ``mark_replied`` on success,
  ``mark_send_failed`` only when the platform definitively rejected
  the send. ``sending`` items are NEVER recycled: the send may or may
  not have been delivered, so they wait for an operator to reconcile
  them rather than risking an automatic duplicate.
- An item with a recorded reply (``replied_at`` set) is NEVER recycled
  and can never be released or send-failed back to pending, however
  old its claim gets: the reply already went out, so re-waking it
  could only produce a duplicate. ``claim_batch`` never selects such
  an item either, even if it somehow sits in ``pending`` (legacy or
  hand-edited state); it is left for operator reconciliation.
- Closing a ``sending`` item that has no recorded reply requires
  reconciliation ``evidence`` in ``mark_done`` — a reference to how
  the send outcome was established (the reply found in platform
  history, or a definitive non-delivery confirmation). A ``reason``
  alone never closes such an item: the outcome is unknown, so the
  close must cite evidence, stored as ``done_evidence`` (kept
  separate from the optional ``done_reason`` context).
- Stale recycle applies only to unreplied ``claimed`` items whose
  heartbeat is stale (see ``HEARTBEAT_STALE_SECONDS``): a wake turn
  that died before sending must not block the queue forever, but a
  slow turn is never raced by a second claim. The heartbeat is the
  fast path — a dead turn's claim recycles about ten minutes after
  its last heartbeat, not after the full stale TTL. The TTL
  (default 1800s) survives only as a backstop for items whose
  heartbeat was never recorded. After a recycle the old batch token
  no longer matches, so the stale turn's late transition calls are
  all rejected.
- Claim lease (P1 latency, see ``sweep_claim_lease``): a claim
  whose turn never started (``turn_started_at`` None) is released
  by the watchdog sweeper once it is older than
  ``CLAIM_LEASE_SECONDS`` (600s), re-queued with an attempt
  counter, and re-woken by the intake's next poll — no re-wake
  before the lease expires. After ``CLAIM_LEASE_MAX_ATTEMPTS``
  (3) expiries the item is escalated: it stays pending with a
  24 h ``wake_after`` backoff and is reported once for an
  operator wake. The lease never touches a started turn, a warm
  claim (the 120s baton guards those), a ``sending`` item, or a
  replied item, and the claim paths never release on lease: only
  the sweeper does, so a claim alone cannot race a turn still
  starting up.
- Unclaimed ``pending`` items are not kept forever either: before any
  claim, a sweep expires old ordinary conversation instead of waking
  a turn for it — a turn answering hours-old room chatter only
  produces late, off-context replies. Expiry is deliberately
  narrow. Only an item whose intake classification (``item_class``)
  is explicitly "conversation" can expire, and only under its chat
  class TTL (group/supergroup default 600s; private items have no
  TTL by default — they carry the principal's own asks, and
  dropping one silently is worse than a late wake). Requests,
  corrections, follow-ups to open work and legacy records with no
  classification are never expired by the sweep. Neither is an item
  that was ever claimed (``ever_claimed``): once a turn has taken
  work, the work is preserved for reassessment, however it later
  returns to ``pending``. Claimed, sending and replied items are
  likewise never touched (see ``expire_stale_pending``).
- A batch is a single conversation lane: ``claim_batch`` takes items
  of one platform and one chat only (the lane of the oldest pending
  item for the requested platform), so a turn never mixes platforms
  or conversations.
- Throttling is a DELAY, never a drop. An item can carry a
  ``wake_after`` timestamp (epoch seconds): ``claim_batch`` skips
  items whose ``wake_after`` is still in the future, so a throttled
  item stays pending and is claimed by a later poll once its time
  comes, instead of being closed with no reply. The pending-expiry
  sweep likewise never expires an item while its ``wake_after`` is
  in the future: a delay must not turn into a silent drop.

Every item records ``received_at``, ``claimed_at``, ``wake_issued_at``
(the epoch second the intake issued the wake for the batch — see
``mark_wake_issued``), ``sending_at`` and ``replied_at`` so intake
latency can be measured end to end.
"""
from __future__ import annotations

import json
import math
import os
import secrets
import time

DEFAULT_BATCH_SIZE = 5
DEFAULT_STALE_TTL = 1800.0

#: Seconds without a heartbeat after which a claimed item counts as
#: abandoned. The batch agent refreshes its heartbeat while it
#: works (see ``heartbeat`` / ``heartbeat_batch``); a claim whose
#: heartbeat is older than this is recycled on the next claim, so a
#: turn that died mid-work blocks its lane for about ten minutes
#: instead of the full stale TTL. 600s: it covers the worst observed
#: wake latency — a turn that started ~8.5 minutes after its claim
#: was polled, whose first heartbeat hit the fence because the
#: 120s horizon had already recycled the claim, so the intake
#: re-woke a new turn and the cycle repeated without ever
#: replying. A live turn heartbeats at least once a minute (wake
#: prompt + automatic refresh on ack / claim-more), so it never
#: trips this.
HEARTBEAT_STALE_SECONDS = 600.0

#: Default age (seconds since intake) after which an unclaimed
#: group/supergroup item is expired by the claim-time sweep instead
#: of being woken. Group conversation moves on quickly; a reply to
#: an hours-old room item lands off-context.
DEFAULT_GROUP_PENDING_TTL = 600.0

#: done_reason recorded when an item is closed for exceeding its
#: age limit (the claim-time sweep or the operator purge).
EXPIRED_REASON = "expired"

#: Receipt TTL (seconds) for the ingest dedupe file (see
#: ``receipt_key`` / ``prune_receipts``): Hermes
#: ``update_admission.py`` persists completed update ids per bot
#: with a 24 h TTL, matching Telegram holding unconfirmed updates
#: for at most 24 h. A receipt older than this is pruned on ingest.
RECEIPT_TTL_SECONDS = 86400.0

#: Warm-baton timeout (seconds): a CLAIMED item that was claimed
#: under a warm hold (``warm_claimed``) but whose turn never sent
#: its first heartbeat/ack/claim-more (``turn_started_at`` is None)
#: is recycled once its heartbeat is older than this. The baton
#: must not be held by a dead warm agent: 120 s is long enough for
#: a live warm turn to heartbeat (it reads MEMORY and context
#: first), short enough that a dead one frees the lane fast. The
#: heartbeat horizon is 600 s and a turn heartbeats every ~60 s,
#: so the 120 s baton sits between the two: a warm turn that is
#: alive but slow to start is never recycled, a dead one is gone
#: in two minutes. See ``_recycle_stale``.
WARM_BATON_TIMEOUT_SECONDS = 120.0

#: Wake delay (seconds) applied to held items: when the bridge is
#: paused, a held item's ``wake_after`` is set to now + this, so it
#: stays unclaimable until the resume turn releases it — or, if
#: nobody ever resumes, it self-heals and becomes claimable again
#: on its own within a day. The queue itself is the store of
#: record for held items (there is no held.jsonl any more); see
#: ``hold_item`` / ``unhold_item`` / ``held_items``.
HELD_WAKE_AFTER_SECONDS = 86400.0

#: Default never-heartbeated recycle age (seconds) under the
#: two-phase heartbeat rule (see ``_recycle_stale``): a claimed
#: item whose turn never started (``turn_started_at`` is None) is
#: recycled once its claim is older than this, when the caller
#: passes ``active_heartbeat_stale``. Argon (corrected): the
#: original Argon figure was a 180 s re-wake for never-heartbeated
#: claims; live heartbeating turns keep the 600 s horizon. 180 s
#: proved too tight for post-gap recovery: on 2026-11-12 a worker
#: turn that was still starting up after a ~60 min runtime gap had
#: its batch recycled mid-startup, the batch was re-claimed under
#: a new token, and the first turn's reply fenced. 600 s gives a
#: slow-starting turn room to start; the cost is a genuinely dead
#: never-started claim waiting up to ten minutes before recycling.
NEVER_STARTED_RECYCLE_SECONDS = 600.0

#: Claim lease (seconds, P1 latency): a claim whose turn never
#: started (``turn_started_at`` is None) is released by the
#: watchdog sweeper once its claim is older than this. 600 s
#: matches the heartbeat horizon (``HEARTBEAT_STALE_SECONDS``),
#: deliberately: the lease was 120 s (the TOP of the
#: synthesis's 60-120 s range) when turns started in 40-95 s,
#: but live wake latency grew past it — wake p50 40 s, p90
#: 110 s, worst observed ~8.5 min — so turns arrived to a
#: dead token and cycled without ever replying (Westin msg
#: 209, 2026-11-13: 10 wakes / 10 recycles on one item).
#: 600 s covers the observed distribution with margin and
#: the ~8.5 min worst case that set the heartbeat horizon.
#: The lease applies ONLY to never-started, non-warm claims
#: (see ``sweep_claim_lease``); a started turn keeps the
#: 600 s heartbeat horizon, untouched. The sweeper enforces
#: the lease (it polls every 60 s), so a dead claim is
#: released 600-660 s after its claim; the intake's next poll
#: re-claims and re-wakes it. No re-wake happens before the
#: lease expires. The cost of the longer lease is slower
#: recycling of a genuinely dead never-started claim (up to
#: ten minutes instead of two); the cost of the short lease
#: was a live turn arriving to a dead token, which wastes
#: the whole turn and the reply.
CLAIM_LEASE_SECONDS = 600.0

#: Failed lease attempts (P1 latency): after this many
#: lease expiries for one claim episode, the sweeper stops
#: looping and escalates the item (see ``sweep_claim_lease``):
#: it stays pending with ``lease_escalated`` set and a 24 h
#: ``wake_after`` backoff, so the intake stops re-waking it,
#: and the sweep reports it once for an operator wake. 3
#: attempts is ~30 minutes of never-started before escalation
#: (3 x 600 s): enough for the runtime lane to drain a backup
#: — each attempt is now a real 10-minute window for a slow
#: turn to start, not a guaranteed failure — and short enough
#: that a genuinely dead claim is still surfaced the same
#: hour. K stays 3 with the longer lease: cutting it would
#: cut the number of chances a slow turn gets to start, and
#: the 24 h hold is unchanged. The counter resets only when
#: an escalated item is re-claimed after its backoff (a fresh
#: episode); ordinary recycles keep it, so the bound is per
#: episode, not per claim.
CLAIM_LEASE_MAX_ATTEMPTS = 3

#: Backoff (seconds) applied to a lease-escalated item: the
#: same 24 h as a held item, so the intake leaves it alone for
#: a day while an operator looks at it. The item is preserved
#: (pending, never dropped) and re-claimable after the backoff
#: or at once via ``clear_lease_escalation``.
LEASE_ESCALATION_BACKOFF_SECONDS = 86400.0

#: Age (seconds) of the intake's last successful poll past which
#: the intake is treated as stale by the watchdog sweep. The
#: intake long-polls on every tick (30 s long poll, ~31 s cycle
#: in live logs: median 31.2 s, p99 50.1 s, worst healthy gap
#: 132.7 s across ~10 h on 2026-11-12), so 120 s is ~4x the normal
#: cycle: one slow poll cannot trip it, but a genuinely stuck
#: intake is caught within about two minutes of its last poll.
#: 60 s would cry wolf (7 healthy gaps over 60 s in the same
#: window); waiting 15 min would leave the bridge down a quarter
#: of an hour for no benefit.
INTAKE_STALE_SECONDS = 120.0

#: Minimum gap (seconds) between intake-stale wakes while the
#: intake stays stale: the watchdog must not wake an agent on
#: every sweep tick for the same stuck intake. One wake per stale
#: episode, then silence until the intake recovers (the marker is
#: cleared on a fresh poll) or ten minutes pass.
INTAKE_STALE_WAKE_COOLDOWN_SECONDS = 600.0

#: State key recording the last intake-stale wake (epoch seconds),
#: so the sweep can enforce the cooldown above.
LAST_INTAKE_STALE_WAKE_KEY = "last_intake_stale_wake"

#: Minimum gap (seconds) between bridge-health wakes while the
#: install stays unhealthy: same one-wake-per-episode pattern as
#: the intake-stale cooldown. A healthy sweep clears the episode.
HEALTH_WAKE_COOLDOWN_SECONDS = 600.0

#: State key recording the last bridge-health wake (epoch seconds).
LAST_HEALTH_WAKE_KEY = "last_health_wake"

#: Age (seconds) past which a ``sending`` item counts as stuck:
#: the send went out (or did not) over an hour ago and nobody
#: reconciled it. The watchdog sweep surfaces these for an
#: operator wake — it never touches them itself, because the send
#: outcome is unknown and only evidence may close one.
STUCK_SENDING_SECONDS = 3600.0

#: The intake hook definition filenames the health check verifies,
#: in order. The hook tool names a definition file after the hook
#: id, and installs use two ids: "telegram-bridge-intake" (this
#: install, and SETUP.md Step 10) and "telegram-bridge" (the older
#: id the 878afe0 health check assumed). Checking only one name
#: fails the health check forever on the other install — that is
#: what the first live watchdog dry run hit on 2026-11-12, so the
#: check accepts either file, first match wins.
INTAKE_DEFINITION_FILENAMES = (
    "telegram-bridge-intake.json",
    "telegram-bridge.json",
)

#: Intake classifications stored on the queue record as
#: ``item_class``. Only CLASS_CONVERSATION ("ordinary conversation")
#: is eligible for pending expiry; CLASS_REQUEST marks work that
#: must be preserved — messages addressed to anyone, commands,
#: replies and other follow-ups, questions and asks, corrections
#: and cancellations, and anything from the owner. Classification
#: itself is the adapter's job (see the Telegram adapter's
#: ``classify_message`` for the protection signals it checks);
#: the queue only consumes the result. A record with no
#: classification (legacy state, or an adapter that does not
#: classify) is treated as protected, never as conversation:
#: chat type is not request classification, and when the class
#: is unclear, not dropping the item is the safe side.
CLASS_CONVERSATION = "conversation"
CLASS_REQUEST = "request"

#: Chat types whose unclaimed items expire under ``group_ttl``.
#: Private (owner DM) items are deliberately NOT in this set: they
#: have no default TTL (see ``pending_ttl``).
GROUP_CHAT_TYPES = ("group", "supergroup")

PENDING = "pending"
CLAIMED = "claimed"
SENDING = "sending"
DONE = "done"

#: Statuses that keep a batch in flight (block a further claim on
#: the same conversation lane).
IN_FLIGHT = (CLAIMED, SENDING)


def ensure_state(state: dict) -> dict:
    state.setdefault("queue", [])
    # Ingest receipts (Hermes update_admission.py: completed ids
    # persisted per bot, 24 h TTL) and lane-scoped warm leases
    # (v2 emulation of the Hermes/OpenClaw warm pattern, our own
    # design — not found in either repo; see inventory §2).
    state.setdefault("receipts", {})
    state.setdefault("warm_leases", {})
    return state


def receipt_key(bot_id, update_id) -> str:
    """Dedupe key for one update: ``"<bot_id>:<update_id>"``.

    Hermes ``update_admission.py`` keys its claims/receipts
    ``{bot_id}:{update_id}``; the same key is used for the
    ``state["receipts"]`` dict the Telegram adapter maintains.
    """
    return f"{bot_id}:{update_id}"


def prune_receipts(state: dict, now: float | None = None) -> int:
    """Drop receipt entries older than RECEIPT_TTL_SECONDS (24 h).

    Returns how many were pruned. Entries without a parseable
    ``received_at`` are kept: dropping a receipt whose age cannot
    be established could re-admit an already-answered update.
    """
    ensure_state(state)
    now = _now(now)
    receipts = state.get("receipts") or {}
    stale = [
        key
        for key, entry in receipts.items()
        if isinstance(entry, dict)
        and isinstance(entry.get("received_at"), (int, float))
        and now - float(entry["received_at"]) > RECEIPT_TTL_SECONDS
    ]
    for key in stale:
        receipts.pop(key, None)
    return len(stale)


def warm_lease_active(state: dict, chat_id, now: float | None = None) -> bool:
    """True when the lane holds a live warm lease.

    Expired leases are ignored and pruned on sight, so a dead
    warm hold never blocks its lane past its expiry.
    """
    now = _now(now)
    leases = state.get("warm_leases") or {}
    lease = leases.get(str(chat_id))
    if not isinstance(lease, dict):
        return False
    expires_at = lease.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        return False
    if now >= float(expires_at):
        leases.pop(str(chat_id), None)
        return False
    return True


def set_warm_lease(
    state: dict, chat_id, token, tier, seconds, now: float | None = None
) -> dict:
    """Register a lane-scoped warm lease (see module docstring in
    telegram.py ``_warm_hold``). ``seconds`` is the hold length;
    ``tier`` records which tier the holding turn booted as."""
    ensure_state(state)
    now = _now(now)
    lease = {
        "expires_at": now + float(seconds),
        "token": token,
        "tier": tier,
    }
    state["warm_leases"][str(chat_id)] = lease
    return lease


def clear_warm_lease(state: dict, chat_id) -> bool:
    """Drop the lane's warm lease, if any. Returns True when one
    existed."""
    leases = state.get("warm_leases") or {}
    return leases.pop(str(chat_id), None) is not None


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def _key(platform: str, external_id) -> tuple[str, str]:
    return (platform, str(external_id))


def find_item(state: dict, platform: str, external_id) -> dict | None:
    key = _key(platform, external_id)
    for item in state.get("queue", []):
        if _key(item["platform"], item["external_id"]) == key:
            return item
    return None


def enqueue(
    state: dict,
    *,
    platform: str,
    external_id,
    chat_id=None,
    chat_type: str | None = None,
    sender_id=None,
    sender_name: str | None = None,
    is_bot: bool = False,
    text: str = "",
    date=None,
    item_class: str | None = None,
    message_id=None,
    source: str | None = None,
    is_reply: bool = False,
    wake_after: float | None = None,
    now: float | None = None,
) -> dict | None:
    """Append an item unless (platform, external_id) is already known.

    Returns the new item, or ``None`` when it was a duplicate delivery.

    ``item_class`` is the adapter's intake classification
    (CLASS_CONVERSATION or CLASS_REQUEST); it decides pending-expiry
    eligibility and nothing else. ``ever_claimed`` starts False and
    is set permanently at the first claim: it is the record that
    this work was already taken once, which excludes it from the
    expiry sweep forever (see ``expire_stale_pending``).

    ``message_id`` is the platform message id (for Telegram, the
    message the reply threads under). A button tap has no user
    message to thread under, so its item carries ``message_id``
    None and ``source`` "callback_query" — the reply to it must be
    standalone, never threaded under the bot's own message.
    ``wake_after`` is an epoch timestamp: the item is not claimable
    before it (see the module docstring). Both default to None.
    """
    ensure_state(state)
    if find_item(state, platform, external_id) is not None:
        return None
    item = {
        "platform": platform,
        "external_id": str(external_id),
        "chat_id": chat_id,
        "chat_type": chat_type,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "is_bot": bool(is_bot),
        "text": text,
        "date": date,
        "item_class": item_class,
        "message_id": message_id,
        "source": source,
        # Whether the message is a reply to an earlier message. The
        # tier classifier uses it: a reply is a follow-up, so it
        # needs context and is never a quick greeting.
        "is_reply": bool(is_reply),
        "wake_after": wake_after,
        "ever_claimed": False,
        "received_at": _now(now),
        "status": PENDING,
        "claimed_at": None,
        "claim_token": None,
        # Liveness proof for the claim (see HEARTBEAT_STALE_SECONDS):
        # set at claim time, refreshed by the owning turn while it
        # works. A claimed item whose heartbeat goes stale is
        # recycled fast; an item that never recorded one falls back
        # to the claimed_at TTL. None until the first claim.
        "heartbeat_at": None,
        # Two-phase heartbeat (Argon, corrected — see
        # ``_recycle_stale``): None until the turn's first explicit
        # heartbeat/ack/claim-more proves it actually started. A
        # claim alone does not start the turn.
        "turn_started_at": None,
        # Wake-issue stamp (P0 latency instrument): the epoch second
        # the intake issued the wake for this claim (see
        # ``mark_wake_issued``). None until the first wake; cleared
        # when the claim recycles, so it always belongs to the live
        # claim. ``wake_count`` counts every wake issued for the
        # item across claims (a re-wake after a recycle counts
        # again); ``recycle_count`` counts stale-claim recycles.
        # Both counters survive a recycle — they are the recovery
        # history ``--latency-report`` reads.
        "wake_issued_at": None,
        "wake_count": 0,
        "recycle_count": 0,
        # Claim-lease attempts (P1 latency, see
        # ``sweep_claim_lease``): how many times this claim
        # episode has been released for never starting. Reset
        # only when an escalated item is re-claimed after its
        # backoff (a fresh episode); ordinary recycles keep the
        # count, so the K-attempt bound is per episode.
        "lease_attempts": 0,
        # True once the sweeper escalated the item (K lease
        # expiries): it stays pending with a 24 h wake_after
        # backoff until an operator clears it (see
        # ``clear_lease_escalation``) or the backoff passes.
        "lease_escalated": False,
        # True while the item was claimed by a warm hold (see
        # telegram._warm_hold): the baton guard recycles such a
        # claim fast when its turn never starts.
        "warm_claimed": False,
        # Dispatch dedupe key (OpenClaw message-dispatch-dedupe):
        # "<platform>:<chat_id>:<update_id>", stable across
        # re-delivery of the same update.
        "dispatch_key": f"{platform}:{chat_id}:{external_id}",
        "sending_at": None,
        "replied_at": None,
        "reply_message_id": None,
        # Acknowledgement for long work (see telegram.send_ack): a
        # short "looking into this" message sent before the full
        # reply, so the owner knows the turn is working. Recorded
        # once; a second ack for the same item is refused. The item
        # stays claimed after an ack — the full reply still closes it.
        "ack_sent_at": None,
        "ack_message_id": None,
        "done_at": None,
    }
    state["queue"].append(item)
    return item


def _is_recyclable(
    item: dict,
    stale_ttl: float,
    now: float,
    heartbeat_stale: float = HEARTBEAT_STALE_SECONDS,
    active_heartbeat_stale: float | None = None,
    warm_baton_timeout: float = WARM_BATON_TIMEOUT_SECONDS,
) -> bool:
    """True when a CLAIMED, unreplied item's claim is dead.

    Rules, in order (only CLAIMED, unreplied items with a
    ``claimed_at`` are candidates at all; SENDING and replied
    items are never recyclable):

    1. Warm baton guard (v2): a claim held under a warm hold
       (``warm_claimed``) whose turn never started
       (``turn_started_at`` is None) is recycled once its
       heartbeat is older than ``warm_baton_timeout`` (120 s).
       ``claimed_at`` alone never triggers this — only the
       heartbeat age, which is the baton proof of life.
    2. Two-phase heartbeat (Argon, corrected) when
       ``active_heartbeat_stale`` is passed (e.g. 600 s): a turn
       that started (``turn_started_at`` set) is recycled once
       its heartbeat is older than that horizon; a turn that
       never started is recycled once its claim is older than
       the same horizon. A live turn that keeps heartbeating is
       never recycled, however long it runs.
    3. Legacy rule when ``active_heartbeat_stale`` is None: a
       recorded heartbeat decides alone (stale past
       ``heartbeat_stale``, default 600 s); with no heartbeat
       ever recorded, the ``claimed_at`` TTL is the backstop.
    """
    if (
        item["status"] != CLAIMED
        or item["replied_at"] is not None
        or item["claimed_at"] is None
    ):
        return False
    heartbeat_at = item.get("heartbeat_at")
    if (
        item.get("warm_claimed")
        and item.get("turn_started_at") is None
        and heartbeat_at is not None
        and now - heartbeat_at > warm_baton_timeout
    ):
        return True
    if active_heartbeat_stale is not None:
        if item.get("turn_started_at") is not None:
            if heartbeat_at is None:
                return now - item["claimed_at"] > active_heartbeat_stale
            return now - heartbeat_at > active_heartbeat_stale
        return now - item["claimed_at"] > active_heartbeat_stale
    if heartbeat_at is not None:
        return now - heartbeat_at > heartbeat_stale
    return now - item["claimed_at"] > stale_ttl


def stale_claims(
    state: dict,
    stale_ttl: float = DEFAULT_STALE_TTL,
    now: float | None = None,
    heartbeat_stale: float = HEARTBEAT_STALE_SECONDS,
    active_heartbeat_stale: float | None = None,
    warm_baton_timeout: float = WARM_BATON_TIMEOUT_SECONDS,
) -> list:
    """Claimed items ``_recycle_stale`` would recycle right now,
    without mutating anything. The fast-claim check uses this to
    tell a recyclable dead claim from a live one."""
    now = _now(now)
    return [
        item
        for item in state.get("queue", [])
        if _is_recyclable(
            item,
            stale_ttl,
            now,
            heartbeat_stale=heartbeat_stale,
            active_heartbeat_stale=active_heartbeat_stale,
            warm_baton_timeout=warm_baton_timeout,
        )
    ]


def _recycle_stale(
    state: dict,
    stale_ttl: float,
    now: float,
    heartbeat_stale: float = HEARTBEAT_STALE_SECONDS,
    active_heartbeat_stale: float | None = None,
    warm_baton_timeout: float = WARM_BATON_TIMEOUT_SECONDS,
) -> list:
    # Only CLAIMED items are candidates. SENDING items are excluded by
    # construction: their send outcome is unknown, so they must never
    # be recycled into a possible duplicate send. Replied items are
    # excluded too — the reply already went out.
    #
    # The staleness decision lives in _is_recyclable (baton guard,
    # then the two-phase rule when active_heartbeat_stale is passed,
    # else the legacy heartbeat/TTL rule). Returns the recycled items.
    recycled = []
    for item in state["queue"]:
        if not _is_recyclable(
            item,
            stale_ttl,
            now,
            heartbeat_stale=heartbeat_stale,
            active_heartbeat_stale=active_heartbeat_stale,
            warm_baton_timeout=warm_baton_timeout,
        ):
            continue
        item["status"] = PENDING
        item["claimed_at"] = None
        item["claim_token"] = None
        item["heartbeat_at"] = None
        # A recycled claim is a fresh start when re-claimed: the
        # next turn must prove it started again, and the warm
        # baton flag belongs to the dead claim, not the next one.
        item["turn_started_at"] = None
        item["warm_claimed"] = False
        # The dead claim's wake stamp dies with it; the next claim
        # is stamped by its own wake. The counters survive: the
        # recycle itself is recovery history (see mark_wake_issued).
        item["wake_issued_at"] = None
        item["recycle_count"] = item.get("recycle_count", 0) + 1
        # A tentative ack reservation dies with the claim: the ack
        # never went out (no message id was recorded), so the next
        # turn may ack again. A completed ack (message id recorded)
        # survives the recycle — the ack went out, and re-acking
        # would send it twice.
        if item.get("ack_message_id") is None:
            item["ack_sent_at"] = None
        # ever_claimed deliberately survives the recycle: the
        # claim history is what keeps a recycled item out of the
        # first-pending expiry sweep on every later poll.
        recycled.append(item)
    return recycled


def sweep_claim_lease(
    state: dict,
    lease_seconds: float = CLAIM_LEASE_SECONDS,
    max_attempts: int = CLAIM_LEASE_MAX_ATTEMPTS,
    now: float | None = None,
) -> dict:
    """Release never-started claims past the claim lease (P1).

    The sweeper's release (the watchdog sweep calls this; the
    claim paths deliberately do NOT — a claim alone must never
    race a turn that is still starting up, and the 600 s
    heartbeat horizon in ``_recycle_stale`` stays the claim
    path's only recycle). Candidates are CLAIMED, unreplied
    items whose turn never started (``turn_started_at`` is
    None), that are not warm claims (the 120 s warm baton
    guards those), and whose claim is older than
    ``lease_seconds``. A started turn is never lease-released,
    however long it runs: only its heartbeat decides that.

    A released item returns to pending (same field clearing
    as ``_recycle_stale``, ``recycle_count`` + 1) with
    ``lease_attempts`` + 1, so the intake's next poll
    re-claims and re-wakes it — no re-wake happens before the
    lease expires. Once ``lease_attempts`` reaches
    ``max_attempts`` the item is ESCALATED instead: it stays
    pending with ``lease_escalated`` set and ``wake_after``
    now + ``LEASE_ESCALATION_BACKOFF_SECONDS`` (24 h), so the
    intake stops re-waking it, and the sweep reports it once
    for an operator wake. The item is never dropped: it is
    preserved for reassessment, re-claimable after the
    backoff or at once via ``clear_lease_escalation``.

    Returns ``{"expired": [...], "escalated": [...]}`` (the
    item dicts, in queue order). Items are in at most one
    list. An item with a completed ack (``ack_message_id``
    set) keeps it: the ack card went out, and the sweeper
    updates its text (see telegram.refresh_ack_cards).
    """
    ensure_state(state)
    now = _now(now)
    expired = []
    escalated = []
    for item in state["queue"]:
        if (
            item["status"] != CLAIMED
            or item["replied_at"] is not None
            or item["claimed_at"] is None
            or item.get("turn_started_at") is not None
            or item.get("warm_claimed")
            or now - item["claimed_at"] <= lease_seconds
        ):
            continue
        attempts = item.get("lease_attempts", 0) + 1
        item["lease_attempts"] = attempts
        item["status"] = PENDING
        item["claimed_at"] = None
        item["claim_token"] = None
        item["heartbeat_at"] = None
        item["turn_started_at"] = None
        item["warm_claimed"] = False
        item["wake_issued_at"] = None
        item["recycle_count"] = item.get("recycle_count", 0) + 1
        if item.get("ack_message_id") is None:
            item["ack_sent_at"] = None
        if attempts >= max_attempts:
            item["lease_escalated"] = True
            item["wake_after"] = now + LEASE_ESCALATION_BACKOFF_SECONDS
            escalated.append(item)
        else:
            expired.append(item)
    return {"expired": expired, "escalated": escalated}


def clear_lease_escalation(state: dict, platform: str, external_id) -> bool:
    """Clear a lease escalation: the operator's re-queue path.

    Only a pending, escalated item can be cleared. Clears
    ``lease_escalated``, the backoff ``wake_after`` (a
    throttle ``wake_after`` is never cleared by mistake —
    only an escalated item carries one set by the lease),
    and resets ``lease_attempts`` to 0, so the next claim
    starts a fresh episode. The item stays pending; the
    intake's next poll re-claims and re-wakes it. Returns
    True when an escalated item was cleared.
    """
    ensure_state(state)
    item = find_item(state, platform, external_id)
    if item is None:
        return False
    if item["status"] != PENDING or not item.get("lease_escalated"):
        return False
    item["lease_escalated"] = False
    item["wake_after"] = None
    item["lease_attempts"] = 0
    return True


def pending_ttl(
    item: dict,
    *,
    group_ttl: float | None = DEFAULT_GROUP_PENDING_TTL,
    private_ttl: float | None = None,
) -> float | None:
    """The expiry TTL (seconds) for an unclaimed item, or None.

    Eligibility starts with the intake classification: only an item
    explicitly classified CLASS_CONVERSATION can expire at all.
    Requests and unclassified/legacy records return None whatever
    their chat type — chat type is not request classification.

    For a conversation item, group/supergroup chats expire under
    ``group_ttl`` and private chats under ``private_ttl`` — None
    (never expire) unless an operator explicitly opted the owner's
    DMs into expiry. Items with any other (or no) chat type never
    expire. A negative TTL disables the limit it tunes, matching
    the adapter layer's documented convention.
    """
    if item.get("item_class") != CLASS_CONVERSATION:
        return None
    chat_type = item.get("chat_type")
    if chat_type in GROUP_CHAT_TYPES:
        ttl = group_ttl
    elif chat_type == "private":
        ttl = private_ttl
    else:
        return None
    if ttl is not None and ttl < 0:
        return None
    return ttl


def _validate_ttl(name: str, ttl: float | None) -> None:
    """Reject a non-finite TTL before anything is mutated.

    A NaN TTL compares false against every age, which would make
    ``age > ttl`` true for all eligible items and expire the whole
    class at once; an infinite TTL silently disables expiry. Both
    are configuration errors, not limits — fail loudly instead.
    Finite values, including the negative disable convention, pass.
    """
    if ttl is not None and not math.isfinite(ttl):
        raise ValueError(f"{name} must be finite or None, got {ttl!r}")


def expire_stale_pending(
    state: dict,
    *,
    platform: str | None = None,
    group_ttl: float | None = DEFAULT_GROUP_PENDING_TTL,
    private_ttl: float | None = None,
    dry_run: bool = False,
    now: float | None = None,
) -> list:
    """Close unclaimed pending items older than their class TTL.

    An expired item becomes ``done`` with ``done_reason`` "expired":
    it is never woken and never replied to, but its record (and the
    ``counts`` totals) stays in state for audit — nothing vanishes
    silently.

    Only items still in ``pending`` with no recorded reply are
    eligible, and only with an explicit intake classification of
    CLASS_CONVERSATION under their chat class TTL (see
    ``pending_ttl``). Requests and unclassified/legacy records are
    never expired. Neither is an item that was ever claimed:
    ``ever_claimed`` survives the stale-claim recycle, so work a
    turn already took is preserved for reassessment instead of
    being swept on a later poll. Claimed, ``sending`` and replied
    items are NEVER expired either: a replied item's answer already
    went out, and a ``sending`` item's outcome may be unknown, so
    expiring either could resurrect it for a duplicate send. A
    ``pending`` item with a recorded reply (legacy or hand-edited
    state) is likewise left for operator reconciliation, exactly as
    ``claim_batch`` leaves it unclaimable.

    Non-finite TTLs (NaN, infinities) raise ValueError before
    anything is mutated.

    With ``dry_run`` the eligible items are returned but nothing is
    mutated. Returns the expired (or, for a dry run, expirable)
    items, in queue order.
    """
    _validate_ttl("group_ttl", group_ttl)
    _validate_ttl("private_ttl", private_ttl)
    ensure_state(state)
    now = _now(now)
    expired = []
    for item in state["queue"]:
        if item["status"] != PENDING or item["replied_at"] is not None:
            continue
        if item.get("ever_claimed"):
            continue
        # A throttled item is never expired while its wake_after is
        # still in the future: the delay must not turn into a drop.
        wake_after = item.get("wake_after")
        if wake_after is not None and wake_after > now:
            continue
        if platform is not None and item["platform"] != platform:
            continue
        ttl = pending_ttl(item, group_ttl=group_ttl, private_ttl=private_ttl)
        if ttl is None or now - item["received_at"] <= ttl:
            continue
        expired.append(item)
        if not dry_run:
            item["status"] = DONE
            item["done_at"] = now
            item["claim_token"] = None
            item["done_reason"] = EXPIRED_REASON
    return expired


def claim_batch(
    state: dict,
    *,
    platform: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    stale_ttl: float = DEFAULT_STALE_TTL,
    heartbeat_stale: float = HEARTBEAT_STALE_SECONDS,
    active_heartbeat_stale: float | None = None,
    warm_baton_timeout: float = WARM_BATON_TIMEOUT_SECONDS,
    group_pending_ttl: float | None = DEFAULT_GROUP_PENDING_TTL,
    private_pending_ttl: float | None = None,
    now: float | None = None,
) -> dict | None:
    """Claim up to ``batch_size`` pending items as one batch.

    The batch is a single conversation lane: when ``platform`` is
    given, only that platform's items are eligible. Eligible items
    are considered in received order, and the batch takes the lane
    of the FIRST eligible item whose (platform, chat_id) lane has
    nothing in flight; up to ``batch_size`` items of that lane are
    claimed, in received order.

    The in-flight guard is scoped per lane: a lane is blocked only
    by its OWN in-flight batch (an item ``claimed`` or ``sending``
    in the same lane, not recycled as stale). Busy lanes are
    skipped during selection instead of blocking the whole claim,
    so a newer item in a free lane is claimed even when the oldest
    pending item sits in a busy lane.

    Items with a recorded reply (``replied_at`` set) are never
    eligible, even if one somehow sits in ``pending``: its reply
    already went out, so claiming it could only duplicate the
    answer. It is left for operator reconciliation.

    Before selection, unclaimed pending items past their class TTL
    are expired in the same call (see :func:`expire_stale_pending`),
    so an item the sweep closes is never claimed here either. The
    sweep runs BEFORE the stale-claim recycle, and it can never
    reach a recycled item at all: claiming sets ``ever_claimed``,
    which permanently excludes the item from first-pending expiry,
    so work recycled by this call (or any earlier one) and left
    behind by the batch limit stays claimable on later polls
    instead of expiring.

    Returns ``{"claim_token": str, "items": [item, ...]}`` or
    ``None`` when there is nothing eligible to claim or every
    eligible item's lane still has a batch in flight.
    """
    ensure_state(state)
    now = _now(now)
    expire_stale_pending(
        state,
        platform=platform,
        group_ttl=group_pending_ttl,
        private_ttl=private_pending_ttl,
        now=now,
    )
    _recycle_stale(
        state,
        stale_ttl,
        now,
        heartbeat_stale=heartbeat_stale,
        active_heartbeat_stale=active_heartbeat_stale,
        warm_baton_timeout=warm_baton_timeout,
    )
    pending = [
        item
        for item in state["queue"]
        if item["status"] == PENDING
        and item["replied_at"] is None
        and (platform is None or item["platform"] == platform)
        # Throttled items are not claimable before their wake_after:
        # they stay pending and are claimed by a later poll.
        and (
            item.get("wake_after") is None
            or item["wake_after"] <= now
        )
    ]
    pending.sort(key=lambda item: item["received_at"])
    if not pending:
        return None
    busy_lanes = {
        (item["platform"], item["chat_id"])
        for item in state["queue"]
        if item["status"] in IN_FLIGHT
    }
    lane = None
    for item in pending:
        candidate = (item["platform"], item["chat_id"])
        if candidate in busy_lanes:
            continue
        # A lane holding an active warm lease is busy too: the
        # warm agent owns that lane until its hold ends (see
        # warm_lease_active). Other lanes stay claimable.
        if warm_lease_active(state, item["chat_id"], now):
            continue
        lane = candidate
        break
    if lane is None:
        return None
    batch = [
        item
        for item in pending
        if (item["platform"], item["chat_id"]) == lane
    ][:batch_size]
    token = secrets.token_hex(8)
    for item in batch:
        item["status"] = CLAIMED
        item["claimed_at"] = now
        item["claim_token"] = token
        item["heartbeat_at"] = now
        item["ever_claimed"] = True
        # A claim alone does not start the turn (turn_started_at
        # stays None until the first heartbeat/ack/claim-more),
        # and a fresh claim is not a warm claim unless the
        # warm-hold caller marks it so afterwards. Likewise no
        # wake has been issued for the fresh claim yet
        # (wake_issued_at stays None until mark_wake_issued).
        item["turn_started_at"] = None
        item["warm_claimed"] = False
        # A re-claim after a lease escalation starts a fresh
        # episode: the backoff is over and the attempt counter
        # resets. Ordinary re-claims keep the counter.
        if item.get("lease_escalated"):
            item["lease_attempts"] = 0
            item["lease_escalated"] = False
        item["wake_issued_at"] = None
    return {"claim_token": token, "items": batch}


def claim_lane_batch(
    state: dict,
    *,
    chat_id,
    platform: str = "telegram",
    batch_size: int = DEFAULT_BATCH_SIZE,
    stale_ttl: float = DEFAULT_STALE_TTL,
    heartbeat_stale: float = HEARTBEAT_STALE_SECONDS,
    active_heartbeat_stale: float | None = None,
    warm_baton_timeout: float = WARM_BATON_TIMEOUT_SECONDS,
    now: float | None = None,
) -> dict | None:
    """Claim up to ``batch_size`` pending items of ONE lane only.

    The warm-hold path: a warm agent that just saw a follow-up in
    its own lane claims exactly that lane, in received order, and
    only when the lane has nothing in flight and holds no active
    warm lease. A lane with an active lease owned by a different
    token is busy; the warm-hold caller clears its OWN lease
    before claiming, so by the time this runs the lease is gone.
    Other lanes are never touched, whatever their state.

    Newly claimed items carry ``warm_claimed=False`` by default;
    the warm-hold caller sets it True afterwards, in the same
    transaction, so the baton guard can tell a warm claim from an
    ordinary one. Like ``claim_batch``, a claim alone does not
    start the turn (``turn_started_at`` stays None).

    Returns ``{"claim_token": token, "items": [...]}`` or None.
    """
    ensure_state(state)
    now = _now(now)
    _recycle_stale(
        state,
        stale_ttl,
        now,
        heartbeat_stale=heartbeat_stale,
        active_heartbeat_stale=active_heartbeat_stale,
        warm_baton_timeout=warm_baton_timeout,
    )
    lane = (platform, chat_id)
    if any(
        (item["platform"], item["chat_id"]) == lane
        and item["status"] in IN_FLIGHT
        for item in state["queue"]
    ):
        return None
    if warm_lease_active(state, chat_id, now):
        return None
    pending = [
        item
        for item in state["queue"]
        if item["status"] == PENDING
        and item["replied_at"] is None
        and (item["platform"], item["chat_id"]) == lane
        and (item.get("wake_after") is None or item["wake_after"] <= now)
    ]
    pending.sort(key=lambda item: item["received_at"])
    batch = pending[:batch_size]
    if not batch:
        return None
    token = secrets.token_hex(8)
    for item in batch:
        item["status"] = CLAIMED
        item["claimed_at"] = now
        item["claim_token"] = token
        item["heartbeat_at"] = now
        item["ever_claimed"] = True
        item["turn_started_at"] = None
        item["warm_claimed"] = False
        # A re-claim after a lease escalation starts a fresh
        # episode: the backoff is over and the attempt counter
        # resets. Ordinary re-claims keep the counter.
        if item.get("lease_escalated"):
            item["lease_attempts"] = 0
            item["lease_escalated"] = False
        # No wake issued for the fresh claim yet (see claim_batch).
        item["wake_issued_at"] = None
    return {"claim_token": token, "items": batch}


def claim_more(
    state: dict,
    *,
    platform: str,
    chat_id,
    token,
    batch_size: int = DEFAULT_BATCH_SIZE,
    now: float | None = None,
) -> dict | None:
    """Claim further pending items of one lane into an existing turn.

    The turn that owns the lane calls this just before sending, so
    messages that arrived while it was thinking join the same reply
    instead of becoming a second ~40s turn. Only the token that
    already owns an in-flight batch in this (platform, chat_id)
    lane may claim more: a caller with no batch in the lane, or a
    stale token, gets None and changes nothing. Newly claimed items
    carry the same token, so the turn closes them with the single
    batch send. Pending items with a recorded reply, or whose
    ``wake_after`` is still in the future, are never claimed.

    Returns ``{"claim_token": token, "items": [...]}`` when at least
    one item was claimed, else None.
    """
    ensure_state(state)
    now = _now(now)
    lane = (platform, chat_id)
    owns_lane = any(
        (item["platform"], item["chat_id"]) == lane
        and item["status"] in IN_FLIGHT
        and item.get("claim_token") == token
        for item in state["queue"]
    )
    if not owns_lane:
        return None
    pending = [
        item
        for item in state["queue"]
        if item["status"] == PENDING
        and item["replied_at"] is None
        and (item["platform"], item["chat_id"]) == lane
        and (item.get("wake_after") is None or item["wake_after"] <= now)
    ]
    pending.sort(key=lambda item: item["received_at"])
    batch = pending[:batch_size]
    if not batch:
        return None
    for item in batch:
        item["status"] = CLAIMED
        item["claimed_at"] = now
        item["claim_token"] = token
        item["heartbeat_at"] = now
        item["ever_claimed"] = True
        # Claiming more proves the turn started: the new items join
        # a live turn, so their turn_started_at is now, and so is
        # the existing batch's (a turn that claims more is alive).
        item["turn_started_at"] = now
    for item in state["queue"]:
        if (
            item["status"] == CLAIMED
            and item.get("claim_token") == token
            and item.get("turn_started_at") is None
        ):
            item["turn_started_at"] = now
    return {"claim_token": token, "items": batch}


def hold_item(
    state: dict,
    platform: str,
    external_id,
    token=None,
    *,
    now: float | None = None,
) -> bool:
    """Hold one item for the resume turn, in the queue itself.

    Sets ``wake_after`` to now + ``HELD_WAKE_AFTER_SECONDS`` (24 h)
    and marks the item ``held``. The queue is the store of record:
    there is no held.jsonl any more, and dedupe by update id falls
    out of the queue / receipts (an update is enqueued once, and
    its receipt stops a re-delivery from re-enqueueing it), so a
    held update can never be held twice.

    The claim is deliberately left intact: the item stays claimed
    under its token until the normal 600 s heartbeat horizon
    recycles it to pending, and the ``wake_after`` keeps the
    recycled item unclaimable until the resume (or the 24 h bound,
    when it self-heals). The old token is dead by design once that
    horizon passes — the resume turn must re-claim (``--unhold``)
    and never ``--reply`` with a held token.

    Only pending or claimed items can be held, and never one with
    a recorded reply. A claimed item requires its token to match.
    Returns True when the item was held.
    """
    ensure_state(state)
    item = find_item(state, platform, external_id)
    if item is None:
        return False
    if item["status"] not in (PENDING, CLAIMED):
        return False
    if item["replied_at"] is not None:
        return False
    if item["status"] == CLAIMED:
        if token is None or item.get("claim_token") != token:
            return False
    item["wake_after"] = _now(now) + HELD_WAKE_AFTER_SECONDS
    item["held"] = True
    return True


def unhold_item(state: dict, platform: str, external_id) -> bool:
    """Release one held item: clear its ``wake_after`` and its
    held flag, so the next claim takes it normally. The item keeps
    whatever status it had (usually pending after its claim
    recycled); the caller re-claims it (see ``claim_ids``). Only
    an item actually marked held can be unheld — clearing a
    throttle ``wake_after`` by mistake would defeat the delay.
    Returns True when a held item was released.
    """
    ensure_state(state)
    item = find_item(state, platform, external_id)
    if item is None or not item.get("held"):
        return False
    item["wake_after"] = None
    item.pop("held", None)
    return True


def held_items(state: dict, platform: str | None = None) -> list:
    """Held items, in queue order (the resume turn's --held list).

    An item counts as held only while its ``held`` flag is set;
    a throttle ``wake_after`` alone does not make an item held.
    """
    ensure_state(state)
    return [
        item
        for item in state.get("queue", [])
        if item.get("held")
        and (platform is None or item["platform"] == platform)
    ]


def claim_ids(
    state: dict,
    *,
    platform: str,
    external_ids,
    batch_size: int = DEFAULT_BATCH_SIZE,
    now: float | None = None,
) -> dict | None:
    """Claim specific pending items by external id, under one token.

    The resume path: after ``--unhold`` clears the holds, the
    named items are re-claimed here — in the same transaction as
    the clear, so the intake hook cannot claim them first. Only
    pending, unreplied items are claimable, and only in one lane:
    the batch takes the lane of the first claimable named item
    (like ``claim_batch`` takes the first eligible lane), and any
    named item in another lane, or in a lane with a batch already
    in flight, is left pending for the intake's next poll. A
    one-lane batch is what ``--reply`` needs — a split batch
    would fail its fence.

    Returns ``{"claim_token": token, "items": [...]}`` or None
    when nothing named was claimable.
    """
    ensure_state(state)
    now = _now(now)
    wanted = [str(eid) for eid in external_ids]
    candidates = [
        item
        for item in state.get("queue", [])
        if item["platform"] == platform
        and str(item["external_id"]) in wanted
        and item["status"] == PENDING
        and item["replied_at"] is None
        and (item.get("wake_after") is None or item["wake_after"] <= now)
    ]
    candidates.sort(key=lambda item: item["received_at"])
    if not candidates:
        return None
    busy_lanes = {
        (item["platform"], item["chat_id"])
        for item in state["queue"]
        if item["status"] in IN_FLIGHT
    }
    lane = None
    for item in candidates:
        candidate = (item["platform"], item["chat_id"])
        if candidate in busy_lanes:
            continue
        if warm_lease_active(state, item["chat_id"], now):
            continue
        lane = candidate
        break
    if lane is None:
        return None
    batch = [
        item
        for item in candidates
        if (item["platform"], item["chat_id"]) == lane
    ][:batch_size]
    if not batch:
        return None
    token = secrets.token_hex(8)
    for item in batch:
        item["status"] = CLAIMED
        item["claimed_at"] = now
        item["claim_token"] = token
        item["heartbeat_at"] = now
        item["ever_claimed"] = True
        item["turn_started_at"] = None
        item["warm_claimed"] = False
        # A re-claim after a lease escalation starts a fresh
        # episode: the backoff is over and the attempt counter
        # resets. Ordinary re-claims keep the counter.
        if item.get("lease_escalated"):
            item["lease_attempts"] = 0
            item["lease_escalated"] = False
        # No wake issued for the fresh claim yet (see claim_batch).
        item["wake_issued_at"] = None
    return {"claim_token": token, "items": batch}


def heartbeat(
    state: dict, platform: str, external_id, token, *, now: float | None = None
) -> bool:
    """Refresh one claimed item's heartbeat: proof its turn is alive.

    Only the token that owns the claim may heartbeat it, and only
    while the item is still ``claimed`` with no reply recorded. A
    stale token (recycled batch) or a replied / sending item gets
    False and changes nothing. The turn heartbeats while it works
    (wake prompt: first thing, and before any step that may take
    over a minute); the ack and claim-more paths refresh it too.
    """
    item = _owned_item(state, platform, external_id, token, (CLAIMED,))
    if item is None:
        return False
    if item["replied_at"] is not None:
        return False
    # An explicit heartbeat is proof the turn started: record it
    # once, on the first heartbeat (two-phase rule, Argon
    # corrected — a claim alone does not start the turn).
    if item.get("turn_started_at") is None:
        item["turn_started_at"] = _now(now)
    item["heartbeat_at"] = _now(now)
    return True


def heartbeat_batch(state: dict, token, *, now: float | None = None) -> int:
    """Refresh the heartbeat of every claimed, unreplied item under
    one batch token. One call keeps a whole batch alive, so the turn
    heartbeats its batch with a single command instead of one call
    per item. Items under another token, replied items, and
    ``sending`` items are untouched. Returns how many items were
    refreshed (0 when the caller owns no live batch).
    """
    if token is None:
        return 0
    now = _now(now)
    refreshed = 0
    for item in state.get("queue", []):
        if (
            item["status"] == CLAIMED
            and item.get("claim_token") == token
            and item["replied_at"] is None
        ):
            # First heartbeat of the batch starts the turn (see
            # heartbeat()): a batch heartbeat is explicit proof
            # the turn is alive and working.
            if item.get("turn_started_at") is None:
                item["turn_started_at"] = now
            item["heartbeat_at"] = now
            refreshed += 1
    return refreshed


def mark_wake_issued(state: dict, token, *, now: float | None = None) -> int:
    """Stamp the wake-issue time on a claimed batch.

    The intake hook calls this immediately before issuing the wake
    for a claimed batch, so wake latency (wake issued -> turn
    started) becomes measurable: ``wake_issued_at`` is the epoch
    second the wake went out, and ``wake_count`` counts every wake
    issued for the item — a re-wake after a recycle counts again,
    so items with ``wake_count`` > 1 are the re-wakes
    ``--latency-report`` counts. Only CLAIMED, unreplied items
    under the caller's token are stamped: a stale token (recycled
    batch) stamps nothing, and items claimed later into the same
    turn (``claim_more``) carry no wake of their own — they joined
    a live turn, so they contribute no wake sample. Returns how
    many items were stamped (0 when the token owns no live batch).
    """
    if token is None:
        return 0
    now = _now(now)
    stamped = 0
    for item in state.get("queue", []):
        if (
            item["status"] == CLAIMED
            and item.get("claim_token") == token
            and item["replied_at"] is None
        ):
            item["wake_issued_at"] = now
            item["wake_count"] = item.get("wake_count", 0) + 1
            stamped += 1
    return stamped


def check_bridge_health(*, definitions_dir, bridge_home, send_cmd=None):
    """Verify the bridge install the intake depends on.

    Reads only; never repairs anything. Four checks, one per thing
    the intake needs to poll and the turn needs to reply:

    - ``intake_definition``: the intake hook definition at
      ``definitions_dir/telegram-bridge-intake.json`` (or the older
      ``telegram-bridge.json``) parses, is enabled,
      carries ``created_at_ms`` (the runtime stamps it when the
      hook is created through the hook tool — a definition copied
      from the shipped template has none and never runs), and
      names an intake script that exists.
    - ``checkout``: ``bridge_home`` holds ``bridge_core`` with the
      modules the CLI imports.
    - ``venv``: ``bridge_home/.venv/bin/python`` exists and is
      executable — the interpreter the intake runs under.
    - ``sender``: the send command exists and is executable; a
      turn that cannot send can poll but never reply.

    Returns ``{"ok": bool, "checks": {name: {"ok": bool,
    "detail": str}}}``. ``ok`` is False when any single check
    fails. The caller (the watchdog sweep) decides whether a
    failure wakes an agent; this function only reports.
    """
    checks = {}

    definition_path = None
    for candidate in INTAKE_DEFINITION_FILENAMES:
        path = os.path.join(
            os.path.expanduser(str(definitions_dir)), candidate
        )
        if os.path.isfile(path):
            definition_path = path
            break
    if definition_path is None:
        checks["intake_definition"] = {
            "ok": False,
            "detail": "definition file missing: looked for "
            + ", ".join(
                os.path.join(
                    os.path.expanduser(str(definitions_dir)), name
                )
                for name in INTAKE_DEFINITION_FILENAMES
            ),
        }
    else:
        try:
            with open(definition_path, encoding="utf-8") as handle:
                definition = json.load(handle)
        except (OSError, ValueError) as exc:
            checks["intake_definition"] = {
                "ok": False,
                "detail": f"definition file is not valid JSON: {exc}",
            }
        else:
            checks["intake_definition"] = _check_definition(definition)

    home = os.path.expanduser(str(bridge_home))
    if not os.path.isdir(home):
        checks["checkout"] = {
            "ok": False,
            "detail": f"checkout missing: {home}",
        }
        checks["venv"] = {
            "ok": False,
            "detail": f"checkout missing: {home}",
        }
    else:
        missing = [
            name
            for name in ("bridge_core/telegram.py", "bridge_core/queue.py")
            if not os.path.isfile(os.path.join(home, name))
        ]
        if missing:
            checks["checkout"] = {
                "ok": False,
                "detail": f"checkout missing files: {', '.join(missing)}",
            }
        else:
            checks["checkout"] = {"ok": True, "detail": "bridge_core present"}
        venv_python = os.path.join(home, ".venv", "bin", "python")
        if not os.path.isfile(venv_python) or not os.access(
            venv_python, os.X_OK
        ):
            checks["venv"] = {
                "ok": False,
                "detail": f"venv python missing or not executable: {venv_python}",
            }
        else:
            checks["venv"] = {"ok": True, "detail": "venv python present"}

    if not send_cmd:
        checks["sender"] = {
            "ok": False,
            "detail": "TELEGRAM_BRIDGE_SEND_CMD is not set",
        }
    else:
        sender_path = os.path.expanduser(str(send_cmd))
        if not os.path.isfile(sender_path) or not os.access(
            sender_path, os.X_OK
        ):
            checks["sender"] = {
                "ok": False,
                "detail": f"sender missing or not executable: {sender_path}",
            }
        else:
            checks["sender"] = {"ok": True, "detail": "sender present"}

    return {"ok": all(check["ok"] for check in checks.values()), "checks": checks}


def _check_definition(definition):
    """Check one parsed intake hook definition (see
    ``check_bridge_health``)."""
    if not isinstance(definition, dict):
        return {"ok": False, "detail": "definition is not an object"}
    created = definition.get("created_at_ms")
    if type(created) is not int:
        return {
            "ok": False,
            "detail": "definition has no created_at_ms: it was copied "
            "from the shipped template instead of created through the "
            "hook tool, so the runtime never runs it — recreate it "
            "through the hook tool",
        }
    if definition.get("enabled") is not True:
        return {"ok": False, "detail": "definition is disabled"}
    script_path = definition.get("script_path") or ""
    script = os.path.expanduser(str(script_path))
    if not script or not os.path.isfile(script):
        return {
            "ok": False,
            "detail": f"intake script missing: {script or script_path}",
        }
    poll = definition.get("poll_interval_secs")
    detail = "valid, enabled"
    if isinstance(poll, (int, float)):
        detail += f", poll {poll}s"
    return {"ok": True, "detail": detail}


def watchdog_sweep(
    state: dict,
    *,
    heartbeat_stale: float = HEARTBEAT_STALE_SECONDS,
    stale_ttl: float = DEFAULT_STALE_TTL,
    active_heartbeat_stale: float | None = None,
    warm_baton_timeout: float = WARM_BATON_TIMEOUT_SECONDS,
    health: dict | None = None,
    now: float | None = None,
) -> dict:
    """One watchdog pass: recycle dead claims, report queue health
    and intake liveness.

    The sweep runs the same stale-claim rule as ``claim_batch``
    (heartbeat first, claimed_at TTL as backstop for items that
    never recorded one) and counts what it found. It NEVER touches
    ``sending`` items (their send outcome is unknown — only an
    operator reconciles those), replied items, or claims whose
    heartbeat is still fresh. Recycled items return to ``pending``
    with their token cleared, so the intake's next poll re-claims
    and re-wakes them — the watchdog itself never sends anything
    and never wakes an agent for a recycled claim; waking is the
    intake's job once the lane is free again.

    Before that, the sweep runs the claim lease (see
    ``sweep_claim_lease``): never-started claims past
    ``CLAIM_LEASE_SECONDS`` are released the same way (or
    escalated at ``CLAIM_LEASE_MAX_ATTEMPTS``), and reported as
    ``lease_expired`` / ``lease_escalated`` alongside the
    merged ``recycled`` list. A started turn is never
    lease-released: only its heartbeat decides that.

    It also reports intake liveness from the state's
    ``last_successful_poll`` (set by the intake on every
    successful poll): ``intake_stale`` is True once the last poll
    is older than ``INTAKE_STALE_SECONDS``, and ``intake_wake``
    is True only on the first stale sweep of an episode (further
    sweeps stay silent for ``INTAKE_STALE_WAKE_COOLDOWN_SECONDS``,
    and a fresh poll clears the episode). ``intake_recovered``
    is True on the one sweep where a stale episode clears, so a
    repair is only counted once fresh output is actually seen.
    The sweep only reports: the hook script reads ``intake_wake``
    and wakes an agent. This catches a stuck intake while the
    runtime is alive; it cannot cover a dead runtime — if the
    runtime itself is down, this sweep never runs either. A state
    with no recorded poll yet (fresh install, intake never ran)
    reports no age and is never stale: there is nothing to judge
    yet.

    When the caller passes ``health`` (the report from
    ``check_bridge_health``), the sweep reports it the same way:
    ``health_ok`` is True when the install is healthy,
    ``health_wake`` is True only on the first unhealthy sweep of
    an episode (further sweeps stay silent for
    ``HEALTH_WAKE_COOLDOWN_SECONDS``, and a healthy sweep clears
    the episode), and ``health_recovered`` is True on the one
    sweep where an unhealthy episode clears. When ``health`` is
    None (the caller did not check), ``health_ok`` and ``health``
    are None and nothing wakes: an unchecked install is reported
    as unchecked, never as healthy.

    The sweep also surfaces items stuck in ``sending`` past
    ``STUCK_SENDING_SECONDS`` (about an hour) as
    ``stuck_sending`` (external ids, in queue order): a send
    whose outcome was never reconciled. It never touches them —
    only an operator with send evidence may close one — but the
    watchdog hook wakes an agent for them, so a stuck send does
    not sit unnoticed. An item with no recorded ``sending_at``
    (legacy or hand-edited state) counts as stuck: it cannot be
    shown to be fresh.

    Returns ``{"recycled": [external_id, ...], "lease_expired":
    [external_id, ...], "lease_escalated": [external_id, ...],
    "pending": n,
    "claimed_live": n, "sending": n, "stuck_sending":
    [external_id, ...], "intake_age_seconds": age
    | None, "intake_stale": bool, "intake_wake": bool,
    "intake_recovered": bool, "health": health | None,
    "health_ok": bool | None, "health_wake": bool,
    "health_recovered": bool}`` — counts taken after the recycle,
    so ``pending`` includes the items just freed. ``recycled``
    merges the lease releases and the heartbeat-stale recycles,
    in queue order.
    """
    ensure_state(state)
    now = _now(now)
    # Claim lease first (P1): never-started claims past
    # CLAIM_LEASE_SECONDS are released (or escalated at K
    # attempts) by the sweeper — never by the claim paths, so
    # a claim alone cannot race a turn still starting up. The
    # 600 s heartbeat recycle below stays the backstop for
    # started turns.
    lease = sweep_claim_lease(state, now=now)
    recycled_stale = _recycle_stale(
        state,
        stale_ttl,
        now,
        heartbeat_stale=heartbeat_stale,
        active_heartbeat_stale=active_heartbeat_stale,
        warm_baton_timeout=warm_baton_timeout,
    )
    # One recycled list, in queue order, across both releases:
    # a lease-released item and a heartbeat-stale item are both
    # freed claims the intake re-claims and re-wakes.
    recycled_keys = {
        (item["platform"], item["external_id"])
        for item in lease["expired"] + lease["escalated"] + recycled_stale
    }
    recycled = [
        item
        for item in state["queue"]
        if (item["platform"], item["external_id"]) in recycled_keys
    ]
    last_poll = state.get("last_successful_poll")
    if not isinstance(last_poll, (int, float)):
        intake_age = None
        intake_stale = False
        intake_wake = False
        intake_recovered = False
    else:
        intake_age = now - last_poll
        intake_stale = intake_age > INTAKE_STALE_SECONDS
        if not intake_stale:
            # Fresh poll ends the stale episode: clear the wake
            # marker so the next episode wakes promptly. Report
            # the recovery on this one sweep, so the repair is
            # only counted once fresh output is seen.
            intake_recovered = (
                state.pop(LAST_INTAKE_STALE_WAKE_KEY, None) is not None
            )
            intake_wake = False
        else:
            intake_recovered = False
            last_wake = state.get(LAST_INTAKE_STALE_WAKE_KEY)
            if (
                not isinstance(last_wake, (int, float))
                or now - last_wake > INTAKE_STALE_WAKE_COOLDOWN_SECONDS
            ):
                state[LAST_INTAKE_STALE_WAKE_KEY] = now
                intake_wake = True
            else:
                intake_wake = False
    if health is None:
        health_ok = None
        health_wake = False
        health_recovered = False
    else:
        health_ok = bool(health.get("ok", False))
        if health_ok:
            # Healthy sweep ends the unhealthy episode: clear the
            # wake marker so the next episode wakes promptly, and
            # report the recovery on this one sweep.
            health_recovered = (
                state.pop(LAST_HEALTH_WAKE_KEY, None) is not None
            )
            health_wake = False
        else:
            health_recovered = False
            last_wake = state.get(LAST_HEALTH_WAKE_KEY)
            if (
                not isinstance(last_wake, (int, float))
                or now - last_wake > HEALTH_WAKE_COOLDOWN_SECONDS
            ):
                state[LAST_HEALTH_WAKE_KEY] = now
                health_wake = True
            else:
                health_wake = False
    counts_now = counts(state)
    stuck_sending = [
        item["external_id"]
        for item in state.get("queue", [])
        if item["status"] == SENDING
        and (
            not isinstance(item.get("sending_at"), (int, float))
            or now - item["sending_at"] > STUCK_SENDING_SECONDS
        )
    ]
    return {
        "recycled": [item["external_id"] for item in recycled],
        "lease_expired": [
            item["external_id"] for item in lease["expired"]
        ],
        "lease_escalated": [
            item["external_id"] for item in lease["escalated"]
        ],
        "pending": counts_now[PENDING],
        "claimed_live": counts_now[CLAIMED],
        "sending": counts_now[SENDING],
        "stuck_sending": stuck_sending,
        "intake_age_seconds": intake_age,
        "intake_stale": intake_stale,
        "intake_wake": intake_wake,
        "intake_recovered": intake_recovered,
        "health": health,
        "health_ok": health_ok,
        "health_wake": health_wake,
        "health_recovered": health_recovered,
    }


def _owned_item(
    state: dict, platform: str, external_id, token, statuses
) -> dict | None:
    """The item, iff it exists, is in one of ``statuses``, and the
    caller's token matches the item's current claim token.

    The token is always required: a missing or stale token (e.g. from
    a batch that has since been recycled) matches nothing, so a stale
    turn can never move an item it no longer owns.
    """
    item = find_item(state, platform, external_id)
    if item is None or item["status"] not in statuses:
        return None
    if token is None or item["claim_token"] != token:
        return None
    return item


def mark_sending(
    state: dict, platform: str, external_id, token, *, now: float | None = None
) -> bool:
    """Move a claimed item to ``sending`` before the platform send.

    From here the item is never recycled: if the turn dies, the send
    outcome is unknown and an operator reconciles the item instead of
    the queue risking an automatic duplicate.

    Refused when a reply is recorded (``replied_at`` set): a claimed
    item stays ``claimed`` until ``mark_done`` even after its reply
    is recorded, and moving it into the send lifecycle would expose
    an already-answered item to a second send. Same guard as
    ``mark_send_failed``.
    """
    item = _owned_item(state, platform, external_id, token, (CLAIMED,))
    if item is None:
        return False
    if item["replied_at"] is not None:
        return False
    item["status"] = SENDING
    item["sending_at"] = _now(now)
    return True


def mark_send_failed(
    state: dict, platform: str, external_id, token, *, now: float | None = None
) -> bool:
    """Return a ``sending`` item to pending after a DEFINITIVE send
    failure (the platform API said the send did not go through).

    This is the only way back from ``sending`` besides ``mark_done``;
    it must never be used when the outcome is merely unknown.

    Refused when a reply is recorded (``replied_at`` set): the item
    sits in ``sending`` until ``mark_done`` even after a successful
    send, and bouncing it back to pending would let a later claim
    answer it a second time.
    """
    item = _owned_item(state, platform, external_id, token, (SENDING,))
    if item is None:
        return False
    if item["replied_at"] is not None:
        return False
    item["status"] = PENDING
    item["claimed_at"] = None
    item["claim_token"] = None
    item["sending_at"] = None
    return True


def mark_send_retryable(
    state: dict,
    platform: str,
    external_id,
    token,
    wake_after: float | None,
) -> bool:
    """Return a ``sending`` item to pending after a RETRYABLE send
    failure (the platform throttled or errored: 429/5xx — the send
    provably did not go through, but may succeed later).

    Like ``mark_send_failed`` (definitive rejection), but the item
    also carries ``wake_after``: it is not claimable before then,
    so the next poll does not immediately re-send into the same
    throttle. Refused when a reply is recorded (``replied_at``
    set), exactly like ``mark_send_failed``.
    """
    item = _owned_item(state, platform, external_id, token, (SENDING,))
    if item is None:
        return False
    if item["replied_at"] is not None:
        return False
    item["status"] = PENDING
    item["claimed_at"] = None
    item["claim_token"] = None
    item["sending_at"] = None
    item["wake_after"] = wake_after
    return True


def mark_done(
    state: dict,
    platform: str,
    external_id,
    token,
    *,
    now: float | None = None,
    reason: str | None = None,
    evidence: str | None = None,
) -> bool:
    """Close a claimed or sending item. A wrong token is a no-op.

    ``reason`` records WHY the item was closed (e.g. "superseded",
    "ack-only"); audit trails use it to tell answered items from
    silently closed ones. It is optional context everywhere.

    Closing a ``sending`` item with no reply record is stricter:
    that item's send outcome is unknown, so the close must cite
    reconciliation ``evidence`` — a non-empty reference to how the
    outcome was established (e.g. the platform message id found in
    history, or a definitive non-delivery confirmation). A reason
    alone does NOT suffice there; without evidence the call returns
    False and changes nothing. Given evidence is stored as
    ``done_evidence``, separate from ``done_reason``. ``claimed``
    items (never sent) and items with a recorded reply need no
    evidence, as before.
    """
    item = _owned_item(state, platform, external_id, token, IN_FLIGHT)
    if item is None:
        return False
    if (
        item["status"] == SENDING
        and item["replied_at"] is None
        and not evidence
    ):
        return False
    item["status"] = DONE
    item["done_at"] = _now(now)
    item["claim_token"] = None
    if reason:
        item["done_reason"] = reason
    if evidence:
        item["done_evidence"] = evidence
    return True


def release(
    state: dict, platform: str, external_id, token
) -> bool:
    """Return a claimed item to pending (the turn could not handle it).

    Refused for items that were already replied to or are in
    ``sending``: their reply went out (or may have), so putting them
    back in the queue would resurrect them for a duplicate answer.
    """
    item = _owned_item(state, platform, external_id, token, (CLAIMED,))
    if item is None:
        return False
    if item["replied_at"] is not None:
        return False
    item["status"] = PENDING
    item["claimed_at"] = None
    item["claim_token"] = None
    return True


def mark_replied(
    state: dict,
    platform: str,
    external_id,
    message_id,
    token,
    *,
    now: float | None = None,
) -> bool:
    """Record that a reply went out for a claimed or sending item.

    The claim token is required and must match, exactly like
    ``mark_done``: a stale worker that no longer owns the claim cannot
    record a reply. Once recorded, the item is never recycled and
    never released (see module docstring); the turn is expected to
    follow up with ``mark_done``.
    """
    item = _owned_item(state, platform, external_id, token, IN_FLIGHT)
    if item is None:
        return False
    item["replied_at"] = _now(now)
    item["reply_message_id"] = str(message_id)
    return True


def mark_ack(
    state: dict,
    platform: str,
    external_id,
    message_id,
    token,
    *,
    now: float | None = None,
    card_text: str | None = None,
) -> bool:
    """Record that the acknowledgement went out for a claimed item.

    The acknowledgement is the short "looking into this" message a
    turn sends before long work (see telegram.send_ack). It does not
    close the item and does not change its status: the item stays
    claimed so the full reply can still close it.

    Refused when the item is not claimed under the caller's token,
    when a full reply is already recorded (``replied_at`` set — no
    ack is needed after the answer), or when a COMPLETED ack is
    already recorded (``ack_message_id`` set — one ack per item,
    never a second). A TENTATIVE reservation (``ack_sent_at`` set
    by ``reserve_ack`` with no message id yet) does NOT refuse:
    completing it is exactly what this call does. Uses ``.get``
    for the ack fields so items enqueued before the ack fields
    existed still work.
    """
    item = _owned_item(state, platform, external_id, token, (CLAIMED,))
    if item is None:
        return False
    if item["replied_at"] is not None:
        return False
    if item.get("ack_message_id") is not None:
        return False
    item["ack_sent_at"] = _now(now)
    item["ack_message_id"] = str(message_id)
    # The text last written to the ack card, when the ack IS a
    # script status card (send_claim_ack passes it): the sweeper
    # edits the card only while this is set, and only when the
    # desired text differs — so a turn's own --send-ack (which
    # passes no card text) is never edited by the sweeper, and a
    # card is never re-edited with identical text (Telegram
    # answers that 400 "message is not modified").
    if card_text is not None:
        item["ack_card_text"] = card_text
    # An ack is explicit proof the turn started (two-phase rule):
    # the turn sent a message, so it is alive — record the start
    # and refresh the heartbeat, so the acking turn is never
    # recycled as never-started.
    if item.get("turn_started_at") is None:
        item["turn_started_at"] = _now(now)
    item["heartbeat_at"] = _now(now)
    return True


def mark_claim_ack(
    state: dict,
    platform: str,
    external_id,
    message_id,
    token,
    *,
    card_text: str,
    now: float | None = None,
) -> bool:
    """Record the script's claim status card (see
    telegram.send_claim_ack): the ack the intake sends at claim,
    before any turn starts.

    Same fence as ``mark_ack`` (claimed under the caller's
    token, no reply recorded, no completed ack already
    recorded; a tentative reservation from ``reserve_ack`` is
    completed, not refused), and it records ``ack_card_text``
    so the sweeper can keep the card current. Unlike
    ``mark_ack`` it does NOT set ``turn_started_at`` and does
    NOT refresh the heartbeat: the card is a receipt sent
    before any turn starts, not proof a turn is alive — so the
    claim lease still applies to a carded batch whose turn
    never starts. (If ``mark_ack`` recorded it, every carded
    batch would look started and the lease could never fire.)
    """
    item = _owned_item(state, platform, external_id, token, (CLAIMED,))
    if item is None:
        return False
    if item["replied_at"] is not None:
        return False
    if item.get("ack_message_id") is not None:
        return False
    item["ack_sent_at"] = _now(now)
    item["ack_message_id"] = str(message_id)
    item["ack_card_text"] = card_text
    return True


def reserve_ack(
    state: dict,
    platform: str,
    external_id,
    token,
    *,
    now: float | None = None,
) -> bool:
    """Reserve the ack inside the send transaction, before sending.

    Sets ``ack_sent_at`` with NO message id: a tentative
    reservation that stops a second concurrent ack from sending
    (``send_ack`` maps it to "already-acked") without claiming the
    ack went out. The reservation is completed by ``mark_ack``
    after a successful send, or cleared by ``clear_ack`` when the
    send is rejected, retryable, or unknown, so a retry is safe.

    Refused when the item is not claimed under the caller's token,
    when a full reply is already recorded, or when any ack is
    already recorded — tentative or completed (a reservation is
    not re-reservable). Returns True when the reservation was set.
    """
    item = _owned_item(state, platform, external_id, token, (CLAIMED,))
    if item is None:
        return False
    if item["replied_at"] is not None:
        return False
    if item.get("ack_sent_at") is not None:
        return False
    if item.get("ack_message_id") is not None:
        return False
    item["ack_sent_at"] = _now(now)
    return True


def clear_ack(state: dict, platform: str, external_id, token) -> bool:
    """Clear a tentative ack reservation (the send did not go out).

    Token-checked, and refused when a message id was ever recorded:
    a completed ack is proof the ack went out and must never be
    un-recorded. Only ``ack_sent_at`` is cleared; a reservation
    that never existed is a no-op success (False only when the
    item is not claimed under the token or a completed ack
    exists). Returns True when the reservation was cleared (or
    there was none to clear on an owned, un-acked item).
    """
    item = _owned_item(state, platform, external_id, token, (CLAIMED,))
    if item is None:
        return False
    if item.get("ack_message_id") is not None:
        return False
    item["ack_sent_at"] = None
    return True


def _pending_item(state: dict, platform: str, external_id) -> dict | None:
    """The item, iff it exists and is still pending (never
    claimed). No token: an intake ack is sent before any claim
    exists, so there is nothing to check a token against.
    Refuses claimed, sending, and done items — a card recorded
    on an item that has since been claimed belongs to the claim
    ack path, not this one."""
    item = find_item(state, platform, external_id)
    if item is None or item["status"] != PENDING:
        return None
    return item


def reserve_intake_ack(
    state: dict, platform: str, external_id, *, now: float | None = None
) -> bool:
    """Reserve the intake ack inside the send transaction, before
    sending (see telegram.send_intake_ack).

    Same two-phase shape as ``reserve_ack``, but for a PENDING
    item with no claim token: sets ``ack_sent_at`` with NO
    message id, a tentative reservation that stops a second
    concurrent intake ack from sending, completed by
    ``mark_intake_ack`` after a successful send, or cleared by
    ``clear_intake_ack`` when the send did not go out, so a
    retry is safe.

    Refused when the item is not pending, when a full reply is
    already recorded, or when any ack is already recorded —
    tentative or completed (a reservation is not re-reservable).
    Returns True when the reservation was set.
    """
    item = _pending_item(state, platform, external_id)
    if item is None:
        return False
    if item["replied_at"] is not None:
        return False
    if item.get("ack_sent_at") is not None:
        return False
    if item.get("ack_message_id") is not None:
        return False
    item["ack_sent_at"] = _now(now)
    return True


def mark_intake_ack(
    state: dict,
    platform: str,
    external_id,
    message_id,
    *,
    card_text: str,
    now: float | None = None,
) -> bool:
    """Record the script's intake status card (see
    telegram.send_intake_ack): the ack the intake sends when it
    first sees a message that is still pending behind a busy
    lane, before any claim exists.

    Same fence as ``reserve_intake_ack`` (pending, no reply
    recorded, no completed ack already recorded; a tentative
    reservation is completed, not refused), and it records
    ``ack_card_text`` so the sweeper can keep the card current.
    Like ``mark_claim_ack`` it does NOT set ``turn_started_at``
    and does NOT refresh the heartbeat: the card is a receipt
    sent before any turn starts, not proof a turn is alive — so
    the claim lease still applies to a carded item whose turn
    never starts.
    """
    item = _pending_item(state, platform, external_id)
    if item is None:
        return False
    if item["replied_at"] is not None:
        return False
    if item.get("ack_message_id") is not None:
        return False
    item["ack_sent_at"] = _now(now)
    item["ack_message_id"] = str(message_id)
    item["ack_card_text"] = card_text
    return True


def clear_intake_ack(state: dict, platform: str, external_id) -> bool:
    """Clear a tentative intake ack reservation (the send did
    not go out).

    Refused when a message id was ever recorded: a completed
    ack is proof the ack went out and must never be
    un-recorded. Only ``ack_sent_at`` is cleared; a reservation
    that never existed is a no-op success (False only when the
    item is not pending or a completed ack exists). Returns True
    when the reservation was cleared (or there was none to
    clear on a pending, un-acked item).
    """
    item = _pending_item(state, platform, external_id)
    if item is None:
        return False
    if item.get("ack_message_id") is not None:
        return False
    item["ack_sent_at"] = None
    return True


def counts(state: dict) -> dict:
    result = {PENDING: 0, CLAIMED: 0, SENDING: 0, DONE: 0}
    for item in state.get("queue", []):
        result[item["status"]] = result.get(item["status"], 0) + 1
    return result
