"""Scenario runner + CLI for the Muse2Telegram v2 simulation harness.

Usage (from the v2 copy root; the .venv is the copy's own):

    python3 simulation/run_scenario.py --list
    python3 simulation/run_scenario.py --scenario single-dm-reply --mode baseline
    python3 simulation/run_scenario.py --scenario all --mode v2
    python3 simulation/run_scenario.py --report            # all scenarios, both modes
    python3 simulation/run_scenario.py --report --json     # machine-readable

``--mode`` only changes the label and the pass bar for now: the
parent re-runs this after the v2 core work lands, and the same
scenarios then measure the new code. A scenario whose v2 feature
is not present in the imported code is reported
"not-implemented" with the reason, never a hard failure.

Timing model (all simulated, no real sleeps):
  WAKE_LATENCY_S  0.5   hook tick + long-poll pickup to wake
  QUICK_WORK_S    2.0   a quick greeting turn's model work
  DEEP_WORK_S    30.0   a deep turn's model work (modelled, not measured)
  REWAKE_LATENCY_S 0.5  intake re-poll after a lane frees
Recovery times are measured on the fake clock: the clock is
advanced in steps and the real watchdog/claim code is run at each
step, so a 600 s recycle costs milliseconds of wall time.

Token model: tokens_for(text) = len(text)//4 (modelled chars/4;
provider caching is unverified). ``input_tokens_modelled`` counts
inbound-text tokens only — the system prompt and history are NOT
modelled, so treat the number as a floor, not a total.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SIM_DIR = Path(__file__).resolve().parent
if str(SIM_DIR) not in sys.path:
    sys.path.insert(0, str(SIM_DIR))

from fake_harness import FEATURES, FakeClock, FakeHarness, FakeTurn, make_turn  # noqa: E402
from fake_tg import FakeTelegram  # noqa: E402

OWNER_ID = 111
GROUP_ID = -777
OTHER_ID = 222

# Modelled latencies (seconds, simulated clock — see module docstring).
WAKE_LATENCY_S = 0.5
QUICK_WORK_S = 2.0
DEEP_WORK_S = 30.0
REWAKE_LATENCY_S = 0.5

V2_HEARTBEAT_TARGET_S = 180.0   # v2 hang recycle target (baseline: 600)
V2_WARM_RECYCLE_TARGET_S = 30.0  # v2 warm-claim baton recycle target
V2_DROPPED_WAKE_TARGET_S = 180.0  # v2 fast re-wake target


def tokens_for(text):
    """Modelled input tokens for text: len(text)//4 (chars/4).

    Provider caching is unverified; this is a planning estimate,
    not a billing figure.
    """
    return len(text or "") // 4


def _result(scenario, mode, *, status, duplicate_sends=0, silent_drops=0,
            time_to_first_signal=None, time_to_reply=None,
            recovery_time=None, warm_lease_hits=None,
            input_tokens_modelled=0, notes=""):
    return {
        "scenario": scenario,
        "mode": mode,
        "status": status,  # pass | not-implemented | fail
        "duplicate_sends": duplicate_sends,
        "silent_drops": silent_drops,
        "time_to_first_signal": time_to_first_signal,
        "time_to_reply": time_to_reply,
        "recovery_time": recovery_time,
        "warm_lease_hits": warm_lease_hits,
        "input_tokens_modelled": input_tokens_modelled,
        "notes": notes,
    }


def _silent_drops(turn, injected_update_ids):
    """Injected updates with no queue item at all: silently dropped.

    Queue items survive in state whatever their status (pending /
    claimed / sending / done), so a missing external_id means the
    intake never recorded the message.
    """
    queued = {i["external_id"] for i in turn.items()}
    return sum(1 for uid in injected_update_ids
               if str(uid) not in queued)


def _dupes(turn):
    """Duplicate sends from the subprocess sender log (unattributed).

    The sender argv carries no update_id, so the (chat_id, text)
    count is used; scenarios use distinct reply texts per turn, so
    a count here means a real re-send of identical text.
    """
    turn.sync_sends()
    return turn.fake.duplicate_sends_unattributed()


def _warm_hits(turn, chat_id):
    """Warm-lease hit for a lane, or None when warm hold is not wired.

    Uses the REAL ``queue.warm_lease_active``. A hit is a follow-up
    wake that finds a live warm lease for the lane. Returns
    (hits_or_None, note).
    """
    if not FEATURES["warm_lease"]:
        return None, "warm hold not yet wired"
    if not FEATURES["warm_lease_wired"]:
        return None, ("warm-lease machinery exists in queue.py but no "
                      "in-tree caller sets a lease yet")
    return (1 if turn.warm_lease_active(chat_id) else 0), ""


def _probe_two_phase():
    """Would the two-phase 180 s rule recycle a never-started claim?

    Separate lightweight turn: inject, wake (claim, no heartbeat),
    advance the fake clock 181 s, then ask the REAL
    ``queue.stale_claims`` with active_heartbeat_stale=180 whether
    the claim is recyclable. This probes the machinery, not the
    wired path — the wired intake/watchdog may not pass the
    parameter yet. Returns True/False, or None when the machinery
    is absent.
    """
    if not FEATURES["two_phase_machinery"]:
        return None
    clock = FakeClock()
    turn, tmp = make_turn(clock=clock)
    try:
        fake = turn.fake
        fake.inject_message(OWNER_ID, "probe never-started claim",
                            sender_id=OWNER_ID)
        clock.advance(WAKE_LATENCY_S)
        payload = turn.wake_once()
        if payload is None:
            return False
        clock.advance(181.0)
        stale = turn.stale_claims_probe(
            now=clock.now(),
            active_heartbeat_stale=FEATURES["two_phase_seconds"])
        return len(stale) > 0
    finally:
        if tmp is not None:
            tmp.cleanup()


# ---------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------

def sc_single_dm_reply(mode):
    clock = FakeClock()
    turn, tmp = make_turn(clock=clock)
    try:
        fake = turn.fake
        text = "hello, is the store open today?"
        t0 = clock.now()
        upd = fake.inject_message(OWNER_ID, text, sender_id=OWNER_ID)
        clock.advance(WAKE_LATENCY_S)
        payload = turn.wake_once()
        assert payload is not None, "wake claimed nothing"
        clock.advance(0.1)  # typing fires at wake + 0.1s (simulated)
        first_signal = clock.now() - t0
        clock.advance(QUICK_WORK_S)
        reply = "Yes, open until 5pm today."
        out = turn.run("succeed", payload, reply, now=clock.now())
        t_reply = clock.now() - t0
        return _result("single-dm-reply", mode, status="pass",
                       duplicate_sends=_dupes(turn),
                       silent_drops=_silent_drops(turn, [upd["update_id"]]),
                       time_to_first_signal=round(first_signal, 2),
                       time_to_reply=round(t_reply, 2),
                       input_tokens_modelled=tokens_for(text),
                       notes=f"1 DM, 1 wake, 1 send; turn={out['mode']}.")
    finally:
        if tmp is not None:
            tmp.cleanup()


def sc_rapid_back_and_forth_5(mode):
    clock = FakeClock()
    turn, tmp = make_turn(clock=clock)
    try:
        fake = turn.fake
        texts = ["hi", "what hay do you have?",
                 "is the alfalfa non-GMO?", "how much per bale?",
                 "thanks, see you Saturday"]
        tokens = 0
        hits = 0
        t_first = None
        t_reply = None
        for n, text in enumerate(texts):
            tokens += tokens_for(text)
            t0 = clock.now()
            upd = fake.inject_message(OWNER_ID, text, sender_id=OWNER_ID)
            clock.advance(WAKE_LATENCY_S)
            payload = turn.wake_once()
            assert payload is not None, f"wake {n+1} claimed nothing"
            if n == 0:
                t_first = round(clock.now() - t0 + 0.1, 2)
            hit, _note = _warm_hits(turn, OWNER_ID)
            if hit:
                hits += 1
            clock.advance(QUICK_WORK_S)
            out = turn.run("succeed", payload, f"reply {n+1} to: {text}",
                           now=clock.now())
            assert out["sends"] == 1, f"turn {n+1} sent {out['sends']}"
            if n == len(texts) - 1:
                t_reply = round(clock.now() - t0, 2)
        warm = hits if FEATURES["warm_lease_wired"] else None
        _, warm_note = _warm_hits(turn, OWNER_ID)
        note = (f"5/5 turns replied. Warm lease hits {hits}/4 follow-ups."
                if FEATURES["warm_lease_wired"] else
                f"5/5 turns replied. warm_lease_hits=None: {warm_note}; "
                f"each follow-up was a fresh claim.")
        return _result("rapid-back-and-forth-5", mode, status="pass",
                       duplicate_sends=_dupes(turn),
                       silent_drops=0,
                       time_to_first_signal=t_first,
                       time_to_reply=t_reply,
                       warm_lease_hits=warm,
                       input_tokens_modelled=tokens,
                       notes=note)
    finally:
        if tmp is not None:
            tmp.cleanup()


def sc_dm_plus_group_concurrent(mode):
    clock = FakeClock()
    turn, tmp = make_turn(clock=clock)
    try:
        fake = turn.fake
        dm_text = "can you hold a bag of layer feed for me?"
        grp_text = "who is bringing the trailer on Saturday?"
        t0 = clock.now()
        u_dm = fake.inject_message(OWNER_ID, dm_text, sender_id=OWNER_ID)
        # The bridge adopts a group only when the owner speaks there;
        # a stranger's message into a never-adopted group is
        # correctly closed, not queued. So the owner adopts first.
        u_adopt = fake.inject_message(GROUP_ID, "adding the group",
                                      sender_id=OWNER_ID, chat_type="group")
        u_grp = fake.inject_message(GROUP_ID, grp_text, sender_id=OTHER_ID,
                                    chat_type="group")
        clock.advance(WAKE_LATENCY_S)
        p_dm = turn.wake_once()
        assert p_dm is not None, "DM wake claimed nothing"
        clock.advance(QUICK_WORK_S)
        turn.run("succeed", p_dm, "Yes, holding one bag for you.",
                 now=clock.now())
        clock.advance(REWAKE_LATENCY_S)
        p_grp = turn.wake_once()
        assert p_grp is not None, "group wake claimed nothing"
        assert len(p_grp["items"]) == 2, \
            f"expected owner-adopt + stranger group items, got {p_grp}"
        clock.advance(QUICK_WORK_S)
        turn.run("succeed", p_grp, "Marlin is bringing the trailer.",
                 now=clock.now())
        t_reply = round(clock.now() - t0, 2)
        lanes = {p_dm["chat_id"], p_grp["chat_id"]}
        assert lanes == {OWNER_ID, GROUP_ID}, f"lanes claimed: {lanes}"
        return _result("dm-plus-group-concurrent", mode, status="pass",
                       duplicate_sends=_dupes(turn),
                       silent_drops=_silent_drops(
                           turn, [u_dm["update_id"], u_grp["update_id"]]),
                       time_to_first_signal=round(WAKE_LATENCY_S + 0.1, 2),
                       time_to_reply=t_reply,
                       input_tokens_modelled=tokens_for(dm_text)
                       + tokens_for(grp_text),
                       notes="DM and group lanes each woke and replied "
                             "independently; no cross-lane blocking. "
                             "Group needed the owner's message first: "
                             "the bridge adopts a group only when the "
                             "owner speaks there.")
    finally:
        if tmp is not None:
            tmp.cleanup()


def _measure_recycle(turn, max_advance=1200.0, step=30.0):
    """Advance the fake clock until the watchdog recycles a claim.

    Returns (recovered_after_seconds, recycled_ids) or (None, []).
    Uses the REAL queue watchdog at each simulated step.
    """
    elapsed = 0.0
    while elapsed < max_advance:
        turn.clock.advance(step)
        elapsed += step
        report = turn.watchdog(now=turn.clock.now())
        if report["recycled"]:
            return elapsed, report["recycled"]
    return None, []


def sc_dropped_wake(mode):
    clock = FakeClock()
    turn, tmp = make_turn(clock=clock)
    try:
        fake = turn.fake
        text = "did you get my delivery request?"
        t0 = clock.now()
        upd = fake.inject_message(OWNER_ID, text, sender_id=OWNER_ID)
        clock.advance(WAKE_LATENCY_S)
        payload = turn.wake_once()
        assert payload is not None, "wake claimed nothing"
        # The wake fired but the turn never started.
        turn.run("drop-wake", payload, "unused", now=clock.now())
        recovered_after, recycled = _measure_recycle(turn)
        assert recovered_after is not None, "claim never recycled"
        # After recycle the lane frees; the intake re-wakes and replies.
        clock.advance(REWAKE_LATENCY_S)
        payload2 = turn.wake_once()
        assert payload2 is not None, "re-wake claimed nothing"
        clock.advance(QUICK_WORK_S)
        turn.run("succeed", payload2, "Yes, got it, Wednesday route.",
                 now=clock.now())
        stale = FEATURES["heartbeat_stale_seconds"] or 600.0
        probe = _probe_two_phase()
        probe_note = (
            "two-phase probe: machinery absent."
            if probe is None else
            f"two-phase probe: a never-started claim IS recyclable at "
            f"181s under active_heartbeat_stale=180 (machinery works) "
            f"but the wired intake/watchdog do not pass it yet."
            if probe else
            "two-phase probe: claim NOT recyclable at 181s even with "
            "active_heartbeat_stale=180.")
        if mode == "v2" and recovered_after > V2_DROPPED_WAKE_TARGET_S:
            status = "not-implemented"
            note = (f"no fast re-wake yet: claim recycled only after "
                    f"{recovered_after:.0f}s (heartbeat stale {stale:.0f}s, "
                    f"v2 target <={V2_DROPPED_WAKE_TARGET_S:.0f}s); "
                    f"message was NOT lost, just late. {probe_note}")
        else:
            status = "pass"
            note = (f"dropped wake recovered after {recovered_after:.0f}s "
                    f"(heartbeat stale {stale:.0f}s); re-wake replied, "
                    f"no duplicate, no silent drop. {probe_note}")
        return _result("dropped-wake", mode, status=status,
                       duplicate_sends=_dupes(turn),
                       silent_drops=_silent_drops(turn, [upd["update_id"]]),
                       time_to_first_signal=None,
                       time_to_reply=round(clock.now() - t0, 2),
                       recovery_time=recovered_after,
                       input_tokens_modelled=tokens_for(text),
                       notes=note)
    finally:
        if tmp is not None:
            tmp.cleanup()


def sc_hang_no_heartbeat(mode):
    clock = FakeClock()
    turn, tmp = make_turn(clock=clock)
    try:
        fake = turn.fake
        text = "can you research the best mineral for my herd?"
        upd = fake.inject_message(OWNER_ID, text, sender_id=OWNER_ID)
        clock.advance(WAKE_LATENCY_S)
        payload = turn.wake_once()
        assert payload is not None, "wake claimed nothing"
        turn.run("hang-no-heartbeat", payload, "unused", now=clock.now())
        recovered_after, _ = _measure_recycle(turn)
        assert recovered_after is not None, "hang never recycled"
        stale = FEATURES["heartbeat_stale_seconds"] or 600.0
        probe = _probe_two_phase()
        probe_note = (
            "two-phase probe: machinery absent."
            if probe is None else
            f"two-phase probe: a never-started claim IS recyclable at "
            f"181s under active_heartbeat_stale=180 (machinery works) "
            f"but the wired intake/watchdog do not pass it yet."
            if probe else
            "two-phase probe: claim NOT recyclable at 181s even with "
            "active_heartbeat_stale=180.")
        if mode == "v2" and recovered_after > V2_HEARTBEAT_TARGET_S:
            status = "not-implemented"
            note = (f"hang recycled after {recovered_after:.0f}s, but v2 "
                    f"target is ~{V2_HEARTBEAT_TARGET_S:.0f}s "
                    f"(heartbeat stale still {stale:.0f}s). {probe_note}")
        else:
            status = "pass"
            note = (f"hung claim recycled after {recovered_after:.0f}s "
                    f"(heartbeat stale {stale:.0f}s); lane freed. "
                    f"{probe_note}")
        return _result("hang-no-heartbeat", mode, status=status,
                       duplicate_sends=_dupes(turn),
                       silent_drops=_silent_drops(turn, [upd["update_id"]]),
                       time_to_first_signal=None,
                       time_to_reply=None,
                       recovery_time=recovered_after,
                       input_tokens_modelled=tokens_for(text),
                       notes=note)
    finally:
        if tmp is not None:
            tmp.cleanup()


def sc_die_mid_send(mode):
    clock = FakeClock()
    turn, tmp = make_turn(clock=clock)
    try:
        fake = turn.fake
        text = "please confirm my order total"
        reply = "Your total is $42.50."
        upd = fake.inject_message(OWNER_ID, text, sender_id=OWNER_ID)
        clock.advance(WAKE_LATENCY_S)
        payload = turn.wake_once()
        assert payload is not None
        clock.advance(QUICK_WORK_S)
        out = turn.run("die-mid-send", payload, reply, now=clock.now())
        assert out["sends"] == 1, "send did not go out before the 'death'"
        stuck = turn.items(status="sending")
        assert len(stuck) == 1, f"expected 1 sending item, got {len(stuck)}"
        # Recovery: probe the send log (the ledger/probe path).
        clock.advance(5.0)  # modelled reconcile delay
        item = stuck[0]
        kind, detail = turn.reconcile_sending(
            item, payload["claim_token"], reply, now=clock.now())
        assert kind == "delivered", f"reconcile said {kind}: {detail}"
        probe = turn.probe_delivery(OWNER_ID, detail["message_id"], reply)
        assert probe == "delivered", f"edit probe said {probe}"
        dupes = _dupes(turn)
        ledger = FEATURES["delivery_ledger"]
        wired = FEATURES["delivery_ledger_wired"]
        note = (f"die-mid-send recovered via probe: send recorded "
                f"message_id {detail['message_id']}, item closed with "
                f"evidence, duplicates={dupes}. "
                + ("Delivery ledger wired into the send path."
                   if wired else
                   "Delivery ledger module exists but is NOT wired into "
                   "telegram.py yet: recovery needed an explicit "
                   "reconcile call; a wired ledger sweep would do "
                   "this on bridge startup."
                   if ledger else
                   "No delivery ledger yet: recovery needed an explicit "
                   "reconcile call."))
        return _result("die-mid-send", mode, status="pass",
                       duplicate_sends=dupes,
                       silent_drops=_silent_drops(turn, [upd["update_id"]]),
                       time_to_first_signal=round(WAKE_LATENCY_S + 0.1, 2),
                       time_to_reply=None,
                       recovery_time=5.0,
                       input_tokens_modelled=tokens_for(text),
                       notes=note)
    finally:
        if tmp is not None:
            tmp.cleanup()


def sc_send_unknown_delivered(mode):
    clock = FakeClock()
    turn, tmp = make_turn(clock=clock)
    try:
        fake = turn.fake
        text = "what time do you close on Saturday?"
        reply = "We close at 1pm on Saturdays."
        upd = fake.inject_message(OWNER_ID, text, sender_id=OWNER_ID)
        clock.advance(WAKE_LATENCY_S)
        payload = turn.wake_once()
        clock.advance(QUICK_WORK_S)
        out = turn.run("send-unknown-delivered", payload, reply,
                       now=clock.now())
        assert out["reported_outcome"] == "unknown"
        stuck = turn.items(status="sending")
        assert len(stuck) == 1
        clock.advance(5.0)
        kind, detail = turn.reconcile_sending(
            stuck[0], payload["claim_token"], reply, now=clock.now())
        probe = turn.probe_delivery(OWNER_ID, detail.get("message_id", -1),
                                    reply) if kind == "delivered" else "n/a"
        ok = kind == "delivered" and probe == "delivered" \
            and _dupes(turn) == 0
        return _result("send-unknown-delivered", mode,
                       status="pass" if ok else "fail",
                       duplicate_sends=_dupes(turn),
                       silent_drops=_silent_drops(turn, [upd["update_id"]]),
                       time_to_first_signal=None,
                       time_to_reply=None,
                       recovery_time=5.0,
                       input_tokens_modelled=tokens_for(text),
                       notes=f"sender reported unknown but the send had "
                             f"landed; probe={probe}, reconcile={kind}, "
                             f"closed with evidence, no re-send.")
    finally:
        if tmp is not None:
            tmp.cleanup()


def sc_send_unknown_not_delivered(mode):
    clock = FakeClock()
    turn, tmp = make_turn(clock=clock)
    try:
        fake = turn.fake
        text = "do you carry goat mineral?"
        reply = "Yes, we carry goat mineral blocks."
        upd = fake.inject_message(OWNER_ID, text, sender_id=OWNER_ID)
        clock.advance(WAKE_LATENCY_S)
        payload = turn.wake_once()
        clock.advance(QUICK_WORK_S)
        out = turn.run("send-unknown-not-delivered", payload, reply,
                       now=clock.now())
        assert out["reported_outcome"] == "unknown"
        stuck = turn.items(status="sending")
        assert len(stuck) == 1
        clock.advance(5.0)
        kind, detail = turn.reconcile_sending(
            stuck[0], payload["claim_token"], reply, now=clock.now())
        assert kind == "not-delivered", f"reconcile said {kind}"
        assert detail["returned_to_pending"] is True
        # Re-wake and send for real: exactly one send in total.
        clock.advance(REWAKE_LATENCY_S)
        payload2 = turn.wake_once()
        assert payload2 is not None, "re-wake claimed nothing"
        clock.advance(QUICK_WORK_S)
        out2 = turn.run("succeed", payload2, reply, now=clock.now())
        assert out2["sends"] == 1
        dupes = _dupes(turn)
        ok = dupes == 0 and len(turn.fake.sends) == 1
        return _result("send-unknown-not-delivered", mode,
                       status="pass" if ok else "fail",
                       duplicate_sends=dupes,
                       silent_drops=_silent_drops(turn, [upd["update_id"]]),
                       time_to_first_signal=None,
                       time_to_reply=None,  # reply went out on the
                       # re-wake turn, not the original turn; the
                       # recovery_time (5s modelled reconcile) is
                       # the metric for this scenario.
                       recovery_time=5.0,
                       input_tokens_modelled=tokens_for(text),
                       notes="sender reported unknown and nothing was "
                             "sent; item returned to pending, re-wake "
                             "sent once. Total sends=1, duplicates=0.")
    finally:
        if tmp is not None:
            tmp.cleanup()


def sc_sibling_single_send(mode):
    clock = FakeClock()
    turn, tmp = make_turn(clock=clock)
    try:
        fake = turn.fake
        texts = ["first question?", "second question?", "third question?"]
        for text in texts:
            fake.inject_message(OWNER_ID, text, sender_id=OWNER_ID)
        clock.advance(WAKE_LATENCY_S)
        payload = turn.wake_once()
        assert payload is not None and len(payload["items"]) == 3, \
            f"expected a 3-item batch, got {payload}"
        clock.advance(QUICK_WORK_S)
        out = turn.run("sibling-single-send", payload,
                       "Answering the first one.", now=clock.now())
        siblings = turn.items(status="claimed")
        orphans = len(siblings)
        if FEATURES["sibling_auto_cover"]:
            status = "not-implemented"
            note = ("auto-cover helper detected but its call shape is "
                    "unknown to this harness; siblings left claimed "
                    f"({orphans} orphans). Wire the helper into "
                    "FakeTurn.run('sibling-single-send').")
        else:
            status = "not-implemented"
            note = (f"auto-cover not implemented: turn answered 1 of 3, "
                    f"{orphans} siblings left claimed (orphans). In "
                    f"baseline they recycle after the heartbeat goes "
                    f"stale and would be re-answered as a new turn.")
        return _result("sibling-single-send", mode, status=status,
                       duplicate_sends=_dupes(turn),
                       silent_drops=0,
                       time_to_first_signal=None,
                       time_to_reply=None,
                       recovery_time=None,
                       input_tokens_modelled=sum(tokens_for(t) for t in texts),
                       notes=note)
    finally:
        if tmp is not None:
            tmp.cleanup()


def sc_quick_to_deep_escalation(mode):
    clock = FakeClock()
    turn, tmp = make_turn(clock=clock)
    try:
        fake = turn.fake
        q = "hi"
        d = ("can you research the full mineral program for a 40-head "
             "cow-calf herd and compare three products with prices?")
        upd1 = fake.inject_message(OWNER_ID, q, sender_id=OWNER_ID)
        clock.advance(WAKE_LATENCY_S)
        p1 = turn.wake_once()
        clock.advance(QUICK_WORK_S)
        turn.run("succeed", p1, "Hi! How can I help?", now=clock.now())
        # Deep follow-up arrives during/after the quick session.
        clock.advance(1.0)
        upd2 = fake.inject_message(OWNER_ID, d, sender_id=OWNER_ID)
        clock.advance(WAKE_LATENCY_S)
        p2 = turn.wake_once()
        assert p2 is not None, "deep follow-up wake claimed nothing"
        hint = FEATURES["tier_escalation_hint"]
        clock.advance(DEEP_WORK_S)
        turn.run("succeed", p2,
                 "Here is the mineral comparison. (modelled deep reply)",
                 now=clock.now())
        if hint:
            status, note = "pass", ("wake payload carries a "
                                    "re-hydration signal (pre-hydrated "
                                    "context_md, no tiers in v2).")
        else:
            status, note = "not-implemented", (
                "no re-hydration signal in the wake "
                "payload: the deep follow-up was handled as a fresh "
                "turn with no pre-hydrated context.")
        return _result("quick-to-deep-escalation", mode, status=status,
                       duplicate_sends=_dupes(turn),
                       silent_drops=_silent_drops(
                           turn, [upd1["update_id"], upd2["update_id"]]),
                       time_to_first_signal=round(WAKE_LATENCY_S + 0.1, 2),
                       time_to_reply=round(DEEP_WORK_S + WAKE_LATENCY_S, 2),
                       input_tokens_modelled=tokens_for(q) + tokens_for(d),
                       notes=note)
    finally:
        if tmp is not None:
            tmp.cleanup()


def sc_warm_claim_no_heartbeat(mode):
    clock = FakeClock()
    turn, tmp = make_turn(clock=clock)
    try:
        fake = turn.fake
        text = "quick question while you are warm?"
        upd = fake.inject_message(OWNER_ID, text, sender_id=OWNER_ID)
        clock.advance(WAKE_LATENCY_S)
        payload = turn.wake_once()
        assert payload is not None
        # Simulate the warm-hold caller: flag the claim as warm with
        # no turn start, exactly as _warm_hold does in
        # bridge_core.telegram (it sets warm_claimed=True on the
        # follow-up it claims). The scenario sets it explicitly
        # because the harness drives the claim directly.
        flagged = turn.flag_warm_claim(payload["claim_token"],
                                       now=clock.now())
        assert flagged == 1, f"flagged {flagged} items"
        recovered_after, _ = _measure_recycle(turn, max_advance=1200.0,
                                              step=10.0)
        baton = FEATURES["warm_baton_timeout_seconds"]
        if recovered_after is not None \
                and recovered_after <= V2_WARM_RECYCLE_TARGET_S + 10:
            status = "pass"
            note = (f"warm-claimed batch (warm_claimed set by the "
                    f"harness, mimicking the warm-hold caller "
                    f"telegram._warm_hold, which sets it on the "
                    f"follow-up it claims) recycled "
                    f"after {recovered_after:.0f}s via the warm-baton "
                    f"guard (timeout {baton:.0f}s).")
        elif not FEATURES["warm_lease"]:
            status = "not-implemented"
            note = ("warm hold not yet wired: no warm_leases state; "
                    f"fallback is the heartbeat-stale recycle at "
                    f"{recovered_after}s (v2 target "
                    f"~{V2_WARM_RECYCLE_TARGET_S:.0f}s).")
        else:
            status = "not-implemented"
            note = (f"warm baton guard did not recycle in target time: "
                    f"recycled after {recovered_after}s (baton timeout "
                    f"{baton}s, v2 target ~{V2_WARM_RECYCLE_TARGET_S:.0f}s).")
        return _result("warm-claim-no-heartbeat", mode, status=status,
                       duplicate_sends=_dupes(turn),
                       silent_drops=_silent_drops(turn, [upd["update_id"]]),
                       time_to_first_signal=None,
                       time_to_reply=None,
                       recovery_time=recovered_after,
                       warm_lease_hits=None,
                       input_tokens_modelled=tokens_for(text),
                       notes=note)
    finally:
        if tmp is not None:
            tmp.cleanup()


def sc_kill_9_mid_send(mode):
    """Kill -9 a worker subprocess mid-send, restart, check recovery.

    The worker (simulation/_kill_worker.py) runs the real intake +
    real send_fenced, writes a "sent" marker once the send is out,
    then sleeps. The parent SIGKILLs it, restarts a harness on the
    SAME state dir, and reconciles from the sender log. Pass bar:
    recovered, exactly one send, no duplicate.
    """
    state_dir = Path(tempfile.mkdtemp(prefix="sim-kill9-"))
    worker = SIM_DIR / "_kill_worker.py"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SIM_DIR) + ":" + str(
        SIM_DIR.parent / "muse-runtime")
    proc = subprocess.Popen(
        [sys.executable, str(worker), "--state-dir", str(state_dir),
         "--owner-id", str(OWNER_ID)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    marker = state_dir / "sent.marker"
    deadline = time.time() + 15
    while not marker.exists() and time.time() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    if not marker.exists():
        proc.kill()
        _, err = proc.communicate()
        return _result("kill-9-mid-send", mode, status="fail",
                       notes=f"worker never reached the send "
                             f"(rc={proc.returncode}): "
                             f"{err.decode()[-500:]}")
    t_kill = time.time()
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=10)
    # Restart: same state dir, fresh harness.
    clock = FakeClock()
    turn, _ = make_turn(state_dir=state_dir, clock=clock)
    turn.sync_sends()
    stuck = turn.items(status="sending")
    if len(stuck) != 1:
        return _result("kill-9-mid-send", mode, status="fail",
                       notes=f"after SIGKILL expected 1 sending item, "
                             f"got {len(stuck)}: "
                             f"{[i['status'] for i in turn.items()]}")
    item = stuck[0]
    reply = "Your total is $42.50."
    kind, detail = turn.reconcile_sending(
        item, item["claim_token"], reply, now=clock.now())
    recovery_wall = round(time.time() - t_kill, 2)
    sends_for_text = [s for s in turn.fake.sends if s["text"] == reply]
    dupes = len(sends_for_text) - 1 if len(sends_for_text) > 1 else 0
    ok = kind == "delivered" and len(sends_for_text) == 1
    return _result("kill-9-mid-send", mode,
                   status="pass" if ok else "fail",
                   duplicate_sends=dupes,
                   silent_drops=0,
                   time_to_first_signal=None,
                   time_to_reply=None,
                   recovery_time=recovery_wall,
                   input_tokens_modelled=tokens_for(
                       "please confirm my order total"),
                   notes=f"SIGKILL mid-send: worker rc={proc.returncode}; "
                         f"restart found item 'sending'; reconcile="
                         f"{kind} (message_id "
                         f"{detail.get('message_id')}); sends of the "
                         f"reply text={len(sends_for_text)}; wall "
                         f"recovery {recovery_wall}s. Recovery time "
                         f"here is wall seconds from kill to reconcile, "
                         f"not simulated time.")


SCENARIOS = {
    "single-dm-reply": sc_single_dm_reply,
    "rapid-back-and-forth-5": sc_rapid_back_and_forth_5,
    "dm-plus-group-concurrent": sc_dm_plus_group_concurrent,
    "dropped-wake": sc_dropped_wake,
    "hang-no-heartbeat": sc_hang_no_heartbeat,
    "die-mid-send": sc_die_mid_send,
    "send-unknown-delivered": sc_send_unknown_delivered,
    "send-unknown-not-delivered": sc_send_unknown_not_delivered,
    "sibling-single-send": sc_sibling_single_send,
    "quick-to-deep-escalation": sc_quick_to_deep_escalation,
    "warm-claim-no-heartbeat": sc_warm_claim_no_heartbeat,
    "kill-9-mid-send": sc_kill_9_mid_send,
}


def run_scenario(name, mode):
    if name not in SCENARIOS:
        raise ValueError(f"unknown scenario: {name!r}")
    try:
        return SCENARIOS[name](mode)
    except Exception as exc:  # never fail hard: report, continue
        return _result(name, mode, status="fail",
                       notes=f"harness error (not a v2 verdict): {exc!r}")


def run_all(mode):
    return [run_scenario(name, mode) for name in SCENARIOS]


def _fmt(value):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def print_table(baseline, v2):
    print("Muse2Telegram v2 simulation — before vs after")
    print("Pass bar (inventory §6): duplicate_sends = 0, silent_drops = 0.")
    print("Times are SIMULATED seconds on the fake clock (no real sleeps);")
    print("kill-9 recovery_time is wall seconds from SIGKILL to reconcile.")
    print("Tokens are modelled chars/4 on inbound text only; provider")
    print("caching is unverified; system prompt/history not modelled.")
    print()
    hdr = (f"{'scenario':<28} {'mode':<9} {'status':<16} {'dup':>3} "
           f"{'drop':>4} {'first_sig':>9} {'reply':>7} {'recov':>7} "
           f"{'warm':>4} {'tok':>5}")
    print(hdr)
    print("-" * len(hdr))
    for b, v in zip(baseline, v2):
        for r in (b, v):
            print(f"{r['scenario']:<28} {r['mode']:<9} {r['status']:<16} "
                  f"{r['duplicate_sends']:>3} {r['silent_drops']:>4} "
                  f"{_fmt(r['time_to_first_signal']):>9} "
                  f"{_fmt(r['time_to_reply']):>7} "
                  f"{_fmt(r['recovery_time']):>7} "
                  f"{_fmt(r['warm_lease_hits']):>4} "
                  f"{r['input_tokens_modelled']:>5}")
    print()
    for r in baseline + v2:
        print(f"[{r['mode']}] {r['scenario']}: {r['status']} — {r['notes']}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Muse2Telegram v2 simulation harness")
    parser.add_argument("--list", action="store_true",
                        help="list scenario names")
    parser.add_argument("--scenario", default="all",
                        help="scenario name, or 'all'")
    parser.add_argument("--mode", choices=["baseline", "v2"], default="v2",
                        help="label + pass bar only; the code under test "
                             "is whatever is in the v2 copy right now")
    parser.add_argument("--report", action="store_true",
                        help="run all scenarios in both modes, print table")
    parser.add_argument("--json", action="store_true",
                        help="print results as JSON")
    args = parser.parse_args(argv)

    if args.list:
        for name in SCENARIOS:
            print(name)
        return 0

    if args.report:
        baseline = run_all("baseline")
        v2 = run_all("v2")
        if args.json:
            print(json.dumps({"baseline": baseline, "v2": v2}, indent=2))
        else:
            print_table(baseline, v2)
        return 0

    if args.scenario == "all":
        results = run_all(args.mode)
    else:
        results = [run_scenario(args.scenario, args.mode)]
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(f"{r['scenario']} [{r['mode']}] {r['status']}: "
                  f"dup={r['duplicate_sends']} drops={r['silent_drops']} "
                  f"first_sig={_fmt(r['time_to_first_signal'])} "
                  f"reply={_fmt(r['time_to_reply'])} "
                  f"recov={_fmt(r['recovery_time'])} "
                  f"warm={_fmt(r['warm_lease_hits'])} "
                  f"tok={r['input_tokens_modelled']}")
            print(f"  {r['notes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
