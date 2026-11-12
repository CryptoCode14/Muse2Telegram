"""Offline tests for multi-group support (bridge_core.telegram).

The bridge serves EVERY group the owner adds it to: it adopts a
group when the owner speaks (or taps a button) there, and ignores
messages from groups that are not adopted. Each adopted group has
its own mode, allowlist status, safety trip, admin list, member
count, and room buffer, so nothing leaks between groups. The Bot
API is stubbed; no network or credentials are used.
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from bridge_core import telegram
from bridge_core.store import JsonStateStore

OWNER = 111
GROUP_A = -777
GROUP_B = -888
GROUP_C = -999
STRANGER = 222
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
    creator; getChatMemberCount returns the configured count, which
    may be a per-chat dict; answerCallbackQuery succeeds. Anything
    else raises, so a test never makes a real network call."""

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
            count = self.member_count
            if isinstance(count, dict):
                count = count.get(params.get("chat_id"), 2)
            return {"ok": True, "result": count}
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


class MultiGroupTests(unittest.TestCase):
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

    def load_state(self):
        store = JsonStateStore(self.state_path)
        return telegram._ensure_adapter_state(store.load())

    def group_state(self, state=None, chat_id=GROUP_A):
        state = state if state is not None else self.load_state()
        return state["groups"][str(chat_id)]

    def run_updates(self, updates, member_count=2):
        return run_cli(
            self.state_path, [], fetch=FakeFetch(updates),
            api_call=FakeApi(member_count=member_count),
            rate_path=self.rate_path,
        )

    def adopt(self, chat_id, mode=None):
        """Adopt a group by having the owner speak in it (closing
        the batch afterwards so the lane can claim again). The
        owner is adopted first (--adopt is idempotent)."""
        run_cli(self.state_path, ["--adopt", str(OWNER)],
                rate_path=self.rate_path)
        payload = self.run_updates([
            msg_update(1000 + abs(chat_id), chat_id, "group", OWNER,
                       "adopt me", username="owner"),
        ])
        self.assertIsNotNone(payload)
        run_cli(self.state_path,
                ["--done", str(1000 + abs(chat_id)), payload["claim_token"]],
                rate_path=self.rate_path)
        if mode is not None:
            result = run_cli(
                self.state_path,
                ["--set-group-mode", mode, str(chat_id)],
                rate_path=self.rate_path,
            )
            self.assertTrue(result["ok"])

    # -- adoption -------------------------------------------------------

    def test_second_group_adopted_when_owner_speaks_in_it(self):
        self.adopt(GROUP_A)
        self.assertEqual(self.load_state()["group_ids"], [GROUP_A])
        payload = self.run_updates([
            msg_update(2, GROUP_B, "group", OWNER, "hello B",
                       username="owner"),
        ])
        self.assertIsNotNone(payload)
        self.assertEqual(payload["chat_id"], GROUP_B)
        state = self.load_state()
        self.assertEqual(state["group_ids"], [GROUP_A, GROUP_B])
        self.assertIn(str(GROUP_B), state["groups"])

    def test_message_from_non_adopted_group_is_ignored(self):
        self.adopt(GROUP_A)
        # A stranger speaks in a group the owner never spoke in:
        # it is not adopted, nothing is queued, nothing is stored.
        payload = self.run_updates([
            msg_update(2, GROUP_B, "group", STRANGER, "hello?",
                       username="stranger"),
        ])
        self.assertIsNone(payload)
        state = self.load_state()
        self.assertEqual(state["group_ids"], [GROUP_A])
        self.assertNotIn(str(GROUP_B), state["groups"])
        # Nothing was queued for the non-adopted group: only the
        # done adoption item from group A is in the queue.
        self.assertEqual(len(state["queue"]), 1)
        self.assertEqual(state["queue"][0]["status"], "done")

    def test_first_group_gets_setup_default_later_groups_start_guarded(self):
        result = run_cli(self.state_path, ["--adopt", str(OWNER), "trusted"],
                         rate_path=self.rate_path)
        self.assertEqual(result["default_group_mode"], "trusted")
        self.adopt(GROUP_A)
        self.assertEqual(self.group_state(chat_id=GROUP_A)["mode"], "trusted")
        self.adopt(GROUP_B)
        # The second group starts guarded even though the setup
        # default was trusted: later groups are guarded until Muse
        # sets their mode.
        self.assertEqual(self.group_state(chat_id=GROUP_B)["mode"], "guarded")

    # -- no mixing between groups ----------------------------------------

    def test_buffers_do_not_mix(self):
        self.adopt(GROUP_A)
        self.adopt(GROUP_B)
        self.run_updates([
            msg_update(3, GROUP_A, "group", OWNER, "hello A",
                       username="owner"),
            msg_update(4, GROUP_B, "group", OWNER, "hello B",
                       username="owner"),
        ])
        state = self.load_state()
        texts_a = [e["text"] for e in self.group_state(state, GROUP_A)["buffer"]]
        texts_b = [e["text"] for e in self.group_state(state, GROUP_B)["buffer"]]
        self.assertIn("hello A", texts_a)
        self.assertNotIn("hello B", texts_a)
        self.assertIn("hello B", texts_b)
        self.assertNotIn("hello A", texts_b)

    def test_payload_context_is_only_this_groups_buffer(self):
        self.adopt(GROUP_A)
        self.adopt(GROUP_B)
        self.run_updates([
            msg_update(3, GROUP_A, "group", OWNER, "hello A",
                       username="owner"),
        ])
        run_cli(self.state_path, ["--done", "3", self._claim_token(3)],
                rate_path=self.rate_path)
        payload = self.run_updates([
            msg_update(4, GROUP_B, "group", OWNER, "hello B",
                       username="owner"),
        ])
        self.assertIsNotNone(payload)
        context = [entry["text"] for entry in payload["items"][0]["context"]]
        self.assertIn("hello B", context)
        self.assertNotIn("hello A", context)

    def test_active_is_per_group(self):
        now = int(time.time())
        self.adopt(GROUP_A)
        self.adopt(GROUP_B)
        self.run_updates([
            msg_update(3, GROUP_A, "group", OWNER, "hi A",
                       username="boss", date=now - 60),
            msg_update(4, GROUP_B, "group", 222, "hi B",
                       username="kai", date=now - 30),
        ])
        result_a = run_cli(self.state_path, ["--active", str(GROUP_A)],
                           rate_path=self.rate_path)
        names_a = {s["sender_name"] for s in result_a["speakers"]}
        self.assertIn("@boss", names_a)
        self.assertNotIn("@kai", names_a)
        result_b = run_cli(self.state_path, ["--active", str(GROUP_B)],
                           rate_path=self.rate_path)
        names_b = {s["sender_name"] for s in result_b["speakers"]}
        self.assertIn("@kai", names_b)
        self.assertNotIn("@boss", names_b)

    # -- trips are per-group ---------------------------------------------

    def test_trip_in_one_group_does_not_affect_another(self):
        self.adopt(GROUP_A, mode="trusted")
        self.adopt(GROUP_B, mode="trusted")
        # A stranger speaks in A only: A trips, B does not.
        payload = self.run_updates([
            msg_update(3, GROUP_A, "group", STRANGER, "hello?",
                       username="stranger"),
        ])
        self.assertIsNotNone(payload)
        self.assertEqual(payload["group_mode_effective"], "guarded")
        state = self.load_state()
        self.assertIsNotNone(self.group_state(state, GROUP_A)["safety_tripped"])
        self.assertIsNone(self.group_state(state, GROUP_B)["safety_tripped"])
        self.assertEqual(
            telegram.effective_group_mode(state, "group", GROUP_B), "trusted")
        # A later wake in B is still trusted.
        run_cli(self.state_path, ["--done", "3", payload["claim_token"]],
                rate_path=self.rate_path)
        payload_b = self.run_updates([
            msg_update(4, GROUP_B, "group", OWNER, "hey B",
                       username="owner"),
        ])
        self.assertIsNotNone(payload_b)
        self.assertEqual(payload_b["group_mode_effective"], "trusted")
        self.assertFalse(payload_b["group_safety_tripped"])

    def test_member_count_trip_is_per_group(self):
        # A has 5 members (over the expected 2), B has 2: only A trips.
        self.adopt(GROUP_A, mode="trusted")
        self.adopt(GROUP_B, mode="trusted")
        payload = self.run_updates([
            msg_update(3, GROUP_A, "group", OWNER, "hey A",
                       username="owner"),
            msg_update(4, GROUP_B, "group", OWNER, "hey B",
                       username="owner"),
        ], member_count={GROUP_A: 5, GROUP_B: 2})
        self.assertIsNotNone(payload)
        state = self.load_state()
        self.assertEqual(
            self.group_state(state, GROUP_A)["safety_tripped"]["reason"],
            "member_count")
        self.assertIsNone(self.group_state(state, GROUP_B)["safety_tripped"])

    # -- allowlist and mode are per-group --------------------------------

    def test_allowlist_applies_per_group(self):
        self.adopt(GROUP_A, mode="trusted")
        self.adopt(GROUP_B, mode="trusted")
        run_cli(self.state_path, ["--allowlist-group", str(GROUP_A)],
                rate_path=self.rate_path)
        # A stranger in the allowlisted group does not trip it;
        # the same stranger in the other group does.
        self.run_updates([
            msg_update(3, GROUP_A, "group", STRANGER, "hello?",
                       username="stranger"),
        ])
        state = self.load_state()
        self.assertIsNone(self.group_state(state, GROUP_A)["safety_tripped"])
        self.run_updates([
            msg_update(4, GROUP_B, "group", STRANGER, "hello?",
                       username="stranger"),
        ])
        state = self.load_state()
        self.assertIsNotNone(self.group_state(state, GROUP_B)["safety_tripped"])

    def test_mode_is_per_group(self):
        self.adopt(GROUP_A)
        self.adopt(GROUP_B)
        result = run_cli(
            self.state_path,
            ["--set-group-mode", "trusted", str(GROUP_A)],
            rate_path=self.rate_path,
        )
        self.assertTrue(result["ok"])
        state = self.load_state()
        self.assertEqual(self.group_state(state, GROUP_A)["mode"], "trusted")
        self.assertEqual(self.group_state(state, GROUP_B)["mode"], "guarded")

    def test_payload_carries_per_group_mode_and_adopted_ids(self):
        self.adopt(GROUP_A, mode="trusted")
        self.adopt(GROUP_B)
        payload = self.run_updates([
            msg_update(3, GROUP_B, "group", OWNER, "hey B",
                       username="owner"),
        ])
        self.assertIsNotNone(payload)
        self.assertEqual(payload["group_mode"], "guarded")
        self.assertEqual(payload["group_mode_effective"], "guarded")
        self.assertEqual(payload["adopted_group_ids"], [GROUP_B, GROUP_A])

    # -- per-group CLIs need a chat id when several groups exist ---------

    def test_group_clis_require_chat_id_with_several_groups(self):
        self.adopt(GROUP_A)
        self.adopt(GROUP_B)
        for argv in (
            ["--set-group-mode", "trusted"],
            ["--clear-group-safety-trip"],
            ["--refresh-admins"],
            ["--active"],
        ):
            result = run_cli(self.state_path, argv, rate_path=self.rate_path)
            self.assertFalse(result["ok"], argv)
            self.assertEqual(result["error"],
                             "multiple-groups-require-chat-id", argv)
        # With the chat id, they work.
        result = run_cli(
            self.state_path,
            ["--set-group-mode", "trusted", str(GROUP_B)],
            rate_path=self.rate_path,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["chat_id"], GROUP_B)

    def test_group_mode_cli_lists_all_groups(self):
        self.adopt(GROUP_A, mode="trusted")
        self.adopt(GROUP_B)
        result = run_cli(self.state_path, ["--group-mode"],
                         rate_path=self.rate_path)
        self.assertTrue(result["ok"])
        self.assertEqual(result["default_group_mode"], "guarded")
        by_id = {g["chat_id"]: g for g in result["groups"]}
        self.assertEqual(by_id[GROUP_A]["mode"], "trusted")
        self.assertEqual(by_id[GROUP_B]["mode"], "guarded")
        # One group on its own.
        result = run_cli(self.state_path, ["--group-mode", str(GROUP_B)],
                         rate_path=self.rate_path)
        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual(result["groups"][0]["chat_id"], GROUP_B)

    # -- migration from the single-group shape ----------------------------

    def test_legacy_single_group_state_migrates(self):
        # A state written by the old single-group build: legacy
        # keys, no groups dict. The master group shape, as live.
        store = JsonStateStore(self.state_path)
        with store.transaction() as state:
            state["offset"] = 5
            state["owner_id"] = OWNER
            state["group_id"] = GROUP_A
            state["group_mode"] = "trusted"
            state["group_admins"] = {
                "chat_id": GROUP_A, "creator_id": OWNER,
                "admin_ids": [OWNER, 9010143960], "fetched_at": 1700000000,
            }
            state["group_member_count"] = {
                "chat_id": GROUP_A, "count": 3, "fetched_at": 1700000000,
            }
            state["group_safety_tripped"] = None
            state["group_mode_notice"] = None
            state["group_buffer"] = [{
                "external_id": "4", "message_id": 25,
                "sender_name": "@owner", "is_bot": False,
                "text": "Hey guys!", "date": 1700000000,
            }]
            state["trusted_bots"] = [BOT_USERNAME.lower()]
            state["trusted_group_ids"] = [GROUP_A]
        state = self.load_state()
        self.assertEqual(state["group_ids"], [GROUP_A])
        record = state["groups"][str(GROUP_A)]
        self.assertEqual(record["mode"], "trusted")
        self.assertEqual(record["admins"]["creator_id"], OWNER)
        self.assertEqual(record["member_count"]["count"], 3)
        self.assertIsNone(record["safety_tripped"])
        self.assertEqual(len(record["buffer"]), 1)
        self.assertEqual(record["buffer"][0]["text"], "Hey guys!")
        # The setup choice was spent on the adopted group, so later
        # groups start guarded.
        self.assertEqual(state["default_group_mode"], "guarded")
        # The legacy keys are gone: one source of truth.
        for key in ("group_id", "group_mode", "group_admins",
                    "group_member_count", "group_safety_tripped",
                    "group_mode_notice", "group_buffer"):
            self.assertNotIn(key, state)

    def test_legacy_mode_with_no_group_becomes_the_default(self):
        # --adopt set a mode but no group was adopted yet: the setup
        # choice is unspent, so the first group adopted gets it.
        store = JsonStateStore(self.state_path)
        with store.transaction() as state:
            state["owner_id"] = OWNER
            state["group_id"] = None
            state["group_mode"] = "trusted"
        state = self.load_state()
        self.assertEqual(state["default_group_mode"], "trusted")
        self.assertEqual(state["group_ids"], [])
        self.adopt(GROUP_A)
        self.assertEqual(self.group_state(chat_id=GROUP_A)["mode"], "trusted")

    def test_migrated_group_keeps_working(self):
        # After migration, the old group still serves messages and
        # a second group can be adopted alongside it.
        store = JsonStateStore(self.state_path)
        with store.transaction() as state:
            state["owner_id"] = OWNER
            state["group_id"] = GROUP_A
            state["group_mode"] = "trusted"
            state["trusted_bots"] = [BOT_USERNAME.lower()]
            state["trusted_group_ids"] = [GROUP_A]
        payload = self.run_updates([
            msg_update(1, GROUP_A, "group", OWNER, "still here",
                       username="owner"),
        ])
        self.assertIsNotNone(payload)
        self.assertEqual(payload["group_mode_effective"], "trusted")
        self.assertTrue(payload["group_allowlisted"])
        payload_b = self.run_updates([
            msg_update(2, GROUP_B, "group", OWNER, "new group",
                       username="owner"),
        ])
        self.assertIsNotNone(payload_b)
        self.assertEqual(payload_b["group_mode_effective"], "guarded")
        self.assertEqual(self.load_state()["group_ids"],
                         [GROUP_A, GROUP_B])

    def _claim_token(self, external_id):
        state = JsonStateStore(self.state_path).load()
        item = next(i for i in state["queue"]
                    if i["external_id"] == str(external_id))
        return item["claim_token"]


if __name__ == "__main__":
    unittest.main()
