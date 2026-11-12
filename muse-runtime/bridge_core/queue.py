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

Every item records ``received_at``, ``claimed_at``, ``sending_at``
and ``replied_at`` so intake latency can be measured end to end.
"""
from __future__ import annotations

import math
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
    return state


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


def _recycle_stale(
    state: dict,
    stale_ttl: float,
    now: float,
    heartbeat_stale: float = HEARTBEAT_STALE_SECONDS,
) -> list:
    # Only CLAIMED items are candidates. SENDING items are excluded by
    # construction: their send outcome is unknown, so they must never
    # be recycled into a possible duplicate send. Replied items are
    # excluded too — the reply already went out.
    #
    # Two rules, in order. When a heartbeat was recorded, ONLY the
    # heartbeat decides: a turn that stops refreshing it is dead,
    # however recent its claim is — and a live long turn that keeps
    # refreshing it is never recycled for its claimed_at age alone.
    # When no heartbeat was ever recorded (an item claimed before
    # heartbeats existed, or by a turn that never sent one), the
    # claimed_at TTL is the backstop. Returns the recycled items.
    recycled = []
    for item in state["queue"]:
        if (
            item["status"] == CLAIMED
            and item["replied_at"] is None
            and item["claimed_at"] is not None
        ):
            heartbeat_at = item.get("heartbeat_at")
            stale = False
            if heartbeat_at is not None:
                stale = now - heartbeat_at > heartbeat_stale
            else:
                stale = now - item["claimed_at"] > stale_ttl
            if not stale:
                continue
            item["status"] = PENDING
            item["claimed_at"] = None
            item["claim_token"] = None
            item["heartbeat_at"] = None
            # ever_claimed deliberately survives the recycle: the
            # claim history is what keeps a recycled item out of the
            # first-pending expiry sweep on every later poll.
            recycled.append(item)
    return recycled


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
    _recycle_stale(state, stale_ttl, now, heartbeat_stale=heartbeat_stale)
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
        if candidate not in busy_lanes:
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
            item["heartbeat_at"] = now
            refreshed += 1
    return refreshed


def watchdog_sweep(
    state: dict,
    *,
    heartbeat_stale: float = HEARTBEAT_STALE_SECONDS,
    stale_ttl: float = DEFAULT_STALE_TTL,
    now: float | None = None,
) -> dict:
    """One watchdog pass: recycle dead claims, report queue health.

    The sweep runs the same stale-claim rule as ``claim_batch``
    (heartbeat first, claimed_at TTL as backstop for items that
    never recorded one) and counts what it found. It NEVER touches
    ``sending`` items (their send outcome is unknown — only an
    operator reconciles those), replied items, or claims whose
    heartbeat is still fresh. Recycled items return to ``pending``
    with their token cleared, so the intake's next poll re-claims
    and re-wakes them — the watchdog itself never sends anything
    and never wakes an agent; waking is the intake's job once the
    lane is free again.

    Returns ``{"recycled": [external_id, ...], "pending": n,
    "claimed_live": n, "sending": n}`` — counts taken after the
    recycle, so ``pending`` includes the items just freed.
    """
    ensure_state(state)
    now = _now(now)
    recycled = _recycle_stale(
        state, stale_ttl, now, heartbeat_stale=heartbeat_stale
    )
    counts_now = counts(state)
    return {
        "recycled": [item["external_id"] for item in recycled],
        "pending": counts_now[PENDING],
        "claimed_live": counts_now[CLAIMED],
        "sending": counts_now[SENDING],
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
) -> bool:
    """Record that the acknowledgement went out for a claimed item.

    The acknowledgement is the short "looking into this" message a
    turn sends before long work (see telegram.send_ack). It does not
    close the item and does not change its status: the item stays
    claimed so the full reply can still close it.

    Refused when the item is not claimed under the caller's token,
    when a full reply is already recorded (``replied_at`` set — no
    ack is needed after the answer), or when an ack is already
    recorded (``ack_sent_at`` set — one ack per item, never a second).
    Uses ``.get`` for the ack fields so items enqueued before the
    ack fields existed still work.
    """
    item = _owned_item(state, platform, external_id, token, (CLAIMED,))
    if item is None:
        return False
    if item["replied_at"] is not None:
        return False
    if item.get("ack_sent_at") is not None:
        return False
    item["ack_sent_at"] = _now(now)
    item["ack_message_id"] = str(message_id)
    return True


def counts(state: dict) -> dict:
    result = {PENDING: 0, CLAIMED: 0, SENDING: 0, DONE: 0}
    for item in state.get("queue", []):
        result[item["status"]] = result.get(item["status"], 0) + 1
    return result
