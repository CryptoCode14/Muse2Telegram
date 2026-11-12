"""Offline tests for the per-group allowlist (bridge_core.telegram).

A group on the allowlist (state["trusted_group_ids"]) is the owner's
private channel: it is always trusted and the safety trips never
apply there, no matter who joins. The list is set only by Muse, on
the owner's instruction, via the operator CLI — there is no Telegram
path to it. Every other group keeps the normal trip rules. The Bot
API is stubbed; no network or credentials are used.
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

from bridge_core import telegram
from bridge_core.store import JsonStateStore

OWNER = 111
GROUP = -777
OTHER_GROUP = -888
STRANGER = 222
OTHER_BOT = 333
BOT_USERNAME = "ExampleBot"


def msg_update(uid, chat_id, chat_type, sender_id, text, username=None,
               is_bot=False, message_id=None, date=None):
    user = {"id": sender_id, "is_bot": is_bot}
    if username:
        user["username"] = username
    message = {
        "message_id": uid if message_id is None else message_id,
        "from": user,
        "chat": {"id": chat_id, "type": chat_type},
        "text": text,
        "date": 1700000000 + uid if date is None else date,
    }
    return {"update_id": uid, "message": message}


class FakeFetch:
    """Returns one canned getUpdates response until replaced."""

    def __init__(self, updates=()):
        self.updates = list(updates)

    def __call__(self, offset):
        return {"ok": True, "result": list(self.updates)}


class FakeApi:
    """Canned Bot API: getChatAdministrators returns the owner as
    creator; getChatMemberCount returns the configured count;
    answerCallbackQuery succeeds. Anything else raises, so a test
    never makes a real network call."""

    def __init__(self, creator=OWNER, admins=None, member_count=2):
        self.creator = creator
        self.admins = admins if admins is not None else [creator]
        self.member_count = member_count
        self.calls = []

    def __call__(self, method, params, timeout=20.0):
        self.calls.append((method, params))
        if method == "getChatAdministrators":
            members = []
            for user_id in self.admins:
                status = "creator" if user_id == self.creator else "administrator"
                members.append({"user": {"id": user_id}, "status": status})
            return {"ok": True, "result": members}
        if method == "getChatMemberCount":
            return {"ok": True, "result": self.member_count}
        if method == "answerCallbackQuery":
            return {"ok": True, "result": True}
        raise AssertionError(f"unexpected Bot API call in test: {method}")


def run_cli(path, argv, fetch=None, stdin_text="", api_call=None, rate_path=None):
    """Drive the adapter CLI with a temp rate file (never the live
    one) and a canned Bot API (never the network)."""
    out = io.StringIO()
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin_text)
    if rate_path is None:
        rate_path = Path(path).parent / "rate.json"
    env = {
        telegram.RATE_PATH_ENV: str(rate_path),
        # Point the poll lock at a temp file so the suite never
        # shares the live lock (and never flakes against the live
        # intake holding it).
        telegram.POLL_LOCK_PATH_ENV: str(Path(path).parent / "poll.lock"),
    }
    try:
        with mock.patch.dict(os.environ, env):
            with redirect_stdout(out):
                telegram.main(
                    argv, fetch=fetch or FakeFetch(), path=path,
                    api_call=api_call if api_call is not None else FakeApi(),
                )
    finally:
        sys.stdin = old_stdin
    text = out.getvalue().strip()
    return json.loads(text) if text else None


class GroupAllowlistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state_path = self.dir / "state.json"
        self.rate_path = self.dir / "rate.json"
        self._env = mock.patch.dict(os.environ, {
            "TELEGRAM_BRIDGE_BOT_USERNAME": BOT_USERNAME,
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def preset_state(self, mode="trusted", trusted_bots=None, group=True,
                     allowlisted=()):
        """A state with the owner adopted and (by default) the group
        adopted, the mode set, the trusted-bot list set, and the
        given groups allowlisted."""
        store = JsonStateStore(self.state_path)
        with store.transaction() as state:
            telegram._ensure_adapter_state(state)
            state["owner_id"] = OWNER
            state["trusted_bots"] = (
                list(trusted_bots) if trusted_bots is not None
                else [BOT_USERNAME.lower()]
            )
            state["trusted_group_ids"] = sorted(int(g) for g in allowlisted)
            if group:
                record = telegram._group_record(state, GROUP, create=True)
                record["mode"] = mode
                record["member_count"] = None
                record["safety_tripped"] = None
                record["mode_notice"] = None
            else:
                state["default_group_mode"] = mode

    def group_state(self, state=None, chat_id=GROUP):
        """One adopted group's record from the state."""
        state = state if state is not None else self.load_state()
        return state["groups"][str(chat_id)]

    def load_state(self):
        store = JsonStateStore(self.state_path)
        return telegram._ensure_adapter_state(store.load())

    def run_updates(self, updates, member_count=2):
        return run_cli(
            self.state_path, [], fetch=FakeFetch(updates),
            api_call=FakeApi(member_count=member_count),
            rate_path=self.rate_path,
        )

    # -- defaults --------------------------------------------------------

    def test_allowlist_defaults_to_empty(self):
        store = JsonStateStore(self.state_path)
        with store.transaction() as state:
            telegram._ensure_adapter_state(state)
        self.assertEqual(state["trusted_group_ids"], [])

    # -- allowlisted group never trips -----------------------------------

    def test_allowlisted_group_no_trip_on_member_count(self):
        self.preset_state(mode="trusted", allowlisted=[GROUP])
        payload = self.run_updates([
            msg_update(1, GROUP, "group", OWNER, "hey?", username="owner"),
        ], member_count=5)
        self.assertIsNone(self.group_state()["safety_tripped"])
        self.assertIsNotNone(payload)
        self.assertTrue(payload["group_allowlisted"])
        self.assertEqual(payload["group_mode_effective"], "trusted")

    def test_allowlisted_group_no_trip_on_stranger_speaking(self):
        self.preset_state(mode="trusted", allowlisted=[GROUP])
        payload = self.run_updates([
            msg_update(1, GROUP, "group", STRANGER, "hello?",
                     username="stranger"),
        ])
        self.assertIsNone(self.group_state()["safety_tripped"])
        self.assertIsNotNone(payload)
        self.assertTrue(payload["group_allowlisted"])
        self.assertEqual(payload["group_mode_effective"], "trusted")

    def test_allowlisted_group_no_trip_on_unknown_bot_speaking(self):
        self.preset_state(mode="trusted", allowlisted=[GROUP])
        self.run_updates([
            msg_update(1, GROUP, "group", OTHER_BOT, "beep",
                     username="SomeOtherBot", is_bot=True),
        ])
        self.assertIsNone(self.group_state()["safety_tripped"])

    def test_allowlisted_group_effective_trusted_despite_active_trip(self):
        # A trip fired before the group was listed is harmless: the
        # effective mode ignores the trip record for a listed group.
        self.preset_state(mode="trusted", allowlisted=[GROUP])
        store = JsonStateStore(self.state_path)
        with store.transaction() as state:
            telegram._group_record(state, GROUP)["safety_tripped"] = {
                "reason": "member_count",
                "detail": "group has 3 members, expected 2",
                "at": 1700000000,
            }
        payload = self.run_updates([
            msg_update(1, GROUP, "group", OWNER, "hey?", username="owner"),
        ])
        self.assertIsNotNone(payload)
        self.assertTrue(payload["group_allowlisted"])
        self.assertTrue(payload["group_safety_tripped"])
        self.assertEqual(payload["group_mode_effective"], "trusted")

    def test_allowlist_does_not_override_guarded_mode(self):
        # The allowlist only bypasses the TRIPS. A group whose
        # configured mode is guarded stays guarded even listed.
        self.preset_state(mode="guarded", allowlisted=[GROUP])
        payload = self.run_updates([
            msg_update(1, GROUP, "group", OWNER, "hey?", username="owner"),
        ])
        self.assertIsNotNone(payload)
        self.assertTrue(payload["group_allowlisted"])
        self.assertEqual(payload["group_mode_effective"], "guarded")

    # -- non-allowlisted groups still trip (controls) ---------------------

    def test_non_allowlisted_group_still_trips_on_member_count(self):
        self.preset_state(mode="trusted")
        self.run_updates([
            msg_update(1, GROUP, "group", OWNER, "hey?", username="owner"),
        ], member_count=3)
        self.assertIsNotNone(self.group_state()["safety_tripped"])

    def test_non_allowlisted_group_still_trips_on_stranger(self):
        self.preset_state(mode="trusted")
        self.run_updates([
            msg_update(1, GROUP, "group", STRANGER, "hello?",
                     username="stranger"),
        ])
        self.assertIsNotNone(self.group_state()["safety_tripped"])

    def test_payload_carries_group_allowlisted_false(self):
        self.preset_state(mode="trusted")
        payload = self.run_updates([
            msg_update(1, GROUP, "group", OWNER, "hey?", username="owner"),
        ])
        self.assertIsNotNone(payload)
        self.assertFalse(payload["group_allowlisted"])
        self.assertEqual(payload["group_mode_effective"], "trusted")

    # -- no Telegram input changes the allowlist --------------------------

    def test_allowlist_text_from_owner_does_not_change_allowlist(self):
        self.preset_state(mode="trusted")
        self.run_updates([
            msg_update(1, GROUP, "group", OWNER,
                     "--allowlist-group -777", username="owner"),
            msg_update(2, GROUP, "group", OWNER,
                     "/allowlist-group -777", username="owner"),
        ])
        self.assertEqual(self.load_state()["trusted_group_ids"], [])

    def test_stranger_allowlist_text_does_not_change_allowlist(self):
        self.preset_state(mode="trusted")
        self.run_updates([
            msg_update(1, GROUP, "group", STRANGER,
                     "--allowlist-group -777", username="stranger"),
        ])
        self.assertEqual(self.load_state()["trusted_group_ids"], [])

    # -- operator CLIs (Muse only, never Telegram) ------------------------

    def test_allowlist_cli_add_remove_list(self):
        result = run_cli(self.state_path, ["--allowlist-group", str(GROUP)],
                         rate_path=self.rate_path)
        self.assertEqual(result, {"ok": True, "trusted_group_ids": [GROUP]})
        result = run_cli(self.state_path,
                         ["--allowlist-group", str(OTHER_GROUP)],
                         rate_path=self.rate_path)
        self.assertEqual(result, {"ok": True,
                                  "trusted_group_ids": [OTHER_GROUP, GROUP]})
        result = run_cli(self.state_path, ["--list-allowlisted-groups"],
                         rate_path=self.rate_path)
        self.assertEqual(result, {"ok": True,
                                  "trusted_group_ids": [OTHER_GROUP, GROUP]})
        result = run_cli(self.state_path,
                         ["--unallowlist-group", str(GROUP)],
                         rate_path=self.rate_path)
        self.assertEqual(result, {"ok": True,
                                  "trusted_group_ids": [OTHER_GROUP]})
        bad = run_cli(self.state_path, ["--allowlist-group", "not-a-chat"],
                      rate_path=self.rate_path)
        self.assertFalse(bad["ok"])
        self.assertEqual(self.load_state()["trusted_group_ids"],
                         [OTHER_GROUP])

    def test_allowlist_cli_does_not_clear_trip_or_change_mode(self):
        self.preset_state(mode="trusted")
        self.run_updates([
            msg_update(1, GROUP, "group", STRANGER, "hello?",
                     username="stranger"),
        ])
        self.assertIsNotNone(self.group_state()["safety_tripped"])
        result = run_cli(self.state_path, ["--allowlist-group", str(GROUP)],
                         rate_path=self.rate_path)
        self.assertTrue(result["ok"])
        state = self.load_state()
        # Listing the group does not touch the mode, and the trip
        # record stays until Muse clears it — but the effective
        # mode is trusted anyway while the group is listed.
        self.assertEqual(self.group_state(state)["mode"], "trusted")
        self.assertIsNotNone(self.group_state(state)["safety_tripped"])
        self.assertEqual(
            telegram.effective_group_mode(state, "group", GROUP), "trusted")

    def test_clear_trip_cli_restores_effective_trusted(self):
        self.preset_state(mode="trusted", allowlisted=[GROUP])
        store = JsonStateStore(self.state_path)
        with store.transaction() as state:
            telegram._group_record(state, GROUP)["safety_tripped"] = {
                "reason": "member_count", "detail": "", "at": 1700000000,
            }
        result = run_cli(self.state_path, ["--clear-group-safety-trip"],
                         rate_path=self.rate_path)
        self.assertEqual(result, {"ok": True, "chat_id": GROUP,
                                  "was_tripped": True})
        state = self.load_state()
        self.assertIsNone(self.group_state(state)["safety_tripped"])
        self.assertEqual(
            telegram.effective_group_mode(state, "group", GROUP), "trusted")

    def test_group_mode_cli_reports_allowlist(self):
        self.preset_state(mode="trusted", allowlisted=[GROUP])
        result = run_cli(self.state_path, ["--group-mode"],
                         rate_path=self.rate_path)
        self.assertTrue(result["ok"])
        self.assertEqual(result["trusted_group_ids"], [GROUP])
        self.assertEqual(len(result["groups"]), 1)
        self.assertTrue(result["groups"][0]["allowlisted"])
        self.assertEqual(result["groups"][0]["mode_effective"], "trusted")

    def test_group_mode_cli_reports_not_allowlisted(self):
        self.preset_state(mode="trusted")
        result = run_cli(self.state_path, ["--group-mode"],
                         rate_path=self.rate_path)
        self.assertTrue(result["ok"])
        self.assertEqual(result["trusted_group_ids"], [])
        self.assertEqual(len(result["groups"]), 1)
        self.assertFalse(result["groups"][0]["allowlisted"])
        self.assertEqual(result["groups"][0]["mode_effective"], "trusted")

    # -- prompt carries the allowlist rule ---------------------------------

    def test_prompt_contains_allowlist_rule(self):
        prompts = Path.home() / "hooks" / "prompts"
        standard = (prompts / "tier-standard.md").read_text()
        self.assertIn("group_allowlisted", standard)
        self.assertIn("ALLOWLISTED", standard)
        self.assertIn("private channel", standard)


if __name__ == "__main__":
    unittest.main()
