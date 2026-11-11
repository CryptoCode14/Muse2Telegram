"""Regression tests for the Nov 11 intake crash and never-lose-a-message.

Root cause (Nov 11, 13:15-13:16 MST): a stale single-group
bridge_core/telegram.py (direct state["group_id"] /
state["group_buffer"] reads) ran against a state already migrated to
the per-group shape, so every poll raised KeyError and the bot went
silent in the group. The fix: defensive state access everywhere, and
per-update isolation in ingest that HOLDS a failed message in pending
(a minimal enqueue, so the next poll retries it) instead of dropping
it, a minimal payload fallback, and a loud unanswered record as the
last resort.

Invariant: a message addressed to the bot is answered (or held
pending for retry), never silently dropped.
"""
import unittest
from unittest import mock

from bridge_core import queue as queue_core
from bridge_core import telegram

OWNER = 111
GROUP = -777


class FakeApi:
    """Canned Bot API: owner is creator, 2 members, no network."""

    def __call__(self, method, params, timeout=20.0):
        if method == "getChatAdministrators":
            return {"ok": True, "result": [{"user": {"id": OWNER}, "status": "creator"}]}
        if method == "getChatMemberCount":
            return {"ok": True, "result": 2}
        if method == "answerCallbackQuery":
            return {"ok": True, "result": True}
        raise AssertionError(f"unexpected Bot API call in test: {method}")


API = FakeApi()


def msg_update(uid, chat_id, sender_id, text, chat_type="group"):
    return {
        "update_id": uid,
        "message": {
            "message_id": uid,
            "from": {"id": sender_id, "is_bot": False},
            "chat": {"id": chat_id, "type": chat_type},
            "text": text,
            "date": 1700000000 + uid,
        },
    }


def voice_update(uid, chat_id, sender_id):
    return {
        "update_id": uid,
        "message": {
            "message_id": uid,
            "from": {"id": sender_id, "is_bot": False},
            "chat": {"id": chat_id, "type": "group"},
            "voice": {"file_id": "voice-file", "duration": 5},
            "date": 1700000000 + uid,
        },
    }


def fresh_state():
    state = {"owner_id": OWNER, "offset": 0}
    telegram._ensure_adapter_state(state)
    return state


def claim_and_payload(state):
    batch = queue_core.claim_batch(state, platform=telegram.PLATFORM, batch_size=10)
    if batch is None:
        return None
    return telegram.build_payload(state, batch)


class LegacyStateTests(unittest.TestCase):
    def test_legacy_state_migrates_and_payloads(self):
        """A pre-multi-group state (group_id/group_buffer keys) must
        ingest and payload without KeyError; the legacy keys migrate
        away. This is the Nov 11 crash shape."""
        state = {
            "owner_id": OWNER,
            "offset": 0,
            "group_id": GROUP,
            "group_mode": "trusted",
            "group_admins": {"chat_id": GROUP, "creator_id": OWNER,
                             "admin_ids": [OWNER], "fetched_at": 1700000000},
            "group_member_count": {"chat_id": GROUP, "count": 2,
                                   "fetched_at": 1700000000},
            "group_safety_tripped": None,
            "group_mode_notice": None,
            "group_buffer": [{"external_id": "1", "message_id": 1,
                              "sender_name": "@o", "is_bot": False,
                              "text": "hi", "date": 1700000001}],
        }
        telegram.ingest(
            state, {"ok": True, "result": [msg_update(10, GROUP, OWNER, "hello")]},
            now=1700000100, api_call=API,
        )
        payload = claim_and_payload(state)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["text"], "hello")
        self.assertNotIn("group_id", state)
        self.assertNotIn("group_buffer", state)

    def test_group_record_without_buffer_does_not_crash(self):
        """A group record predating the 'buffer' field must not
        KeyError on the next group message; the record is normalized."""
        state = fresh_state()
        state["groups"][str(GROUP)] = {
            "mode": "trusted", "admins": None, "member_count": None,
            "safety_tripped": None, "mode_notice": None,
            # NOTE: no "buffer" key
        }
        state["group_ids"] = [GROUP]
        telegram.ingest(
            state, {"ok": True, "result": [msg_update(10, GROUP, OWNER, "hello")]},
            now=1700000100, api_call=API,
        )
        self.assertEqual(len(state["queue"]), 1)
        self.assertEqual(state["queue"][0]["text"], "hello")
        self.assertIn("buffer", state["groups"][str(GROUP)])

    def test_item_missing_display_fields_still_payloads(self):
        """A queue item missing sender_name/text (older version) must
        still reach the agent instead of KeyError in _payload_item."""
        state = fresh_state()
        telegram.ingest(
            state, {"ok": True, "result": [msg_update(10, GROUP, OWNER, "hello")]},
            now=1700000100, api_call=API,
        )
        del state["queue"][0]["sender_name"]
        del state["queue"][0]["text"]
        payload = claim_and_payload(state)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["sender_name"], "")
        self.assertEqual(payload["items"][0]["text"], "")


class NeverLoseAMessageTests(unittest.TestCase):
    def test_failed_processing_holds_message_pending(self):
        """INVARIANT: a message whose normal processing raises is
        HELD in pending (minimal enqueue) so the next poll retries
        it -- not dropped. It must end up answerable."""
        state = fresh_state()
        with mock.patch.object(
            telegram, "_adopt_group", side_effect=RuntimeError("boom")
        ):
            telegram.ingest(
                state, {"ok": True, "result": [msg_update(10, GROUP, OWNER, "hello")]},
                now=1700000100, api_call=API,
            )
        # Held, not dropped: one pending item with the message text.
        self.assertEqual(len(state["queue"]), 1)
        item = state["queue"][0]
        self.assertEqual(item["status"], queue_core.PENDING)
        self.assertEqual(item["text"], "hello")
        self.assertEqual(item["chat_id"], GROUP)
        # The offset advanced (the update was consumed into the
        # queue); the retry happens via the queue, not re-delivery.
        self.assertEqual(state["offset"], 11)
        # And it is answerable on the next poll: claim + payload.
        payload = claim_and_payload(state)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["text"], "hello")

    def test_failed_voice_message_held_with_placeholder(self):
        """A voice message whose download fails is still held in
        pending with a placeholder, so the agent can answer it
        (asking for a resend) instead of staying silent."""
        state = fresh_state()
        with mock.patch.object(
            telegram, "voice_marker", side_effect=RuntimeError("download failed")
        ):
            telegram.ingest(
                state, {"ok": True, "result": [voice_update(10, GROUP, OWNER)]},
                now=1700000100, api_call=API,
            )
        self.assertEqual(len(state["queue"]), 1)
        item = state["queue"][0]
        self.assertEqual(item["status"], queue_core.PENDING)
        self.assertIn("voice message", item["text"])
        payload = claim_and_payload(state)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["count"], 1)

    def test_bad_update_among_good_ones_keeps_good(self):
        """One bad update must not abort the poll or lose the good
        messages around it. The bad one is surfaced loudly."""
        state = fresh_state()
        good1 = msg_update(10, GROUP, OWNER, "first")
        bad = msg_update(11, GROUP, OWNER, "boom")
        del bad["update_id"]
        good2 = msg_update(12, GROUP, OWNER, "third")
        telegram.ingest(
            state, {"ok": True, "result": [good1, bad, good2]},
            now=1700000100, api_call=API,
        )
        queued = [item["text"] for item in state["queue"]]
        self.assertEqual(queued, ["first", "third"])
        unanswered = state.get("intake_unanswered", [])
        self.assertEqual(len(unanswered), 1)
        self.assertEqual(unanswered[0]["text"], "boom")
        payload = claim_and_payload(state)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["count"], 2)

    def test_unprocessable_update_is_surfaced_loudly(self):
        """An update that cannot be held at all (no update id) is
        recorded as unanswered with its text -- never silently
        closed -- and does not raise."""
        state = fresh_state()
        bad = msg_update(10, GROUP, OWNER, "no id here")
        del bad["update_id"]
        telegram.ingest(
            state, {"ok": True, "result": [bad]},
            now=1700000100, api_call=API,
        )
        self.assertEqual(state["queue"], [])
        unanswered = state.get("intake_unanswered", [])
        self.assertEqual(len(unanswered), 1)
        self.assertEqual(unanswered[0]["text"], "no id here")
        self.assertIsNone(unanswered[0]["update_id"])

    def test_payload_falls_back_to_minimal_view(self):
        """When the full payload view raises for an item, the batch
        still returns a payload with a minimal view of that item, so
        the agent can answer it."""
        state = fresh_state()
        telegram.ingest(
            state, {"ok": True, "result": [msg_update(10, GROUP, OWNER, "hello")]},
            now=1700000100, api_call=API,
        )
        batch = queue_core.claim_batch(
            state, platform=telegram.PLATFORM, batch_size=10
        )
        self.assertIsNotNone(batch)
        with mock.patch.object(
            telegram, "_payload_item", side_effect=RuntimeError("boom")
        ):
            payload = telegram.build_payload(state, batch)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["text"], "hello")

    def test_build_payload_empty_batch_returns_none(self):
        """An empty batch builds no payload instead of IndexError."""
        state = fresh_state()
        self.assertIsNone(
            telegram.build_payload(state, {"claim_token": "t", "items": []})
        )


if __name__ == "__main__":
    unittest.main()
