"""Offline tests for the Telegram group sharing modes (trusted / guarded)
and the safety trip (bridge_core.telegram).

The mode is per-group, locked in state["groups"][chat_id]["mode"];
no Telegram message or command can change it. The safety trip stops personal-detail
sharing when someone who should not be there is detected, without
changing the stored mode. The Bot API is stubbed; no network or
credentials are used.
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
STRANGER = 222
OTHER_BOT = 333
BOT_USERNAME = "ExampleBot"

LIVE_UNIFIED = Path.home() / "hooks" / "prompts" / "unified-live.md"


def _live_unified_texts():
    """The live unified wake prompt (v2), from every place it lives.

    The hook definition prompt is what the agent actually sees; the
    unified-live.md reference copy is the fallback. Returns a list of
    (label, text) pairs; empty when no live unified prompt is present.
    """
    texts = []
    for definition_name in (
        "telegram-bridge-intake.json",
        "telegram-bridge.json",
    ):
        hook_def = Path.home() / "hooks" / "definitions" / definition_name
        if hook_def.is_file():
            try:
                texts.append((
                    f"live {definition_name} prompt",
                    json.loads(hook_def.read_text())["prompt"],
                ))
            except (KeyError, ValueError):
                pass
    if LIVE_UNIFIED.is_file():
        texts.append(("live unified-live.md", LIVE_UNIFIED.read_text()))
    return texts


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


class GroupModeTests(unittest.TestCase):
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

    def preset_state(self, mode="guarded", trusted_bots=None, group=True):
        """A state with the owner adopted and (by default) the group
        adopted, the mode set, and the trusted-bot list set."""
        store = JsonStateStore(self.state_path)
        with store.transaction() as state:
            telegram._ensure_adapter_state(state)
            state["owner_id"] = OWNER
            state["trusted_bots"] = (
                list(trusted_bots) if trusted_bots is not None
                else [BOT_USERNAME.lower()]
            )
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

    # -- defaults and config -------------------------------------------

    def test_mode_defaults_to_guarded_and_trusted_bots_seeded(self):
        store = JsonStateStore(self.state_path)
        with store.transaction() as state:
            telegram._ensure_adapter_state(state)
        # No group is adopted yet: the setup default is guarded.
        self.assertEqual(state["default_group_mode"], "guarded")
        self.assertEqual(state["group_ids"], [])
        self.assertEqual(state["trusted_bots"], ["examplebot"])

    def test_payload_carries_mode_fields_trusted(self):
        self.preset_state(mode="trusted")
        payload = self.run_updates([
            msg_update(1, GROUP, "group", OWNER, "hey?", username="owner"),
        ])
        self.assertIsNotNone(payload)
        self.assertEqual(payload["group_mode"], "trusted")
        self.assertFalse(payload["group_safety_tripped"])
        self.assertEqual(payload["group_mode_effective"], "trusted")
        self.assertNotIn("group_mode_notice", payload)

    def test_payload_guarded_mode(self):
        self.preset_state(mode="guarded")
        payload = self.run_updates([
            msg_update(1, GROUP, "group", OWNER, "hey?", username="owner"),
        ])
        self.assertIsNotNone(payload)
        self.assertEqual(payload["group_mode"], "guarded")
        self.assertEqual(payload["group_mode_effective"], "guarded")

    def test_dm_payload_effective_trusted_even_when_group_guarded(self):
        self.preset_state(mode="guarded", group=False)
        payload = self.run_updates([
            msg_update(1, 999, "private", OWNER, "hey?", username="owner"),
        ])
        self.assertIsNotNone(payload)
        self.assertEqual(payload["group_mode"], "guarded")
        self.assertEqual(payload["group_mode_effective"], "trusted")

    # -- no Telegram input changes the mode ----------------------------

    def test_trust_text_from_owner_does_not_change_mode(self):
        self.preset_state(mode="guarded")
        self.run_updates([
            msg_update(1, GROUP, "group", OWNER, "/trust", username="owner"),
        ])
        self.assertEqual(self.group_state()["mode"], "guarded")

    def test_guard_text_from_owner_does_not_change_trusted_mode(self):
        self.preset_state(mode="trusted")
        self.run_updates([
            msg_update(1, GROUP, "group", OWNER, "/guard", username="owner"),
            msg_update(2, GROUP, "group", OWNER, "/mode", username="owner"),
            msg_update(3, GROUP, "group", OWNER, "/trustbot @WorkBot",
                     username="owner"),
        ])
        state = self.load_state()
        self.assertEqual(self.group_state(state)["mode"], "trusted")
        # The list is unchanged too: no Telegram command manages it.
        self.assertEqual(state["trusted_bots"], ["examplebot"])

    def test_stranger_mode_word_does_not_change_mode(self):
        self.preset_state(mode="trusted")
        self.run_updates([
            msg_update(1, GROUP, "group", STRANGER, "/trust",
                     username="stranger"),
        ])
        # The mode is untouched even though the stranger's message
        # trips the safety (a stranger speaking is a trip trigger).
        state = self.load_state()
        self.assertEqual(self.group_state(state)["mode"], "trusted")
        self.assertIsNotNone(self.group_state(state)["safety_tripped"])

    # -- safety trip ----------------------------------------------------

    def test_stranger_speaking_trips_safety_without_changing_mode(self):
        self.preset_state(mode="trusted")
        payload = self.run_updates([
            msg_update(1, GROUP, "group", STRANGER, "hello?",
                     username="stranger"),
        ])
        self.assertIsNotNone(payload)
        state = self.load_state()
        self.assertEqual(self.group_state(state)["mode"], "trusted")
        self.assertIsNotNone(self.group_state(state)["safety_tripped"])
        self.assertEqual(self.group_state(state)["safety_tripped"]["reason"], "stranger")
        self.assertEqual(payload["group_mode"], "trusted")
        self.assertTrue(payload["group_safety_tripped"])
        self.assertEqual(payload["group_mode_effective"], "guarded")
        self.assertEqual(payload["group_mode_notice"]["reason"], "stranger")

    def test_unknown_bot_speaking_trips(self):
        self.preset_state(mode="trusted")
        self.run_updates([
            msg_update(1, GROUP, "group", OTHER_BOT, "hello?",
                     username="SomeOtherBot", is_bot=True),
        ])
        state = self.load_state()
        self.assertEqual(self.group_state(state)["mode"], "trusted")
        self.assertIsNotNone(self.group_state(state)["safety_tripped"])
        self.assertEqual(self.group_state(state)["safety_tripped"]["reason"], "stranger")

    def test_trusted_bot_speaking_does_not_trip(self):
        self.preset_state(mode="trusted")
        payload = self.run_updates([
            msg_update(1, GROUP, "group", 444, "hello?",
                     username="ExampleBot", is_bot=True),
        ])
        self.assertIsNotNone(payload)
        self.assertFalse(payload["group_safety_tripped"])
        self.assertEqual(payload["group_mode_effective"], "trusted")

    def test_owner_speaking_does_not_trip(self):
        self.preset_state(mode="trusted")
        payload = self.run_updates([
            msg_update(1, GROUP, "group", OWNER, "hey?", username="owner"),
        ])
        self.assertIsNotNone(payload)
        self.assertFalse(payload["group_safety_tripped"])
        self.assertEqual(payload["group_mode_effective"], "trusted")

    def test_member_count_over_expected_trips(self):
        # Expected trusted count is 2 (owner + this bot); the group
        # reports 5, so someone extra is in there.
        self.preset_state(mode="trusted")
        payload = self.run_updates([
            msg_update(1, GROUP, "group", OWNER, "hey?", username="owner"),
        ], member_count=5)
        self.assertIsNotNone(payload)
        state = self.load_state()
        self.assertEqual(self.group_state(state)["mode"], "trusted")
        self.assertIsNotNone(self.group_state(state)["safety_tripped"])
        self.assertEqual(self.group_state(state)["safety_tripped"]["reason"],
                         "member_count")
        self.assertEqual(payload["group_mode_effective"], "guarded")
        self.assertEqual(payload["group_mode_notice"]["reason"],
                         "member_count")

    def test_member_count_at_expected_does_not_trip(self):
        self.preset_state(mode="trusted")
        payload = self.run_updates([
            msg_update(1, GROUP, "group", OWNER, "hey?", username="owner"),
        ], member_count=2)
        self.assertIsNotNone(payload)
        self.assertFalse(payload["group_safety_tripped"])
        self.assertEqual(payload["group_mode_effective"], "trusted")

    def test_notice_included_once_then_cleared(self):
        self.preset_state(mode="trusted")
        first = self.run_updates([
            msg_update(1, GROUP, "group", STRANGER, "hello?",
                     username="stranger"),
        ])
        self.assertIn("group_mode_notice", first)
        # Close the first batch (as a real turn would) so the lane
        # can claim the next one.
        run_cli(self.state_path, ["--done", "1", first["claim_token"]],
                rate_path=self.rate_path)
        second = self.run_updates([
            msg_update(2, GROUP, "group", OWNER, "hey?", username="owner"),
        ])
        self.assertIsNotNone(second)
        self.assertNotIn("group_mode_notice", second)
        # The trip itself persists until Muse clears it.
        self.assertTrue(second["group_safety_tripped"])
        self.assertEqual(second["group_mode_effective"], "guarded")

    def test_guarded_mode_never_trips(self):
        # In guarded mode sharing is already off, so a stranger
        # speaking is normal group traffic, not a trip.
        self.preset_state(mode="guarded")
        self.run_updates([
            msg_update(1, GROUP, "group", STRANGER, "hello?",
                     username="stranger"),
        ])
        self.assertIsNone(self.group_state()["safety_tripped"])

    # -- operator CLIs (Muse only, never Telegram) ----------------------

    def test_set_group_mode_cli(self):
        self.preset_state(mode="guarded")
        result = run_cli(self.state_path, ["--set-group-mode", "trusted"],
                         rate_path=self.rate_path)
        self.assertEqual(result, {"ok": True, "chat_id": GROUP,
                                  "group_mode": "trusted"})
        self.assertEqual(self.group_state()["mode"], "trusted")
        bad = run_cli(self.state_path, ["--set-group-mode", "bogus"],
                      rate_path=self.rate_path)
        self.assertFalse(bad["ok"])
        self.assertEqual(self.group_state()["mode"], "trusted")

    def test_set_group_mode_clears_trip(self):
        self.preset_state(mode="trusted")
        self.run_updates([
            msg_update(1, GROUP, "group", STRANGER, "hello?",
                     username="stranger"),
        ])
        self.assertIsNotNone(self.group_state()["safety_tripped"])
        result = run_cli(self.state_path, ["--set-group-mode", "guarded"],
                         rate_path=self.rate_path)
        self.assertTrue(result["ok"])
        state = self.load_state()
        self.assertEqual(self.group_state(state)["mode"], "guarded")
        self.assertIsNone(self.group_state(state)["safety_tripped"])
        self.assertIsNone(self.group_state(state)["mode_notice"])

    def test_set_trusted_bots_cli_keeps_this_bot_and_changes_nothing_else(self):
        self.preset_state(mode="trusted")
        self.run_updates([
            msg_update(1, GROUP, "group", STRANGER, "hello?",
                     username="stranger"),
        ])
        result = run_cli(
            self.state_path,
            ["--set-trusted-bots", "WorkBot, @BusinessBot"],
            rate_path=self.rate_path,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["trusted_bots"],
                         ["businessbot", "examplebot", "workbot"])
        state = self.load_state()
        # Adding a bot does not change the mode and does not clear
        # a safety trip.
        self.assertEqual(self.group_state(state)["mode"], "trusted")
        self.assertIsNotNone(self.group_state(state)["safety_tripped"])

    def test_clear_trip_cli(self):
        self.preset_state(mode="trusted")
        self.run_updates([
            msg_update(1, GROUP, "group", STRANGER, "hello?",
                     username="stranger"),
        ])
        result = run_cli(self.state_path, ["--clear-group-safety-trip"],
                         rate_path=self.rate_path)
        self.assertEqual(result, {"ok": True, "chat_id": GROUP,
                                  "was_tripped": True})
        state = self.load_state()
        self.assertIsNone(self.group_state(state)["safety_tripped"])
        self.assertIsNone(self.group_state(state)["mode_notice"])
        # The stored mode is untouched.
        self.assertEqual(self.group_state(state)["mode"], "trusted")

    def test_adopt_with_mode(self):
        result = run_cli(self.state_path, ["--adopt", str(OWNER), "trusted"],
                         rate_path=self.rate_path)
        self.assertEqual(result["owner_id"], OWNER)
        self.assertEqual(result["default_group_mode"], "trusted")
        state = self.load_state()
        self.assertEqual(state["owner_id"], OWNER)
        # --adopt records the setup default; no group is adopted
        # yet, so the first group adopted later gets it.
        self.assertEqual(state["default_group_mode"], "trusted")
        self.assertEqual(state["group_ids"], [])
        bad = run_cli(self.state_path, ["--adopt", str(OWNER), "bogus"],
                      rate_path=self.rate_path)
        self.assertFalse(bad["ok"])

    def test_group_mode_cli_read(self):
        self.preset_state(mode="trusted")
        result = run_cli(self.state_path, ["--group-mode"],
                         rate_path=self.rate_path)
        self.assertTrue(result["ok"])
        self.assertEqual(result["trusted_bots"], ["examplebot"])
        self.assertEqual(len(result["groups"]), 1)
        view = result["groups"][0]
        self.assertEqual(view["chat_id"], GROUP)
        self.assertEqual(view["mode"], "trusted")
        self.assertIsNone(view["safety_tripped"])
        self.assertEqual(view["mode_effective"], "trusted")

    # -- prompt carries the mode rules ----------------------------------

    def test_prompt_contains_mode_rules(self):
        # The live unified wake prompt (v2) carries the mode rules;
        # the tier files it replaces are gone on a v2 install. The
        # unified prompt is one prompt, so the quick tier's subset
        # ("group_mode_effective", "/trust") is subsumed by the full
        # set asserted here.
        texts = _live_unified_texts()
        if not texts:
            self.skipTest("live unified prompt not present on this machine")
        for where, text in texts:
            self.assertIn("group_mode_effective", text, where)
            self.assertIn("TRUSTED", text, where)
            self.assertIn("GUARDED", text, where)
            self.assertIn("No fishing", text, where)
            self.assertIn("/trust", text, where)
            self.assertIn("group_mode_notice", text, where)


if __name__ == "__main__":
    unittest.main()
