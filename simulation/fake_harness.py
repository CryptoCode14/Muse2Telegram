"""Fake hook harness + fake Muse turns for the v2 simulation.

The turns drive the REAL v2 code wherever possible:

- Intake/wake: ``bridge_core.telegram.main`` (the real CLI entry
  point) with ``fetch`` bound to ``FakeTelegram.getUpdates`` and
  ``api_call`` bound to ``FakeTelegram.api_call``.
- Queue: ``bridge_core.queue`` functions inside
  ``JsonStateStore`` transactions, on a temp state dir.
- Sends: the real ``bridge_core.telegram.send_and_close`` /
  ``send_batch_and_close`` / ``send_fenced`` paths, with the
  ``TELEGRAM_BRIDGE_SEND_CMD`` env var pointed at a generated fake
  sender script (see :func:`write_fake_sender`). That script is a
  separate subprocess, exactly like the live ``telegram-sender``,
  and records every send to a JSONL log the harness then merges
  back into the FakeTelegram (``reload_from_log``).

What is NOT real here (and is labelled wherever it matters):
the Muse model turn itself (no LLM runs; the reply text is canned),
the hook harness process (``FakeHarness`` just records wake
receipts), and wall-clock time (``FakeClock`` — scenarios advance
it explicitly, so a "180 s recycle" is measured in simulated
seconds, never by sleeping).

No network, no real token, no live state: every run uses
``tempfile.mkdtemp`` unless the caller passes an explicit state dir
(the kill-9 scenario needs a dir shared with its worker subprocess).
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

# The v2 copy's muse-runtime dir holds bridge_core. Resolve it from
# this file's location so the harness works from any cwd.
RUNTIME_DIR = Path(__file__).resolve().parent.parent / "muse-runtime"
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

from bridge_core import queue as queue_core  # noqa: E402
from bridge_core import telegram  # noqa: E402
from bridge_core.store import JsonStateStore  # noqa: E402

try:
    from fake_tg import FakeTelegram as _FakeTelegram
except ImportError:  # imported as simulation.fake_harness
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fake_tg import FakeTelegram as _FakeTelegram

PLATFORM = "telegram"
OWNER_ID = 111

#: Default fake-clock start: real wall time at construction. The
#: real code stamps intake with time.time() (run_once), so a fake
#: clock fixed far from wall time would make every item look
#: ancient (heartbeat-stale / group-TTL expiry firing at once).
#: Scenarios advance from wall time instead.
CLOCK_START = None


class FakeClock:
    """A manually advanced clock; ``now()`` replaces time.time().

    Scenarios advance this instead of sleeping, so recovery times
    (30 s / 180 s / 600 s) are measured in simulated seconds.
    """

    def __init__(self, start=CLOCK_START):
        self._now = float(start) if start is not None else time.time()

    def now(self):
        return self._now

    def advance(self, seconds):
        self._now += float(seconds)
        return self._now


class FakeHarness:
    """Stand-in for Muse's hook harness: records wakes, never runs a model."""

    def __init__(self, clock=None):
        self.clock = clock or FakeClock()
        self.wakes = []      # {"payload","at"} in wake order
        self.silences = []   # {"reason","at"}
        self.logs = []

    def wake(self, payload):
        receipt = {"payload": payload, "at": self.clock.now()}
        self.wakes.append(receipt)
        return "woken"

    def silent(self, reason):
        self.silences.append({"reason": reason, "at": self.clock.now()})
        return "silent"

    def log(self, msg):
        self.logs.append({"msg": msg, "at": self.clock.now()})


# ---------------------------------------------------------------------
# Fake sender subprocess
# ---------------------------------------------------------------------

FAKE_SENDER_SOURCE = '''#!/usr/bin/env python3
"""Fake telegram-sender for the simulation harness.

Contract mirrors bin/telegram-sender (text form only, plus --typing
and --react): argv [chat_id] [--reply-to ID] [--keyboard]; reply
text on stdin; exit 0 with the message_id as the last stdout line.
Every send/typing is appended to $SIM_FAKE_SENDS_LOG (JSONL).

Behaviour is driven by $SIM_FAKE_SENDER_MODE:
  ok     - deliver normally (default)
  fail   - exit 1 without sending (definitive local failure)
  sleep  - sleep $SIM_FAKE_SENDER_SLEEP_SECONDS, then deliver
           (used with a patched SEND_TIMEOUT_SECONDS to model a
           send whose response is lost: the send DID go out, the
           caller reports "unknown")
"""
import json
import os
import sys
import time

log_path = os.environ["SIM_FAKE_SENDS_LOG"]
mode = os.environ.get("SIM_FAKE_SENDER_MODE", "ok")

args = sys.argv[1:]
chat_id = None
reply_to = None
i = 0
while i < len(args):
    a = args[i]
    if a == "--typing":
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "typing",
                                 "chat_id": int(args[i + 1]),
                                 "at": time.time()}) + "\\n")
        sys.exit(0)
    if a == "--react":
        sys.exit(0)
    if a in ("--photo", "--document"):
        chat_id = int(args[i + 1])
        i += 3
        continue
    if a == "--reply-to":
        reply_to = int(args[i + 1])
        i += 2
        continue
    if a == "--keyboard":
        i += 1
        continue
    if chat_id is None:
        chat_id = int(a)
    i += 1

text = sys.stdin.read()
if mode == "fail":
    print("fake sender: simulated local failure", file=sys.stderr)
    sys.exit(1)

# Sequential message_id = 1 + prior sends in the log.
prior = 0
if os.path.exists(log_path):
    with open(log_path, encoding="utf-8") as fh:
        prior = sum(1 for line in fh
                    if line.strip()
                    and json.loads(line).get("kind") == "send")
message_id = prior + 1
with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"kind": "send", "chat_id": chat_id,
                         "text": text, "reply_to": reply_to,
                         "message_id": message_id,
                         "at": time.time()}) + "\\n")
print(message_id, flush=True)
if mode == "sleep":
    # The send already went out (and is in the log) — only the
    # response is lost: the caller times out waiting and reports
    # "unknown". This is the classic lost-response case, not a
    # send that never happened.
    time.sleep(float(os.environ.get("SIM_FAKE_SENDER_SLEEP_SECONDS", "5")))
'''


def write_fake_sender(state_dir):
    """Write the fake sender script into ``state_dir``; return its path."""
    path = Path(state_dir) / "fake-sender.py"
    path.write_text(FAKE_SENDER_SOURCE, encoding="utf-8")
    path.chmod(0o700)
    return path


# ---------------------------------------------------------------------
# Feature detection (so the harness degrades, never fails hard, when
# a v2 feature is not yet implemented by a parallel worker)
# ---------------------------------------------------------------------

def _telegram_source():
    """Source text of bridge_core.telegram (for wiring checks)."""
    try:
        return Path(telegram.__file__).read_text(encoding="utf-8")
    except Exception:
        return ""


def _source_of(func):
    try:
        import inspect
        return inspect.getsource(func)
    except Exception:
        return ""


def detect_features():
    """Probe the imported v2 code for the features scenarios expect.

    Two levels are reported per feature: the machinery exists
    (helper/constant/module present) and it is wired (called from
    the intake / watchdog / send path). A feature can be present
    but unwired — that is the common v2 mid-build state, and
    scenarios report it as "not-implemented" with the measured
    fallback instead of failing. Returns a dict of booleans/values.
    """
    tg_src = _telegram_source()
    feats = {}
    feats["heartbeat_stale_seconds"] = getattr(
        queue_core, "HEARTBEAT_STALE_SECONDS", None)
    # Lane-scoped warm lease (v2 design): state["warm_leases"] and/or
    # queue helpers. Baseline has neither.
    feats["warm_lease"] = all(
        hasattr(queue_core, name)
        for name in ("warm_lease_active", "set_warm_lease",
                     "clear_warm_lease"))
    feats["warm_lease_hold_seconds"] = getattr(
        telegram, "WARM_HOLD_DEFAULT_SECONDS", None)
    feats["warm_lease_wired"] = (
        feats["warm_lease"] and tg_src.count("set_warm_lease(") > 0)
    feats["warm_baton_timeout_seconds"] = getattr(
        queue_core, "WARM_BATON_TIMEOUT_SECONDS", None)
    # Two-phase heartbeat: 180 s never-started recycle (Argon,
    # corrected). Machinery = the constant + _is_recyclable support;
    # wired = run_once / watchdog_sweep pass active_heartbeat_stale.
    feats["two_phase_machinery"] = hasattr(
        queue_core, "NEVER_STARTED_RECYCLE_SECONDS")
    feats["two_phase_seconds"] = getattr(
        queue_core, "NEVER_STARTED_RECYCLE_SECONDS", None)
    feats["two_phase_wired"] = (
        "active_heartbeat_stale=" in _source_of(telegram.run_once)
        or "active_heartbeat_stale=" in _source_of(telegram.watchdog_sweep))
    # Delivery ledger (v2 design): a bridge_core.ledger module or a
    # delivery_obligations table/key.
    try:
        import importlib
        importlib.import_module("bridge_core.delivery_ledger")
        feats["delivery_ledger"] = True
    except Exception:
        feats["delivery_ledger"] = hasattr(
            telegram, "delivery_ledger") or hasattr(
            queue_core, "delivery_ledger")
    feats["delivery_ledger_wired"] = (
        feats["delivery_ledger"]
        and ("record_obligation" in tg_src
             or "DeliveryLedger(" in tg_src))
    # Sibling auto-cover: siblings closed as DONE (not replied) when
    # a batch reply goes out, without an explicit --cover list.
    # (The baseline's explicit --cover via send_batch_and_close is
    # NOT auto-cover; it needs the turn to name the siblings.)
    feats["sibling_auto_cover"] = any(
        hasattr(queue_core, name)
        for name in ("close_siblings_as_done", "auto_cover_siblings",
                     "cover_siblings"))
    feats["sibling_auto_cover"] = feats["sibling_auto_cover"] or any(
        hasattr(telegram, name)
        for name in ("close_siblings_as_done", "auto_cover_siblings"))
    # Edit-probe reconciliation: a helper that probes a message_id
    # with editMessageText to establish delivered / never-sent.
    feats["edit_probe"] = any(
        hasattr(telegram, name)
        for name in ("probe_send", "edit_probe", "reconcile_sending",
                     "probe_delivery"))
    # Tier escalation / re-hydration hint on the wake payload.
    # v2 has no tiers: the signal is pre-hydrated context
    # (context_md, added by the hook layer in decide.py, not by
    # build_payload), so a decide.py carrying it counts.
    _decide_src = ""
    try:
        _decide_src = (
            Path(telegram.__file__).resolve().parent.parent
            / "hooks" / "telegram-bridge-decide.py"
        ).read_text(encoding="utf-8")
    except OSError:
        pass
    feats["tier_escalation_hint"] = (
        "escalat" in _source_of(telegram.build_payload)
        or "rehydrat" in _source_of(telegram.build_payload)
        or "context_md" in _decide_src)
    return feats


FEATURES = detect_features()


# ---------------------------------------------------------------------
# FakeTurn
# ---------------------------------------------------------------------

class FakeTurn:
    """A simulated Muse turn driving the real v2 queue/telegram code.

    ``mode`` selects the turn's behaviour (see :meth:`run`).
    ``reply_text`` is the canned model reply. ``now`` values come
    from the fake clock; pass ``clock=None`` to use real time.
    """

    MODES = ("succeed", "drop-wake", "hang-no-heartbeat", "die-mid-send",
             "send-unknown-delivered", "send-unknown-not-delivered",
             "sibling-single-send")

    def __init__(self, store, fake_tg, harness=None, clock=None,
                 owner_id=OWNER_ID, sender_mode="ok"):
        self.store = store
        self.fake = fake_tg
        self.harness = harness or FakeHarness(clock=clock)
        self.clock = clock or FakeClock()
        self.owner_id = owner_id
        self.sender_mode = sender_mode
        self.state_dir = Path(store.path).parent
        self.sender_path = write_fake_sender(self.state_dir)
        self.sends_log = self.state_dir / "sends.jsonl"
        self.sends_log.touch(exist_ok=True)
        self._adopted = False

    # -- environment ---------------------------------------------------

    @contextmanager
    def env(self):
        """Env vars that point the real code at the fake sender/state.

        Restores the caller's environment on exit. SETTLE_SECONDS=0
        keeps run_once from sleeping; LONG_POLL_SECONDS=0 keeps the
        fetch non-blocking (the fake returns immediately anyway).
        """
        overrides = {
            telegram.SEND_CMD_ENV: str(self.sender_path),
            telegram.RATE_PATH_ENV: str(self.state_dir / "rate.json"),
            telegram.POLL_LOCK_PATH_ENV: str(self.state_dir / "poll.lock"),
            "SIM_FAKE_SENDS_LOG": str(self.sends_log),
            "SIM_FAKE_SENDER_MODE": self.sender_mode,
            telegram.LONG_POLL_SECONDS_ENV: "0",
            telegram.SETTLE_SECONDS_ENV: "0",
        }
        saved = {k: os.environ.get(k) for k in overrides}
        os.environ.update(overrides)
        try:
            yield
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    # -- intake / wake ---------------------------------------------------

    def adopt_owner(self):
        """Run the real --adopt step once per fresh state dir."""
        if self._adopted:
            return
        with self.env():
            out = io.StringIO()
            with redirect_stdout(out):
                telegram.main(["--adopt", str(self.owner_id)],
                              path=self.store.path, api_call=self.fake.api_call)
        self._adopted = True

    def fetch(self, offset):
        return self.fake.getUpdates(offset)

    def wake_once(self):
        """Run the real intake (telegram.main, no argv) and record the wake.

        Returns the wake payload dict, or None when nothing was
        claimable. The harness receipt is recorded either way, so a
        dropped wake is visible as a wake with no turn behind it.
        """
        self.adopt_owner()
        with self.env():
            out = io.StringIO()
            with redirect_stdout(out):
                telegram.main([], fetch=self.fetch, path=self.store.path,
                              api_call=self.fake.api_call)
        text = out.getvalue().strip()
        payload = json.loads(text) if text else None
        if payload is not None:
            self.harness.wake(payload)
        return payload

    def ingest_updates(self, now=None):
        """Fold the fake's pending updates into state via real ingest."""
        now = self.clock.now() if now is None else now
        with self.store.transaction() as state:
            telegram.ingest(state, self.fake.getUpdates(
                state.get("offset", 0)), now=now, api_call=self.fake.api_call)

    # -- queue inspection --------------------------------------------------

    @staticmethod
    def target_id(item):
        """Queue id for an item, payload view or queue record alike.

        Wake-payload items carry ``update_id``; queue records carry
        ``external_id`` (same value, different key). Both are the
        update id the send fence expects.
        """
        return item.get("update_id", item.get("external_id"))

    def load_state(self):
        return self.store.load()

    def items(self, status=None):
        queue = self.load_state().get("queue", [])
        if status is None:
            return queue
        return [i for i in queue if i["status"] == status]

    def sync_sends(self):
        """Merge subprocess sender log records into the FakeTelegram."""
        return self.fake.reload_from_log(self.sends_log)

    # -- send helpers (real send paths) -------------------------------------

    def send_and_close(self, item, token, text, now=None):
        """Real telegram.send_and_close for one claimed item."""
        now = self.clock.now() if now is None else now
        with self.env():
            result = telegram.send_and_close(
                self.store, int(self.target_id(item)), token,
                item["chat_id"], text,
                reply_to=item.get("message_id"), now=now)
        self.sync_sends()
        return result

    def send_batch_and_close(self, primary, token, text, cover=(), now=None):
        """Real telegram.send_batch_and_close (one reply, covers siblings)."""
        now = self.clock.now() if now is None else now
        with self.env():
            result = telegram.send_batch_and_close(
                self.store, int(self.target_id(primary)), token,
                primary["chat_id"], text,
                reply_to=primary.get("message_id"),
                extra_targets=[int(self.target_id(c)) for c in cover],
                now=now)
        self.sync_sends()
        return result

    def send_fenced_only(self, item, token, text, now=None):
        """Real telegram.send_fenced: reserve + send, no reply record.

        This is the die-mid-send primitive: after it returns, the
        item sits in "sending" with the send already out. A turn
        that dies here never records the reply.
        """
        now = self.clock.now() if now is None else now
        with self.env():
            result = telegram.send_fenced(
                self.store, int(self.target_id(item)), token,
                item["chat_id"], text,
                reply_to=item.get("message_id"), now=now)
        self.sync_sends()
        return result

    def heartbeat(self, token, now=None):
        now = self.clock.now() if now is None else now
        with self.env():
            return telegram.heartbeat_batch(self.store, token, now=now)

    def watchdog(self, now=None):
        """Real telegram.watchdog_sweep at a fake-clock time.

        This is the WIRED path (whatever run_once / the watchdog
        actually pass), not the queue function with hand-picked
        parameters — so it measures what the deployed code does.
        """
        now = self.clock.now() if now is None else now
        with self.env():
            return telegram.watchdog_sweep(self.store, now=now)

    def stale_claims_probe(self, now=None, **kwargs):
        """Read-only: what queue.stale_claims would recycle right now.

        Used to probe UNWIRED machinery (e.g. the two-phase 180 s
        rule) without changing what the wired watchdog does.
        """
        now = self.clock.now() if now is None else now
        state = self.load_state()
        return queue_core.stale_claims(state, now=now, **kwargs)

    def flag_warm_claim(self, token, now=None):
        """Mark the batch's claimed items as a warm claim (test-only).

        Mirrors what a warm-hold caller does after claim_lane_batch:
        ``warm_claimed=True`` with ``turn_started_at`` left None, so
        the warm-baton guard can tell this claim's turn never
        started. No in-tree caller sets this yet (warm hold not
        wired end-to-end); scenarios that need a warm claim set it
        explicitly and say so.
        """
        now = self.clock.now() if now is None else now
        with self.store.transaction() as state:
            flagged = 0
            for item in state.get("queue", []):
                if (item["status"] == queue_core.CLAIMED
                        and item.get("claim_token") == token):
                    item["warm_claimed"] = True
                    item["turn_started_at"] = None
                    item["heartbeat_at"] = now
                    flagged += 1
            return flagged

    def warm_lease_active(self, chat_id, now=None):
        """Real queue.warm_lease_active for a lane (False when unwired)."""
        now = self.clock.now() if now is None else now
        return queue_core.warm_lease_active(
            self.load_state(), chat_id, now=now)

    # -- reconciliation -------------------------------------------------------

    def probe_delivery(self, chat_id, message_id, text):
        """Edit-probe: does message_id exist, and does it carry text?

        Returns "delivered" (edit returns "not modified": the exact
        text is already there), "not-sent" (edit returns "not
        found"), or "different-text" (the id exists with other
        text — evidence the slot was used by something else).
        A v2 ``probe_send`` helper is used when one exists.
        """
        helper = getattr(telegram, "probe_send", None) or getattr(
            telegram, "probe_delivery", None)
        if helper is not None:
            try:
                return helper(self.fake, chat_id, message_id, text)
            except Exception:
                pass
        result = self.fake.editMessageText(chat_id, message_id, text)
        if result.get("ok"):
            return "different-text"
        desc = result.get("description", "")
        if "not modified" in desc:
            return "delivered"
        if "not found" in desc:
            return "not-sent"
        return f"unknown:{desc}"

    def reconcile_sending(self, item, token, reply_text, now=None):
        """Reconcile one item stuck in "sending" using send-log evidence.

        If the fake sender log / FakeTelegram recorded a send for
        this chat with the reply text, the send DID go out: record
        the reply and close with evidence (the message_id). If no
        send was recorded, return the item to pending via the real
        mark_send_failed path so it can be re-claimed and re-sent.
        Returns ("delivered"|"not-delivered", detail).
        """
        now = self.clock.now() if now is None else now
        self.sync_sends()
        send = self.fake.find_send(item["chat_id"], reply_text)
        if send is not None:
            with self.store.transaction() as state:
                queue_core.mark_replied(
                    state, PLATFORM, self.target_id(item),
                    send["message_id"], token, now=now)
                closed = queue_core.mark_done(
                    state, PLATFORM, self.target_id(item), token, now=now,
                    reason="reconciled-delivered",
                    evidence=f"fake-tg send recorded message_id "
                             f"{send['message_id']}")
            return "delivered", {"message_id": send["message_id"],
                                 "closed": closed}
        with self.store.transaction() as state:
            ok = queue_core.mark_send_failed(
                state, PLATFORM, self.target_id(item), token, now=now)
        return "not-delivered", {"returned_to_pending": ok}

    # -- the turn ---------------------------------------------------------------

    def run(self, mode, payload, reply_text, now=None):
        """Simulate one Muse turn in ``mode`` over a wake ``payload``.

        Returns a dict describing what the turn did (sends made,
        heartbeats, items closed, outcome). The fake clock is
        advanced by the caller (scenarios), never slept on here,
        except for the explicit simulated work durations below,
        which are clock advances, not real sleeps.
        """
        if mode not in self.MODES:
            raise ValueError(f"unknown FakeTurn mode: {mode!r}")
        now = self.clock.now() if now is None else now
        token = payload["claim_token"]
        items = payload["items"]
        chat_id = payload["chat_id"]
        outcome = {"mode": mode, "items": len(items), "sends": 0,
                   "heartbeats": 0, "closed": 0, "notes": []}

        if mode == "drop-wake":
            # The wake fired, the turn never started: no heartbeat,
            # no send, no state change at all.
            self.harness.silent("drop-wake: turn never started")
            outcome["notes"].append("wake receipt recorded, turn did nothing")
            return outcome

        if mode == "hang-no-heartbeat":
            # Claims but never heartbeats: the heartbeat recorded at
            # claim time goes stale, and the watchdog recycles it.
            outcome["notes"].append(
                "claimed, never heartbeated; awaiting watchdog recycle")
            return outcome

        # Every other mode heartbeats first (a live turn proves liveness).
        hb = self.heartbeat(token, now=now)
        outcome["heartbeats"] = hb.get("heartbeated", 0)

        if mode == "succeed":
            # Typing first (first visible signal), then one reply
            # closing the whole batch via --cover semantics.
            self.fake.sendChatAction(chat_id, "typing")
            first = items[0]
            cover = items[1:]
            result = self.send_batch_and_close(
                first, token, reply_text, cover=cover, now=now)
            outcome["sends"] = 1 if result.get("ok") else 0
            outcome["result"] = result
            outcome["closed"] = len(items) if result.get("ok") else 0
            return outcome

        if mode == "die-mid-send":
            result = self.send_fenced_only(items[0], token, reply_text, now=now)
            outcome["sends"] = 1 if result.get("ok") else 0
            outcome["result"] = result
            outcome["notes"].append(
                "process died after send_fenced, before reply record: "
                "item left in 'sending'")
            return outcome

        if mode == "send-unknown-delivered":
            # Sender subprocess reports unknown (exit != 0 / timeout)
            # but the send DID go out: use the sleep-mode sender so
            # the log records the send, with a patched timeout so
            # the caller still reports "unknown".
            return self._run_send_unknown(items[0], token, reply_text,
                                          outcome, now, delivered=True)

        if mode == "send-unknown-not-delivered":
            return self._run_send_unknown(items[0], token, reply_text,
                                          outcome, now, delivered=False)

        if mode == "sibling-single-send":
            # Sends only the first item of the batch, as a turn that
            # never uses --cover would. Siblings are left claimed.
            self.fake.sendChatAction(chat_id, "typing")
            result = self.send_and_close(items[0], token, reply_text, now=now)
            outcome["sends"] = 1 if result.get("ok") else 0
            outcome["result"] = result
            outcome["closed"] = 1 if result.get("ok") else 0
            outcome["siblings_left"] = len(items) - 1
            outcome["notes"].append(
                "only the first item was answered; siblings left claimed")
            return outcome

        raise AssertionError(f"unhandled mode {mode!r}")

    def _run_send_unknown(self, item, token, reply_text, outcome, now,
                          delivered):
        """Model a sender that reports "unknown".

        delivered=True: the fake sender sleeps past a patched
        SEND_TIMEOUT_SECONDS, so send_fenced reports "unknown" even
        though the send landed in the log (the classic lost-response
        case). delivered=False: the sender exits 1 having sent
        nothing, so "unknown" coincides with no delivery.
        """
        if delivered:
            old_timeout = telegram.SEND_TIMEOUT_SECONDS
            telegram.SEND_TIMEOUT_SECONDS = 1.0
            old_mode = self.sender_mode
            self.sender_mode = "sleep"
            os.environ["SIM_FAKE_SENDER_SLEEP_SECONDS"] = "3"
            try:
                with self.env():
                    result = telegram.send_fenced(
                        self.store, int(self.target_id(item)), token,
                        item["chat_id"], reply_text,
                        reply_to=item.get("message_id"), now=now)
            finally:
                telegram.SEND_TIMEOUT_SECONDS = old_timeout
                self.sender_mode = old_mode
                os.environ.pop("SIM_FAKE_SENDER_SLEEP_SECONDS", None)
            self.sync_sends()
            outcome["result"] = result
            outcome["sends"] = 1  # the log proves the send went out
            outcome["reported_outcome"] = "unknown"
            outcome["notes"].append(
                "sender timed out (patched 1s budget) after the send "
                "landed: caller sees 'unknown', log proves delivery")
        else:
            old_mode = self.sender_mode
            self.sender_mode = "fail"
            try:
                with self.env():
                    result = telegram.send_fenced(
                        self.store, int(self.target_id(item)), token,
                        item["chat_id"], reply_text,
                        reply_to=item.get("message_id"), now=now)
            finally:
                self.sender_mode = old_mode
            self.sync_sends()
            outcome["result"] = result
            outcome["sends"] = 0
            outcome["reported_outcome"] = "unknown"
            outcome["notes"].append(
                "sender exited 1 having sent nothing: caller sees "
                "'unknown', log proves non-delivery")
        return outcome


def make_turn(state_dir=None, fake_tg=None, clock=None, owner_id=OWNER_ID,
              sender_mode="ok"):
    """Build a FakeTurn on a fresh temp state dir (or ``state_dir``).

    Returns (turn, tmpdir-or-None). When state_dir is None the
    caller owns cleanup via the returned TemporaryDirectory.
    """
    import tempfile
    tmpdir = None
    if state_dir is None:
        tmpdir = tempfile.TemporaryDirectory()
        state_dir = Path(tmpdir.name)
    else:
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
    fake = fake_tg or _FakeTelegram()
    store = JsonStateStore(state_dir / "state.json")
    turn = FakeTurn(store, fake, clock=clock or FakeClock(),
                    owner_id=owner_id, sender_mode=sender_mode)
    return turn, tmpdir
