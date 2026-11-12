"""Tests for the claim heartbeat and the queue watchdog.

A batch turn refreshes its heartbeat while it works; a claim whose
heartbeat goes stale is recycled (about ten minutes) instead of
blocking its lane for the full stale TTL. A live turn that keeps
heartbeating is never recycled for its age alone. The watchdog sweep
recycles dead claims and never touches sending / replied / live ones.

No Muse credentials, Telegram calls, or external sender are used.
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from bridge_core import queue as queue_core
from bridge_core import telegram
from bridge_core.store import JsonStateStore


def _state_with_claimed(path, *, count=1, at=1000.0, chat_id=111):
    """A store with ``count`` items claimed at time ``at``.

    Returns (store, token). Items are in one lane (platform
    telegram, chat_id)."""
    store = JsonStateStore(path)
    with store.transaction() as state:
        for i in range(count):
            queue_core.enqueue(
                state, platform="telegram", external_id=100 + i,
                chat_id=chat_id, chat_type="group", sender_id=7,
                text=f"message {i}", message_id=50 + i, now=at,
            )
        batch = queue_core.claim_batch(state, platform="telegram", now=at)
    return store, batch["claim_token"]


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"

    def items(self, store):
        return store.load()["queue"]

    def test_claim_sets_heartbeat_at_claim_time(self):
        store, _token = _state_with_claimed(self.path, at=1000.0)
        item = self.items(store)[0]
        self.assertEqual(item["status"], "claimed")
        self.assertEqual(item["claimed_at"], 1000.0)
        self.assertEqual(item["heartbeat_at"], 1000.0)

    def test_heartbeat_keeps_live_claim_from_recycling(self):
        store, token = _state_with_claimed(self.path, at=1000.0)
        with store.transaction() as state:
            self.assertTrue(
                queue_core.heartbeat(state, "telegram", 100, token, now=1100.0)
            )
        # Heartbeat is 590s old at 1690: still fresh under the 600s
        # horizon, lane still busy.
        with store.transaction() as state:
            batch = queue_core.claim_batch(state, platform="telegram", now=1690.0)
        self.assertIsNone(batch)
        self.assertEqual(self.items(store)[0]["status"], "claimed")

    def test_late_wake_heartbeat_within_window(self):
        # Regression: a turn that started ~8.5 minutes after its
        # claim was polled used to find its claim already recycled
        # (120s horizon) and its first heartbeat fenced out, so
        # the intake re-woke a new turn and the cycle repeated
        # without ever replying. With the 600s horizon the late
        # first heartbeat still lands and the lane stays owned.
        store, token = _state_with_claimed(self.path, at=1000.0)
        with store.transaction() as state:
            self.assertTrue(
                queue_core.heartbeat(state, "telegram", 100, token, now=1510.0)
            )
        # Heartbeat is 10s old at 1520: fresh, lane still busy.
        with store.transaction() as state:
            batch = queue_core.claim_batch(state, platform="telegram", now=1520.0)
        self.assertIsNone(batch)
        self.assertEqual(self.items(store)[0]["status"], "claimed")

    def test_never_started_claim_survives_slow_start(self):
        # Regression (2026-11-12): a worker turn still starting up
        # after a runtime gap had its batch recycled at 180 s, the
        # batch was re-claimed under a new token, and the first
        # turn's reply fenced. With the 600 s never-started horizon
        # a claim 300 s old is still owned; at 601 s it recycles.
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id=100,
                chat_id=111, chat_type="group", sender_id=7,
                text="slow start", message_id=50, now=1000.0,
            )
            batch = queue_core.claim_batch(
                state, platform="telegram",
                active_heartbeat_stale=queue_core.NEVER_STARTED_RECYCLE_SECONDS,
                now=1000.0,
            )
        token = batch["claim_token"]
        with store.transaction() as state:
            batch = queue_core.claim_batch(
                state, platform="telegram",
                active_heartbeat_stale=queue_core.NEVER_STARTED_RECYCLE_SECONDS,
                now=1300.0,
            )
        self.assertIsNone(batch)
        self.assertEqual(self.items(store)[0]["status"], "claimed")
        with store.transaction() as state:
            batch = queue_core.claim_batch(
                state, platform="telegram",
                active_heartbeat_stale=queue_core.NEVER_STARTED_RECYCLE_SECONDS,
                now=1601.0,
            )
        self.assertIsNotNone(batch)
        self.assertNotEqual(batch["claim_token"], token)
        self.assertEqual(self.items(store)[0]["status"], "claimed")

    def test_stale_heartbeat_recycles_claim_fast(self):
        store, token = _state_with_claimed(self.path, at=1000.0)
        with store.transaction() as state:
            self.assertTrue(
                queue_core.heartbeat(state, "telegram", 100, token, now=1100.0)
            )
        # Heartbeat is 610s old at 1710: stale, recycled and reclaimed.
        with store.transaction() as state:
            batch = queue_core.claim_batch(state, platform="telegram", now=1710.0)
        self.assertIsNotNone(batch)
        self.assertNotEqual(batch["claim_token"], token)
        item = self.items(store)[0]
        self.assertEqual(item["status"], "claimed")
        self.assertEqual(item["heartbeat_at"], 1710.0)
        self.assertTrue(item["ever_claimed"])

    def test_heartbeat_supersedes_claimed_at_ttl(self):
        # A live long turn: claimed at 1000, heartbeat at 2850, still
        # working at 2900. claimed_at age (1900s) is past the 1800s
        # TTL, but the fresh heartbeat means it is NOT recycled.
        store, token = _state_with_claimed(self.path, at=1000.0)
        with store.transaction() as state:
            self.assertTrue(
                queue_core.heartbeat(state, "telegram", 100, token, now=2850.0)
            )
        with store.transaction() as state:
            batch = queue_core.claim_batch(state, platform="telegram", now=2900.0)
        self.assertIsNone(batch)
        self.assertEqual(self.items(store)[0]["status"], "claimed")

    def test_no_heartbeat_falls_back_to_claimed_at_ttl(self):
        # An item claimed before heartbeats existed (heartbeat_at
        # None): the old TTL is the backstop.
        store, token = _state_with_claimed(self.path, at=1000.0)
        with store.transaction() as state:
            item = queue_core.find_item(state, "telegram", 100)
            item["heartbeat_at"] = None
        with store.transaction() as state:
            batch = queue_core.claim_batch(state, platform="telegram", now=2900.0)
        self.assertIsNotNone(batch)
        self.assertNotEqual(batch["claim_token"], token)

    def test_heartbeat_refused_for_wrong_token(self):
        store, _token = _state_with_claimed(self.path, at=1000.0)
        with store.transaction() as state:
            ok = queue_core.heartbeat(
                state, "telegram", 100, "stale-token", now=1100.0
            )
        self.assertFalse(ok)
        self.assertEqual(self.items(store)[0]["heartbeat_at"], 1000.0)

    def test_heartbeat_batch_refreshes_whole_batch(self):
        store, token = _state_with_claimed(self.path, count=3, at=1000.0)
        with store.transaction() as state:
            refreshed = queue_core.heartbeat_batch(state, token, now=1150.0)
        self.assertEqual(refreshed, 3)
        for item in self.items(store):
            self.assertEqual(item["heartbeat_at"], 1150.0)

    def test_heartbeat_batch_wrong_token_refreshes_nothing(self):
        store, _token = _state_with_claimed(self.path, count=2, at=1000.0)
        with store.transaction() as state:
            refreshed = queue_core.heartbeat_batch(state, "nope", now=1150.0)
        self.assertEqual(refreshed, 0)
        for item in self.items(store):
            self.assertEqual(item["heartbeat_at"], 1000.0)

    def test_heartbeat_refused_after_reply_recorded(self):
        store, token = _state_with_claimed(self.path, at=1000.0)
        with store.transaction() as state:
            queue_core.mark_sending(state, "telegram", 100, token, now=1010.0)
            queue_core.mark_replied(state, "telegram", 100, "555", token, now=1020.0)
            ok = queue_core.heartbeat(state, "telegram", 100, token, now=1100.0)
        self.assertFalse(ok)


class WatchdogSweepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"

    def test_sweep_recycles_dead_claim_only(self):
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            # Both items in one lane, so one batch claims both.
            queue_core.enqueue(
                state, platform="telegram", external_id=100,
                chat_id=111, chat_type="group", sender_id=7,
                text="dead turn", message_id=50, now=1000.0,
            )
            queue_core.enqueue(
                state, platform="telegram", external_id=200,
                chat_id=111, chat_type="group", sender_id=8,
                text="live turn", message_id=60, now=1000.0,
            )
            queue_core.claim_batch(state, platform="telegram", now=1000.0)
            # Only the live turn heartbeats.
            live = queue_core.find_item(state, "telegram", 200)
            queue_core.heartbeat(
                state, "telegram", 200, live["claim_token"], now=1150.0
            )
        # Sweep at 1700: the dead claim's heartbeat is 700s old
        # (stale, recycled); the live turn's is 550s old (fresh).
        with store.transaction() as state:
            report = queue_core.watchdog_sweep(state, now=1700.0)
        self.assertEqual(report["recycled"], ["100"])
        self.assertEqual(report["pending"], 1)
        self.assertEqual(report["claimed_live"], 1)
        self.assertEqual(report["sending"], 0)
        items = store.load()["queue"]
        self.assertEqual(items[0]["status"], "pending")
        self.assertIsNone(items[0]["claim_token"])
        self.assertEqual(items[1]["status"], "claimed")

    def test_sweep_never_touches_sending_item(self):
        store, token = _state_with_claimed(self.path, at=1000.0)
        with store.transaction() as state:
            queue_core.mark_sending(state, "telegram", 100, token, now=1010.0)
        with store.transaction() as state:
            report = queue_core.watchdog_sweep(state, now=5000.0)
        self.assertEqual(report["recycled"], [])
        self.assertEqual(report["sending"], 1)
        self.assertEqual(store.load()["queue"][0]["status"], "sending")

    def test_sweep_on_healthy_queue_recycles_nothing(self):
        store, _token = _state_with_claimed(self.path, at=1000.0)
        with store.transaction() as state:
            report = queue_core.watchdog_sweep(state, now=1050.0)
        self.assertEqual(report, {
            "recycled": [], "pending": 0, "claimed_live": 1, "sending": 0,
            # No poll ever recorded: no age to judge, never stale.
            "intake_age_seconds": None, "intake_stale": False,
            "intake_wake": False,
        })

    def _intake_state(self, last_poll):
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            state["last_successful_poll"] = last_poll
        return store

    def test_sweep_fresh_intake_is_not_stale(self):
        store = self._intake_state(1970.0)
        with store.transaction() as state:
            report = queue_core.watchdog_sweep(state, now=2000.0)
        self.assertEqual(report["intake_age_seconds"], 30.0)
        self.assertFalse(report["intake_stale"])
        self.assertFalse(report["intake_wake"])

    def test_sweep_stale_intake_wakes_once_per_episode(self):
        store = self._intake_state(1800.0)
        # First stale sweep (age 200s): wakes.
        with store.transaction() as state:
            report = queue_core.watchdog_sweep(state, now=2000.0)
        self.assertEqual(report["intake_age_seconds"], 200.0)
        self.assertTrue(report["intake_stale"])
        self.assertTrue(report["intake_wake"])
        # Second stale sweep a minute later: still stale, silent
        # (cooldown, same episode).
        with store.transaction() as state:
            report = queue_core.watchdog_sweep(state, now=2060.0)
        self.assertTrue(report["intake_stale"])
        self.assertFalse(report["intake_wake"])
        # After the 10-minute cooldown: wakes again.
        with store.transaction() as state:
            report = queue_core.watchdog_sweep(state, now=2601.0)
        self.assertTrue(report["intake_stale"])
        self.assertTrue(report["intake_wake"])

    def test_sweep_fresh_poll_clears_stale_episode(self):
        store = self._intake_state(1800.0)
        with store.transaction() as state:
            report = queue_core.watchdog_sweep(state, now=2000.0)
        self.assertTrue(report["intake_wake"])
        # The intake recovers: a fresh poll ends the episode.
        with store.transaction() as state:
            state["last_successful_poll"] = 2090.0
            report = queue_core.watchdog_sweep(state, now=2100.0)
        self.assertFalse(report["intake_stale"])
        self.assertFalse(report["intake_wake"])
        self.assertNotIn(
            queue_core.LAST_INTAKE_STALE_WAKE_KEY, store.load()
        )
        # It goes stale again right away: a NEW episode wakes
        # immediately, without waiting out the old cooldown.
        with store.transaction() as state:
            state["last_successful_poll"] = 1800.0
            report = queue_core.watchdog_sweep(state, now=2200.0)
        self.assertTrue(report["intake_stale"])
        self.assertTrue(report["intake_wake"])

    def test_sweep_never_polled_intake_is_not_stale(self):
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            report = queue_core.watchdog_sweep(state, now=2000.0)
        self.assertIsNone(report["intake_age_seconds"])
        self.assertFalse(report["intake_stale"])
        self.assertFalse(report["intake_wake"])


class HeartbeatCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.store, self.token = _state_with_claimed(self.path, at=1000.0)

    def run_cli(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = telegram.main(argv=argv, path=self.path)
        return code, json.loads(out.getvalue())

    def test_cli_heartbeat_refreshes_batch(self):
        code, result = self.run_cli(["--heartbeat", self.token])
        self.assertEqual(code, 0)
        self.assertEqual(result, {"ok": True, "heartbeated": 1})
        self.assertGreater(
            self.store.load()["queue"][0]["heartbeat_at"], 1000.0
        )

    def test_cli_heartbeat_wrong_token_is_fence(self):
        code, result = self.run_cli(["--heartbeat", "stale-token"])
        self.assertEqual(code, 0)
        self.assertEqual(result, {"ok": False, "stage": "fence"})

    def test_cli_heartbeat_usage_error(self):
        code, result = self.run_cli(["--heartbeat"])
        self.assertEqual(code, 2)
        self.assertFalse(result["ok"])

    def test_cli_watchdog_recycles_dead_claim(self):
        code, result = self.run_cli(["--watchdog"])
        self.assertEqual(code, 0)
        # Heartbeat was set at claim (1000); the real now is far
        # later, so the claim is stale and recycled.
        self.assertEqual(result["recycled"], ["100"])
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["claimed_live"], 0)
        self.assertEqual(self.store.load()["queue"][0]["status"], "pending")

    def test_cli_watchdog_healthy_queue(self):
        # Heartbeat the claim just before the sweep: live.
        with mock.patch("time.time", return_value=1050.0):
            code, result = self.run_cli(["--watchdog"])
        self.assertEqual(code, 0)
        self.assertEqual(result["recycled"], [])
        self.assertEqual(result["claimed_live"], 1)

    def test_cli_watchdog_reports_stale_intake(self):
        with self.store.transaction() as state:
            state["last_successful_poll"] = 1800.0
        with mock.patch("time.time", return_value=2000.0):
            code, result = self.run_cli(["--watchdog"])
        self.assertEqual(code, 0)
        self.assertEqual(result["intake_age_seconds"], 200.0)
        self.assertTrue(result["intake_stale"])
        self.assertTrue(result["intake_wake"])

    def test_cli_watchdog_reports_fresh_intake(self):
        with self.store.transaction() as state:
            state["last_successful_poll"] = 1990.0
        with mock.patch("time.time", return_value=2000.0):
            code, result = self.run_cli(["--watchdog"])
        self.assertEqual(code, 0)
        self.assertEqual(result["intake_age_seconds"], 10.0)
        self.assertFalse(result["intake_stale"])
        self.assertFalse(result["intake_wake"])


if __name__ == "__main__":
    unittest.main()
