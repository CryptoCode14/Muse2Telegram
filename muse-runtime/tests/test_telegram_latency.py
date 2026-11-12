"""Offline tests for P0 latency instrumentation.

Covers ``wake_issued_at`` (stamped at wake, cleared on recycle,
counted per wake) and the ``--latency-report`` command (per-stage
medians with targets, recovery metrics, insufficient-data handling
on empty state). Fast, offline, stdlib unittest.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge_core import queue as queue_core  # noqa: E402
from bridge_core import telegram  # noqa: E402
from bridge_core.store import JsonStateStore  # noqa: E402

OWNER = 1658762691


def run_cli(path, argv, extra_env=None):
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
            code = telegram.main(argv, path=path)
    finally:
        sys.stdout = old_stdout
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else None)


def build_item(state, external_id, chat_id, received_at):
    return queue_core.enqueue(
        state,
        platform="telegram",
        external_id=external_id,
        chat_id=chat_id,
        chat_type="private",
        sender_name="@owner",
        text=f"message {external_id}",
        now=received_at,
    )


class WakeIssuedQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.store = JsonStateStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_enqueue_initializes_wake_fields(self):
        with self.store.transaction() as state:
            item = build_item(state, 1, 111, 1000.0)
        self.assertIsNone(item["wake_issued_at"])
        self.assertEqual(item["wake_count"], 0)
        self.assertEqual(item["recycle_count"], 0)

    def test_mark_wake_issued_stamps_batch_and_counts(self):
        with self.store.transaction() as state:
            build_item(state, 1, 111, 1000.0)
            build_item(state, 2, 111, 1001.0)
            batch = queue_core.claim_batch(
                state, platform="telegram", now=1002.0
            )
            token = batch["claim_token"]
            stamped = queue_core.mark_wake_issued(
                state, token, now=1005.0
            )
        self.assertEqual(stamped, 2)
        items = self.store.load()["queue"]
        for item in items:
            self.assertEqual(item["wake_issued_at"], 1005.0)
            self.assertEqual(item["wake_count"], 1)
        # A second wake for the same batch counts again.
        with self.store.transaction() as state:
            stamped = queue_core.mark_wake_issued(
                state, token, now=1006.0
            )
        self.assertEqual(stamped, 2)
        items = self.store.load()["queue"]
        for item in items:
            self.assertEqual(item["wake_issued_at"], 1006.0)
            self.assertEqual(item["wake_count"], 2)

    def test_mark_wake_issued_stale_token_stamps_nothing(self):
        with self.store.transaction() as state:
            build_item(state, 1, 111, 1000.0)
            build_item(state, 2, 222, 1000.0)
            batch = queue_core.claim_batch(
                state, platform="telegram", now=1002.0
            )
            # Only the first lane's item is claimed; the second
            # stays pending, and a wrong token stamps neither.
            stamped = queue_core.mark_wake_issued(
                state, "deadbeefdeadbeef", now=1005.0
            )
            self.assertEqual(stamped, 0)
            self.assertEqual(batch["items"][0]["wake_issued_at"], None)
            self.assertEqual(batch["items"][0]["wake_count"], 0)
            pending = queue_core.find_item(state, "telegram", 2)
            self.assertEqual(pending["status"], queue_core.PENDING)
            self.assertIsNone(pending["wake_issued_at"])

    def test_mark_wake_issued_none_token_stamps_nothing(self):
        with self.store.transaction() as state:
            build_item(state, 1, 111, 1000.0)
            queue_core.claim_batch(state, platform="telegram", now=1002.0)
            self.assertEqual(
                queue_core.mark_wake_issued(state, None, now=1005.0), 0
            )

    def test_recycle_clears_stamp_and_counts_but_keeps_wake_count(self):
        with self.store.transaction() as state:
            build_item(state, 1, 111, 1000.0)
            batch = queue_core.claim_batch(
                state, platform="telegram", now=1001.0
            )
            queue_core.mark_wake_issued(
                state, batch["claim_token"], now=1002.0
            )
            # A watchdog pass long after the heartbeat went stale
            # recycles the dead claim.
            report = queue_core.watchdog_sweep(state, now=1001.0 + 3600.0)
        self.assertEqual(report["recycled"], ["1"])
        item = self.store.load()["queue"][0]
        self.assertEqual(item["status"], queue_core.PENDING)
        self.assertIsNone(item["wake_issued_at"])
        self.assertEqual(item["recycle_count"], 1)
        self.assertEqual(item["wake_count"], 1)
        # Re-claim + re-wake: the wake count keeps climbing, the
        # recycle count survives, the fresh claim has no stamp
        # until its own wake.
        with self.store.transaction() as state:
            batch = queue_core.claim_batch(
                state, platform="telegram", now=5000.0
            )
            item = batch["items"][0]
            self.assertIsNone(item["wake_issued_at"])
            self.assertEqual(item["recycle_count"], 1)
            queue_core.mark_wake_issued(
                state, batch["claim_token"], now=5001.0
            )
        item = self.store.load()["queue"][0]
        self.assertEqual(item["wake_issued_at"], 5001.0)
        self.assertEqual(item["wake_count"], 2)
        self.assertEqual(item["recycle_count"], 1)

    def test_claim_more_items_carry_no_wake_stamp(self):
        # Items claimed into a live turn joined it mid-flight;
        # they were never woken themselves, so they contribute no
        # wake sample (but still get a turn start).
        with self.store.transaction() as state:
            build_item(state, 1, 111, 1000.0)
            batch = queue_core.claim_batch(
                state, platform="telegram", now=1001.0
            )
            queue_core.mark_wake_issued(
                state, batch["claim_token"], now=1002.0
            )
            build_item(state, 2, 111, 1003.0)
            more = queue_core.claim_more(
                state,
                platform="telegram",
                chat_id=111,
                token=batch["claim_token"],
                now=1004.0,
            )
        self.assertEqual(len(more["items"]), 1)
        late = more["items"][0]
        self.assertIsNone(late["wake_issued_at"])
        self.assertEqual(late["wake_count"], 0)
        self.assertEqual(late["turn_started_at"], 1004.0)


class LatencyReportCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "state.json"
        code, _ = run_cli(self.path, ["--adopt", str(OWNER)])
        self.assertEqual(code, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def _build_median_fixture(self):
        """Three items with known stage durations.

        A: received 1000, claimed 1002 (pickup 2), wake 1005
           (claim->wake 3), ack 1007 (ack 7, wake 2), replied 1120
           (turn 113, total 120).
        B: received 2000, claimed 2005 (pickup 5), wake 2010
           (claim->wake 5), turn start 2020 (wake 10), replied 2200
           (turn 180, total 200). No ack.
        C: received 3000, claimed 3001 (pickup 1), wake 3002
           (claim->wake 1), turn never started.
        """
        with JsonStateStore(self.path).transaction() as state:
            build_item(state, 1, 111, 1000.0)
            batch_a = queue_core.claim_batch(
                state, platform="telegram", now=1002.0
            )
            queue_core.mark_wake_issued(
                state, batch_a["claim_token"], now=1005.0
            )
            self.assertTrue(
                queue_core.mark_ack(
                    state, "telegram", 1, "7", batch_a["claim_token"],
                    now=1007.0,
                )
            )
            self.assertTrue(
                queue_core.mark_replied(
                    state, "telegram", 1, "4242", batch_a["claim_token"],
                    now=1120.0,
                )
            )
            self.assertTrue(
                queue_core.mark_done(
                    state, "telegram", 1, batch_a["claim_token"],
                    now=1120.0,
                )
            )
            build_item(state, 2, 222, 2000.0)
            batch_b = queue_core.claim_batch(
                state, platform="telegram", now=2005.0
            )
            queue_core.mark_wake_issued(
                state, batch_b["claim_token"], now=2010.0
            )
            self.assertTrue(
                queue_core.heartbeat(
                    state, "telegram", 2, batch_b["claim_token"], now=2020.0
                )
            )
            self.assertTrue(
                queue_core.mark_replied(
                    state, "telegram", 2, "4243", batch_b["claim_token"],
                    now=2200.0,
                )
            )
            self.assertTrue(
                queue_core.mark_done(
                    state, "telegram", 2, batch_b["claim_token"],
                    now=2200.0,
                )
            )
            build_item(state, 3, 333, 3000.0)
            batch_c = queue_core.claim_batch(
                state, platform="telegram", now=3001.0
            )
            queue_core.mark_wake_issued(
                state, batch_c["claim_token"], now=3002.0
            )

    def test_latency_report_medians_and_targets(self):
        self._build_median_fixture()
        code, report = run_cli(self.path, ["--latency-report"])
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["items"], 3)
        stages = report["stages"]
        # ack: [7] -> p50 7, meets the 10s target.
        self.assertEqual(stages["ack"]["n"], 1)
        self.assertEqual(stages["ack"]["p50"], 7.0)
        self.assertEqual(stages["ack"]["p90"], 7.0)
        self.assertEqual(stages["ack"]["target_p50"], 10.0)
        self.assertTrue(stages["ack"]["meets_target"])
        # pickup: [2, 5, 1] -> p50 2, p90 5, meets.
        self.assertEqual(stages["pickup"]["n"], 3)
        self.assertEqual(stages["pickup"]["p50"], 2.0)
        self.assertEqual(stages["pickup"]["p90"], 5.0)
        self.assertTrue(stages["pickup"]["meets_target"])
        # wake: [2, 10] -> p50 6, p90 10, meets the 30s target.
        self.assertEqual(stages["wake"]["n"], 2)
        self.assertEqual(stages["wake"]["p50"], 6.0)
        self.assertEqual(stages["wake"]["p90"], 10.0)
        self.assertTrue(stages["wake"]["meets_target"])
        # turn: [113, 180] -> p50 146.5, misses the 120s target.
        self.assertEqual(stages["turn"]["n"], 2)
        self.assertEqual(stages["turn"]["p50"], 146.5)
        self.assertEqual(stages["turn"]["target_p50"], 120.0)
        self.assertFalse(stages["turn"]["meets_target"])
        # total: [120, 200] -> p50 160, p90 200, meets both targets.
        self.assertEqual(stages["total"]["n"], 2)
        self.assertEqual(stages["total"]["p50"], 160.0)
        self.assertEqual(stages["total"]["p90"], 200.0)
        self.assertEqual(stages["total"]["target_p50"], 180.0)
        self.assertEqual(stages["total"]["target_p90"], 300.0)
        self.assertTrue(stages["total"]["meets_target"])
        # claim_to_wake diagnostic: [3, 5, 1] -> p50 3, no target.
        self.assertEqual(stages["claim_to_wake"]["n"], 3)
        self.assertEqual(stages["claim_to_wake"]["p50"], 3.0)
        self.assertIsNone(stages["claim_to_wake"]["target_p50"])
        self.assertIsNone(stages["claim_to_wake"]["meets_target"])
        # Every stage states its definition.
        for stage in stages.values():
            self.assertTrue(stage["definition"])
        # Recovery: C is woken with no turn start (Issue 1 signal).
        recovery = report["recovery"]
        self.assertEqual(recovery["recycles"], 0)
        self.assertEqual(recovery["re_woken_items"], 0)
        self.assertEqual(recovery["extra_wakes"], 0)
        self.assertEqual(recovery["wakes_without_turn"], 1)
        self.assertEqual(recovery["stuck_sending_now"], 0)

    def test_latency_report_empty_state_insufficient_data(self):
        # A fresh state file: no crash, no invented numbers.
        fresh = self.dir / "fresh.json"
        code, report = run_cli(fresh, ["--latency-report"])
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["items"], 0)
        for stage in report["stages"].values():
            self.assertEqual(stage["n"], 0)
            self.assertIsNone(stage["p50"])
            self.assertIsNone(stage["p90"])
            self.assertEqual(stage["status"], "insufficient-data")
            self.assertIsNone(stage["meets_target"])
        self.assertEqual(
            report["recovery"],
            {
                "recycles": 0,
                "re_woken_items": 0,
                "extra_wakes": 0,
                "wakes_without_turn": 0,
                "lease_escalated_now": 0,
                "stuck_claims_now": 0,
                "stuck_sending_now": 0,
                "stuck_sending": [],
            },
        )

    def test_latency_report_recovery_metrics(self):
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            # D: woken, recycled as stale, re-claimed, re-woken.
            build_item(state, 4, 111, 1000.0)
            batch = queue_core.claim_batch(
                state, platform="telegram", now=1001.0
            )
            queue_core.mark_wake_issued(
                state, batch["claim_token"], now=1002.0
            )
            queue_core.watchdog_sweep(state, now=1001.0 + 3600.0)
            batch = queue_core.claim_batch(
                state, platform="telegram", now=5000.0
            )
            queue_core.mark_wake_issued(
                state, batch["claim_token"], now=5001.0
            )
            queue_core.heartbeat(
                state, "telegram", 4, batch["claim_token"], now=5002.0
            )
            queue_core.mark_replied(
                state, "telegram", 4, "4244", batch["claim_token"],
                now=5100.0,
            )
            queue_core.mark_done(
                state, "telegram", 4, batch["claim_token"], now=5100.0
            )
            # E: claimed, moved to sending long ago, never closed.
            build_item(state, 5, 222, 1000.0)
            batch_e = queue_core.claim_batch(
                state, platform="telegram", now=1001.0
            )
            queue_core.mark_sending(
                state, "telegram", 5, batch_e["claim_token"], now=1002.0
            )
        code, report = run_cli(self.path, ["--latency-report"])
        self.assertEqual(code, 0)
        recovery = report["recovery"]
        self.assertEqual(recovery["recycles"], 1)
        self.assertEqual(recovery["re_woken_items"], 1)
        self.assertEqual(recovery["extra_wakes"], 1)
        self.assertEqual(recovery["wakes_without_turn"], 0)
        self.assertEqual(recovery["stuck_claims_now"], 0)
        self.assertEqual(recovery["stuck_sending_now"], 1)
        self.assertEqual(recovery["stuck_sending"], ["5"])

    def test_latency_report_is_read_only(self):
        self._build_median_fixture()
        before = self.path.read_bytes()
        code, _ = run_cli(self.path, ["--latency-report"])
        self.assertEqual(code, 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_latency_report_rejects_extra_args(self):
        code, result = run_cli(
            self.path, ["--latency-report", "extra"]
        )
        self.assertEqual(code, 2)
        self.assertFalse(result["ok"])

    def test_wake_issued_cli_stamps_batch(self):
        with JsonStateStore(self.path).transaction() as state:
            build_item(state, 1, 111, 1000.0)
            build_item(state, 2, 111, 1001.0)
            batch = queue_core.claim_batch(
                state, platform="telegram", now=1002.0
            )
        code, result = run_cli(
            self.path, ["--wake-issued", batch["claim_token"]]
        )
        self.assertEqual(code, 0)
        self.assertEqual(result, {"ok": True, "stamped": 2})
        items = JsonStateStore(self.path).load()["queue"]
        for item in items:
            self.assertIsNotNone(item["wake_issued_at"])
            self.assertEqual(item["wake_count"], 1)

    def test_wake_issued_cli_stale_token(self):
        code, result = run_cli(
            self.path, ["--wake-issued", "deadbeefdeadbeef"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(result, {"ok": False, "stage": "stale-token"})

    def test_wake_issued_cli_usage_error(self):
        code, result = run_cli(self.path, ["--wake-issued"])
        self.assertEqual(code, 2)
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
