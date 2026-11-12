"""Offline tests for the P1 script-level latency package.

Covers (see latency-synthesis-20261112.md, "Build order" P1):
- Batch-first intake: one wake per batch, not per message.
- Ack as a live status card: the intake sends one card at claim
  (with the queue position); the sweeper EDITS that same message
  (never a second card, never a resend) to keep it current.
- Claim lease 60-120s + script sweeper: the sweeper releases
  never-started claims past the lease (120s), re-queues with an
  attempt counter, and escalates at K=3; the 600s heartbeat
  horizon for started turns is untouched.
- No pre-wake typing (see test_v2_unified_prompt.py).
- Long poll stays 30s: the 10s test was not run live (no live
  load/rate measurement is possible from this sandbox), so the
  default is pinned, not lowered on assumption.
- Agent-first reaction: --react-batch is the agent's first tool
  call; no script ever reacts.

No network or credentials are used: the sender subprocess is
mocked, and the telegram-sender script is loaded as a module
with its _post mocked.
"""
import io
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge_core import queue as queue_core  # noqa: E402
from bridge_core import telegram  # noqa: E402
from bridge_core.store import JsonStateStore  # noqa: E402

OWNER = 1658762691
CHAT = 123456789


def run_cli(path, argv, fetch=None, extra_env=None):
    """Drive the adapter CLI with a temp rate file (never the live
    one) and a fixture bot username. Returns (exit code, parsed
    JSON output)."""
    out = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = out
    rate_path = Path(path).parent / "rate.json"
    env = {
        telegram.RATE_PATH_ENV: str(rate_path),
        telegram.POLL_LOCK_PATH_ENV: str(Path(path).parent / "poll.lock"),
        telegram.BOT_USERNAME_ENV: "ExampleBot",
    }
    if extra_env:
        env.update(extra_env)
    try:
        with mock.patch.dict(os.environ, env):
            code = telegram.main(argv, fetch=fetch, path=path)
    finally:
        sys.stdout = old_stdout
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else None)


def msg_update(uid, chat_id, chat_type, sender_id, text, message_id=None):
    return {
        "update_id": uid,
        "message": {
            "message_id": uid if message_id is None else message_id,
            "from": {"id": sender_id, "is_bot": False},
            "chat": {"id": chat_id, "type": chat_type},
            "text": text,
            "date": 1700000000 + uid,
        },
    }


class FakeFetch:
    """Returns one canned getUpdates response until replaced."""

    def __init__(self, updates=()):
        self.updates = list(updates)

    def __call__(self, offset):
        return {"ok": True, "result": list(self.updates)}


def _claim(state, external_id=101, chat_id=CHAT, at=1000.0,
           message_id=55, text="hello"):
    queue_core.enqueue(
        state, platform="telegram", external_id=external_id,
        chat_id=chat_id, chat_type="private", sender_id=OWNER,
        text=text, message_id=message_id, now=at,
    )
    return queue_core.claim_batch(state, platform="telegram", now=at)


class ClaimLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.store = JsonStateStore(self.path)

    def test_lease_releases_never_started_claim(self):
        with self.store.transaction() as state:
            batch = _claim(state)
            token = batch["claim_token"]
        with self.store.transaction() as state:
            out = queue_core.sweep_claim_lease(state, now=1000.0 + 121.0)
        self.assertEqual([i["external_id"] for i in out["expired"]], ["101"])
        self.assertEqual(out["escalated"], [])
        item = queue_core.find_item(self.store.load(), "telegram", 101)
        self.assertEqual(item["status"], "pending")
        self.assertIsNone(item["claim_token"])
        self.assertIsNone(item["claimed_at"])
        self.assertIsNone(item["turn_started_at"])
        self.assertIsNone(item["wake_issued_at"])
        self.assertEqual(item["lease_attempts"], 1)
        self.assertEqual(item["recycle_count"], 1)
        # The old token is dead: a late transition is refused.
        with self.store.transaction() as state:
            self.assertFalse(
                queue_core.heartbeat(state, "telegram", 101, token, now=1130.0))

    def test_lease_does_not_fire_before_120s(self):
        with self.store.transaction() as state:
            _claim(state)
            out = queue_core.sweep_claim_lease(state, now=1000.0 + 119.0)
        self.assertEqual(out, {"expired": [], "escalated": []})
        item = queue_core.find_item(self.store.load(), "telegram", 101)
        self.assertEqual(item["status"], "claimed")

    def test_lease_never_releases_started_claim(self):
        with self.store.transaction() as state:
            batch = _claim(state)
            queue_core.heartbeat(
                state, "telegram", 101, batch["claim_token"], now=1001.0)
            out = queue_core.sweep_claim_lease(state, now=2000.0)
        self.assertEqual(out, {"expired": [], "escalated": []})
        item = queue_core.find_item(self.store.load(), "telegram", 101)
        self.assertEqual(item["status"], "claimed")

    def test_lease_never_releases_warm_claim(self):
        # A warm claim's turn is the warm hold's: the 120s baton
        # guards it, not the lease.
        with self.store.transaction() as state:
            batch = _claim(state)
            item = queue_core.find_item(state, "telegram", 101)
            item["warm_claimed"] = True
            out = queue_core.sweep_claim_lease(state, now=2000.0)
        self.assertEqual(out, {"expired": [], "escalated": []})
        item = queue_core.find_item(self.store.load(), "telegram", 101)
        self.assertEqual(item["status"], "claimed")

    def test_lease_never_releases_sending_or_replied(self):
        with self.store.transaction() as state:
            batch = _claim(state)
            queue_core.mark_sending(
                state, "telegram", 101, batch["claim_token"], now=1010.0)
            out = queue_core.sweep_claim_lease(state, now=2000.0)
        self.assertEqual(out, {"expired": [], "escalated": []})
        with self.store.transaction() as state:
            batch = _claim(state, external_id=102, chat_id=222,
                           at=1000.0, message_id=66)
            queue_core.mark_replied(
                state, "telegram", 102, "999", batch["claim_token"], now=1001.0)
            out = queue_core.sweep_claim_lease(state, now=2000.0)
        self.assertEqual(out, {"expired": [], "escalated": []})

    def test_three_expiries_escalate_and_stop_rewaking(self):
        # Attempt 1: released, re-claimed.
        with self.store.transaction() as state:
            _claim(state)
            out = queue_core.sweep_claim_lease(state, now=1121.0)
        self.assertEqual(len(out["expired"]), 1)
        with self.store.transaction() as state:
            batch = queue_core.claim_batch(state, platform="telegram", now=1125.0)
        self.assertIsNotNone(batch)
        # Attempt 2: released, re-claimed.
        with self.store.transaction() as state:
            out = queue_core.sweep_claim_lease(state, now=1246.0)
        self.assertEqual(len(out["expired"]), 1)
        with self.store.transaction() as state:
            batch = queue_core.claim_batch(state, platform="telegram", now=1250.0)
        self.assertIsNotNone(batch)
        # Attempt 3: escalated, not merely released.
        with self.store.transaction() as state:
            out = queue_core.sweep_claim_lease(state, now=1371.0)
        self.assertEqual(out["expired"], [])
        self.assertEqual(
            [i["external_id"] for i in out["escalated"]], ["101"])
        item = queue_core.find_item(self.store.load(), "telegram", 101)
        self.assertEqual(item["status"], "pending")
        self.assertTrue(item["lease_escalated"])
        self.assertEqual(item["lease_attempts"], 3)
        self.assertEqual(item["wake_after"], 1371.0 + 86400.0)
        # The intake stops re-waking it while the backoff runs.
        with self.store.transaction() as state:
            self.assertIsNone(
                queue_core.claim_batch(state, platform="telegram", now=1400.0))

    def test_watchdog_reports_lease_once_and_merges_recycled(self):
        with self.store.transaction() as state:
            _claim(state)
            report = queue_core.watchdog_sweep(state, now=1121.0)
        self.assertEqual(report["lease_expired"], ["101"])
        self.assertEqual(report["lease_escalated"], [])
        self.assertEqual(report["recycled"], ["101"])
        self.assertEqual(report["pending"], 1)

    def test_escalated_item_requeues_after_backoff_with_fresh_attempts(self):
        with self.store.transaction() as state:
            _claim(state)
            for at in (1121.0, 1246.0, 1371.0):
                queue_core.sweep_claim_lease(state, now=at)
                if at < 1371.0:
                    queue_core.claim_batch(state, platform="telegram", now=at + 4.0)
        item = queue_core.find_item(self.store.load(), "telegram", 101)
        self.assertTrue(item["lease_escalated"])
        # After the 24 h backoff the intake re-claims it, and the
        # counter resets: a fresh episode, not a fourth strike.
        with self.store.transaction() as state:
            batch = queue_core.claim_batch(
                state, platform="telegram", now=1371.0 + 86400.0 + 1.0)
        self.assertIsNotNone(batch)
        item = queue_core.find_item(self.store.load(), "telegram", 101)
        self.assertFalse(item["lease_escalated"])
        self.assertEqual(item["lease_attempts"], 0)

    def test_clear_lease_cli_requeues_at_once(self):
        with self.store.transaction() as state:
            _claim(state)
            for at in (1121.0, 1246.0, 1371.0):
                queue_core.sweep_claim_lease(state, now=at)
                if at < 1371.0:
                    queue_core.claim_batch(state, platform="telegram", now=at + 4.0)
        code, result = run_cli(self.path, ["--clear-lease", "101"])
        self.assertEqual(code, 0)
        self.assertEqual(result, {"ok": True})
        item = queue_core.find_item(self.store.load(), "telegram", 101)
        self.assertFalse(item["lease_escalated"])
        self.assertIsNone(item["wake_after"])
        self.assertEqual(item["lease_attempts"], 0)
        with self.store.transaction() as state:
            self.assertIsNotNone(
                queue_core.claim_batch(state, platform="telegram", now=1400.0))
        # Clearing a non-escalated item is a no-op false.
        code, result = run_cli(self.path, ["--clear-lease", "101"])
        self.assertEqual(result, {"ok": False})

    def test_600s_heartbeat_horizon_is_untouched(self):
        # The lease is sweeper-only: the claim paths never
        # release on lease. A never-started claim at +200s is
        # still in flight for claim_batch (lane busy), and at
        # +700s the 600s heartbeat rule recycles it — exactly as
        # before P1.
        with self.store.transaction() as state:
            batch = _claim(state)
            token = batch["claim_token"]
        with self.store.transaction() as state:
            self.assertIsNone(
                queue_core.claim_batch(state, platform="telegram", now=1200.0))
        with self.store.transaction() as state:
            batch = queue_core.claim_batch(state, platform="telegram", now=1700.0)
        self.assertIsNotNone(batch)
        self.assertNotEqual(batch["claim_token"], token)

    def test_lease_constants_are_in_range(self):
        # Synthesis: lease 60-120s, K attempts. 120s is the top
        # of the range (a live turn heartbeats first thing even
        # when reading MEMORY first); K=3 is ~6 min of
        # never-started before escalation.
        self.assertEqual(queue_core.CLAIM_LEASE_SECONDS, 120.0)
        self.assertGreaterEqual(queue_core.CLAIM_LEASE_SECONDS, 60.0)
        self.assertLessEqual(queue_core.CLAIM_LEASE_SECONDS, 120.0)
        self.assertEqual(queue_core.CLAIM_LEASE_MAX_ATTEMPTS, 3)


class BatchFirstTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.store = JsonStateStore(self.path)

    def test_claim_batch_takes_burst_under_one_token(self):
        with self.store.transaction() as state:
            for i in range(5):
                queue_core.enqueue(
                    state, platform="telegram", external_id=100 + i,
                    chat_id=CHAT, chat_type="private", sender_id=OWNER,
                    text=f"message {i}", message_id=50 + i, now=1000.0 + i,
                )
            batch = queue_core.claim_batch(state, platform="telegram", now=1010.0)
        self.assertIsNotNone(batch)
        self.assertEqual(len(batch["items"]), 5)
        tokens = {item["claim_token"] for item in batch["items"]}
        self.assertEqual(len(tokens), 1)

    def test_run_once_returns_one_payload_for_burst(self):
        # One wake per batch: a burst of 5 in one lane costs one
        # lane wait, not five. The payload is the wake — one
        # payload with one token and all five items.
        run_cli(self.path, ["--adopt", str(OWNER)])
        fetch = FakeFetch([
            msg_update(1 + i, CHAT, "private", OWNER, f"message {i}")
            for i in range(5)
        ])
        code, payload = run_cli(
            self.path, [], fetch=fetch,
            extra_env={"TELEGRAM_BRIDGE_SETTLE_SECONDS": "0"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 5)
        self.assertEqual(len(payload["items"]), 5)
        self.assertTrue(payload["claim_token"])
        state = JsonStateStore(self.path).load()
        claimed = [i for i in state["queue"] if i["status"] == "claimed"]
        self.assertEqual(len(claimed), 5)
        self.assertEqual(len({i["claim_token"] for i in claimed}), 1)

    def test_run_once_claims_one_lane_per_poll(self):
        # Two lanes in one fetch: the first lane's batch is
        # claimed and woken; the second lane waits for the next
        # poll — still one wake per batch, never one per message.
        run_cli(self.path, ["--adopt", str(OWNER)])
        fetch = FakeFetch([
            msg_update(1 + i, CHAT, "private", OWNER, f"lane A {i}")
            for i in range(3)
        ] + [
            msg_update(10 + i, 222, "private", OWNER, f"lane B {i}")
            for i in range(3)
        ])
        code, payload = run_cli(
            self.path, [], fetch=fetch,
            extra_env={"TELEGRAM_BRIDGE_SETTLE_SECONDS": "0"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 3)
        state = JsonStateStore(self.path).load()
        pending = [i for i in state["queue"] if i["status"] == "pending"]
        self.assertEqual(len(pending), 3)
        self.assertTrue(all(i["chat_id"] == 222 for i in pending))


class ClaimAckCardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.store = JsonStateStore(self.path)
        with self.store.transaction() as state:
            batch = _claim(state)
            self.token = batch["claim_token"]

    def item(self):
        return queue_core.find_item(self.store.load(), "telegram", 101)

    def claim_ack(self, sender_result=("sent", "777"), token=None):
        if token is None:
            token = self.token
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_send_command", return_value=sender_result
            ) as sender:
                result = telegram.send_claim_ack(self.store, token)
        return result, sender

    def test_claim_ack_sends_card_with_position_and_records_it(self):
        result, sender = self.claim_ack()
        self.assertEqual(result, {
            "ok": True, "message_id": "777", "queue_position": 1,
        })
        sender.assert_called_once_with(
            "/fake/sender", CHAT, telegram.claim_ack_text(1), reply_to=55
        )
        item = self.item()
        self.assertEqual(item["status"], "claimed")
        self.assertEqual(item["ack_message_id"], "777")
        self.assertEqual(item["ack_card_text"], telegram.claim_ack_text(1))
        self.assertIsNotNone(item["ack_sent_at"])
        self.assertIsNone(item["replied_at"])

    def test_claim_ack_does_not_start_the_turn(self):
        # The hinge: the card is a receipt, not proof a turn is
        # alive. If it set turn_started_at, the lease could never
        # fire on a carded batch — every batch is carded.
        result, _sender = self.claim_ack()
        self.assertTrue(result["ok"])
        item = self.item()
        self.assertIsNone(item["turn_started_at"])
        self.assertEqual(item["heartbeat_at"], 1000.0)
        with self.store.transaction() as state:
            report = queue_core.watchdog_sweep(state, now=1121.0)
        self.assertEqual(report["lease_expired"], ["101"])
        self.assertEqual(self.item()["status"], "pending")

    def test_second_claim_ack_is_refused_and_sender_not_called(self):
        first, _sender = self.claim_ack()
        self.assertTrue(first["ok"])
        second, sender = self.claim_ack()
        self.assertEqual(second, {"ok": False, "stage": "already-acked"})
        sender.assert_not_called()
        self.assertEqual(self.item()["ack_message_id"], "777")

    def test_claim_ack_unknown_clears_reservation_for_retry(self):
        result, _sender = self.claim_ack(sender_result=("unknown", None))
        self.assertEqual(result, {"ok": False, "stage": "unknown"})
        self.assertIsNone(self.item()["ack_sent_at"])
        self.assertIsNone(self.item()["ack_message_id"])
        result, _sender = self.claim_ack(sender_result=("sent", "778"))
        self.assertTrue(result["ok"])
        self.assertEqual(self.item()["ack_message_id"], "778")

    def test_claim_ack_stale_token_and_config(self):
        result, sender = self.claim_ack(token="deadbeefdeadbeef")
        self.assertEqual(result, {"ok": False, "stage": "stale-token"})
        sender.assert_not_called()
        with mock.patch.dict(os.environ, {
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            os.environ.pop(telegram.SEND_CMD_ENV, None)
            result = telegram.send_claim_ack(self.store, self.token)
        self.assertEqual(result, {"ok": False, "stage": "config"})

    def test_claim_ack_position_counts_batches_ahead(self):
        # Lane A claims first; lane B's card says #2.
        with self.store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id=201,
                chat_id=222, chat_type="private", sender_id=OWNER,
                text="lane B", message_id=66, now=1001.0,
            )
            batch_b = queue_core.claim_batch(
                state, platform="telegram", now=1001.0)
        result, _sender = self.claim_ack(token=batch_b["claim_token"])
        self.assertEqual(result["queue_position"], 2)
        self.assertIn("#2 in the queue", telegram.claim_ack_text(2))

    def test_claim_ack_cli(self):
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_send_command", return_value=("sent", "777")
            ):
                code, result = run_cli(
                    self.path, ["--send-claim-ack", self.token])
        self.assertEqual(code, 0)
        self.assertEqual(result, {
            "ok": True, "message_id": "777", "queue_position": 1,
        })
        code, result = run_cli(self.path, ["--send-claim-ack"])
        self.assertEqual(code, 2)
        self.assertFalse(result["ok"])

    def test_sweeper_edits_card_on_lease_expiry_never_resends(self):
        result, send_mock = self.claim_ack()
        self.assertTrue(result["ok"])
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_sender_action", return_value=True
            ) as edit_mock:
                report = telegram.watchdog_sweep(self.store, now=1121.0)
        self.assertEqual(report["lease_expired"], ["101"])
        self.assertEqual(report["ack_cards"]["edited"], 1)
        # The SAME message id, edited — not a second card sent.
        edit_mock.assert_called_once_with(
            "/fake/sender", ["--edit", CHAT, 777],
            stdin_text=telegram.CLAIM_ACK_DELAYED_TEXT,
        )
        send_mock.assert_called_once()
        self.assertEqual(self.item()["status"], "pending")
        self.assertEqual(
            self.item()["ack_card_text"], telegram.CLAIM_ACK_DELAYED_TEXT)

    def test_sweeper_escalates_card_text_at_k_attempts(self):
        _result, _send = self.claim_ack()
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_sender_action", return_value=True
            ) as edit_mock:
                telegram.watchdog_sweep(self.store, now=1121.0)
                with self.store.transaction() as state:
                    queue_core.claim_batch(
                        state, platform="telegram", now=1125.0)
                telegram.watchdog_sweep(self.store, now=1246.0)
                with self.store.transaction() as state:
                    queue_core.claim_batch(
                        state, platform="telegram", now=1250.0)
                report = telegram.watchdog_sweep(self.store, now=1371.0)
        self.assertEqual(report["lease_escalated"], ["101"])
        self.assertTrue(self.item()["lease_escalated"])
        self.assertEqual(
            self.item()["ack_card_text"], telegram.CLAIM_ACK_ESCALATED_TEXT)
        # Two edits total: delayed on the first expiry, escalated
        # on the third (the second expiry's text was already
        # "delayed", so no identical-text edit was attempted).
        self.assertEqual(edit_mock.call_count, 2)
        self.assertEqual(
            edit_mock.call_args_list[-1].kwargs["stdin_text"],
            telegram.CLAIM_ACK_ESCALATED_TEXT,
        )

    def test_sweeper_refreshes_position_when_batches_ahead_clear(self):
        with self.store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id=201,
                chat_id=222, chat_type="private", sender_id=OWNER,
                text="lane B", message_id=66, now=1001.0,
            )
            batch_b = queue_core.claim_batch(
                state, platform="telegram", now=1001.0)
            token_b = batch_b["claim_token"]
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_send_command", return_value=("sent", "778")
            ):
                result = telegram.send_claim_ack(self.store, token_b)
        self.assertEqual(result["queue_position"], 2)
        # Lane A closes: lane B is first now, and the next sweep
        # edits its card to say so.
        with self.store.transaction() as state:
            queue_core.mark_done(state, "telegram", 101, self.token)
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_sender_action", return_value=True
            ) as edit_mock:
                telegram.watchdog_sweep(self.store, now=1010.0)
        edit_mock.assert_called_once_with(
            "/fake/sender", ["--edit", 222, 778],
            stdin_text=telegram.claim_ack_text(1),
        )

    def test_sweeper_never_edits_a_turn_ack(self):
        # A turn's own --send-ack records no card text, so the
        # sweeper leaves it alone: only script claim cards are
        # ever edited.
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_send_command", return_value=("sent", "777")
            ):
                result = telegram.send_ack(
                    self.store, 101, self.token, CHAT, reply_to=55)
        self.assertTrue(result["ok"])
        self.assertIsNone(self.item().get("ack_card_text"))
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_sender_action", return_value=True
            ) as edit_mock:
                telegram.watchdog_sweep(self.store, now=1010.0)
        edit_mock.assert_not_called()

    def test_sweeper_does_not_reedit_unchanged_card(self):
        _result, _send = self.claim_ack()
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_sender_action", return_value=True
            ) as edit_mock:
                telegram.watchdog_sweep(self.store, now=1010.0)
                telegram.watchdog_sweep(self.store, now=1020.0)
        # No lease expiry, no position change: nothing to edit,
        # and an identical-text edit would be a Telegram 400.
        edit_mock.assert_not_called()

    def test_latency_report_counts_lease_escalations(self):
        with self.store.transaction() as state:
            for at in (1121.0, 1246.0, 1371.0):
                queue_core.sweep_claim_lease(state, now=at)
                if at < 1371.0:
                    queue_core.claim_batch(state, platform="telegram", now=at + 4.0)
        code, report = run_cli(self.path, ["--latency-report"])
        self.assertEqual(code, 0)
        self.assertEqual(report["recovery"]["lease_escalated_now"], 1)


class IntakeAckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.store = JsonStateStore(self.path)

    def enqueue_pending(self, external_id=101, chat_id=CHAT, at=1000.0,
                        message_id=55, text="hello"):
        with self.store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id=external_id,
                chat_id=chat_id, chat_type="private", sender_id=OWNER,
                text=text, message_id=message_id, now=at,
            )

    def item(self, external_id=101):
        return queue_core.find_item(self.store.load(), "telegram", external_id)

    def intake_ack(self, external_id=101, sender_result=("sent", "777")):
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_send_command", return_value=sender_result
            ) as sender:
                result = telegram.send_intake_ack(self.store, external_id)
        return result, sender

    def test_intake_ack_sends_card_with_lane_position_and_records_it(self):
        self.enqueue_pending()
        result, sender = self.intake_ack()
        self.assertEqual(result, {
            "ok": True, "message_id": "777", "queue_position": 1,
        })
        sender.assert_called_once_with(
            "/fake/sender", CHAT, telegram.claim_ack_text(1), reply_to=55
        )
        item = self.item()
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["ack_message_id"], "777")
        self.assertEqual(item["ack_card_text"], telegram.claim_ack_text(1))
        self.assertIsNotNone(item["ack_sent_at"])
        self.assertIsNone(item["replied_at"])

    def test_intake_ack_does_not_start_the_turn(self):
        # The hinge, same as the claim card: the card is a
        # receipt, not proof a turn is alive. If it set
        # turn_started_at or refreshed the heartbeat, a carded
        # pending item would look started and the claim lease
        # could never fire on it.
        self.enqueue_pending()
        result, _sender = self.intake_ack()
        self.assertTrue(result["ok"])
        item = self.item()
        self.assertIsNone(item["turn_started_at"])
        # A pending item has no heartbeat yet, and the card must
        # not create one: the heartbeat is the turn's proof of
        # life, not the card's.
        self.assertIsNone(item["heartbeat_at"])
        self.assertEqual(item["status"], "pending")

    def test_second_intake_ack_is_refused_and_sender_not_called(self):
        self.enqueue_pending()
        first, _sender = self.intake_ack()
        self.assertTrue(first["ok"])
        second, sender = self.intake_ack()
        self.assertEqual(second, {"ok": False, "stage": "already-acked"})
        sender.assert_not_called()
        self.assertEqual(self.item()["ack_message_id"], "777")

    def test_intake_ack_unknown_and_rejected_clear_reservation(self):
        self.enqueue_pending()
        result, _sender = self.intake_ack(sender_result=("unknown", None))
        self.assertEqual(result, {"ok": False, "stage": "unknown"})
        self.assertIsNone(self.item()["ack_sent_at"])
        self.assertIsNone(self.item()["ack_message_id"])
        result, _sender = self.intake_ack(sender_result=("rejected", None))
        self.assertEqual(result, {"ok": False, "stage": "rejected"})
        self.assertIsNone(self.item()["ack_sent_at"])
        result, _sender = self.intake_ack(sender_result=("sent", "778"))
        self.assertTrue(result["ok"])
        self.assertEqual(self.item()["ack_message_id"], "778")

    def test_intake_ack_not_pending_and_config(self):
        result, sender = self.intake_ack(external_id=999)
        self.assertEqual(result, {"ok": False, "stage": "not-pending"})
        sender.assert_not_called()
        self.enqueue_pending()
        with self.store.transaction() as state:
            batch = queue_core.claim_batch(
                state, platform="telegram", now=1000.0)
        token = batch["claim_token"]
        result, sender = self.intake_ack()
        self.assertEqual(result, {"ok": False, "stage": "not-pending"})
        sender.assert_not_called()
        with self.store.transaction() as state:
            queue_core.release(state, "telegram", 101, token)
        with mock.patch.dict(os.environ, {
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            os.environ.pop(telegram.SEND_CMD_ENV, None)
            result = telegram.send_intake_ack(self.store, 101)
        self.assertEqual(result, {"ok": False, "stage": "config"})

    def test_intake_ack_position_counts_messages_ahead_in_lane(self):
        # Lane A: two messages; lane B: one. The second lane-A
        # message is #2 (one ahead in its own lane); the lane-B
        # message does not count against it.
        self.enqueue_pending(external_id=101, at=1000.0, message_id=55)
        self.enqueue_pending(external_id=102, at=1001.0, message_id=56)
        self.enqueue_pending(external_id=201, chat_id=222, at=1002.0,
                             message_id=66)
        result, _sender = self.intake_ack(external_id=102)
        self.assertEqual(result["queue_position"], 2)
        self.assertIn("#2 in the queue", telegram.claim_ack_text(2))
        result, _sender = self.intake_ack(external_id=101)
        self.assertEqual(result["queue_position"], 1)
        result, _sender = self.intake_ack(external_id=201)
        self.assertEqual(result["queue_position"], 1)

    def test_run_once_acks_pending_lane_at_intake(self):
        # Two lanes in one fetch: lane A is claimed and woken;
        # lane B stays pending — and each of its messages gets
        # its intake card in the same tick, while the claimed
        # lane-A items get no intake card.
        run_cli(self.path, ["--adopt", str(OWNER)])
        fetch = FakeFetch([
            msg_update(1 + i, CHAT, "private", OWNER, f"lane A {i}")
            for i in range(3)
        ] + [
            msg_update(10 + i, 222, "private", OWNER, f"lane B {i}")
            for i in range(3)
        ])
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_send_command",
                side_effect=[("sent", "771"), ("sent", "772"), ("sent", "773")],
            ) as sender:
                code, payload = run_cli(
                    self.path, [], fetch=fetch,
                    extra_env={
                        "TELEGRAM_BRIDGE_SETTLE_SECONDS": "0",
                        telegram.SEND_CMD_ENV: "/fake/sender",
                    },
                )
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 3)
        state = JsonStateStore(self.path).load()
        claimed = [i for i in state["queue"] if i["status"] == "claimed"]
        self.assertEqual(len(claimed), 3)
        self.assertTrue(all(i["ack_message_id"] is None for i in claimed))
        pending = [i for i in state["queue"] if i["status"] == "pending"]
        self.assertEqual(len(pending), 3)
        self.assertEqual(
            [i["ack_message_id"] for i in pending], ["771", "772", "773"])
        self.assertEqual(sender.call_count, 3)
        self.assertEqual(
            [call.kwargs["reply_to"] for call in sender.call_args_list],
            [10, 11, 12],
        )

    def test_claim_ack_adopts_intake_card_no_second_card(self):
        self.enqueue_pending()
        result, _sender = self.intake_ack()
        self.assertTrue(result["ok"])
        with self.store.transaction() as state:
            batch = queue_core.claim_batch(
                state, platform="telegram", now=1000.0)
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_send_command", return_value=("sent", "778")
            ) as sender:
                result = telegram.send_claim_ack(
                    self.store, batch["claim_token"])
        self.assertEqual(result, {"ok": False, "stage": "already-acked"})
        sender.assert_not_called()
        self.assertEqual(self.item()["ack_message_id"], "777")

    def test_sweeper_edits_intake_card_when_lane_shortens(self):
        self.enqueue_pending(external_id=101, at=1000.0, message_id=55)
        self.enqueue_pending(external_id=102, at=1001.0, message_id=56)
        result, _sender = self.intake_ack(external_id=102)
        self.assertEqual(result["queue_position"], 2)
        self.assertEqual(
            self.item(102)["ack_card_text"], telegram.claim_ack_text(2))
        # The item ahead is answered elsewhere: the lane
        # shortens, and the next sweep edits the card to #1.
        with self.store.transaction() as state:
            state["queue"] = [
                item for item in state["queue"]
                if str(item["external_id"]) != "101"
            ]
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_sender_action", return_value=True
            ) as edit_mock:
                report = telegram.watchdog_sweep(self.store, now=1010.0)
        self.assertEqual(report["ack_cards"]["edited"], 1)
        edit_mock.assert_called_once_with(
            "/fake/sender", ["--edit", CHAT, 777],
            stdin_text=telegram.claim_ack_text(1),
        )
        self.assertEqual(
            self.item(102)["ack_card_text"], telegram.claim_ack_text(1))

    def test_sweeper_deletes_card_on_done_unreplied_item(self):
        self.enqueue_pending()
        result, _sender = self.intake_ack()
        self.assertTrue(result["ok"])
        with self.store.transaction() as state:
            batch = queue_core.claim_batch(
                state, platform="telegram", now=1000.0)
            queue_core.mark_done(
                state, "telegram", 101, batch["claim_token"],
                reason="closed-no-reply",
            )
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_sender_action", return_value=True
            ) as action_mock:
                report = telegram.refresh_ack_cards(self.store)
        self.assertEqual(report["deleted"], 1)
        action_mock.assert_called_once_with(
            "/fake/sender", ["--delete", CHAT, 777])
        self.assertIsNone(self.item()["ack_message_id"])
        self.assertIsNone(self.item().get("ack_card_text"))
        # A second sweep has nothing left to delete.
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_sender_action", return_value=True
            ) as action_mock:
                report = telegram.refresh_ack_cards(self.store)
        self.assertEqual(report["deleted"], 0)
        action_mock.assert_not_called()

    def test_done_deletes_card_when_closing_unreplied_carded_item(self):
        self.enqueue_pending()
        result, _sender = self.intake_ack()
        self.assertTrue(result["ok"])
        with self.store.transaction() as state:
            batch = queue_core.claim_batch(
                state, platform="telegram", now=1000.0)
            token = batch["claim_token"]
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_sender_action", return_value=True
            ) as action_mock:
                code, result = run_cli(
                    self.path, ["--done", "101", token, "closed-no-reply"],
                    extra_env={telegram.SEND_CMD_ENV: "/fake/sender"},
                )
        self.assertEqual(code, 0)
        self.assertEqual(result, {"ok": True})
        action_mock.assert_called_once_with(
            "/fake/sender", ["--delete", CHAT, 777])
        self.assertIsNone(self.item()["ack_message_id"])


class ReactBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.store = JsonStateStore(self.path)
        with self.store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id=101,
                chat_id=CHAT, chat_type="private", sender_id=OWNER,
                text="hello", message_id=55, now=1000.0,
            )
            queue_core.enqueue(
                state, platform="telegram", external_id=102,
                chat_id=CHAT, chat_type="private", sender_id=OWNER,
                text="button tap", message_id=None,
                source="callback_query", now=1001.0,
            )
            batch = queue_core.claim_batch(state, platform="telegram", now=1002.0)
            self.token = batch["claim_token"]

    def test_react_batch_reacts_to_each_message_skips_taps(self):
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_sender_action", return_value=True
            ) as action:
                result = telegram.react_batch(self.store, self.token, "👀")
        self.assertEqual(result, {"ok": True, "reacted": [55], "skipped": ["102"]})
        action.assert_called_once_with(
            "/fake/sender", ["--react", CHAT, 55, "👀"])

    def test_react_batch_stale_token_config_and_input(self):
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_sender_action", return_value=True
            ) as action:
                self.assertEqual(
                    telegram.react_batch(self.store, "deadbeefdeadbeef", "👀"),
                    {"ok": False, "stage": "stale-token"})
                action.assert_not_called()
                self.assertEqual(
                    telegram.react_batch(self.store, self.token, "  "),
                    {"ok": False, "stage": "input"})
        with mock.patch.dict(os.environ, {
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            os.environ.pop(telegram.SEND_CMD_ENV, None)
            self.assertEqual(
                telegram.react_batch(self.store, self.token, "👀"),
                {"ok": False, "stage": "config"})

    def test_react_batch_cli(self):
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_sender_action", return_value=True
            ):
                code, result = run_cli(
                    self.path, ["--react-batch", self.token, "👀"])
        self.assertEqual(code, 0)
        self.assertEqual(result, {"ok": True, "reacted": [55], "skipped": ["102"]})
        code, result = run_cli(self.path, ["--react-batch", self.token])
        self.assertEqual(code, 2)
        self.assertFalse(result["ok"])

    def test_prompt_carries_first_tool_call_react_rule(self):
        prompt = (Path(__file__).resolve().parent.parent
                  / "prompts" / "unified.md").read_text()
        for phrase in (
            "your very first tool call in this turn",
            "--react-batch CLAIM_TOKEN EMOJI",
            "using an emoji YOU choose as fitting",
            "not a fixed thumbs-up",
            "before any other work, including before --heartbeat",
            "No script ever reacts for you",
            "SECOND, prove the turn started",
        ):
            self.assertIn(phrase, prompt)
        # And the old reaction guidance is gone: the reaction is
        # no longer only set on --reply at send time.
        self.assertNotIn("The hook may have fired it pre-wake", prompt)

    def test_no_scripted_reaction_anywhere(self):
        # No script path reacts: the intake hook and the watchdog
        # hook send no --react, and the bridge core calls the
        # sender's --react in exactly two places — react_batch
        # (the agent's first tool call) and send_reply's --react
        # option (agent-passed) — never on its own initiative.
        runtime = Path(__file__).resolve().parent.parent
        for hook in ("hooks/telegram-bridge-intake.sh",
                     "hooks/telegram-bridge-watchdog.sh"):
            self.assertNotIn("--react", (runtime / hook).read_text(), hook)
        core = (runtime / "bridge_core" / "telegram.py").read_text()
        self.assertEqual(core.count('["--react",'), 2)


class LongPollTests(unittest.TestCase):
    def test_default_long_poll_stays_30s(self):
        # P1: the 30s -> 10s test was NOT run live — no live
        # load/rate measurement is possible from this sandbox —
        # so the default stays 30 until a live measurement
        # justifies a change. Never lowered on assumption.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(telegram.LONG_POLL_SECONDS_ENV, None)
            self.assertEqual(telegram._long_poll_seconds(), 30)
        with mock.patch.dict(os.environ, {
                telegram.LONG_POLL_SECONDS_ENV: "10"}):
            self.assertEqual(telegram._long_poll_seconds(), 10)


class SenderEditTests(unittest.TestCase):
    def setUp(self):
        here = Path(__file__).resolve()
        sender_path = here.parent.parent / "bin" / "telegram-sender"
        loader = SourceFileLoader(
            "telegram_sender_p1_test", str(sender_path))
        spec = importlib.util.spec_from_loader(
            "telegram_sender_p1_test", loader)
        self.sender = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sender)

    def test_edit_message_text_posts_edit_message_text(self):
        calls = []

        def fake_post(method, payload):
            calls.append((method, payload))
            return {"ok": True, "result": True}

        with mock.patch.object(self.sender, "_post", fake_post):
            self.assertTrue(self.sender.edit_message_text(111, 55, "new text"))
        self.assertEqual(calls, [
            ("editMessageText", {
                "chat_id": 111, "message_id": 55,
                "text": "new text", "parse_mode": "HTML",
            }),
        ])

    def test_edit_message_text_retries_plain_on_400(self):
        import urllib.error
        calls = []

        def fake_post(method, payload):
            calls.append(payload.get("parse_mode"))
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    "http://x", 400, "bad", {}, None)
            return {"ok": True, "result": True}

        with mock.patch.object(self.sender, "_post", fake_post):
            self.assertTrue(self.sender.edit_message_text(111, 55, "new <b>text</b>"))
        self.assertEqual(calls, ["HTML", None])

    def test_edit_main_dispatch(self):
        with mock.patch.object(
            self.sender, "_post",
            lambda m, p: {"ok": True, "result": True},
        ):
            with mock.patch.object(
                sys, "argv",
                ["telegram-sender", "--edit", "111", "55"],
            ):
                with mock.patch.object(
                    sys, "stdin", io.StringIO("edited card text")
                ):
                    self.assertEqual(self.sender.main(), 0)

    def test_edit_main_dispatch_bad_id_is_usage_error(self):
        with mock.patch.object(
            sys, "argv",
            ["telegram-sender", "--edit", "111", "nope"],
        ):
            self.assertEqual(self.sender.main(), 2)

    def test_bridge_edit_message_calls_sender_edit(self):
        with mock.patch.object(
            telegram, "_run_sender_action", return_value=True
        ) as action:
            self.assertTrue(
                telegram.edit_message("/fake/sender", CHAT, 777, "new text"))
        action.assert_called_once_with(
            "/fake/sender", ["--edit", CHAT, 777], stdin_text="new text")
        with mock.patch.object(
            telegram, "_run_sender_action", return_value=True
        ) as action:
            self.assertFalse(telegram.edit_message("/fake/sender", CHAT, "x", "t"))
            self.assertFalse(telegram.edit_message("/fake/sender", CHAT, 777, ""))
            self.assertFalse(telegram.edit_message("/fake/sender", CHAT, 777, "x" * 4097))
        action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
