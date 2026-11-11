"""Tests for the send fence's reply-age limits.

Never-claimed items use the short limit (default 1800s): a late
reply to hours-old room chatter lands off-context. Work a turn
already claimed (ever_claimed) is unfinished work the owner is
waiting on, so it stays answerable for a day
(DEFAULT_MAX_REPLY_AGE_CLAIMED) — a recovery after an outage is
never refused by the fence as stale.

No Muse credentials, Telegram calls, or external sender are used:
the sender subprocess is mocked.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bridge_core import queue as queue_core
from bridge_core import telegram
from bridge_core.store import JsonStateStore


class ReplyAgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.store = JsonStateStore(self.path)

    def claim_item(self, *, received_at=1000.0, claimed_at=1000.0,
                   ever_claimed=True):
        """One claimed group item, received at ``received_at``."""
        with self.store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id=101,
                chat_id=123456789, chat_type="group", sender_id=7,
                text="please do this work", message_id=55, now=received_at,
            )
            batch = queue_core.claim_batch(
                state, platform="telegram", now=claimed_at
            )
            item = queue_core.find_item(state, "telegram", 101)
            item["ever_claimed"] = ever_claimed
            return batch["claim_token"]

    def send(self, token, *, now, sender_result=("sent", "777")):
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: "/fake/sender"}):
            with mock.patch.object(
                telegram, "_run_send_command", return_value=sender_result
            ) as sender:
                result = telegram.send_fenced(
                    self.store, 101, token, 123456789, "the answer",
                    reply_to=55, max_reply_age=1800.0, now=now,
                )
        return result, sender

    def test_claimed_work_passes_fence_past_1800s(self):
        # Received 2000s ago, claimed by a turn: the claimed limit
        # (a day) applies, so the send goes out.
        token = self.claim_item(received_at=1000.0, claimed_at=1000.0)
        result, sender = self.send(token, now=3000.0)
        self.assertEqual(result, {"ok": True, "message_id": "777"})
        sender.assert_called_once()

    def test_never_claimed_item_refused_past_1800s(self):
        token = self.claim_item(
            received_at=1000.0, claimed_at=1000.0, ever_claimed=False
        )
        result, sender = self.send(token, now=3000.0)
        self.assertEqual(result, {"ok": False, "stage": "stale"})
        sender.assert_not_called()
        item = queue_core.find_item(self.store.load(), "telegram", 101)
        self.assertEqual(item["status"], "claimed")

    def test_claimed_work_refused_past_one_day(self):
        # The claimed limit is a day, not forever: 90000s old is stale.
        token = self.claim_item(received_at=1000.0, claimed_at=1000.0)
        result, sender = self.send(token, now=91000.0)
        self.assertEqual(result, {"ok": False, "stage": "stale"})
        sender.assert_not_called()

    def test_negative_age_disables_guard_for_both(self):
        token = self.claim_item(
            received_at=1000.0, claimed_at=1000.0, ever_claimed=False
        )
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: "/fake/sender"}):
            with mock.patch.object(
                telegram, "_run_send_command", return_value=("sent", "777")
            ) as sender:
                result = telegram.send_fenced(
                    self.store, 101, token, 123456789, "the answer",
                    reply_to=55, max_reply_age=-1, now=99999.0,
                )
        self.assertEqual(result, {"ok": True, "message_id": "777"})
        sender.assert_called_once()

    def test_batch_preflight_uses_claimed_limit_for_extra(self):
        # A batch reply covering an old claimed extra passes the
        # pre-flight stale check under the claimed limit.
        with self.store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id=101,
                chat_id=123456789, chat_type="group", sender_id=7,
                text="first", message_id=55, now=1000.0,
            )
            queue_core.enqueue(
                state, platform="telegram", external_id=102,
                chat_id=123456789, chat_type="group", sender_id=7,
                text="second", message_id=56, now=1000.0,
            )
            batch = queue_core.claim_batch(state, platform="telegram", now=1000.0)
            token = batch["claim_token"]
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: "/fake/sender"}):
            with mock.patch.object(
                telegram, "_run_send_command", return_value=("sent", "777")
            ):
                result = telegram.send_batch_and_close(
                    self.store, 101, token, 123456789, "one reply for both",
                    reply_to=55, extra_targets=(102,),
                    max_reply_age=1800.0, now=3000.0,
                )
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["covered"], [102])


if __name__ == "__main__":
    unittest.main()
