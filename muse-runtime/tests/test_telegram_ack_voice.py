"""Per-bot ack card voices (Westin, 2026-11-13).

When every bot runs the same bridge code, the ack status card
must not read the same robotic "#1 in queue" from all four of
them: each bot says it in its own voice, like a human would
("I'm working on this, you're first in line"). The queue
position stays honest, it is just phrased per personality.
An unknown bot (or none) keeps the plain numbers-only card.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge_core import queue as queue_core  # noqa: E402
from bridge_core import telegram  # noqa: E402
from bridge_core.store import JsonStateStore  # noqa: E402


KNOWN_BOTS = (
    "doriancodingbot",
    "westinmusebot",
    "gusgrandvalleybot",
    "dexthedeveloperbot",
)


class AckVoiceTests(unittest.TestCase):
    def test_no_bot_keeps_numbers_only_card(self):
        self.assertEqual(telegram.claim_ack_text(1), "#1 in queue")
        self.assertEqual(
            telegram.claim_ack_text(2), "#2 in queue, 1 ahead of you"
        )

    def test_unknown_bot_falls_back_to_numbers_only(self):
        self.assertEqual(
            telegram.claim_ack_text(1, "ExampleBot"), "#1 in queue"
        )

    def test_each_known_bot_has_its_own_first_in_line_voice(self):
        texts = {
            bot: telegram.claim_ack_text(1, bot) for bot in KNOWN_BOTS
        }
        # Four bots, four different cards, none of them the
        # robotic numbers-only card.
        self.assertEqual(len(set(texts.values())), 4)
        for text in texts.values():
            self.assertNotEqual(text, "#1 in queue")
            self.assertIn("first", text.lower())

    def test_dorian_uses_westins_own_phrasing(self):
        text = telegram.claim_ack_text(1, "doriancodingbot")
        self.assertIn("working on this", text.lower())
        self.assertIn("first", text.lower())

    def test_later_position_stays_honest_per_bot(self):
        for bot in KNOWN_BOTS:
            text = telegram.claim_ack_text(3, bot)
            self.assertIn("3", text)
            self.assertIn("2 ahead", text)

    def test_bot_name_is_normalized(self):
        self.assertEqual(
            telegram.claim_ack_text(1, "DorianCodingBot"),
            telegram.claim_ack_text(1, "doriancodingbot"),
        )
        self.assertEqual(
            telegram.claim_ack_text(1, "@doriancodingbot"),
            telegram.claim_ack_text(1, "doriancodingbot"),
        )

    def test_delayed_and_escalated_are_personalized_too(self):
        for bot in KNOWN_BOTS:
            delayed = telegram.claim_ack_delayed_text(bot)
            escalated = telegram.claim_ack_escalated_text(bot)
            self.assertNotEqual(delayed, telegram.CLAIM_ACK_DELAYED_TEXT)
            self.assertNotEqual(
                escalated, telegram.CLAIM_ACK_ESCALATED_TEXT
            )
            self.assertNotEqual(delayed, escalated)
        self.assertEqual(
            telegram.claim_ack_delayed_text("ExampleBot"),
            telegram.CLAIM_ACK_DELAYED_TEXT,
        )
        self.assertEqual(
            telegram.claim_ack_escalated_text("ExampleBot"),
            telegram.CLAIM_ACK_ESCALATED_TEXT,
        )


class AckVoiceWiringTests(unittest.TestCase):
    """The card senders resolve the voice from this bot's own
    username (env, as the intake exports it), so a fresh
    install of the same code speaks as whichever bot runs it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.store = JsonStateStore(self.path)

    def _claimed(self):
        with self.store.transaction() as state:
            state.setdefault("queue", []).append({
                "platform": "telegram",
                "external_id": "101",
                "chat_id": -5280361168,
                "message_id": 55,
                "status": queue_core.CLAIMED,
                "claim_token": "tok",
                "claimed_at": 1000.0,
                "received_at": 999.0,
                "replied_at": None,
            })
        return "tok"

    def test_claim_ack_uses_this_bots_voice(self):
        token = self._claimed()
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "DorianCodingBot"}):
            with mock.patch.object(
                telegram, "_run_send_command",
                return_value=("sent", "777"),
            ) as sender:
                result = telegram.send_claim_ack(self.store, token)
        self.assertTrue(result["ok"])
        sender.assert_called_once_with(
            "/fake/sender", -5280361168,
            telegram.claim_ack_text(1, "doriancodingbot"), reply_to=55,
        )


if __name__ == "__main__":
    unittest.main()
