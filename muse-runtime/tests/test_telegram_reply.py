"""Tests for the unified v2 --reply command and the deleteMessage path.

--reply (bridge_core.telegram.send_reply / main "--reply") is the
single send path for a turn: the caller passes only the claim token
and the reply text on stdin; the batch, chat, reply target and cover
list are derived from the claimed items. In order it heartbeats the
batch, fires typing, reacts on the latest user message when --react
is given, deletes any recorded ack message, sends once threaded
under the latest user message, and closes every covered item.

The Bot API is never touched: the send command is a fake script
that logs its argv/stdin and prints a canned message id, and the
sender script's deleteMessage is tested with a mocked _post.

Offline: no network, no credentials, no live bot.
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
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
        telegram.POLL_LOCK_PATH_ENV: str(Path(path).parent / "poll.lock"),
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


# The fake send script mirrors the real telegram-sender contract:
# a text send prints the message id ("4242") as its last stdout
# line; --typing/--react/--delete exit 0 with no output. Only the
# text send reads stdin (the best-effort subcommands never do).
_FAKE_SEND_BODY = """\
#!/usr/bin/env python3
import sys
with open({log!r}, 'a', encoding='utf-8') as fh:
    fh.write('argv=' + repr(sys.argv[1:]) + '\\n')
if sys.argv[1:2] and not sys.argv[1].startswith('--'):
    with open({log!r}, 'a', encoding='utf-8') as fh:
        fh.write('stdin=' + sys.stdin.read() + '\\n')
{fail}
print("4242")
"""


class ReplyTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "state.json"
        self.calls = self.dir / "send-calls.log"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def write_send_cmd(self, fail_on=None):
        """A fake send command. ``fail_on`` names the subcommand
        that exits 1 instead of 0 ("delete" or "send"); anything
        else succeeds. Every invocation is logged."""
        fail = ""
        if fail_on == "delete":
            fail = (
                "if sys.argv[1:2] == ['--delete']:\n"
                "    raise SystemExit(1)\n"
            )
        elif fail_on == "send":
            fail = (
                "if sys.argv[1:2] and not sys.argv[1].startswith('--'):\n"
                "    raise SystemExit(1)\n"
            )
        script = self.dir / "fake-send.py"
        script.write_text(
            _FAKE_SEND_BODY.format(log=str(self.calls), fail=fail),
            encoding="utf-8",
        )
        script.chmod(0o755)
        return str(script)

    def send_env(self, cmd):
        # The warm hold is disabled for the --reply tests in this
        # file (they assert next_batch None and must stay fast);
        # WarmHoldTests below covers the hold itself with small
        # explicit hold lengths.
        return mock.patch.dict(
            os.environ,
            {
                telegram.SEND_CMD_ENV: cmd,
                telegram.WARM_HOLD_SECONDS_ENV: "0",
            },
        )

    def claim(self, updates):
        payload = run_cli(self.path, [], fetch=FakeFetch(updates))
        self.assertIsNotNone(payload)
        return payload

    def items(self):
        return JsonStateStore(self.path).load()["queue"]

    def item(self, external_id):
        return next(
            i for i in self.items() if i["external_id"] == str(external_id)
        )

    def call_log(self):
        if not self.calls.exists():
            return ""
        return self.calls.read_text(encoding="utf-8")


class ReplySingleTests(ReplyTestBase):
    """--reply for one item: heartbeat, typing, threaded send, close."""

    def test_reply_single_item(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        with self.send_env(cmd):
            result = run_cli(
                self.path, ["--reply", payload["claim_token"]],
                stdin_text="hey bro",
            )
        self.assertEqual(result, {
            "ok": True,
            "stage": "sent",
            "message_id": 4242,
            "chat_id": OWNER,
            "replied_update_ids": [1],
            "closed_update_ids": [1],
            "ack_deleted": False,
            "next_batch": None,
        })
        self.assertIsInstance(result["message_id"], int)
        calls = self.call_log()
        # Typing fires first, then the single send, threaded under
        # the item's own message.
        self.assertIn(f"argv=['--typing', '{OWNER}']", calls)
        self.assertIn(f"argv=['{OWNER}', '--reply-to', '9001']", calls)
        self.assertIn("stdin=hey bro", calls)
        self.assertEqual(calls.count("stdin="), 1)
        item = self.item(1)
        self.assertEqual(item["status"], "done")
        self.assertEqual(item["reply_message_id"], "4242")
        self.assertIsNotNone(item["replied_at"])
        self.assertIsNone(item.get("done_reason"))

    def test_reply_empty_text_is_input_stage(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        with self.send_env(cmd):
            result = run_cli(
                self.path, ["--reply", payload["claim_token"]],
                stdin_text="   ",
            )
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["stage"], "input")
        # Empty text is refused before typing fires: nothing ran.
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.item(1)["status"], "claimed")

    def test_reply_callback_batch_is_standalone_and_never_reacted_to(self):
        payload = self.claim([callback_update(1, OWNER, "cmd:who")])
        cmd = self.write_send_cmd()
        with self.send_env(cmd):
            # --react is passed, but a button tap has no user
            # message: the react must be skipped, not attempted.
            result = run_cli(
                self.path, ["--reply", payload["claim_token"],
                            "--react", "👍"],
                stdin_text="I'm Westin Jr.",
            )
        self.assertEqual(result["ok"], True)
        calls = self.call_log()
        self.assertNotIn("--react", calls)
        self.assertIn(f"argv=['{OWNER}']", calls)
        self.assertNotIn("--reply-to", calls)
        self.assertEqual(self.item(1)["status"], "done")


class ReplyBatchTests(ReplyTestBase):
    """--reply for a whole batch: one send closes every sibling."""

    def test_reply_batch_closes_all_siblings(self):
        first = self.claim([
            msg_update(1, OWNER, "private", OWNER, "first",
                       message_id=9001),
            msg_update(2, OWNER, "private", OWNER, "second",
                       message_id=9002),
        ])
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
        with self.send_env(cmd):
            result = run_cli(
                self.path, ["--reply", first["claim_token"]],
                stdin_text="got all three, bro",
            )
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["stage"], "sent")
        self.assertEqual(result["message_id"], 4242)
        # Primary first (the latest message), then the covered
        # siblings in received order.
        self.assertEqual(result["replied_update_ids"], [3, 1, 2])
        self.assertEqual(result["closed_update_ids"], [3, 1, 2])
        self.assertIsNone(result["next_batch"])
        calls = self.call_log()
        self.assertEqual(calls.count("stdin="), 1)
        self.assertIn(f"argv=['{OWNER}', '--reply-to', '9003']", calls)
        self.assertIn("stdin=got all three, bro", calls)
        for item in self.items():
            self.assertEqual(item["status"], "done")
            self.assertEqual(item["reply_message_id"], "4242")
            self.assertIsNotNone(item["replied_at"])
        covered = {
            i["external_id"]: i.get("done_reason") for i in self.items()
        }
        self.assertIsNone(covered["3"])
        self.assertEqual(covered["1"], "covered-by-batch-reply")
        self.assertEqual(covered["2"], "covered-by-batch-reply")

    def test_reply_with_react(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "first",
                       message_id=9001),
            msg_update(2, OWNER, "private", OWNER, "second",
                       message_id=9002),
        ])
        cmd = self.write_send_cmd()
        with self.send_env(cmd):
            result = run_cli(
                self.path, ["--reply", payload["claim_token"],
                            "--react", "👍"],
                stdin_text="got both",
            )
        self.assertEqual(result["ok"], True)
        calls = self.call_log()
        # React on the LATEST user message, before the send.
        self.assertIn(
            f"argv=['--react', '{OWNER}', '9002', '👍']", calls
        )
        self.assertLess(
            calls.index("--react"), calls.index("--reply-to")
        )
        for item in self.items():
            self.assertEqual(item["status"], "done")

    def test_reply_with_keyboard_on_command(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "/who",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        with self.send_env(cmd):
            result = run_cli(
                self.path, ["--reply", payload["claim_token"],
                            "--keyboard"],
                stdin_text="I'm Westin Jr.",
            )
        self.assertEqual(result["ok"], True)
        calls = self.call_log()
        self.assertIn(
            f"argv=['{OWNER}', '--reply-to', '9001', '--keyboard']", calls
        )
        self.assertEqual(self.item(1)["status"], "done")

    def test_reply_keyboard_on_plain_chat_is_refused(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hey, what's up",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        with self.send_env(cmd):
            result = run_cli(
                self.path, ["--reply", payload["claim_token"],
                            "--keyboard"],
                stdin_text="hey bro",
            )
        self.assertEqual(result, {
            "ok": False,
            "stage": "keyboard",
            "message_id": None,
            "chat_id": OWNER,
            "replied_update_ids": [],
            "closed_update_ids": [],
            "ack_deleted": False,
            "next_batch": None,
        })
        # Typing fires before the send fence is reached, so the log
        # holds the typing call — but no text send happened.
        self.assertEqual(self.call_log().count("stdin="), 0)
        self.assertNotIn("--reply-to", self.call_log())
        self.assertEqual(self.item(1)["status"], "claimed")


class ReplyAckDeleteTests(ReplyTestBase):
    """--reply deletes a recorded ack message, best-effort only."""

    def _claim_and_ack(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "deep question",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        with self.send_env(cmd):
            ack = run_cli(
                self.path,
                ["--send-ack", "1", payload["claim_token"],
                 str(OWNER), "9001"],
            )
        self.assertEqual(ack, {"ok": True, "message_id": "4242"})
        self.assertEqual(self.item(1).get("ack_message_id"), "4242")
        self.assertEqual(self.item(1)["status"], "claimed")
        # Fresh log for the --reply run, so its call order can be
        # checked without the ack's own send in the way.
        if self.calls.exists():
            self.calls.unlink()
        return payload, cmd

    def test_reply_deletes_recorded_ack(self):
        payload, _cmd = self._claim_and_ack()
        cmd = self.write_send_cmd()
        with self.send_env(cmd):
            result = run_cli(
                self.path, ["--reply", payload["claim_token"]],
                stdin_text="here is the full answer",
            )
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["ack_deleted"], True)
        calls = self.call_log()
        self.assertIn(f"argv=['--delete', '{OWNER}', '4242']", calls)
        # The ack delete happens before the send.
        self.assertLess(
            calls.index("--delete"), calls.index("--reply-to")
        )
        self.assertEqual(self.item(1)["status"], "done")

    def test_reply_ack_delete_failure_still_sends(self):
        payload, _cmd = self._claim_and_ack()
        cmd = self.write_send_cmd(fail_on="delete")
        with self.send_env(cmd):
            result = run_cli(
                self.path, ["--reply", payload["claim_token"]],
                stdin_text="here is the full answer",
            )
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["stage"], "sent")
        # Deletion was attempted and failed; the reply still went
        # out and closed the item.
        self.assertEqual(result["ack_deleted"], False)
        calls = self.call_log()
        self.assertIn(f"argv=['--delete', '{OWNER}', '4242']", calls)
        self.assertEqual(self.item(1)["status"], "done")


class ReplyFailClosedTests(ReplyTestBase):
    """--reply with no batch, or a split batch, sends nothing."""

    def test_reply_no_claimed_items_fails_closed(self):
        # No live batch under the token (recycled while the turn
        # worked, or a bogus token): nothing is sent, and the stage
        # is the distinct "stale-token", not the generic "fence"
        # (which is kept for a split batch, below).
        cmd = self.write_send_cmd()
        with self.send_env(cmd):
            result = run_cli(
                self.path, ["--reply", "bogus-token"],
                stdin_text="hello?",
            )
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["stage"], "stale-token")
        self.assertIsNone(result["chat_id"])
        self.assertEqual(result["replied_update_ids"], [])
        self.assertEqual(result["closed_update_ids"], [])
        self.assertFalse(self.calls.exists())

    def test_reply_items_spanning_chat_ids_fails_closed(self):
        # Two lanes claimed under two tokens; force both items onto
        # one token to simulate a split batch, then --reply must
        # refuse without sending anything.
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id=1,
                chat_id=OWNER, chat_type="private", sender_id=OWNER,
                text="first", message_id=9001,
            )
            queue_core.enqueue(
                state, platform="telegram", external_id=2,
                chat_id=GROUP, chat_type="group", sender_id=OWNER,
                text="other lane", message_id=9002,
            )
            batch1 = queue_core.claim_batch(
                state, platform="telegram", batch_size=5
            )
            batch2 = queue_core.claim_batch(
                state, platform="telegram", batch_size=5
            )
            token = batch1["claim_token"]
            self.assertIsNotNone(batch2)
            queue_core.find_item(state, "telegram", 2)["claim_token"] = token
        cmd = self.write_send_cmd()
        with self.send_env(cmd):
            result = run_cli(
                self.path, ["--reply", token],
                stdin_text="hello?",
            )
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["stage"], "fence")
        self.assertFalse(self.calls.exists())
        for item in self.items():
            self.assertEqual(item["status"], "claimed")
            self.assertIsNone(item["replied_at"])

    def test_reply_usage_errors_name_the_ack_rule(self):
        # No token at all.
        result = run_cli(self.path, ["--reply"], stdin_text="x")
        self.assertEqual(result["ok"], False)
        self.assertIn("--send-ack", result["error"])
        # Unknown flag.
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        result = run_cli(
            self.path, ["--reply", payload["claim_token"], "--bogus"],
            stdin_text="x",
        )
        self.assertEqual(result["ok"], False)
        self.assertIn("--send-ack", result["error"])
        # --react with no emoji.
        result = run_cli(
            self.path, ["--reply", payload["claim_token"], "--react"],
            stdin_text="x",
        )
        self.assertEqual(result["ok"], False)
        self.assertEqual(self.item(1)["status"], "claimed")
        self.assertFalse(self.calls.exists())


class ReplyUnknownSendTests(ReplyTestBase):
    """An unknown send is never resent and never marked replied."""

    def test_reply_unknown_send_does_not_resend_or_close(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "first",
                       message_id=9001),
            msg_update(2, OWNER, "private", OWNER, "second",
                       message_id=9002),
        ])
        cmd = self.write_send_cmd(fail_on="send")
        with self.send_env(cmd):
            result = run_cli(
                self.path, ["--reply", payload["claim_token"]],
                stdin_text="maybe delivered?",
            )
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["stage"], "unknown")
        self.assertIsNone(result["message_id"])
        self.assertEqual(result["chat_id"], OWNER)
        self.assertEqual(result["replied_update_ids"], [])
        self.assertEqual(result["closed_update_ids"], [])
        # Exactly ONE send attempt: no retry inside --reply.
        calls = self.call_log()
        self.assertEqual(calls.count("stdin="), 1)
        # The primary went to "sending" for reconciliation; the
        # sibling stays claimed; neither is recorded as replied.
        self.assertEqual(self.item(2)["status"], "sending")
        self.assertEqual(self.item(1)["status"], "claimed")
        for item in self.items():
            self.assertIsNone(item["replied_at"])
            self.assertIsNone(item.get("reply_message_id"))


def load_sender_script():
    """Load bin/telegram-sender as a module (no .py suffix, so the
    loader must be explicit)."""
    here = Path(__file__).resolve()
    sender_path = here.parent.parent / "bin" / "telegram-sender"
    loader = SourceFileLoader("telegram_sender_under_test", str(sender_path))
    spec = importlib.util.spec_from_loader(
        "telegram_sender_under_test", loader
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SenderDeleteTests(unittest.TestCase):
    """bin/telegram-sender --delete: deleteMessage, best-effort."""

    def setUp(self):
        self.sender = load_sender_script()

    def test_delete_message_posts_delete_message(self):
        calls = []

        def fake_post(method, payload):
            calls.append((method, payload))
            return {"ok": True, "result": True}

        with mock.patch.object(self.sender, "_post", fake_post):
            self.assertTrue(self.sender.delete_message(111, 55))
        self.assertEqual(calls, [
            ("deleteMessage", {"chat_id": 111, "message_id": 55}),
        ])

    def test_delete_message_false_when_telegram_refuses(self):
        with mock.patch.object(
            self.sender, "_post", lambda m, p: {"ok": False}
        ):
            self.assertFalse(self.sender.delete_message(111, 55))

    def test_delete_main_dispatch(self):
        with mock.patch.object(
            self.sender, "_post",
            lambda m, p: {"ok": True, "result": True},
        ):
            with mock.patch.object(
                sys, "argv",
                ["telegram-sender", "--delete", "111", "55"],
            ):
                self.assertEqual(self.sender.main(), 0)

    def test_delete_main_dispatch_bad_id_is_usage_error(self):
        with mock.patch.object(
            sys, "argv",
            ["telegram-sender", "--delete", "111", "nope"],
        ):
            self.assertEqual(self.sender.main(), 2)

    def test_delete_message_unit_level(self):
        # telegram.delete_message only returns True on sender exit
        # 0; a failing sender (exit 1) is a plain False, never an
        # exception, so --reply can send anyway.
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "failing-send.py"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            self.assertFalse(
                telegram.delete_message(str(script), 111, 55)
            )
            self.assertFalse(telegram.delete_message(None, 111, 55))
            self.assertFalse(
                telegram.delete_message(str(script), 111, "nope")
            )


class FollowUpFetch:
    """Fake getUpdates: returns one follow-up update on the first
    call, then nothing. The warm hold ingests it on its refetch
    pass and claims it on the next state check."""

    def __init__(self, updates):
        self.updates = list(updates)
        self.calls = 0
        self.offsets = []

    def __call__(self, offset):
        self.calls += 1
        self.offsets.append(offset)
        if self.calls == 1:
            return {"ok": True, "result": list(self.updates)}
        return {"ok": True, "result": []}


class WarmHoldTests(ReplyTestBase):
    """_warm_hold inside --reply: same-lane hit, other-lane
    untouched, expiry, the 30 s heartbeat-age baton guard, and a
    dead hold's lease expiring on its own (kill -9 mechanism)."""

    def reply_with_hold(self, payload, text, hold, fetch):
        cmd = self.write_send_cmd()
        store = JsonStateStore(self.path)
        env = {
            telegram.SEND_CMD_ENV: cmd,
            telegram.POLL_LOCK_PATH_ENV: str(self.dir / "poll.lock"),
        }
        with mock.patch.dict(os.environ, env):
            return telegram.send_reply(
                store, payload["claim_token"], text,
                warm_hold_seconds=hold, fetch=fetch,
                api_call=FakeApi(),
            )

    def test_follow_up_same_lane_is_claimed_warm(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        fetch = FollowUpFetch([
            msg_update(2, OWNER, "private", OWNER, "follow up",
                       message_id=9002),
        ])
        result = self.reply_with_hold(payload, "hey", 1.0, fetch)
        self.assertTrue(result["ok"])
        next_batch = result["next_batch"]
        self.assertIsNotNone(next_batch)
        self.assertEqual(
            [it["update_id"] for it in next_batch["items"]], [2]
        )
        self.assertEqual(next_batch["items"][0]["text"], "follow up")
        self.assertNotEqual(
            next_batch["claim_token"], payload["claim_token"]
        )
        item2 = self.item(2)
        self.assertEqual(item2["status"], "claimed")
        self.assertTrue(item2["warm_claimed"])
        # The lease is cleared once the follow-up was claimed.
        leases = JsonStateStore(self.path).load().get("warm_leases") or {}
        self.assertEqual(leases, {})
        # Typing fired for the send and again for the warm claim.
        self.assertGreaterEqual(
            self.call_log().count(f"argv=['--typing', '{OWNER}']"), 2
        )

    def test_follow_up_other_lane_is_left_pending(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        other = 222
        fetch = FollowUpFetch([
            msg_update(2, other, "private", OWNER, "other lane",
                       message_id=9003),
        ])
        result = self.reply_with_hold(payload, "hey", 0.8, fetch)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["next_batch"])
        item2 = self.item(2)
        self.assertEqual(item2["status"], "pending")
        self.assertEqual(item2["chat_id"], other)
        # Zero cross-lane blocking: intake can still claim it
        # afterwards as a normal cold wake.
        payload2 = run_cli(self.path, [], fetch=FakeFetch())
        self.assertIsNotNone(payload2)
        self.assertEqual(
            [it["update_id"] for it in payload2["items"]], [2]
        )

    def test_no_follow_up_expires_and_clears_lease(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        result = self.reply_with_hold(payload, "hey", 0.5, FollowUpFetch([]))
        self.assertTrue(result["ok"])
        self.assertIsNone(result["next_batch"])
        leases = JsonStateStore(self.path).load().get("warm_leases") or {}
        self.assertEqual(leases, {})
        self.assertEqual(self.item(1)["status"], "done")

    def test_no_warm_hold_flag_returns_immediately(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        started = time.monotonic()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--reply", payload["claim_token"], "--no-warm-hold"],
                stdin_text="hey",
            )
        self.assertTrue(result["ok"])
        self.assertIsNone(result["next_batch"])
        self.assertLess(time.monotonic() - started, 5.0)

    def test_warm_claim_no_heartbeat_recycles_after_30s(self):
        # Baton guard is heartbeat age, NOT claimed_at: a warm
        # agent reading MEMORY before its first heartbeat must
        # not be recycled while thinking.
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            item = next(
                i for i in state["queue"] if i["external_id"] == "1"
            )
            item["warm_claimed"] = True
            heartbeat_at = item["heartbeat_at"]
        self.assertIsNotNone(heartbeat_at)
        with store.transaction() as state:
            self.assertEqual(
                queue_core.stale_claims(
                    state, now=heartbeat_at + 29,
                    warm_baton_timeout=30.0,
                ),
                [],
            )
        with store.transaction() as state:
            recycled = queue_core._recycle_stale(
                state, queue_core.DEFAULT_STALE_TTL,
                now=heartbeat_at + 31, warm_baton_timeout=30.0,
            )
            self.assertEqual(len(recycled), 1)
            item = next(
                i for i in state["queue"] if i["external_id"] == "1"
            )
            self.assertEqual(item["status"], "pending")
            self.assertIsNone(item["claim_token"])
            self.assertFalse(item["warm_claimed"])

    def test_dead_warm_hold_lease_expires_and_lane_reopens(self):
        # Kill -9 mechanism: a warm hold that dies leaves only its
        # lease behind. While live, the lease keeps the lane busy
        # (intake ingests but cannot claim); once expired, the
        # lane re-opens and the pending item is claimable, with
        # nothing lost.
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        with self.send_env(cmd):
            result = run_cli(
                self.path, ["--reply", payload["claim_token"]],
                stdin_text="hey",
            )
        self.assertTrue(result["ok"])
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            queue_core.set_warm_lease(
                state, OWNER, payload["claim_token"], "unified", 25.0,
                now=time.time(),
            )
        payload2 = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(2, OWNER, "private", OWNER, "after death",
                           message_id=9002),
            ]),
        )
        self.assertIsNone(payload2)
        self.assertEqual(self.item(2)["status"], "pending")
        with store.transaction() as state:
            state["warm_leases"][str(OWNER)]["expires_at"] = (
                time.time() - 1
            )
        payload3 = run_cli(self.path, [], fetch=FakeFetch())
        self.assertIsNotNone(payload3)
        self.assertEqual(
            [it["update_id"] for it in payload3["items"]], [2]
        )


if __name__ == "__main__":
    unittest.main()
