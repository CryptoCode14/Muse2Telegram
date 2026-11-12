"""Unit tests for the simulation harness's FakeTelegram only.

Fast, offline, stdlib unittest. Run with the v2 copy's venv:

    muse-runtime/.venv/bin/python -m unittest discover \\
        -s muse-runtime/tests -p 'test_v2_simulation.py' -v

Covers: edit probe delivered / never-sent, delete, duplicate-send
counting, sequential message ids, getUpdates offset filtering, and
inject_message shape. The real bridge_core code is NOT exercised
here — that is run_scenario.py's job.
"""
import sys
import unittest
from pathlib import Path

SIM_DIR = Path(__file__).resolve().parent.parent.parent / "simulation"
if str(SIM_DIR) not in sys.path:
    sys.path.insert(0, str(SIM_DIR))

from fake_tg import FakeTelegram  # noqa: E402


class FakeTelegramSendTests(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTelegram()

    def test_send_message_sequential_ids_and_record(self):
        first = self.tg.sendMessage(111, "hello")
        second = self.tg.sendMessage(111, "again")
        self.assertTrue(first["ok"])
        self.assertEqual(first["result"]["message_id"], 1)
        self.assertEqual(second["result"]["message_id"], 2)
        self.assertEqual(len(self.tg.sends), 2)
        self.assertEqual(self.tg.sends[0]["chat_id"], 111)
        self.assertEqual(self.tg.sends[0]["text"], "hello")
        self.assertIsNone(self.tg.sends[0]["reply_to"])

    def test_send_message_reply_to_recorded(self):
        self.tg.sendMessage(111, "reply", reply_to=42)
        self.assertEqual(self.tg.sends[0]["reply_to"], 42)

    def test_send_chat_action_recorded(self):
        result = self.tg.sendChatAction(111, "typing")
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.tg.typing_calls), 1)
        self.assertEqual(self.tg.typing_calls[0]["chat_id"], 111)
        self.assertEqual(self.tg.typing_calls[0]["action"], "typing")


class FakeTelegramEditProbeTests(unittest.TestCase):
    """The edit probe is the delivered / never-sent evidence."""

    def setUp(self):
        self.tg = FakeTelegram()
        self.sent = self.tg.sendMessage(111, "hello there")
        self.mid = self.sent["result"]["message_id"]

    def test_edit_same_text_is_not_modified_delivered(self):
        result = self.tg.editMessageText(111, self.mid, "hello there")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], 400)
        self.assertIn("message is not modified", result["description"])

    def test_edit_never_sent_id_is_not_found(self):
        result = self.tg.editMessageText(111, 9999, "hello there")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], 400)
        self.assertIn("message to edit not found", result["description"])

    def test_edit_different_text_succeeds_and_updates(self):
        result = self.tg.editMessageText(111, self.mid, "edited text")
        self.assertTrue(result["ok"])
        # The new text is now the delivered text: editing to it
        # again reports "not modified".
        again = self.tg.editMessageText(111, self.mid, "edited text")
        self.assertIn("not modified", again["description"])

    def test_edit_wrong_chat_is_not_found(self):
        result = self.tg.editMessageText(222, self.mid, "hello there")
        self.assertIn("not found", result["description"])


class FakeTelegramDeleteTests(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTelegram()
        self.mid = self.tg.sendMessage(111, "to delete")["result"]["message_id"]

    def test_delete_existing_ok_and_removes(self):
        result = self.tg.deleteMessage(111, self.mid)
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.tg.delete_calls), 1)
        # After deletion the id is gone: the probe reports not found.
        probe = self.tg.editMessageText(111, self.mid, "to delete")
        self.assertIn("not found", probe["description"])

    def test_delete_missing_is_error(self):
        result = self.tg.deleteMessage(111, 9999)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], 400)
        self.assertIn("not found", result["description"])


class FakeTelegramDuplicateTests(unittest.TestCase):
    def test_duplicate_sends_counts_extra_per_update(self):
        tg = FakeTelegram()
        tg.sendMessage(111, "same", update_id=7)
        tg.sendMessage(111, "same", update_id=7)
        tg.sendMessage(111, "same", update_id=8)  # different update: not a dupe
        tg.sendMessage(111, "other", update_id=7)
        self.assertEqual(tg.duplicate_sends(), 1)
        self.assertEqual(tg.duplicate_sends(update_id=7), 1)
        self.assertEqual(tg.duplicate_sends(update_id=8), 0)

    def test_no_duplicate_when_each_update_answered_once(self):
        tg = FakeTelegram()
        for uid in (1, 2, 3):
            tg.sendMessage(111, "reply", update_id=uid)
        self.assertEqual(tg.duplicate_sends(), 0)

    def test_unattributed_sends_excluded_from_attributed_count(self):
        tg = FakeTelegram()
        tg.sendMessage(111, "same")
        tg.sendMessage(111, "same")
        self.assertEqual(tg.duplicate_sends(), 0)
        # The coarser unattributed count does catch them.
        self.assertEqual(tg.duplicate_sends_unattributed(), 1)

    def test_find_send_returns_first_match(self):
        tg = FakeTelegram()
        tg.sendMessage(111, "hello")
        tg.sendMessage(222, "hello")
        found = tg.find_send(222, "hello")
        self.assertEqual(found["chat_id"], 222)
        self.assertIsNone(tg.find_send(111, "missing"))


class FakeTelegramUpdatesTests(unittest.TestCase):
    def test_get_updates_offset_filtering(self):
        tg = FakeTelegram()
        tg.inject_message(111, "one", sender_id=111, update_id=5)
        tg.inject_message(111, "two", sender_id=111, update_id=6)
        tg.inject_message(111, "three", sender_id=111, update_id=9)
        self.assertEqual([u["update_id"] for u in
                          tg.getUpdates(0)["result"]], [5, 6, 9])
        self.assertEqual([u["update_id"] for u in
                          tg.getUpdates(6)["result"]], [6, 9])
        self.assertEqual(tg.getUpdates(10)["result"], [])
        self.assertTrue(tg.getUpdates(0)["ok"])

    def test_inject_message_shape(self):
        tg = FakeTelegram()
        upd = tg.inject_message(-777, "group hi", sender_id=222,
                                chat_type="group", sender_name="Kai")
        msg = upd["message"]
        self.assertEqual(msg["chat"], {"id": -777, "type": "group"})
        self.assertEqual(msg["from"]["id"], 222)
        self.assertEqual(msg["from"]["first_name"], "Kai")
        self.assertEqual(msg["text"], "group hi")
        self.assertIn("message_id", msg)
        self.assertIn("date", msg)

    def test_inject_update_verbatim(self):
        tg = FakeTelegram()
        raw = {"update_id": 42, "message": {"message_id": 1, "text": "x"}}
        self.assertIs(tg.inject_update(raw), raw)
        self.assertEqual(tg.getUpdates(42)["result"], [raw])

    def test_api_call_dispatches_send_and_admins(self):
        tg = FakeTelegram()
        sent = tg.api_call("sendMessage",
                           {"chat_id": 111, "text": "via api_call"})
        self.assertTrue(sent["ok"])
        admins = tg.api_call("getChatAdministrators", {"chat_id": -777})
        self.assertTrue(admins["ok"])
        self.assertEqual(admins["result"][0]["status"], "creator")
        with self.assertRaises(Exception):
            tg.api_call("bogusMethod", {})


if __name__ == "__main__":
    unittest.main()
