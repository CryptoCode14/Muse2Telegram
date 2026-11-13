"""Tests for the P1.5 outbox dispatcher (OUTBOX-FIRST delivery).

The agent writes its reply into a SQLite outbox with --outbox and
closes the turn; the poll script (run_once, before the long poll)
delivers, retries, and records reply_message_id before marking the
queue item done. Delivery never needs a turn.

Covered here:
- --outbox write path: fence (same claim token rules as --reply),
  batch cover, keyboard rule, stale-age guard, already-queued.
- Dispatcher outcomes through the existing B1 three-way sender
  contract: sent (reply_message_id recorded before done), retryable
  retries with Retry-After (item stays SENDING — the entry owns
  the retry), definitive marks failed without retry (item back to
  pending), unknown never resent, supersede (already replied /
  recycled claim), no double-send of the same entry, ack-card
  delete, record failure.
- run_once delivers a queued reply with no wake (no new updates).
- OutboxStore transitions (CAS, overlap refusal, terminal states).
- Stale-sending recovery: a dispatcher killed mid-attempt leaves
  an entry in "sending" with an old attempted_at; the next
  dispatcher pass re-queues it (CAS) and delivers exactly once,
  a live attempt inside the horizon is never touched, and the
  watchdog sweep reports stale-sending entries.
- --outbox-status / --outbox-drop operator commands.

The Bot API is never touched: the send command is a fake script
whose behaviour is driven by a control file (ok / rejected /
retryable / unknown), so one test can flip it between dispatch
passes. Offline: no network, no credentials, no live bot.
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from bridge_core import outbox
from bridge_core import queue as queue_core
from bridge_core import telegram
from bridge_core.outbox import OutboxStore
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
    """Canned Bot API: no network, no credentials."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, params, timeout=20.0):
        self.calls.append((method, params))
        if method == "getChatAdministrators":
            return {"ok": True, "result": []}
        if method == "getChatMemberCount":
            return {"ok": True, "result": 2}
        return {"ok": True, "result": {}}


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


# The fake send script mirrors the real telegram-sender contract:
# a text send prints the message id ("4242") as its last stdout
# line; --typing/--react/--delete exit 0 with no output. The
# behaviour is driven by a control file so a test can flip it
# between dispatch passes: first line ok | rejected | retryable |
# unknown, second line optional retry-after=N. Every invocation is
# logged (argv + stdin for text sends).
_FAKE_SEND_BODY = """\
#!/usr/bin/env python3
import sys
with open({log!r}, 'a', encoding='utf-8') as fh:
    fh.write('argv=' + repr(sys.argv[1:]) + '\\n')
mode = "ok"
retry_after = None
try:
    with open({ctl!r}, encoding='utf-8') as fh:
        lines = [ln.strip() for ln in fh if ln.strip()]
    if lines:
        mode = lines[0]
    for ln in lines[1:]:
        if ln.startswith("retry-after="):
            retry_after = ln.split("=", 1)[1]
except OSError:
    pass
if sys.argv[1:2] and not sys.argv[1].startswith('--'):
    with open({log!r}, 'a', encoding='utf-8') as fh:
        fh.write('stdin=' + sys.stdin.read() + '\\n')
if mode == "rejected":
    print("telegram rejected: telegram HTTP 403", file=sys.stderr)
    raise SystemExit(2)
if mode == "retryable":
    if retry_after is not None:
        print(f"telegram HTTP 429 retry-after={{retry_after}}", file=sys.stderr)
    else:
        print("telegram HTTP 429", file=sys.stderr)
    raise SystemExit(3)
if mode == "unknown":
    print("telegram call failed: boom", file=sys.stderr)
    raise SystemExit(1)
print("4242")
"""


class OutboxTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "state.json"
        self.calls = self.dir / "send-calls.log"
        self.ctl = self.dir / "send-behavior.txt"
        self.ctl.write_text("ok\n", encoding="utf-8")
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def write_send_cmd(self):
        script = self.dir / "fake-send.py"
        script.write_text(
            _FAKE_SEND_BODY.format(log=str(self.calls), ctl=str(self.ctl)),
            encoding="utf-8",
        )
        script.chmod(0o755)
        return str(script)

    def set_send_mode(self, mode, retry_after=None):
        text = mode + "\n"
        if retry_after is not None:
            text += f"retry-after={retry_after}\n"
        self.ctl.write_text(text, encoding="utf-8")

    def send_env(self, cmd):
        # The warm hold is disabled for the --reply calls in this
        # file (they must stay fast); the outbox path never holds.
        return mock.patch.dict(
            os.environ,
            {
                telegram.SEND_CMD_ENV: cmd,
                telegram.WARM_HOLD_SECONDS_ENV: "0",
            },
        )

    def no_send_env(self):
        return mock.patch.dict(
            os.environ,
            {telegram.WARM_HOLD_SECONDS_ENV: "0"},
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

    def text_sends(self):
        """Argv/stdin pairs for the text sends in the call log."""
        log = self.call_log()
        sends = []
        current_argv = None
        for line in log.splitlines():
            if line.startswith("argv="):
                current_argv = line[len("argv="):]
            elif line.startswith("stdin=") and current_argv is not None:
                sends.append((current_argv, line[len("stdin="):]))
                current_argv = None
        return sends

    def outbox_entries(self):
        return OutboxStore(
            self.path.with_suffix(".outbox.sqlite3")
        ).list_entries()

    def outbox_entry(self, entry_id):
        return OutboxStore(
            self.path.with_suffix(".outbox.sqlite3")
        ).get(entry_id)

    def write_outbox(self, token, text, cmd, extra_args=()):
        with self.send_env(cmd):
            return run_cli(
                self.path, ["--outbox", token, *extra_args],
                stdin_text=text,
            )

    def no_send_cmd(self):
        """Environment with no send command configured (the
        dispatcher defers): the --outbox write queues and the
        immediate dispatch is skipped."""
        env = dict(os.environ)
        env.pop(telegram.SEND_CMD_ENV, None)
        env[telegram.WARM_HOLD_SECONDS_ENV] = "0"
        return mock.patch.dict(os.environ, env, clear=True)

    def write_outbox_pending(self, token, text, extra_args=()):
        """--outbox with no sender configured: the write queues
        the reply and the immediate dispatch is deferred (no
        send command), so the entry stays pending — the
        killed-turn setup, and what the poll then delivers."""
        with self.no_send_cmd():
            result = run_cli(
                self.path, ["--outbox", token, *extra_args],
                stdin_text=text,
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["delivery"], "deferred", result)
        return result

    def dispatch(self, cmd, token=None, **kwargs):
        argv = ["--dispatch-outbox"] + ([token] if token else [])
        with self.send_env(cmd):
            return run_cli(self.path, argv, **kwargs)


class OutboxWriteTests(OutboxTestBase):
    """--outbox writes AND dispatches in one call."""

    def test_write_delivers_in_the_same_call(self):
        # The fold: one --outbox call writes the reply and sends
        # it, with no poll tick and no second call in between.
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        result = self.write_outbox(
            payload["claim_token"], "hi there", cmd,
        )
        self.assertTrue(result["ok"], result)
        self.assertIsInstance(result["outbox_id"], int)
        self.assertEqual(result["target_update_id"], 1)
        self.assertEqual(result["covered_update_ids"], [1])
        self.assertEqual(result["chat_id"], OWNER)
        self.assertEqual(result["delivery"], "delivered")
        self.assertEqual(result["message_id"], "4242")
        # Exactly one send, in this call: no dispatch was needed.
        self.assertEqual(len(self.text_sends()), 1)
        self.assertEqual(self.text_sends()[0][1], "hi there")
        # The item is done, the entry delivered, the reply
        # recorded before the item was closed.
        item = self.item(1)
        self.assertEqual(item["status"], "done")
        self.assertEqual(item["reply_message_id"], "4242")
        self.assertIsNotNone(item["replied_at"])
        entry = self.outbox_entry(result["outbox_id"])
        self.assertEqual(entry["status"], "delivered")
        self.assertEqual(entry["text"], "hi there")
        self.assertEqual(entry["chat_id"], OWNER)
        self.assertEqual(entry["reply_to_message_id"], 9001)
        self.assertEqual(entry["extra_update_ids"], [])
        self.assertEqual(entry["attempts"], 1)
        self.assertEqual(entry["reply_message_id"], "4242")

    def test_write_without_sender_queues_and_defers(self):
        # No send command configured: the write still succeeds,
        # the entry stays pending, the item stays CLAIMED under
        # the token — the poll is the backstop.
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        result = self.write_outbox_pending(
            payload["claim_token"], "hi there",
        )
        self.assertIsInstance(result["outbox_id"], int)
        self.assertEqual(result["target_update_id"], 1)
        self.assertEqual(result["covered_update_ids"], [1])
        self.assertEqual(result["chat_id"], OWNER)
        item = self.item(1)
        self.assertEqual(item["status"], "claimed")
        self.assertIsNone(item["replied_at"])
        entry = self.outbox_entry(result["outbox_id"])
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["text"], "hi there")
        self.assertEqual(entry["chat_id"], OWNER)
        self.assertEqual(entry["reply_to_message_id"], 9001)
        self.assertEqual(entry["extra_update_ids"], [])
        self.assertEqual(entry["attempts"], 0)
        self.assertIsNone(entry["reply_message_id"])
        self.assertFalse(self.calls.exists())

    def test_write_batch_covers_all_items(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "first",
                       message_id=9001),
            msg_update(2, OWNER, "private", OWNER, "second",
                       message_id=9002),
        ])
        self.assertEqual(len(payload["items"]), 2)
        cmd = self.write_send_cmd()
        result = self.write_outbox(
            payload["claim_token"], "both answered", cmd,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["delivery"], "delivered")
        self.assertEqual(result["covered_update_ids"], [2, 1])
        entry = self.outbox_entry(result["outbox_id"])
        self.assertEqual(entry["target_update_id"], 2)
        self.assertEqual(entry["reply_to_message_id"], 9002)
        self.assertEqual(entry["extra_update_ids"], [1])
        self.assertEqual(entry["status"], "delivered")
        for external_id in (1, 2):
            self.assertEqual(self.item(external_id)["status"], "done")

    def test_write_callback_batch_is_standalone(self):
        payload = self.claim([
            callback_update(1, OWNER, "cmd:status"),
        ])
        cmd = self.write_send_cmd()
        result = self.write_outbox(
            payload["claim_token"], "status: on", cmd,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["delivery"], "delivered")
        entry = self.outbox_entry(result["outbox_id"])
        self.assertIsNone(entry["reply_to_message_id"])

    def test_write_empty_text_is_input_stage(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        result = self.write_outbox(payload["claim_token"], "   ", cmd)
        self.assertEqual(result, {"ok": False, "stage": "input"})
        self.assertEqual(self.outbox_entries(), [])

    def test_write_oversized_text_is_input_stage(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        result = self.write_outbox(
            payload["claim_token"], "x" * 4097, cmd,
        )
        self.assertEqual(result, {"ok": False, "stage": "input"})
        self.assertEqual(self.outbox_entries(), [])

    def test_write_stale_token_refused(self):
        self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        result = self.write_outbox("no-such-token", "hi", cmd)
        self.assertEqual(result, {"ok": False, "stage": "stale-token"})
        self.assertEqual(self.outbox_entries(), [])

    def test_write_done_item_refused(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        token = payload["claim_token"]
        run_cli(self.path, ["--done", "1", token, "not-for-me"])
        cmd = self.write_send_cmd()
        result = self.write_outbox(token, "hi", cmd)
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["stage"], "stale-token")
        self.assertEqual(self.outbox_entries(), [])

    def test_write_second_write_is_already_queued(self):
        # Written with no sender, so the entry stays pending and
        # the batch is still live: the second write refuses.
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        first = self.write_outbox_pending(
            payload["claim_token"], "hi",
        )
        with self.no_send_cmd():
            second = run_cli(
                self.path, ["--outbox", payload["claim_token"]],
                stdin_text="hi again",
            )
        self.assertEqual(second["ok"], False)
        self.assertEqual(second["stage"], "already-queued")
        self.assertEqual(second["target_update_id"], 1)
        self.assertEqual(len(self.outbox_entries()), 1)
        self.assertEqual(
            self.outbox_entries()[0]["text"], "hi",
        )
        self.assertEqual(
            self.outbox_entries()[0]["id"], first["outbox_id"],
        )

    def test_write_after_delivered_write_is_stale_token(self):
        # The first write delivered in the same call, so the
        # batch is done: a second write is a stale token, and
        # nothing is sent twice.
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        first = self.write_outbox(payload["claim_token"], "hi", cmd)
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["delivery"], "delivered")
        second = self.write_outbox(
            payload["claim_token"], "hi again", cmd,
        )
        self.assertEqual(second["ok"], False)
        self.assertEqual(second["stage"], "stale-token")
        self.assertEqual(len(self.outbox_entries()), 1)
        self.assertEqual(len(self.text_sends()), 1)

    def test_write_keyboard_on_plain_chat_refused(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        result = self.write_outbox(
            payload["claim_token"], "hi", cmd, extra_args=("--keyboard",),
        )
        self.assertEqual(result, {"ok": False, "stage": "keyboard"})
        self.assertEqual(self.outbox_entries(), [])

    def test_write_keyboard_on_command_ok(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "/status",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        result = self.write_outbox(
            payload["claim_token"], "on", cmd, extra_args=("--keyboard",),
        )
        self.assertTrue(result["ok"], result)
        entry = self.outbox_entry(result["outbox_id"])
        self.assertTrue(entry["keyboard"])

    def test_write_split_batch_fails_closed(self):
        # Two lanes claimed under two tokens; force both items onto
        # one token to simulate a split batch, then --outbox must
        # refuse without queueing anything.
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
        result = self.write_outbox(token, "hello?", cmd)
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["stage"], "fence")
        self.assertEqual(self.outbox_entries(), [])

    def test_write_usage_error(self):
        result = run_cli(self.path, ["--outbox"], stdin_text="x")
        self.assertEqual(result["ok"], False)
        self.assertIn("usage", result["error"])
        result = run_cli(
            self.path, ["--outbox", "tok", "--bogus"], stdin_text="x",
        )
        self.assertEqual(result["ok"], False)
        self.assertIn("usage", result["error"])


class OutboxDispatchTests(OutboxTestBase):
    """The dispatcher delivers, retries, and never double-sends."""

    def _queued(self, text="hi there", updates=None):
        # The killed-turn setup: the reply is queued with no
        # sender, so the entry stays pending for the dispatcher.
        payload = self.claim(updates or [
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        result = self.write_outbox_pending(
            payload["claim_token"], text,
        )
        return cmd, result["outbox_id"]

    def test_dispatch_delivers_after_killed_turn(self):
        # The headline case: the agent writes the outbox entry and
        # the turn dies (nothing else happens — no --done, no
        # heartbeat, no --reply). The next poll's dispatcher
        # delivers it and closes the item.
        cmd, entry_id = self._queued()
        report = self.dispatch(cmd)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["delivered"], [entry_id])
        self.assertEqual(report["checked"], 1)
        sends = self.text_sends()
        self.assertEqual(len(sends), 1)
        argv, stdin = sends[0]
        self.assertIn(f"'{OWNER}'", argv)
        self.assertIn("'--reply-to', '9001'", argv)
        self.assertEqual(stdin, "hi there")
        # reply_message_id is recorded on the entry AND the item,
        # and the item is done — the same close --reply does.
        entry = self.outbox_entry(entry_id)
        self.assertEqual(entry["status"], "delivered")
        self.assertEqual(entry["reply_message_id"], "4242")
        self.assertIsNotNone(entry["delivered_at"])
        item = self.item(1)
        self.assertEqual(item["status"], "done")
        self.assertIsNotNone(item["replied_at"])
        self.assertEqual(item["reply_message_id"], "4242")

    def test_dispatch_never_double_sends_same_entry(self):
        cmd, entry_id = self._queued()
        first = self.dispatch(cmd)
        self.assertEqual(first["delivered"], [entry_id])
        second = self.dispatch(cmd)
        self.assertEqual(second["checked"], 0)
        self.assertEqual(second["delivered"], [])
        self.assertEqual(len(self.text_sends()), 1)

    def test_dispatch_token_scoped_delivers_only_that_token(self):
        # Two batches in two lanes, both queued pending; a
        # token-scoped dispatch delivers only its own token's
        # entry, leaving the other's pending for the poll (bare
        # dispatch).
        cmd = self.write_send_cmd()
        store = JsonStateStore(self.path)
        now = time.time()
        with store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id=1,
                chat_id=OWNER, chat_type="private", sender_id=OWNER,
                text="hello A", message_id=9001, now=now,
            )
            queue_core.enqueue(
                state, platform="telegram", external_id=2,
                chat_id=222, chat_type="private", sender_id=OWNER,
                text="hello B", message_id=9002, now=now,
            )
            batch_a = queue_core.claim_batch(
                state, platform="telegram", now=now)
            batch_b = queue_core.claim_batch(
                state, platform="telegram", now=now)
        token_a = batch_a["claim_token"]
        token_b = batch_b["claim_token"]
        self.assertNotEqual(token_a, token_b)
        result_a = self.write_outbox_pending(token_a, "reply A")
        result_b = self.write_outbox_pending(token_b, "reply B")
        report = self.dispatch(cmd, token=token_a)
        self.assertEqual(report["delivered"], [result_a["outbox_id"]])
        self.assertEqual(report["checked"], 1)
        self.assertEqual(
            self.outbox_entry(result_b["outbox_id"])["status"], "pending")
        self.assertEqual(len(self.text_sends()), 1)
        report = self.dispatch(cmd)
        self.assertEqual(report["delivered"], [result_b["outbox_id"]])
        self.assertEqual(len(self.text_sends()), 2)

    def test_write_dispatch_then_poll_no_double_delivery(self):
        # The fold: the --outbox call itself delivers in the
        # turn; a later poll pass (bare dispatch) finds nothing
        # due and never sends twice.
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        result = self.write_outbox(
            payload["claim_token"], "hi there", cmd)
        entry_id = result["outbox_id"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["delivery"], "delivered")
        poll_pass = self.dispatch(cmd)
        self.assertEqual(poll_pass["checked"], 0)
        self.assertEqual(poll_pass["delivered"], [])
        again = self.dispatch(cmd, token=payload["claim_token"])
        self.assertEqual(again["checked"], 0)
        self.assertEqual(again["delivered"], [])
        self.assertEqual(len(self.text_sends()), 1)
        self.assertEqual(
            self.outbox_entry(entry_id)["status"], "delivered")

    def test_write_definitive_returns_items_to_pending(self):
        # The fold's definitive outcome: the send provably did
        # not go out, so the entry fails and the item is back to
        # pending for a fresh turn — all inside the one call.
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        self.set_send_mode("rejected")
        result = self.write_outbox(
            payload["claim_token"], "hi there", cmd,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["delivery"], "failed")
        self.assertNotIn("message_id", result)
        entry = self.outbox_entry(result["outbox_id"])
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["attempts"], 1)
        item = self.item(1)
        self.assertEqual(item["status"], "pending")
        self.assertIsNone(item["claim_token"])
        self.assertIsNone(item["replied_at"])
        # The send was attempted once, and never retried.
        self.assertEqual(len(self.text_sends()), 1)
        self.assertEqual(self.dispatch(cmd)["checked"], 0)

    def test_write_retryable_requeues_with_capped_delay(self):
        # The fold's retryable outcome: throttled, so the entry
        # re-queues with next_attempt_at = now + min(Retry-After,
        # 300) and the item stays SENDING — the entry owns the
        # retry, and the caller must not send another.
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        self.set_send_mode("retryable", retry_after=120)
        before = time.time()
        result = self.write_outbox(
            payload["claim_token"], "hi there", cmd,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["delivery"], "retried")
        entry = self.outbox_entry(result["outbox_id"])
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["attempts"], 1)
        self.assertEqual(entry["retry_after"], 120)
        self.assertGreaterEqual(entry["next_attempt_at"], before + 120)
        self.assertLess(entry["next_attempt_at"], before + 121)
        item = self.item(1)
        self.assertEqual(item["status"], "sending")
        self.assertIsNone(item["replied_at"])
        self.assertEqual(len(self.text_sends()), 1)
        # Not due yet: a poll pass before next_attempt_at does
        # nothing; due again it delivers, exactly once more.
        self.set_send_mode("ok")
        with self.send_env(cmd):
            late = telegram.dispatch_outbox(
                JsonStateStore(self.path),
                now=entry["next_attempt_at"],
            )
        self.assertEqual(late["delivered"], [result["outbox_id"]])
        self.assertEqual(len(self.text_sends()), 2)

    def test_write_retryable_default_delay_without_retry_after(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        self.set_send_mode("retryable")
        before = time.time()
        result = self.write_outbox(
            payload["claim_token"], "hi there", cmd,
        )
        self.assertEqual(result["delivery"], "retried")
        entry = self.outbox_entry(result["outbox_id"])
        self.assertIsNone(entry["retry_after"])
        self.assertGreaterEqual(entry["next_attempt_at"], before + 60)
        self.assertLess(entry["next_attempt_at"], before + 61)
        self.assertEqual(self.item(1)["status"], "sending")

    def test_write_unknown_never_resent(self):
        # The fold's unknown outcome: the send's fate is unclear,
        # so the entry is unknown, the item stays SENDING, and
        # nothing is ever resent blindly — not by this call, not
        # by a later poll pass.
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        self.set_send_mode("unknown")
        result = self.write_outbox(
            payload["claim_token"], "hi there", cmd,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["delivery"], "unknown")
        entry = self.outbox_entry(result["outbox_id"])
        self.assertEqual(entry["status"], "unknown")
        item = self.item(1)
        self.assertEqual(item["status"], "sending")
        self.assertIsNone(item["replied_at"])
        again = self.dispatch(cmd)
        self.assertEqual(again["checked"], 0)
        self.assertEqual(len(self.text_sends()), 1)

    def test_write_dispatch_race_with_poll_keeps_entry_pending(self):
        # The poll wins the entry first: the write's dispatch
        # finds nothing due, so the delivery is "pending", the
        # entry stays queued and the item stays claimed — and the
        # poll then delivers it exactly once. (Simulated by
        # hiding the due entry from the write's dispatch pass;
        # the status-guarded transitions are what make the real
        # race safe.)
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        with mock.patch.object(
            OutboxStore, "due_entries", return_value=[],
        ):
            result = self.write_outbox(
                payload["claim_token"], "hi there", cmd,
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["delivery"], "pending")
        self.assertNotIn("message_id", result)
        entry = self.outbox_entry(result["outbox_id"])
        self.assertEqual(entry["status"], "pending")
        item = self.item(1)
        self.assertEqual(item["status"], "claimed")
        self.assertEqual(len(self.text_sends()), 0)
        report = self.dispatch(cmd)
        self.assertEqual(report["delivered"], [result["outbox_id"]])
        self.assertEqual(len(self.text_sends()), 1)

    def test_dispatch_retryable_retries_with_retry_after(self):
        cmd, entry_id = self._queued()
        self.set_send_mode("retryable", retry_after=120)
        before = time.time()
        report = self.dispatch(cmd)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["retried"], [entry_id])
        entry = self.outbox_entry(entry_id)
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["attempts"], 1)
        self.assertEqual(entry["retry_after"], 120)
        self.assertGreaterEqual(entry["next_attempt_at"], before + 120)
        self.assertLess(entry["next_attempt_at"], before + 121)
        # The queue item stays SENDING: the entry owns the retry,
        # so no fresh turn can claim the item and answer in parallel.
        item = self.item(1)
        self.assertEqual(item["status"], "sending")
        self.assertIsNone(item["replied_at"])
        # Not due yet: a pass before next_attempt_at attempts nothing.
        with self.send_env(cmd):
            early = telegram.dispatch_outbox(
                JsonStateStore(self.path),
                now=entry["next_attempt_at"] - 1,
            )
        self.assertEqual(early["checked"], 0)
        self.assertEqual(len(self.text_sends()), 1)
        # Due again: flip the sender to ok and dispatch at the due
        # time — the same entry delivers, exactly once more.
        self.set_send_mode("ok")
        with self.send_env(cmd):
            late = telegram.dispatch_outbox(
                JsonStateStore(self.path),
                now=entry["next_attempt_at"],
            )
        self.assertEqual(late["delivered"], [entry_id])
        self.assertEqual(len(self.text_sends()), 2)
        item = self.item(1)
        self.assertEqual(item["status"], "done")
        self.assertEqual(item["reply_message_id"], "4242")

    def test_dispatch_retryable_default_delay_without_retry_after(self):
        cmd, entry_id = self._queued()
        self.set_send_mode("retryable")
        before = time.time()
        report = self.dispatch(cmd)
        self.assertEqual(report["retried"], [entry_id])
        entry = self.outbox_entry(entry_id)
        self.assertIsNone(entry["retry_after"])
        self.assertGreaterEqual(entry["next_attempt_at"], before + 60)
        self.assertLess(entry["next_attempt_at"], before + 61)

    def test_dispatch_definitive_marks_failed_without_retry(self):
        cmd, entry_id = self._queued()
        self.set_send_mode("rejected")
        report = self.dispatch(cmd)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["failed"], [entry_id])
        entry = self.outbox_entry(entry_id)
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["attempts"], 1)
        # The item is back to pending, so a later turn can answer it.
        item = self.item(1)
        self.assertEqual(item["status"], "pending")
        self.assertIsNone(item["claim_token"])
        # Never retried: a second pass attempts nothing.
        again = self.dispatch(cmd)
        self.assertEqual(again["checked"], 0)
        self.assertEqual(len(self.text_sends()), 1)
        # And a fresh write for the same item is allowed again.
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        result = self.write_outbox(payload["claim_token"], "retry", cmd)
        self.assertTrue(result["ok"], result)

    def test_dispatch_unknown_never_resent(self):
        cmd, entry_id = self._queued()
        self.set_send_mode("unknown")
        report = self.dispatch(cmd)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["unknown"], [entry_id])
        entry = self.outbox_entry(entry_id)
        self.assertEqual(entry["status"], "unknown")
        # The item stays in "sending" for operator reconciliation.
        item = self.item(1)
        self.assertEqual(item["status"], "sending")
        self.assertIsNone(item["replied_at"])
        # Never resent: later passes attempt nothing.
        again = self.dispatch(cmd)
        self.assertEqual(again["checked"], 0)
        self.assertEqual(len(self.text_sends()), 1)

    def test_dispatch_supersedes_already_replied(self):
        # The turn wrote the outbox entry (queued pending) and
        # then (against the prompt) also sent with --reply: the
        # dispatcher must not send the queued text a second time.
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        written = self.write_outbox_pending(
            payload["claim_token"], "hi there",
        )
        with self.send_env(cmd):
            replied = run_cli(
                self.path, ["--reply", payload["claim_token"]],
                stdin_text="hi there",
            )
        self.assertTrue(replied["ok"], replied)
        self.assertEqual(len(self.text_sends()), 1)
        report = self.dispatch(cmd)
        self.assertEqual(report["superseded"], [written["outbox_id"]])
        self.assertEqual(len(self.text_sends()), 1)
        entry = self.outbox_entry(written["outbox_id"])
        self.assertEqual(entry["status"], "superseded")

    def test_dispatch_supersedes_recycled_claim(self):
        # The claim was recycled (turn died before the dispatcher
        # ran): the queued reply is dropped and the item is left
        # pending for the re-wake to answer fresh.
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        written = self.write_outbox_pending(
            payload["claim_token"], "hi there",
        )
        run_cli(
            self.path,
            ["--release", "1", payload["claim_token"]],
        )
        self.assertEqual(self.item(1)["status"], "pending")
        report = self.dispatch(cmd)
        self.assertEqual(report["superseded"], [written["outbox_id"]])
        self.assertEqual(len(self.text_sends()), 0)

    def test_dispatch_config_without_send_cmd(self):
        cmd, entry_id = self._queued()
        with self.no_send_env():
            result = run_cli(self.path, ["--dispatch-outbox"])
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["stage"], "config")
        self.assertEqual(result["deferred"], [entry_id])
        entry = self.outbox_entry(entry_id)
        self.assertEqual(entry["status"], "pending")
        self.assertFalse(self.calls.exists())

    def test_dispatch_deletes_ack_card(self):
        # The hook's claim card goes out at wake; the dispatcher
        # deletes it when the reply lands, exactly like --reply.
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        with self.send_env(cmd):
            acked = run_cli(
                self.path, ["--send-claim-ack", payload["claim_token"]],
            )
        self.assertTrue(acked["ok"], acked)
        card_id = acked["message_id"]
        written = self.write_outbox_pending(
            payload["claim_token"], "hi there",
        )
        report = self.dispatch(cmd)
        self.assertEqual(report["delivered"], [written["outbox_id"]])
        self.assertIn(
            f"argv=['--delete', '{OWNER}', '{card_id}']",
            self.call_log(),
        )

    def test_dispatch_batch_entry_closes_all_items(self):
        cmd, entry_id = self._queued(
            text="both answered",
            updates=[
                msg_update(1, OWNER, "private", OWNER, "first",
                           message_id=9001),
                msg_update(2, OWNER, "private", OWNER, "second",
                           message_id=9002),
            ],
        )
        report = self.dispatch(cmd)
        self.assertEqual(report["delivered"], [entry_id])
        self.assertEqual(len(self.text_sends()), 1)
        for external_id in (1, 2):
            item = self.item(external_id)
            self.assertEqual(item["status"], "done")
            self.assertEqual(item["reply_message_id"], "4242")
        self.assertEqual(
            self.item(1)["done_reason"], "covered-by-batch-reply",
        )

    def test_dispatch_record_failure_keeps_message_id(self):
        # The send went out but the queue record could not be
        # written: the outbox row already carries the message id
        # (recorded BEFORE the queue close), the item stays in
        # "sending" for reconciliation, and nothing is resent.
        cmd, entry_id = self._queued()
        store = JsonStateStore(self.path)
        with mock.patch.object(
            telegram, "_finish_send", return_value=(False, []),
        ), self.send_env(cmd):
            report = telegram.dispatch_outbox(store)
        self.assertEqual(report["record_failed"], [entry_id])
        entry = self.outbox_entry(entry_id)
        self.assertEqual(entry["status"], "delivered")
        self.assertEqual(entry["reply_message_id"], "4242")
        item = self.item(1)
        self.assertEqual(item["status"], "sending")
        self.assertIsNone(item["replied_at"])
        again = self.dispatch(cmd)
        self.assertEqual(again["checked"], 0)
        self.assertEqual(len(self.text_sends()), 1)


class OutboxStaleSendingTests(OutboxTestBase):
    """A dispatcher killed mid-attempt must not strand the reply.

    The stranded state: the outbox entry is in "sending" with an
    old attempted_at, and the queue item is in "sending" under
    the entry's token — exactly what _dispatch_one_outbox_entry
    leaves behind when the dispatcher dies after claiming the
    attempt and reserving the item, before the send outcome is
    recorded. The next dispatcher pass recovers the entry and
    delivers it exactly once, from the item's own reservation.
    """

    def _stranded(self, text="hi there", attempted_age=None,
                  updates=None):
        # Claim + queue with no sender (the entry stays pending),
        # then drive the attempt halfway by hand: the outbox entry
        # claims the attempt (mark_sending, with an old
        # attempted_at) and the queue item is reserved to sending
        # — the state a dispatcher killed mid-attempt leaves.
        payload = self.claim(updates or [
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        result = self.write_outbox_pending(payload["claim_token"], text)
        entry_id = result["outbox_id"]
        store = OutboxStore(self.path.with_suffix(".outbox.sqlite3"))
        age = (
            outbox.STALE_SENDING_SECONDS + 60.0
            if attempted_age is None else attempted_age
        )
        self.assertTrue(
            store.mark_sending(entry_id, now=time.time() - age))
        with self.send_env(cmd):
            error, _queued_chat_id = telegram._reserve_send(
                JsonStateStore(self.path), 1, payload["claim_token"],
                OWNER, now=time.time(),
            )
        self.assertIsNone(error, error)
        return cmd, entry_id, payload["claim_token"]

    def test_dispatch_recovers_stale_sending_after_killed_attempt(self):
        cmd, entry_id, _token = self._stranded()
        entry = self.outbox_entry(entry_id)
        self.assertEqual(entry["status"], "sending")
        self.assertEqual(entry["attempts"], 1)
        self.assertIsNotNone(entry["attempted_at"])
        self.assertEqual(self.item(1)["status"], "sending")
        report = self.dispatch(cmd)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["recovered"], [entry_id])
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["delivered"], [entry_id])
        self.assertEqual(len(self.text_sends()), 1)
        entry = self.outbox_entry(entry_id)
        self.assertEqual(entry["status"], "delivered")
        self.assertEqual(entry["reply_message_id"], "4242")
        item = self.item(1)
        self.assertEqual(item["status"], "done")
        self.assertIsNotNone(item["replied_at"])
        self.assertEqual(item["reply_message_id"], "4242")
        # A second pass finds nothing: exactly once, no re-send.
        again = self.dispatch(cmd)
        self.assertEqual(again["checked"], 0)
        self.assertEqual(again["delivered"], [])
        self.assertEqual(len(self.text_sends()), 1)

    def test_dispatch_leaves_fresh_sending_entry_alone(self):
        # A live attempt inside the horizon is never touched: no
        # recovery, no send, no reset — a dispatcher may be
        # mid-send on it right now.
        cmd, entry_id, _token = self._stranded(attempted_age=100.0)
        report = self.dispatch(cmd)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["recovered"], [])
        self.assertEqual(report["delivered"], [])
        self.assertEqual(report["checked"], 0)
        self.assertEqual(self.text_sends(), [])
        entry = self.outbox_entry(entry_id)
        self.assertEqual(entry["status"], "sending")
        self.assertEqual(entry["attempts"], 1)
        self.assertEqual(self.item(1)["status"], "sending")

    def test_recovered_entry_item_stays_sending_until_send(self):
        # Recovery touches the outbox entry only: the queue item
        # stays SENDING under the entry's token (its resend
        # reservation), so no fresh turn can claim it and answer
        # in parallel — the item is NOT bounced to pending.
        cmd, entry_id, _token = self._stranded()
        store = OutboxStore(self.path.with_suffix(".outbox.sqlite3"))
        recovered = store.recover_stale_sending(time.time())
        self.assertEqual([e["id"] for e in recovered], [entry_id])
        entry = self.outbox_entry(entry_id)
        self.assertEqual(entry["status"], "pending")
        self.assertIsNotNone(entry["next_attempt_at"])
        item = self.item(1)
        self.assertEqual(item["status"], "sending")
        self.assertIsNotNone(item["claim_token"])
        # The next pass delivers it exactly once from that
        # reservation and closes the item.
        report = self.dispatch(cmd)
        self.assertEqual(report["delivered"], [entry_id])
        self.assertEqual(len(self.text_sends()), 1)
        self.assertEqual(self.item(1)["status"], "done")

    def test_watchdog_reports_stale_sending_outbox_entries(self):
        store = OutboxStore(self.path.with_suffix(".outbox.sqlite3"))

        def _entry(target):
            return {
                "claim_token": "tok",
                "target_update_id": target,
                "chat_id": OWNER,
                "reply_to_message_id": 9001,
                "keyboard": False,
                "text": "hi",
                "extra_update_ids": [],
                "created_at": 1000.0,
            }

        stale_id = store.enqueue(_entry(1))
        fresh_id = store.enqueue(_entry(2))
        self.assertTrue(store.mark_sending(stale_id, now=1000.0))
        self.assertTrue(store.mark_sending(fresh_id, now=time.time()))
        report = telegram.watchdog_sweep(JsonStateStore(self.path))
        stale = report["outbox_stale_sending"]
        self.assertEqual([e["id"] for e in stale], [stale_id])
        self.assertEqual(stale[0]["target_update_id"], 1)
        self.assertEqual(stale[0]["attempted_at"], 1000.0)

    def test_watchdog_reports_no_stale_sending_when_none(self):
        report = telegram.watchdog_sweep(JsonStateStore(self.path))
        self.assertEqual(report["outbox_stale_sending"], [])


class OutboxRunOnceTests(OutboxTestBase):
    """The poll loop delivers a queued reply with no wake."""

    def test_run_once_dispatches_outbox_before_poll(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        written = self.write_outbox_pending(
            payload["claim_token"], "hi there",
        )
        # No new updates arrive; the poll still delivers the reply.
        with self.send_env(cmd):
            result = run_cli(self.path, [], fetch=FakeFetch([]))
        self.assertIsNone(result)
        self.assertEqual(len(self.text_sends()), 1)
        item = self.item(1)
        self.assertEqual(item["status"], "done")
        self.assertEqual(item["reply_message_id"], "4242")
        entry = self.outbox_entry(written["outbox_id"])
        self.assertEqual(entry["status"], "delivered")

    def test_run_once_survives_dispatcher_failure(self):
        # A corrupt outbox DB must never kill the poll: the intake
        # still claims and wakes.
        outbox_path = self.path.with_suffix(".outbox.sqlite3")
        outbox_path.write_bytes(b"not a sqlite database")
        cmd = self.write_send_cmd()
        with self.send_env(cmd):
            result = run_cli(
                self.path, [], fetch=FakeFetch([
                    msg_update(1, OWNER, "private", OWNER, "hello",
                               message_id=9001),
                ]),
            )
        self.assertIsNotNone(result)
        self.assertEqual(
            [item["update_id"] for item in result["items"]], [1],
        )


class OutboxStoreTests(unittest.TestCase):
    """OutboxStore transitions, directly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "state.outbox.sqlite3"
        self.store = OutboxStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _entry(self, target=1, extras=(), token="tok"):
        return {
            "claim_token": token,
            "target_update_id": target,
            "chat_id": OWNER,
            "reply_to_message_id": 9001,
            "keyboard": False,
            "text": "hi",
            "extra_update_ids": list(extras),
            "created_at": 1000.0,
        }

    def test_outbox_path_is_next_to_state(self):
        # The default derivation: <state stem>.outbox.sqlite3,
        # following the store in use (not just the environment).
        store = JsonStateStore(self.dir / "state.json")
        self.assertEqual(
            telegram.outbox_path(store), self.dir / "state.outbox.sqlite3"
        )
        with mock.patch.dict(
            os.environ, {"TELEGRAM_BRIDGE_STATE_PATH": str(self.dir / "state.json")}
        ):
            self.assertEqual(
                telegram.outbox_path(), self.dir / "state.outbox.sqlite3"
            )
        with mock.patch.dict(
            os.environ, {"TELEGRAM_BRIDGE_OUTBOX_PATH": str(self.path)}
        ):
            self.assertEqual(telegram.outbox_path(), self.path)

    def test_enqueue_refuses_overlapping_live_entry(self):
        first = self.store.enqueue(self._entry())
        self.assertIsNotNone(first)
        self.assertIsNone(self.store.enqueue(self._entry()))
        # Overlapping via an extra is refused too.
        self.assertIsNone(
            self.store.enqueue(self._entry(target=2, extras=[1]))
        )
        # A disjoint entry is fine.
        self.assertIsNotNone(self.store.enqueue(self._entry(target=3)))

    def test_enqueue_allowed_again_after_terminal(self):
        entry_id = self.store.enqueue(self._entry())
        self.assertTrue(self.store.mark_failed(entry_id, "nope"))
        again = self.store.enqueue(self._entry())
        self.assertIsNotNone(again)
        self.assertNotEqual(again, entry_id)

    def test_mark_sending_is_cas(self):
        entry_id = self.store.enqueue(self._entry())
        self.assertTrue(self.store.mark_sending(entry_id))
        self.assertFalse(self.store.mark_sending(entry_id))
        entry = self.store.get(entry_id)
        self.assertEqual(entry["status"], "sending")
        self.assertEqual(entry["attempts"], 1)

    def test_mark_sending_records_attempted_at(self):
        entry_id = self.store.enqueue(self._entry())
        self.assertTrue(self.store.mark_sending(entry_id, now=1500.0))
        entry = self.store.get(entry_id)
        self.assertEqual(entry["status"], "sending")
        self.assertEqual(entry["attempts"], 1)
        self.assertEqual(entry["attempted_at"], 1500.0)
        # A second claim is refused (CAS) and does not move the stamp.
        self.assertFalse(self.store.mark_sending(entry_id, now=1600.0))
        self.assertEqual(
            self.store.get(entry_id)["attempted_at"], 1500.0)

    def test_recover_stale_sending_requeues_only_stale(self):
        stale = self.store.enqueue(self._entry())
        fresh = self.store.enqueue(self._entry(target=2))
        self.assertTrue(self.store.mark_sending(stale, now=1000.0))
        self.assertTrue(self.store.mark_sending(fresh, now=1900.0))
        recovered = self.store.recover_stale_sending(2000.0)
        self.assertEqual([e["id"] for e in recovered], [stale])
        entry = self.store.get(stale)
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["next_attempt_at"], 2000.0)
        self.assertEqual(entry["attempts"], 1)
        self.assertEqual(self.store.get(fresh)["status"], "sending")
        self.assertEqual(
            [row["id"] for row in self.store.due_entries(2000.0, 10)],
            [stale],
        )

    def test_recover_stale_sending_is_cas(self):
        first = self.store.enqueue(self._entry())
        second = self.store.enqueue(self._entry(target=2))
        self.assertTrue(self.store.mark_sending(first, now=1000.0))
        self.assertTrue(self.store.mark_sending(second, now=1000.0))
        # Two dispatchers racing the same stale entries: each
        # entry is recovered exactly once across both passes.
        barrier = threading.Barrier(2)
        recovered = []

        def recover():
            barrier.wait(timeout=10)
            recovered.extend(
                e["id"]
                for e in self.store.recover_stale_sending(2000.0)
            )

        threads = [threading.Thread(target=recover) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sorted(recovered), [first, second])
        self.assertEqual(self.store.get(first)["status"], "pending")
        self.assertEqual(self.store.get(second)["status"], "pending")
        # A second recovery finds nothing: the CAS already moved them.
        self.assertEqual(self.store.recover_stale_sending(2000.0), [])

    def test_stale_entries_without_attempted_at_never_recovered(self):
        # A legacy sending entry: claimed before attempted_at
        # existed, so its attempt start is unprovable — the
        # recovery never touches it (the watchdog reports it for
        # the operator instead).
        entry_id = self.store.enqueue(self._entry())
        conn = sqlite3.connect(str(self.path))
        conn.execute(
            "UPDATE outbox SET status = 'sending', attempts = 1 "
            "WHERE id = ?",
            (entry_id,),
        )
        conn.commit()
        conn.close()
        self.assertEqual(
            self.store.recover_stale_sending(100000.0), [])
        self.assertEqual(
            self.store.get(entry_id)["status"], "sending")

    def test_old_schema_migrates_attempted_at_column(self):
        # A DB created before attempted_at existed (the live
        # outbox, pre-fix): opening it adds the column, and new
        # attempts record it.
        conn = sqlite3.connect(str(self.path))
        conn.executescript(
            "CREATE TABLE outbox ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " claim_token TEXT NOT NULL,"
            " target_update_id INTEGER NOT NULL,"
            " chat_id INTEGER NOT NULL,"
            " reply_to_message_id INTEGER,"
            " keyboard INTEGER NOT NULL DEFAULT 0,"
            " text TEXT NOT NULL,"
            " extra_update_ids TEXT NOT NULL DEFAULT '[]',"
            " status TEXT NOT NULL DEFAULT 'pending',"
            " attempts INTEGER NOT NULL DEFAULT 0,"
            " created_at REAL NOT NULL,"
            " next_attempt_at REAL,"
            " delivered_at REAL,"
            " reply_message_id TEXT,"
            " last_error TEXT,"
            " retry_after REAL"
            ");"
        )
        conn.commit()
        conn.close()
        entry_id = self.store.enqueue(self._entry())
        self.assertIsNotNone(entry_id)
        self.assertTrue(self.store.mark_sending(entry_id, now=1500.0))
        entry = self.store.get(entry_id)
        self.assertEqual(entry["attempted_at"], 1500.0)
        columns = [
            row[1]
            for row in sqlite3.connect(str(self.path)).execute(
                "PRAGMA table_info(outbox)"
            )
        ]
        self.assertIn("attempted_at", columns)

    def test_due_entries_honor_next_attempt_at(self):
        entry_id = self.store.enqueue(self._entry())
        self.assertTrue(self.store.mark_sending(entry_id))
        self.assertTrue(
            self.store.requeue(entry_id, 2000.0, 120, "retryable")
        )
        self.assertEqual(self.store.due_entries(1999.0, 10), [])
        due = self.store.due_entries(2000.0, 10)
        self.assertEqual([row["id"] for row in due], [entry_id])

    def test_outbox_file_is_mode_0600(self):
        self.store.enqueue(self._entry())
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")

    def test_corrupt_db_raises_not_silently(self):
        self.path.write_bytes(b"not a sqlite database")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.due_entries(0, 10)


class OutboxOperatorTests(OutboxTestBase):
    """--outbox-status (read-only) and --outbox-drop."""

    def test_status_lists_entries_newest_first(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        written = self.write_outbox(
            payload["claim_token"], "hi there", cmd,
        )
        self.assertEqual(written["delivery"], "delivered")
        result = run_cli(self.path, ["--outbox-status"])
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["outbox_path"],
            str(self.path.with_suffix(".outbox.sqlite3")),
        )
        self.assertEqual(len(result["entries"]), 1)
        entry = result["entries"][0]
        self.assertEqual(entry["id"], written["outbox_id"])
        self.assertEqual(entry["status"], "delivered")
        self.assertEqual(entry["text"], "hi there")
        self.assertEqual(entry["reply_message_id"], "4242")

    def test_drop_marks_failed_and_blocks_delivery(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        written = self.write_outbox_pending(
            payload["claim_token"], "hi there",
        )
        entry_id = written["outbox_id"]
        result = run_cli(self.path, ["--outbox-drop", str(entry_id)])
        self.assertEqual(result, {"ok": True})
        entry = self.outbox_entry(entry_id)
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["last_error"], "dropped by operator")
        # A dropped entry is terminal: dispatch attempts nothing,
        # and a second drop refuses.
        report = self.dispatch(cmd)
        self.assertEqual(report["checked"], 0)
        self.assertEqual(
            run_cli(self.path, ["--outbox-drop", str(entry_id)]),
            {"ok": False},
        )
        self.assertEqual(
            run_cli(self.path, ["--outbox-drop", "bogus"]),
            {"ok": False, "error": "invalid-outbox-id"},
        )

    def test_status_usage_error(self):
        result = run_cli(self.path, ["--outbox-status", "extra"])
        self.assertEqual(result["ok"], False)
        self.assertIn("usage", result["error"])
        # --dispatch-outbox now takes an optional claim token, so
        # one extra arg is a token, not a usage error — two are.
        result = run_cli(self.path, ["--dispatch-outbox", "extra", "more"])
        self.assertEqual(result["ok"], False)
        self.assertIn("usage", result["error"])
        # A token that matches nothing is not an error: the pass
        # simply finds nothing due for that token.
        cmd = self.write_send_cmd()
        result = self.dispatch(cmd, token="no-such-token")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["delivered"], [])


if __name__ == "__main__":
    unittest.main()
