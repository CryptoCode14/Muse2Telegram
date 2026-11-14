"""Regression tests for the Nov 11 Telegram bridge fixes.

Fix 1 — a button tap's reply must never thread under the bot's own
message (callback items carry message_id None; the send fence
refuses any reply_to that is not the target item's own message_id).
Fix 2 — the inline keyboard goes only on command replies.
Fix 3 — one reply per batch: --claim-more pulls late arrivals into
the turn, --send-batch-and-close sends once and closes every item,
and the settle window coalesces rapid follow-ups at intake.

Offline: the Bot API fetch is stubbed, no network or credentials.
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

from bridge_core import queue as queue_core
from bridge_core import telegram
from bridge_core.store import JsonStateStore

OWNER = 111
GROUP = -777


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


def callback_update(uid, sender_id, data, chat_id=OWNER,
                    chat_type="private", message_id=77):
    return {
        "update_id": uid,
        "callback_query": {
            "id": f"cb-{uid}",
            "from": {"id": sender_id, "is_bot": False},
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id, "type": chat_type},
                "text": "old bot reply",
            },
            "data": data,
        },
    }


class FakeFetch:
    """Returns one canned getUpdates response on every call."""

    def __init__(self, updates=()):
        self.updates = list(updates)
        self.calls = 0

    def __call__(self, offset):
        self.calls += 1
        return {"ok": True, "result": list(self.updates)}


class SeqFetch:
    """Returns a different canned response on each call (call 0, 1,
    ...); later calls repeat the last response."""

    def __init__(self, responses):
        self.responses = [list(r) for r in responses]
        self.calls = 0

    def __call__(self, offset):
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return {"ok": True, "result": list(self.responses[index])}


class FakeApi:
    """Canned Bot API: answerCallbackQuery succeeds, anything else
    raises, so a test never makes a real network call."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, params, timeout=20.0):
        self.calls.append((method, params))
        if method == "answerCallbackQuery":
            return {"ok": True, "result": True}
        raise AssertionError(f"unexpected Bot API call in test: {method}")


def run_cli(path, argv, fetch=None, stdin_text="", api_call=None,
            extra_env=None):
    """Drive the adapter CLI with a temp rate file (never the live
    one) and a canned Bot API (never the network)."""
    out = io.StringIO()
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin_text)
    rate_path = Path(path).parent / "rate.json"
    env = {
        telegram.RATE_PATH_ENV: str(rate_path),
        # Point the poll lock at a temp file so the suite never
        # shares the live lock (and never flakes against the live
        # intake holding it).
        telegram.POLL_LOCK_PATH_ENV: str(Path(path).parent / "poll.lock"),
        # The bridge refuses to run without its bot username (no
        # default): the fixture name stands in for a real bot.
        telegram.BOT_USERNAME_ENV: "ExampleBot",
    }
    if extra_env:
        env.update(extra_env)
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


class CallbackThreadingTests(unittest.TestCase):
    """Fix 1: a button tap has no user message to thread under."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "state.json"
        self.calls = self.dir / "send-calls.log"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def write_send_cmd(self):
        script = self.dir / "fake-send.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"with open({str(self.calls)!r}, 'a', encoding='utf-8') as fh:\n"
            "    fh.write('argv=' + repr(sys.argv[1:]) + '\\n')\n"
            "    fh.write('stdin=' + sys.stdin.read() + '\\n')\n"
            "print(\"4242\")\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return str(script)

    def claim_callback(self):
        payload = run_cli(
            self.path, [],
            fetch=FakeFetch([callback_update(1, OWNER, "cmd:who")]),
        )
        self.assertIsNotNone(payload)
        return payload

    def item(self, external_id):
        state = JsonStateStore(self.path).load()
        return next(
            i for i in state["queue"] if i["external_id"] == str(external_id)
        )

    def test_callback_reply_goes_out_standalone(self):
        payload = self.claim_callback()
        cmd = self.write_send_cmd()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--send-and-close", "1", payload["claim_token"], str(OWNER)],
                stdin_text="I'm ExampleBot.",
            )
        self.assertEqual(result, {"ok": True, "message_id": "4242"})
        # No --reply-to: the reply is a plain new message, not
        # threaded under the bot's own message (id 77).
        calls = self.calls.read_text(encoding="utf-8")
        self.assertIn(f"argv=['{OWNER}']", calls)
        self.assertNotIn("--reply-to", calls)
        self.assertEqual(self.item(1)["status"], "done")

    def test_callback_reply_with_reply_to_is_refused(self):
        payload = self.claim_callback()
        cmd = self.write_send_cmd()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--send-and-close", "1", payload["claim_token"],
                 str(OWNER), "77"],
                stdin_text="I'm ExampleBot.",
            )
        self.assertEqual(result, {"ok": False, "stage": "fence"})
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.item(1)["status"], "claimed")

    def test_reply_to_a_different_message_is_refused(self):
        payload = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(1, OWNER, "private", OWNER, "/who",
                           message_id=555),
            ]),
        )
        cmd = self.write_send_cmd()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            # 14 is some other message (e.g. the bot's own /help
            # reply): threading under it is refused at the fence.
            result = run_cli(
                self.path,
                ["--send-and-close", "1", payload["claim_token"],
                 str(OWNER), "14"],
                stdin_text="I'm ExampleBot.",
            )
        self.assertEqual(result, {"ok": False, "stage": "fence"})
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.item(1)["status"], "claimed")

    def test_typed_command_still_threads_under_its_own_message(self):
        payload = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(1, OWNER, "private", OWNER, "/who",
                           message_id=555),
            ]),
        )
        cmd = self.write_send_cmd()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--send-and-close", "1", payload["claim_token"],
                 str(OWNER), "555"],
                stdin_text="I'm ExampleBot.",
            )
        self.assertEqual(result, {"ok": True, "message_id": "4242"})
        calls = self.calls.read_text(encoding="utf-8")
        self.assertIn(f"argv=['{OWNER}', '--reply-to', '555']", calls)


class KeyboardRuleTests(unittest.TestCase):
    """Fix 2: the keyboard goes only on command replies."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "state.json"
        self.calls = self.dir / "send-calls.log"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def write_send_cmd(self):
        script = self.dir / "fake-send.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"with open({str(self.calls)!r}, 'a', encoding='utf-8') as fh:\n"
            "    fh.write('argv=' + repr(sys.argv[1:]) + '\\n')\n"
            "print(\"4242\")\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return str(script)

    def claim(self, text, uid=1):
        payload = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(uid, OWNER, "private", OWNER, text),
            ]),
        )
        self.assertIsNotNone(payload)
        return payload

    def item(self, external_id):
        state = JsonStateStore(self.path).load()
        return next(
            i for i in state["queue"] if i["external_id"] == str(external_id)
        )

    def test_keyboard_on_plain_chat_is_refused(self):
        payload = self.claim("hey, what's up")
        cmd = self.write_send_cmd()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--send-and-close", "1", payload["claim_token"],
                 str(OWNER), "--keyboard"],
                stdin_text="hey bro",
            )
        self.assertEqual(result, {"ok": False, "stage": "keyboard"})
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.item(1)["status"], "claimed")

    def test_keyboard_on_command_reply_succeeds(self):
        payload = self.claim("/who")
        cmd = self.write_send_cmd()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--send-and-close", "1", payload["claim_token"],
                 str(OWNER), "--keyboard"],
                stdin_text="I'm ExampleBot.",
            )
        self.assertEqual(result, {"ok": True, "message_id": "4242"})
        calls = self.calls.read_text(encoding="utf-8")
        self.assertIn(f"argv=['{OWNER}', '--keyboard']", calls)

    def test_keyboard_on_button_tap_reply_succeeds(self):
        payload = run_cli(
            self.path, [],
            fetch=FakeFetch([callback_update(1, OWNER, "cmd:who")]),
        )
        cmd = self.write_send_cmd()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--send-and-close", "1", payload["claim_token"],
                 str(OWNER), "--keyboard"],
                stdin_text="I'm ExampleBot.",
            )
        # A tap IS a command, so the keyboard is allowed — and the
        # reply is still standalone (no --reply-to).
        self.assertEqual(result, {"ok": True, "message_id": "4242"})
        calls = self.calls.read_text(encoding="utf-8")
        self.assertIn(f"argv=['{OWNER}', '--keyboard']", calls)
        self.assertNotIn("--reply-to", calls)


class BatchReplyTests(unittest.TestCase):
    """Fix 3: one reply for the whole batch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "state.json"
        self.calls = self.dir / "send-calls.log"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def write_send_cmd(self):
        script = self.dir / "fake-send.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"with open({str(self.calls)!r}, 'a', encoding='utf-8') as fh:\n"
            "    fh.write('argv=' + repr(sys.argv[1:]) + '\\n')\n"
            "    fh.write('stdin=' + sys.stdin.read() + '\\n')\n"
            "print(\"4242\")\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return str(script)

    def items(self):
        return JsonStateStore(self.path).load()["queue"]

    def test_claim_more_pulls_a_late_arrival_into_the_turn(self):
        first = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(1, OWNER, "private", OWNER, "first",
                           message_id=9001),
                msg_update(2, OWNER, "private", OWNER, "second",
                           message_id=9002),
            ]),
        )
        self.assertEqual(len(first["items"]), 2)
        # A third message arrives while the turn is thinking: the
        # next poll queues it but cannot claim the busy lane.
        second = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(3, OWNER, "private", OWNER, "third",
                           message_id=9003),
            ]),
        )
        self.assertIsNone(second)
        # The turn re-checks before sending and folds it in.
        more = run_cli(
            self.path, ["--claim-more", str(OWNER), first["claim_token"]],
        )
        self.assertEqual(
            [it["update_id"] for it in more["items"]], [3]
        )
        self.assertEqual(more["items"][0]["text"], "third")
        # Nothing left to claim a second time.
        again = run_cli(
            self.path, ["--claim-more", str(OWNER), first["claim_token"]],
        )
        self.assertEqual(again, {"ok": True, "items": []})

    def test_claim_more_rejects_a_foreign_token(self):
        first = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(1, OWNER, "private", OWNER, "first"),
            ]),
        )
        run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(2, OWNER, "private", OWNER, "second"),
            ]),
        )
        result = run_cli(
            self.path, ["--claim-more", str(OWNER), "bogus-token"],
        )
        self.assertEqual(result, {"ok": True, "items": []})
        # The late item is still pending, untouched.
        item2 = next(i for i in self.items() if i["external_id"] == "2")
        self.assertEqual(item2["status"], "pending")
        self.assertIsNone(item2["claim_token"])
        _ = first

    def test_one_send_covers_the_whole_batch(self):
        first = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(1, OWNER, "private", OWNER, "first",
                           message_id=9001),
                msg_update(2, OWNER, "private", OWNER, "second",
                           message_id=9002),
            ]),
        )
        run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(3, OWNER, "private", OWNER, "third",
                           message_id=9003),
            ]),
        )
        run_cli(
            self.path, ["--claim-more", str(OWNER), first["claim_token"]],
        )
        cmd = self.write_send_cmd()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--send-batch-and-close", "2", first["claim_token"],
                 str(OWNER), "9002", "--cover", "1", "3"],
                stdin_text="got all three, bro",
            )
        self.assertEqual(
            result, {"ok": True, "message_id": "4242", "covered": [1, 3]}
        )
        # Exactly ONE send happened, threaded under the latest
        # message, with the single reply text.
        calls = self.calls.read_text(encoding="utf-8")
        self.assertEqual(calls.count("argv="), 1)
        self.assertIn(f"argv=['{OWNER}', '--reply-to', '9002']", calls)
        self.assertIn("stdin=got all three, bro", calls)
        for item in self.items():
            self.assertEqual(item["status"], "done")
            self.assertEqual(item["reply_message_id"], "4242")
        covered = {
            i["external_id"]: i.get("done_reason") for i in self.items()
        }
        self.assertEqual(covered["1"], "covered-by-batch-reply")
        self.assertEqual(covered["3"], "covered-by-batch-reply")
        self.assertIsNone(covered["2"])

    def test_batch_send_refuses_a_reply_target_outside_the_batch(self):
        first = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(1, OWNER, "private", OWNER, "first",
                           message_id=9001),
                msg_update(2, OWNER, "private", OWNER, "second",
                           message_id=9002),
            ]),
        )
        cmd = self.write_send_cmd()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--send-batch-and-close", "2", first["claim_token"],
                 str(OWNER), "14", "--cover", "1"],
                stdin_text="nope",
            )
        self.assertEqual(result, {"ok": False, "stage": "fence"})
        self.assertFalse(self.calls.exists())
        for item in self.items():
            self.assertEqual(item["status"], "claimed")

    def test_batch_keyboard_needs_a_command_in_the_batch(self):
        first = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(1, OWNER, "private", OWNER, "first"),
                msg_update(2, OWNER, "private", OWNER, "second"),
            ]),
        )
        cmd = self.write_send_cmd()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--send-batch-and-close", "2", first["claim_token"],
                 str(OWNER), "--keyboard", "--cover", "1"],
                stdin_text="nope",
            )
        self.assertEqual(result, {"ok": False, "stage": "keyboard"})
        self.assertFalse(self.calls.exists())
        # One command in the batch is enough for the keyboard.
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--send-batch-and-close", "2", first["claim_token"],
                 str(OWNER), "--cover", "1"],
                stdin_text="plain reply, no keyboard",
            )
        self.assertEqual(result["ok"], True)

    def test_batch_send_refuses_when_a_covered_item_is_stale(self):
        first = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(1, OWNER, "private", OWNER, "first"),
                msg_update(2, OWNER, "private", OWNER, "second"),
            ]),
        )
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            # 25 hours old: past the claimed-work limit (a day), so
            # the covered item is stale even though claimed work
            # gets the longer limit.
            queue_core.find_item(state, "telegram", 1)["received_at"] -= 90000
        cmd = self.write_send_cmd()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--send-batch-and-close", "2", first["claim_token"],
                 str(OWNER), "--cover", "1"],
                stdin_text="too late for one of them",
            )
        self.assertEqual(result, {"ok": False, "stage": "stale"})
        self.assertFalse(self.calls.exists())
        for item in self.items():
            self.assertEqual(item["status"], "claimed")


class SettleWindowTests(unittest.TestCase):
    """Fix 3a: the settle window coalesces a rapid follow-up."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_settle_coalesces_a_followup_into_one_batch(self):
        fetch = SeqFetch([
            [msg_update(1, OWNER, "private", OWNER, "first")],
            [msg_update(2, OWNER, "private", OWNER, "second")],
        ])
        payload = run_cli(
            self.path, [], fetch=fetch,
            extra_env={telegram.SETTLE_SECONDS_ENV: "0.2"},
        )
        self.assertIsNotNone(payload)
        self.assertEqual(
            [it["update_id"] for it in payload["items"]], [1, 2]
        )
        self.assertEqual(fetch.calls, 2)

    def test_no_settle_means_a_single_fetch(self):
        # v2: the settle default is now 0.3 s (OpenClaw debounce
        # retune), so "no settle" must be requested explicitly
        # with TELEGRAM_BRIDGE_SETTLE_SECONDS=0; an unset env no
        # longer means "off".
        fetch = SeqFetch([
            [msg_update(1, OWNER, "private", OWNER, "first")],
            [msg_update(2, OWNER, "private", OWNER, "second")],
        ])
        payload = run_cli(
            self.path, [], fetch=fetch,
            extra_env={telegram.SETTLE_SECONDS_ENV: "0"},
        )
        self.assertIsNotNone(payload)
        self.assertEqual(
            [it["update_id"] for it in payload["items"]], [1]
        )
        self.assertEqual(fetch.calls, 1)


class ClaimMoreUnitTests(unittest.TestCase):
    """queue.claim_more: only the lane owner's token, same lane only."""

    def test_claim_more_scoping(self):
        state = {}
        queue_core.enqueue(
            state, platform="telegram", external_id=1, chat_id=OWNER,
            chat_type="private", sender_id=OWNER, text="first",
        )
        queue_core.enqueue(
            state, platform="telegram", external_id=2, chat_id=OWNER,
            chat_type="private", sender_id=OWNER, text="second",
        )
        queue_core.enqueue(
            state, platform="telegram", external_id=3, chat_id=GROUP,
            chat_type="group", sender_id=OWNER, text="other lane",
        )
        batch = queue_core.claim_batch(state, platform="telegram",
                                       batch_size=1)
        token = batch["claim_token"]
        # A foreign token claims nothing.
        self.assertIsNone(
            queue_core.claim_more(
                state, platform="telegram", chat_id=OWNER,
                token="bogus",
            )
        )
        # The owner's token pulls the second item, but never the
        # other lane's item.
        more = queue_core.claim_more(
            state, platform="telegram", chat_id=OWNER, token=token,
        )
        self.assertEqual(
            [i["external_id"] for i in more["items"]], ["2"]
        )
        self.assertEqual(more["claim_token"], token)
        other = queue_core.find_item(state, "telegram", 3)
        self.assertEqual(other["status"], "pending")
        # Nothing left to claim.
        self.assertIsNone(
            queue_core.claim_more(
                state, platform="telegram", chat_id=OWNER, token=token,
            )
        )


class BatchFirstIntakeTests(unittest.TestCase):
    """Fix C (batch-first intake): one turn clears the backlog.

    - The intake claims a whole lane backlog (default batch 10) in
      one wake, so a burst costs one lane wait instead of one per
      message.
    - --outbox folds messages that arrived while the turn was
      working into the same reply, so they do not wake a fresh
      turn (a fresh turn costs a whole lane wait, 2-5 min).
    - The settle window coalesces a burst at intake: default 2s
      (the live installed value), max 15s (the synthesis range).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "state.json"
        self.calls = self.dir / "send-calls.log"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def write_send_cmd(self):
        script = self.dir / "fake-send.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"with open({str(self.calls)!r}, 'a', encoding='utf-8') as fh:\n"
            "    fh.write('argv=' + repr(sys.argv[1:]) + '\\n')\n"
            "    fh.write('stdin=' + sys.stdin.read() + '\\n')\n"
            "print(\"4242\")\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return str(script)

    def items(self):
        return JsonStateStore(self.path).load()["queue"]

    def test_one_intake_claim_clears_a_10_message_backlog(self):
        updates = [
            msg_update(uid, OWNER, "private", OWNER, f"msg {uid}",
                       message_id=9000 + uid)
            for uid in range(1, 11)
        ]
        payload = run_cli(
            self.path, [], fetch=FakeFetch(updates),
            extra_env={telegram.SETTLE_SECONDS_ENV: "0"},
        )
        self.assertIsNotNone(payload)
        self.assertEqual(
            [it["update_id"] for it in payload["items"]],
            list(range(1, 11)),
        )

    def test_one_turn_clears_a_10_message_backlog_end_to_end(self):
        # The Fix C acceptance test: a 10-message backlog is claimed
        # in one intake, answered in one --outbox reply, and every
        # item is done — one turn, one send, no second lane wait.
        payload = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(uid, OWNER, "private", OWNER, f"msg {uid}",
                           message_id=9000 + uid)
                for uid in range(1, 11)
            ]),
            extra_env={telegram.SETTLE_SECONDS_ENV: "0"},
        )
        self.assertEqual(len(payload["items"]), 10)
        cmd = self.write_send_cmd()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--outbox", payload["claim_token"]],
                stdin_text="got all ten",
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["delivery"], "delivered", result)
        self.assertEqual(len(result["covered_update_ids"]), 10, result)
        self.assertEqual(result["claimed_more_update_ids"], [], result)
        calls = self.calls.read_text(encoding="utf-8")
        self.assertEqual(calls.count("stdin=got all ten"), 1)
        for item in self.items():
            self.assertEqual(item["status"], "done")
            self.assertEqual(item["reply_message_id"], "4242")

    def test_outbox_folds_a_late_arrival_into_the_same_reply(self):
        first = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(1, OWNER, "private", OWNER, "first",
                           message_id=9001),
                msg_update(2, OWNER, "private", OWNER, "second",
                           message_id=9002),
            ]),
            extra_env={telegram.SETTLE_SECONDS_ENV: "0"},
        )
        self.assertEqual(len(first["items"]), 2)
        # A third message arrives while the turn is thinking: the
        # next poll queues it but cannot claim the busy lane.
        second = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(3, OWNER, "private", OWNER, "third",
                           message_id=9003),
            ]),
            extra_env={telegram.SETTLE_SECONDS_ENV: "0"},
        )
        self.assertIsNone(second)
        # The turn sends via --outbox: the late arrival joins the
        # same reply instead of waking a fresh turn.
        cmd = self.write_send_cmd()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--outbox", first["claim_token"]],
                stdin_text="got all three, bro",
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["delivery"], "delivered", result)
        self.assertEqual(result["claimed_more_update_ids"], [3], result)
        self.assertEqual(result["covered_update_ids"], [3, 1, 2], result)
        # Exactly ONE text send, threaded under the latest message.
        calls = self.calls.read_text(encoding="utf-8")
        self.assertEqual(calls.count("stdin=got all three, bro"), 1)
        self.assertIn(f"argv=['{OWNER}', '--reply-to', '9003']", calls)
        for item in self.items():
            self.assertEqual(item["status"], "done")
            self.assertEqual(item["reply_message_id"], "4242")

    def test_outbox_fold_does_not_exceed_the_batch_cap(self):
        # Ten claimed, five more arrive while the turn works: the
        # fold takes only the headroom to the 12-message cap, and
        # the rest stay pending for the next turn.
        first = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(uid, OWNER, "private", OWNER, f"msg {uid}",
                           message_id=9000 + uid)
                for uid in range(1, 11)
            ]),
            extra_env={telegram.SETTLE_SECONDS_ENV: "0"},
        )
        self.assertEqual(len(first["items"]), 10)
        second = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(uid, OWNER, "private", OWNER, f"msg {uid}",
                           message_id=9000 + uid)
                for uid in range(11, 16)
            ]),
            extra_env={telegram.SETTLE_SECONDS_ENV: "0"},
        )
        self.assertIsNone(second)
        cmd = self.write_send_cmd()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--outbox", first["claim_token"]],
                stdin_text="got twelve of them",
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["claimed_more_update_ids"], [11, 12], result)
        self.assertEqual(len(result["covered_update_ids"]), 12, result)
        pending = [
            item for item in self.items()
            if item["status"] == "pending"
        ]
        self.assertEqual(
            [item["external_id"] for item in pending], ["13", "14", "15"]
        )

    def test_settle_default_is_2s_and_max_is_15s(self):
        with mock.patch.dict(
            os.environ, {telegram.SETTLE_SECONDS_ENV: ""},
        ):
            self.assertEqual(telegram._settle_seconds(), 2.0)
        with mock.patch.dict(
            os.environ, {telegram.SETTLE_SECONDS_ENV: "99"},
        ):
            self.assertEqual(telegram._settle_seconds(), 15.0)
        with mock.patch.dict(
            os.environ, {telegram.SETTLE_SECONDS_ENV: "0"},
        ):
            self.assertEqual(telegram._settle_seconds(), 0.0)


if __name__ == "__main__":
    unittest.main()
