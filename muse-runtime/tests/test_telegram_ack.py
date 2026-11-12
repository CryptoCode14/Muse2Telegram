"""Tests for the acknowledgement path (telegram.send_ack / --send-ack).

A turn whose work will take 10+ minutes sends the fixed ack first,
so the owner knows the turn is working. The ack does not close the
item: it stays claimed for the full reply. One ack per item.

No Muse credentials, Telegram calls, or external sender are used:
the sender subprocess is mocked.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from bridge_core import queue as queue_core
from bridge_core import telegram
from bridge_core.store import JsonStateStore


class TelegramAckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.store = JsonStateStore(self.path)
        with self.store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id=101,
                chat_id=123456789, chat_type="private", sender_id=123456789,
                text="please research this thoroughly",
                message_id=55,
            )
            self.token = queue_core.claim_batch(state, platform="telegram")["claim_token"]

    def item(self):
        return queue_core.find_item(self.store.load(), "telegram", 101)

    def ack(self, target=101, token=None, chat_id=123456789, reply_to=55,
            sender_result=("sent", "777")):
        if token is None:
            token = self.token
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_send_command", return_value=sender_result
            ) as sender:
                result = telegram.send_ack(
                    self.store, target, token, chat_id, reply_to=reply_to
                )
        return result, sender

    def test_ack_sends_fixed_text_and_item_stays_claimed(self):
        result, sender = self.ack()
        self.assertEqual(result, {"ok": True, "message_id": "777"})
        sender.assert_called_once_with(
            "/fake/sender", 123456789, telegram.ACK_TEXT, reply_to=55
        )
        self.assertEqual(telegram.ACK_TEXT,
                         "Got it, looking into this, will reply when ready.")
        item = self.item()
        self.assertEqual(item["status"], "claimed")
        self.assertIsNotNone(item["ack_sent_at"])
        self.assertEqual(item["ack_message_id"], "777")
        self.assertIsNone(item["replied_at"])
        self.assertIsNone(item["done_at"])

    def test_full_reply_still_works_after_ack(self):
        result, _sender = self.ack()
        self.assertTrue(result["ok"])
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_send_command", return_value=("sent", "888")
            ):
                close = telegram.send_and_close(
                    self.store, 101, self.token, 123456789,
                    "here is the full answer", reply_to=55,
                )
        self.assertEqual(close, {"ok": True, "message_id": "888"})
        item = self.item()
        self.assertEqual(item["status"], "done")
        self.assertEqual(item["reply_message_id"], "888")
        self.assertEqual(item["ack_message_id"], "777")

    def test_second_ack_is_refused_and_sender_not_called(self):
        first, _sender = self.ack()
        self.assertTrue(first["ok"])
        second, sender = self.ack()
        self.assertEqual(second, {"ok": False, "stage": "already-acked"})
        sender.assert_not_called()
        self.assertEqual(self.item()["ack_message_id"], "777")

    def test_ack_refused_when_reply_already_recorded(self):
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_send_command", return_value=("sent", "888")
            ):
                close = telegram.send_and_close(
                    self.store, 101, self.token, 123456789,
                    "the answer", reply_to=55,
                )
        self.assertTrue(close["ok"])
        result, sender = self.ack()
        self.assertEqual(result, {"ok": False, "stage": "fence"})
        sender.assert_not_called()

    def test_ack_refused_with_wrong_token(self):
        result, sender = self.ack(token="stale-token")
        self.assertEqual(result, {"ok": False, "stage": "fence"})
        sender.assert_not_called()
        self.assertIsNone(self.item()["ack_sent_at"])

    def test_ack_refused_with_wrong_chat(self):
        result, sender = self.ack(chat_id=987654321)
        self.assertEqual(result, {"ok": False, "stage": "fence"})
        sender.assert_not_called()
        self.assertIsNone(self.item()["ack_sent_at"])

    def test_ack_refused_with_wrong_reply_target(self):
        result, sender = self.ack(reply_to=999)
        self.assertEqual(result, {"ok": False, "stage": "fence"})
        sender.assert_not_called()
        self.assertIsNone(self.item()["ack_sent_at"])

    def test_ack_unknown_leaves_item_claimed_with_no_ack_recorded(self):
        result, sender = self.ack(sender_result=("unknown", None))
        self.assertEqual(result, {"ok": False, "stage": "unknown"})
        sender.assert_called_once()
        item = self.item()
        self.assertEqual(item["status"], "claimed")
        self.assertIsNone(item["ack_sent_at"])
        self.assertIsNone(item["ack_message_id"])
        # A retry is possible: the ack was not recorded.
        retry, _sender = self.ack()
        self.assertTrue(retry["ok"])
        self.assertEqual(self.item()["ack_message_id"], "777")

    def test_ack_without_send_command_is_config_error(self):
        # No send command, but the bot username is present: the
        # config error under test is the missing sender, not the
        # (separately fail-loud) username.
        with mock.patch.dict(
            os.environ, {telegram.BOT_USERNAME_ENV: "ExampleBot"},
            clear=True):
            with mock.patch.object(
                telegram, "_run_send_command"
            ) as sender:
                result = telegram.send_ack(
                    self.store, 101, self.token, 123456789, reply_to=55
                )
        self.assertEqual(result, {"ok": False, "stage": "config"})
        sender.assert_not_called()
        self.assertIsNone(self.item()["ack_sent_at"])

    def test_ack_for_button_tap_item_is_standalone(self):
        with self.store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id=202,
                chat_id=987654321, chat_type="private", sender_id=987654321,
                text="/menu", message_id=None, source="callback_query",
            )
            token2 = queue_core.claim_batch(state, platform="telegram")["claim_token"]
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_send_command", return_value=("sent", "999")
            ) as sender:
                result = telegram.send_ack(
                    self.store, 202, token2, 987654321, reply_to=None
                )
        self.assertEqual(result, {"ok": True, "message_id": "999"})
        sender.assert_called_once_with(
            "/fake/sender", 987654321, telegram.ACK_TEXT, reply_to=None
        )
        item = queue_core.find_item(self.store.load(), "telegram", 202)
        self.assertEqual(item["ack_message_id"], "999")

    def test_cli_send_ack_parses_args(self):
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_send_command", return_value=("sent", "777")
            ):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = telegram.main(
                        argv=["--send-ack", "101", self.token, "123456789", "55"],
                        path=self.path,
                    )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), {"ok": True, "message_id": "777"})
        self.assertEqual(self.item()["ack_message_id"], "777")


if __name__ == "__main__":
    unittest.main()
