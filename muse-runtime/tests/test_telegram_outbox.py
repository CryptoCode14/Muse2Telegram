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
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

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

    def dispatch(self, cmd, token=None, **kwargs):
        argv = ["--dispatch-outbox"] + ([token] if token else [])
        with self.send_env(cmd):
            return run_cli(self.path, argv, **kwargs)


class OutboxWriteTests(OutboxTestBase):
    """--outbox queues the reply and leaves the item claimed."""

    def test_write_queues_reply_and_leaves_item_claimed(self):
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
        # The item stays CLAIMED under the token: the dispatcher
        # reserves it at delivery time.
        item = self.item(1)
        self.assertEqual(item["status"], "claimed")
        self.assertIsNone(item["replied_at"])
        # The row carries the whole payload for the dispatcher.
        entry = self.outbox_entry(result["outbox_id"])
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["text"], "hi there")
        self.assertEqual(entry["chat_id"], OWNER)
        self.assertEqual(entry["reply_to_message_id"], 9001)
        self.assertEqual(entry["extra_update_ids"], [])
        self.assertEqual(entry["attempts"], 0)
        self.assertIsNone(entry["reply_message_id"])
        # Nothing was sent yet: the write never sends.
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
        self.assertEqual(result["covered_update_ids"], [2, 1])
        entry = self.outbox_entry(result["outbox_id"])
        self.assertEqual(entry["target_update_id"], 2)
        self.assertEqual(entry["reply_to_message_id"], 9002)
        self.assertEqual(entry["extra_update_ids"], [1])

    def test_write_callback_batch_is_standalone(self):
        payload = self.claim([
            callback_update(1, OWNER, "cmd:status"),
        ])
        cmd = self.write_send_cmd()
        result = self.write_outbox(
            payload["claim_token"], "status: on", cmd,
        )
        self.assertTrue(result["ok"], result)
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
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        first = self.write_outbox(payload["claim_token"], "hi", cmd)
        self.assertTrue(first["ok"], first)
        second = self.write_outbox(
            payload["claim_token"], "hi again", cmd,
        )
        self.assertEqual(second["ok"], False)
        self.assertEqual(second["stage"], "already-queued")
        self.assertEqual(second["target_update_id"], 1)
        self.assertEqual(len(self.outbox_entries()), 1)
        self.assertEqual(
            self.outbox_entries()[0]["text"], "hi",
        )

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
        payload = self.claim(updates or [
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        result = self.write_outbox(payload["claim_token"], text, cmd)
        self.assertTrue(result["ok"], result)
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
        # Two batches in two lanes; a token-scoped dispatch (the
        # agent's self-dispatch after an --outbox write) delivers
        # only its own token's entry, leaving the other's pending
        # for the poll (bare dispatch).
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
        result_a = self.write_outbox(token_a, "reply A", cmd)
        result_b = self.write_outbox(token_b, "reply B", cmd)
        self.assertTrue(result_a["ok"], result_a)
        self.assertTrue(result_b["ok"], result_b)
        report = self.dispatch(cmd, token=token_a)
        self.assertEqual(report["delivered"], [result_a["outbox_id"]])
        self.assertEqual(report["checked"], 1)
        self.assertEqual(
            self.outbox_entry(result_b["outbox_id"])["status"], "pending")
        self.assertEqual(len(self.text_sends()), 1)
        report = self.dispatch(cmd)
        self.assertEqual(report["delivered"], [result_b["outbox_id"]])
        self.assertEqual(len(self.text_sends()), 2)

    def test_agent_dispatch_then_poll_no_double_delivery(self):
        # The agent's self-dispatch after an --outbox write
        # delivers in the turn; a later poll pass (bare dispatch)
        # finds nothing due and never sends twice.
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        result = self.write_outbox(
            payload["claim_token"], "hi there", cmd)
        entry_id = result["outbox_id"]
        agent_pass = self.dispatch(cmd, token=payload["claim_token"])
        self.assertEqual(agent_pass["delivered"], [entry_id])
        poll_pass = self.dispatch(cmd)
        self.assertEqual(poll_pass["checked"], 0)
        self.assertEqual(poll_pass["delivered"], [])
        again = self.dispatch(cmd, token=payload["claim_token"])
        self.assertEqual(again["checked"], 0)
        self.assertEqual(again["delivered"], [])
        self.assertEqual(len(self.text_sends()), 1)
        self.assertEqual(
            self.outbox_entry(entry_id)["status"], "delivered")

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
        # The turn wrote the outbox entry and then (against the
        # prompt) also sent with --reply: the dispatcher must not
        # send the queued text a second time.
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        written = self.write_outbox(
            payload["claim_token"], "hi there", cmd,
        )
        self.assertTrue(written["ok"], written)
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
        written = self.write_outbox(
            payload["claim_token"], "hi there", cmd,
        )
        self.assertTrue(written["ok"], written)
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
        written = self.write_outbox(
            payload["claim_token"], "hi there", cmd,
        )
        self.assertTrue(written["ok"], written)
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


class OutboxRunOnceTests(OutboxTestBase):
    """The poll loop delivers a queued reply with no wake."""

    def test_run_once_dispatches_outbox_before_poll(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        written = self.write_outbox(
            payload["claim_token"], "hi there", cmd,
        )
        self.assertTrue(written["ok"], written)
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
        result = run_cli(self.path, ["--outbox-status"])
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["outbox_path"],
            str(self.path.with_suffix(".outbox.sqlite3")),
        )
        self.assertEqual(len(result["entries"]), 1)
        entry = result["entries"][0]
        self.assertEqual(entry["id"], written["outbox_id"])
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["text"], "hi there")

    def test_drop_marks_failed_and_blocks_delivery(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello",
                       message_id=9001),
        ])
        cmd = self.write_send_cmd()
        written = self.write_outbox(
            payload["claim_token"], "hi there", cmd,
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
