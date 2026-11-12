"""Offline tests for messages from another bot in the group
(bridge_core.telegram ingest + the decider).

Root cause this pins down: Telegram's Bot API does NOT deliver
messages sent by one bot to another bot, even if the receiving bot
is a group admin and even with Privacy Mode off (Bots FAQ: "bots
will not be able to see messages from other bots regardless of
mode"). The fix is the BotFather setting *Bot-to-Bot Communication
Mode*, enabled for the receiving bot (with admin rights, or
privacy off, it then receives ALL messages from other bots in the
group). The intake and the decider already handle a bot sender
correctly — these tests prove it, so a missing bot message is a
delivery problem (the BotFather setting), never an intake crash
or a silent drop:

  - a bot message in the adopted group is buffered (group_buffer)
    and enqueued, and the intake does not crash on it;
  - a bot message that does not @mention this bot is closed
    silently by the decider (done, no wake) — no auto-reply, so
    no bot-to-bot loop;
  - a bot message that DOES @mention this bot wakes the agent
    (so it can answer when asked).

The Bot API is stubbed; no network or credentials are used. The
decider is loaded by path (live hook script first, then the
fork's copy), so the same file runs in both suites.
"""
import importlib.util
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
OTHER_BOT = 333
OTHER_BOT_USERNAME = "SomeOtherBot"
BOT_USERNAME = "Westinmusebot"


def _decider_paths():
    here = Path(__file__).resolve()
    return [
        # Live hook script.
        Path.home() / "hooks" / "scripts" / "telegram-bridge-decide.py",
        # Fork copy (muse-runtime/hooks/) when run from the fork.
        here.parent.parent / "hooks" / "telegram-bridge-decide.py",
    ]


def load_decider():
    for path in _decider_paths():
        if path.exists():
            spec = importlib.util.spec_from_file_location(
                "telegram_bridge_decide", path
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise FileNotFoundError(
        "telegram-bridge-decide.py not found in any known location"
    )


def msg_update(uid, chat_id, chat_type, sender_id, text, username=None,
               is_bot=False, message_id=None, reply_to=None):
    user = {"id": sender_id, "is_bot": is_bot}
    if username:
        user["username"] = username
    message = {
        "message_id": uid if message_id is None else message_id,
        "from": user,
        "chat": {"id": chat_id, "type": chat_type},
        "text": text,
        "date": 1700000000 + uid,
    }
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": uid, "message": message}


def decider_item(update_id, chat_id, chat_type, sender_id, text,
                 is_bot=False, sender_name=None):
    return {
        "update_id": update_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "sender_id": sender_id,
        "sender_name": sender_name or str(sender_id),
        "is_bot": is_bot,
        "text": text,
    }


class FakeFetch:
    """Returns one canned getUpdates response until replaced."""

    def __init__(self, updates=()):
        self.updates = list(updates)

    def __call__(self, offset):
        return {"ok": True, "result": list(self.updates)}


class FakeApi:
    """Canned Bot API: getChatAdministrators returns the owner as
    creator; getChatMemberCount returns the configured count.
    Anything else raises, so a test never makes a real call."""

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


class BotSenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state_path = self.dir / "state.json"
        self.rate_path = self.dir / "rate.json"
        self.paused_path = self.dir / "PAUSED"
        self._env = mock.patch.dict(os.environ, {
            "TELEGRAM_BRIDGE_BOT_USERNAME": BOT_USERNAME,
            "TELEGRAM_BRIDGE_PAUSED_PATH": str(self.paused_path),
        })
        self._env.start()
        self.decide = load_decider()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def preset_state(self, mode="trusted", trusted_bots=None,
                     allowlisted=()):
        """Owner + group adopted, mode set, trusted-bot list set,
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
            record = telegram._group_record(state, GROUP, create=True)
            record["mode"] = mode
            record["member_count"] = None
            record["safety_tripped"] = None
            record["mode_notice"] = None

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

    def run_decider(self, payload):
        out = io.StringIO()
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        try:
            with redirect_stdout(out):
                self.decide.main()
        finally:
            sys.stdin = old_stdin
        return out.getvalue().strip()

    def bot_update(self, uid=1, text="Hey! I am the other bot."):
        return msg_update(
            uid, GROUP, "group", OTHER_BOT, text,
            username=OTHER_BOT_USERNAME, is_bot=True,
        )

    # -- intake: a bot sender is buffered + enqueued, never dropped --

    def test_bot_message_appears_in_group_buffer_and_queue(self):
        # The group is allowlisted (the owner's private channel), so
        # no trip fires for the unknown bot. The message must still
        # be visible in the room history and enqueued.
        self.preset_state(mode="trusted", allowlisted=[GROUP])
        payload = self.run_updates([self.bot_update(1)])
        state = self.load_state()
        buffered = [e for e in state["groups"][str(GROUP)]["buffer"]
                    if e["external_id"] == "1"]
        self.assertEqual(len(buffered), 1)
        self.assertTrue(buffered[0]["is_bot"])
        self.assertEqual(buffered[0]["sender_name"], "@" + OTHER_BOT_USERNAME)
        queued = [i for i in state["queue"] if i["external_id"] == "1"]
        self.assertEqual(len(queued), 1)
        self.assertTrue(queued[0]["is_bot"])
        self.assertEqual(queued[0]["text"], "Hey! I am the other bot.")
        self.assertIsNotNone(payload)
        self.assertTrue(payload["group_allowlisted"])

    def test_bot_message_advances_offset(self):
        # The intake must not get stuck on a bot sender's update:
        # the offset moves past it, so the next poll fetches new
        # updates instead of re-reading the same one.
        self.preset_state(mode="trusted", allowlisted=[GROUP])
        self.run_updates([self.bot_update(7)])
        self.assertEqual(self.load_state()["offset"], 8)

    def test_bot_message_replying_to_this_bot_is_buffered(self):
        # A bot replying to one of this bot's messages is a normal
        # group message: buffered and enqueued, no crash.
        self.preset_state(mode="trusted", allowlisted=[GROUP])
        self.run_updates([
            msg_update(1, GROUP, "group", OTHER_BOT,
                       "Here is my intro.", username=OTHER_BOT_USERNAME,
                       is_bot=True, reply_to=25),
        ])
        state = self.load_state()
        self.assertEqual(len(self.group_state(state)["buffer"]), 1)
        self.assertTrue(self.group_state(state)["buffer"][0]["is_bot"])
        self.assertEqual(len(state["queue"]), 1)

    def test_bot_message_in_non_allowlisted_group_trips_but_is_kept(self):
        # In a trusted group that is NOT allowlisted, an unknown bot
        # speaking is a stranger and trips the safety — but its
        # message is still buffered and enqueued, not dropped.
        self.preset_state(mode="trusted", allowlisted=[])
        self.run_updates([self.bot_update(1)])
        state = self.load_state()
        self.assertIsNotNone(self.group_state(state)["safety_tripped"])
        self.assertEqual(self.group_state(state)["safety_tripped"]["reason"], "stranger")
        self.assertEqual(len(self.group_state(state)["buffer"]), 1)
        self.assertEqual(len(state["queue"]), 1)

    # -- decider: silent close unless it mentions this bot ----------

    def test_bot_message_without_mention_is_closed_silently(self):
        # No auto-reply to an unprompted bot message: this is the
        # loop breaker. The hook closes the item with no wake.
        result = self.run_decider({"items": [
            decider_item(1, GROUP, "group", OTHER_BOT,
                         "Hey! I am the other bot.", is_bot=True,
                         sender_name="@" + OTHER_BOT_USERNAME),
        ]})
        self.assertEqual(result, "done 1")

    def test_bot_message_mentioning_this_bot_wakes(self):
        # When the other bot asks this bot directly, it wakes so the
        # agent can answer. The decider prints a bare "wake" (v2:
        # no tier word; the agent always follows the unified prompt).
        result = self.run_decider({"items": [
            decider_item(1, GROUP, "group", OTHER_BOT,
                         "@Westinmusebot what do you think?",
                         is_bot=True,
                         sender_name="@" + OTHER_BOT_USERNAME),
        ]})
        self.assertEqual(result, "wake")

    def test_bot_message_with_mention_of_another_bot_is_closed(self):
        # A bot message @mentioning a third bot is not for this bot.
        result = self.run_decider({"items": [
            decider_item(1, GROUP, "group", OTHER_BOT,
                         "@SomeOtherBot check this out", is_bot=True,
                         sender_name="@" + OTHER_BOT_USERNAME),
        ]})
        self.assertEqual(result, "done 1")


class PreclaimAliasTests(unittest.TestCase):
    """preclaim_filter treats a configured alias like the bot's own
    username: "@<alias>" is not noise, "@<someone else>" still is.
    This is the layer that runs before the decider, so an alias
    has to pass here too or the item never reaches the decider."""

    def _state(self, text):
        return {"queue": [{
            "status": "pending", "chat_type": "group",
            "text": text, "is_bot": False, "sender_name": "Westin",
            "sender_id": OWNER, "external_id": 1, "replied_at": None,
        }]}

    def test_alias_mention_is_not_noise(self):
        with mock.patch.dict(os.environ, {
            "TELEGRAM_BRIDGE_BOT_USERNAME": BOT_USERNAME,
            "TELEGRAM_BRIDGE_BOT_ALIASES": "Westin",
        }):
            closed = telegram.preclaim_filter(
                self._state("@Westin check this"))
        self.assertEqual(closed, [])

    def test_alias_mention_is_case_insensitive(self):
        with mock.patch.dict(os.environ, {
            "TELEGRAM_BRIDGE_BOT_USERNAME": BOT_USERNAME,
            "TELEGRAM_BRIDGE_BOT_ALIASES": "westin",
        }):
            closed = telegram.preclaim_filter(
                self._state("@WESTIN check this"))
        self.assertEqual(closed, [])

    def test_unaliased_short_name_is_still_noise(self):
        with mock.patch.dict(os.environ, {
            "TELEGRAM_BRIDGE_BOT_USERNAME": BOT_USERNAME,
        }, clear=False):
            os.environ.pop("TELEGRAM_BRIDGE_BOT_ALIASES", None)
            closed = telegram.preclaim_filter(
                self._state("@Westin check this"))
        self.assertEqual(closed, [1])

    def test_other_bot_mention_is_still_noise_with_alias_set(self):
        with mock.patch.dict(os.environ, {
            "TELEGRAM_BRIDGE_BOT_USERNAME": BOT_USERNAME,
            "TELEGRAM_BRIDGE_BOT_ALIASES": "Westin",
        }):
            closed = telegram.preclaim_filter(
                self._state("@SomeOtherBot check this"))
        self.assertEqual(closed, [1])


if __name__ == "__main__":
    unittest.main()
