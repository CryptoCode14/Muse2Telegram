"""Offline tests for the Telegram adapter CLI (bridge_core.telegram).

The Bot API fetch is stubbed, so no network or credentials are used.
The tests drive the adapter through its real CLI modes -- the same
ones the wake hook and woken turns call -- and check the wake payload
shape, the in-flight rule, token-checked completion, reply recording,
and cross-poll dedupe.
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from bridge_core import queue as queue_core
from bridge_core import telegram
from bridge_core.store import JsonStateStore

OWNER = 111
GROUP = -777


def msg_update(uid, chat_id, chat_type, sender_id, text, username=None, is_bot=False, reply_to=None, message_id=None, date=None):
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
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return {
        "update_id": uid,
        "message": message,
    }


def voice_update(uid, chat_id, chat_type, sender_id, duration=12, file_id="voice-file-1", username=None, is_bot=False):
    user = {"id": sender_id, "is_bot": is_bot}
    if username:
        user["username"] = username
    message = {
        "from": user,
        "chat": {"id": chat_id, "type": chat_type},
        "voice": {"file_id": file_id, "duration": duration, "mime_type": "audio/ogg"},
        "date": 1700000000 + uid,
    }
    return {
        "update_id": uid,
        "message": message,
    }


class FakeFetch:
    """Returns one canned getUpdates response until replaced."""

    def __init__(self, updates=()):
        self.updates = list(updates)
        self.offsets = []

    def __call__(self, offset):
        self.offsets.append(offset)
        return {"ok": True, "result": list(self.updates)}


class FakeApi:
    """Canned Bot API for tests: getChatAdministrators returns the
    owner as creator (plus any extra admins); answerCallbackQuery
    succeeds. Anything else raises, so a test never makes a real
    network call."""

    def __init__(self, creator=OWNER, admins=None):
        self.creator = creator
        self.admins = admins if admins is not None else [creator]
        self.calls = []

    def __call__(self, method, params, timeout=20.0):
        self.calls.append((method, params))
        if method == "getChatAdministrators":
            members = []
            for user_id in self.admins:
                status = "creator" if user_id == self.creator else "administrator"
                members.append({"user": {"id": user_id}, "status": status})
            return {"ok": True, "result": members}
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
        # The bridge refuses to run without its bot username (no
        # default): the fixture name stands in for a real bot.
        telegram.BOT_USERNAME_ENV: "ExampleBot",
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


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_wake_payload_shape_and_legacy_fields(self):
        fetch = FakeFetch(
            [
                msg_update(1, OWNER, "private", OWNER, "hello dm", "boss"),
                msg_update(2, GROUP, "group", OWNER, "first in group", "boss"),
                msg_update(3, GROUP, "group", 222, "agent note", "kai_bot", True),
                msg_update(4, 999, "private", 999, "stranger, ignored"),
            ]
        )
        # P5 lane discipline: the oldest pending item is the owner's
        # DM, so the first wake carries that conversation lane alone.
        payload = run_cli(self.path, [], fetch=fetch)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(len(payload["items"]), 1)
        self.assertTrue(payload["claim_token"])
        # Legacy single-item fields mirror the first item.
        self.assertEqual(payload["update_id"], 1)
        self.assertEqual(payload["text"], "hello dm")
        for key in ("chat_id", "chat_type", "sender_name", "is_bot"):
            self.assertEqual(payload[key], payload["items"][0][key])
        # DM items carry no conversation context.
        self.assertNotIn("context", payload["items"][0])
        run_cli(self.path, ["--done", "1", payload["claim_token"]])
        # The next wake takes the group lane: the owner's group item
        # alone. The bot message in the lane is closed as noise by
        # the v2 preclaim (a bot message on its own never wakes a
        # turn), but its text still rides along as context.
        payload = run_cli(self.path, [], fetch=FakeFetch())
        self.assertEqual(payload["count"], 1)
        self.assertEqual(
            [item["update_id"] for item in payload["items"]], [2]
        )
        for key in ("chat_id", "chat_type", "sender_name", "is_bot"):
            self.assertEqual(payload[key], payload["items"][0][key])
        self.assertIn("context", payload["items"][0])
        # No content is lost: the bot message's text is in the
        # wake's context, marked as a third-party bot message.
        texts = [e["text"] for e in payload["items"][0]["context"]]
        self.assertIn("first in group", texts)
        self.assertIn("agent note", texts)
        # Group adoption happened through the owner's group message.
        state = JsonStateStore(self.path).load()
        self.assertEqual(state["group_ids"], [GROUP])
        self.assertEqual(state["offset"], 5)

    def test_in_flight_batch_blocks_second_wake(self):
        fetch = FakeFetch([msg_update(1, OWNER, "private", OWNER, "one")])
        self.assertIsNotNone(run_cli(self.path, [], fetch=fetch))
        # Nothing new arrives and the first batch is still claimed:
        # the next poll must stay silent instead of re-waking the item.
        fetch.updates = [msg_update(2, OWNER, "private", OWNER, "two")]
        self.assertIsNone(run_cli(self.path, [], fetch=fetch))

    def test_token_checked_done_release_replied_flow(self):
        fetch = FakeFetch(
            [
                msg_update(1, OWNER, "private", OWNER, "one"),
                msg_update(2, OWNER, "private", OWNER, "two"),
            ]
        )
        payload = run_cli(self.path, [], fetch=fetch)
        token = payload["claim_token"]
        # Wrong token: no-op, item stays claimed.
        self.assertEqual(
            run_cli(self.path, ["--done", "1", "bogus"]), {"ok": False}
        )
        state = JsonStateStore(self.path).load()
        item1 = next(i for i in state["queue"] if i["external_id"] == "1")
        self.assertEqual(item1["status"], "claimed")
        # Send lifecycle: --sending before the send (token-checked),
        # then --replied, then --done from the sending state.
        self.assertEqual(
            run_cli(self.path, ["--sending", "1", "bogus"]), {"ok": False}
        )
        self.assertEqual(
            run_cli(self.path, ["--sending", "1", token]), {"ok": True}
        )
        self.assertEqual(
            run_cli(self.path, ["--replied", "1", "555", token]), {"ok": True}
        )
        self.assertEqual(run_cli(self.path, ["--done", "1", token]), {"ok": True})
        # Release the second item back to pending; it can be claimed again.
        self.assertEqual(
            run_cli(self.path, ["--release", "2", token]), {"ok": True}
        )
        fetch.updates = []
        nxt = run_cli(self.path, [], fetch=fetch)
        self.assertEqual(nxt["count"], 1)
        self.assertEqual(nxt["update_id"], 2)
        state = JsonStateStore(self.path).load()
        item1 = next(i for i in state["queue"] if i["external_id"] == "1")
        self.assertEqual(item1["status"], "done")
        self.assertEqual(item1["reply_message_id"], "555")
        self.assertIsNotNone(item1["replied_at"])
        self.assertIsNotNone(item1["received_at"])

    def test_p1_p2_p4_send_lifecycle_via_cli(self):
        """P1/P2/P4: the CLI enforces the send lifecycle —
        token-checked --sending before the send, --replied only with
        the batch token, no --release of a replied item, and
        --send-failed as the only way back from a failed send."""
        fetch = FakeFetch(
            [
                msg_update(1, OWNER, "private", OWNER, "one"),
                msg_update(2, OWNER, "private", OWNER, "two"),
            ]
        )
        payload = run_cli(self.path, [], fetch=fetch)
        token = payload["claim_token"]
        # P4: --replied without the batch token records nothing.
        self.assertEqual(
            run_cli(self.path, ["--replied", "1", "555"]), {"ok": False}
        )
        # P1 definitive-failure path: sending -> send-failed -> pending.
        self.assertEqual(
            run_cli(self.path, ["--sending", "2", token]), {"ok": True}
        )
        self.assertEqual(
            run_cli(self.path, ["--send-failed", "2", "bogus"]), {"ok": False}
        )
        self.assertEqual(
            run_cli(self.path, ["--send-failed", "2", token]), {"ok": True}
        )
        # P1 success path + P2: sending -> replied -> release refused
        # -> done.
        self.assertEqual(
            run_cli(self.path, ["--sending", "1", token]), {"ok": True}
        )
        self.assertEqual(
            run_cli(self.path, ["--replied", "1", "555", token]), {"ok": True}
        )
        self.assertEqual(
            run_cli(self.path, ["--release", "1", token]), {"ok": False}
        )
        self.assertEqual(run_cli(self.path, ["--done", "1", token]), {"ok": True})
        state = JsonStateStore(self.path).load()
        by_id = {i["external_id"]: i for i in state["queue"]}
        self.assertEqual(by_id["1"]["status"], "done")
        self.assertEqual(by_id["1"]["reply_message_id"], "555")
        self.assertIsNotNone(by_id["1"]["replied_at"])
        self.assertEqual(by_id["2"]["status"], "pending")

    def test_redelivery_is_deduped_across_polls(self):
        fetch = FakeFetch([msg_update(1, OWNER, "private", OWNER, "same")])
        payload = run_cli(self.path, [], fetch=fetch)
        run_cli(self.path, ["--done", "1", payload["claim_token"]])
        # Telegram redelivers update 1 (e.g. after an offset hiccup):
        # it must not queue or wake a second time.
        self.assertIsNone(run_cli(self.path, [], fetch=fetch))
        state = JsonStateStore(self.path).load()
        self.assertEqual(len(state["queue"]), 1)


class SendFenceTests(unittest.TestCase):
    """--send-fenced: the bridge-side send fence.

    A fake send executable written to a tmp dir stands in for the
    deployment's send command (TELEGRAM_BRIDGE_SEND_CMD). It records
    every invocation (argv + stdin) to a calls file, so tests can
    prove the fence never invokes it when it refuses, and invokes it
    exactly once on a real send. No real paths, ids or credentials
    are used.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "state.json"
        self.calls = self.dir / "send-calls.log"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def write_send_cmd(self, body):
        script = self.dir / "fake-send.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"with open({str(self.calls)!r}, 'a', encoding='utf-8') as fh:\n"
            "    fh.write('argv=' + repr(sys.argv[1:]) + '\\n')\n"
            "    fh.write('stdin=' + sys.stdin.read() + '\\n')\n"
            + body,
            encoding="utf-8",
        )
        script.chmod(0o755)
        return str(script)

    def send_env(self, cmd):
        return mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd})

    def claim_dm(self, uid=1, text="please reply"):
        fetch = FakeFetch([msg_update(uid, OWNER, "private", OWNER, text)])
        payload = run_cli(self.path, [], fetch=fetch)
        return payload["claim_token"]

    def item(self, external_id):
        state = JsonStateStore(self.path).load()
        return next(
            i for i in state["queue"] if i["external_id"] == str(external_id)
        )

    def call_count(self):
        if not self.calls.exists():
            return 0
        return self.calls.read_text(encoding="utf-8").count("argv=")

    def fenced(self, cmd, target, token, stdin_text="the reply text"):
        with self.send_env(cmd):
            return run_cli(
                self.path,
                ["--send-fenced", str(target), token, str(OWNER)],
                stdin_text=stdin_text,
            )

    def test_fence_refuses_without_any_send_invocation(self):
        """Fence refusals (wrong token, unknown item, never
        claimed) print stage "fence" and the send command is
        never invoked. An already-replied item instead prints
        stage "stale-token" with its disposition (the reply
        already went out, so there is nothing to re-send)."""
        token = self.claim_dm()
        cmd = self.write_send_cmd('print("4242")\n')
        self.assertEqual(
            self.fenced(cmd, 1, "bogus"),
            {"ok": False, "stage": "fence"},
        )
        self.assertEqual(
            self.fenced(cmd, 99, token),
            {"ok": False, "stage": "fence"},
        )
        self.assertEqual(self.call_count(), 0)
        self.assertEqual(self.item(1)["status"], "claimed")
        # Already replied: stale-token with disposition (the reply
        # already went out, so there is nothing to re-send).
        self.assertEqual(
            run_cli(self.path, ["--replied", "1", "555", token]),
            {"ok": True},
        )
        self.assertEqual(
            self.fenced(cmd, 1, token),
            {
                "ok": False,
                "stage": "stale-token",
                "item_status": "claimed",
                "replied": True,
            },
        )
        self.assertEqual(self.call_count(), 0)
        # Pending but never claimed: refused as well.
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id="50",
                chat_id=OWNER, chat_type="private", text="queued",
            )
        self.assertEqual(
            self.fenced(cmd, 50, token),
            {"ok": False, "stage": "fence"},
        )
        self.assertEqual(self.call_count(), 0)

    def test_fence_happy_path_sends_once_then_replied_and_done(self):
        """The fence marks sending and persists BEFORE the send runs
        once with argv [chat id] and the text on stdin; the last
        stdout line is the message id. The item stays in sending
        until the caller records --replied and closes with --done."""
        token = self.claim_dm()
        cmd = self.write_send_cmd('print("send log line")\nprint("4242")\n')
        result = self.fenced(cmd, 1, token)
        self.assertEqual(result, {"ok": True, "message_id": "4242"})
        self.assertEqual(self.call_count(), 1)
        calls = self.calls.read_text(encoding="utf-8")
        self.assertIn(f"argv=['{OWNER}']", calls)
        self.assertIn("stdin=the reply text", calls)
        item = self.item(1)
        self.assertEqual(item["status"], "sending")
        self.assertIsNotNone(item["sending_at"])
        # Caller closes the loop exactly as in the turn protocol.
        self.assertEqual(
            run_cli(self.path, ["--replied", "1", "4242", token]),
            {"ok": True},
        )
        self.assertEqual(
            run_cli(self.path, ["--done", "1", token]), {"ok": True}
        )
        item = self.item(1)
        self.assertEqual(item["status"], "done")
        self.assertEqual(item["reply_message_id"], "4242")
        self.assertIsNotNone(item["replied_at"])

    def assert_unknown_reservation(self, cmd, token):
        """Unknown delivery holds its token and lane beyond TTL, without retry."""
        item = self.item(1)
        self.assertEqual(item["status"], "sending")
        self.assertEqual(item["claim_token"], token)
        self.assertIsNotNone(item["sending_at"])
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            item = next(i for i in state["queue"] if i["external_id"] == "1")
            item["claimed_at"] = 1.0
            item["sending_at"] = 1.0
        fetch = FakeFetch([msg_update(2, OWNER, "private", OWNER, "another")])
        self.assertIsNone(run_cli(self.path, [], fetch=fetch))
        self.assertEqual(self.item(1)["status"], "sending")
        self.assertEqual(self.item(1)["claim_token"], token)
        self.assertEqual(self.item(2)["status"], "pending")
        self.assertEqual(run_cli(self.path, ["--release", "1", token]), {"ok": False})
        self.assertEqual(run_cli(self.path, ["--done", "1", token, "cancelled"]), {"ok": False})
        self.assertEqual(self.fenced(cmd, 1, token), {"ok": False, "stage": "fence"})
        self.assertEqual(self.call_count(), 1)

    def test_fence_nonzero_exit_is_unknown(self):
        """An arbitrary nonzero exit does not establish non-delivery."""
        token = self.claim_dm()
        cmd = self.write_send_cmd("sys.exit(3)\n")
        result = self.fenced(cmd, 1, token)
        self.assertEqual(result, {"ok": False, "stage": "unknown"})
        self.assert_unknown_reservation(cmd, token)

    def accepted_then_failed(self, failure):
        token = self.claim_dm()
        accepted = self.dir / "fake-remote-acceptance"
        cmd = self.write_send_cmd(
            "from pathlib import Path\n"
            f"Path({str(accepted)!r}).write_text('accepted')\n" + failure
        )
        self.assertEqual(self.fenced(cmd, 1, token), {"ok": False, "stage": "unknown"})
        self.assertEqual(accepted.read_text(), "accepted")
        self.assert_unknown_reservation(cmd, token)

    def test_fence_accepted_then_crashed(self):
        self.accepted_then_failed("raise RuntimeError('synthetic post-send crash')\n")

    def test_fence_accepted_then_response_lost(self):
        self.accepted_then_failed("raise ConnectionError('synthetic lost response')\n")

    def test_fence_accepted_then_signal(self):
        self.accepted_then_failed("import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n")

    def test_fence_message_id_then_nonzero_is_unknown(self):
        self.accepted_then_failed("print('4242', flush=True)\nsys.exit(1)\n")

    def test_fence_accepted_then_response_missing(self):
        self.accepted_then_failed("")

    def test_fence_accepted_then_undecodable_response(self):
        self.accepted_then_failed("sys.stdout.buffer.write(b'\\xff')\n")

    def test_verified_no_send_requires_explicit_operator_transition(self):
        """No sender exit code authorizes retry, even a pre-send failure.

        Here the fixture verifiably stops before sending. Only the separate
        token-checked operator transition may make it retryable.
        """
        token = self.claim_dm()
        cmd = self.write_send_cmd("sys.exit(3)\n")
        self.assertEqual(self.fenced(cmd, 1, token), {"ok": False, "stage": "unknown"})
        self.assertEqual(run_cli(self.path, ["--send-failed", "1", "wrong"]), {"ok": False})
        self.assertEqual(self.item(1)["status"], "sending")
        self.assertEqual(run_cli(self.path, ["--send-failed", "1", token]), {"ok": True})
        item = self.item(1)
        self.assertEqual(item["status"], "pending")
        self.assertIsNone(item["claim_token"])
        self.assertIsNone(item["sending_at"])
        payload = run_cli(self.path, [], fetch=FakeFetch())
        self.assertEqual(payload["update_id"], 1)
        self.assertNotEqual(payload["claim_token"], token)
        self.assertEqual(self.call_count(), 1)

    def test_fence_ambiguous_outcome_leaves_sending_and_never_recycles(self):
        """Exit 0 without a message id is ambiguous: the item stays
        in sending (send-unknown), a later poll neither recycles it
        nor claims its lane, and the send is never retried."""
        token = self.claim_dm()
        cmd = self.write_send_cmd("")  # exits 0, prints nothing
        result = self.fenced(cmd, 1, token)
        self.assertEqual(result, {"ok": False, "stage": "unknown"})
        self.assertEqual(self.item(1)["status"], "sending")
        # Age the item far beyond the stale TTL.
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            item = next(
                i for i in state["queue"] if i["external_id"] == "1"
            )
            item["claimed_at"] = 1.0
            item["sending_at"] = 1.0
        fetch = FakeFetch([msg_update(2, OWNER, "private", OWNER, "another")])
        self.assertIsNone(run_cli(self.path, [], fetch=fetch))
        self.assertEqual(self.item(1)["status"], "sending")
        self.assertEqual(self.item(2)["status"], "pending")
        self.assertEqual(self.call_count(), 1)

    def test_fence_unstartable_send_command_is_unknown(self):
        """A send command that cannot be started at all is
        ambiguous, not definitive: the item stays in sending."""
        token = self.claim_dm()
        result = self.fenced(str(self.dir / "no-such-send-command"), 1, token)
        self.assertEqual(result, {"ok": False, "stage": "unknown"})
        self.assertEqual(self.item(1)["status"], "sending")

    def test_fence_send_timeout_is_unknown(self):
        """A send command that outlives the runner timeout is
        ambiguous: the send may have gone out, so the item stays in
        sending rather than returning to pending."""
        token = self.claim_dm()
        cmd = self.write_send_cmd("import time\ntime.sleep(30)\n")
        with mock.patch.object(telegram, "SEND_TIMEOUT_SECONDS", 0.5):
            result = self.fenced(cmd, 1, token)
        self.assertEqual(result, {"ok": False, "stage": "unknown"})
        self.assertEqual(self.item(1)["status"], "sending")

    def test_fence_missing_send_cmd_config_leaves_item_claimed(self):
        """Without TELEGRAM_BRIDGE_SEND_CMD the result is stage
        "config" and the fence never marks: the item stays claimed
        (not sending), so nothing about it changes."""
        token = self.claim_dm()
        with mock.patch.dict(os.environ):
            os.environ.pop(telegram.SEND_CMD_ENV, None)
            result = run_cli(
                self.path,
                ["--send-fenced", "1", token, str(OWNER)],
                stdin_text="the reply text",
            )
        self.assertEqual(result, {"ok": False, "stage": "config"})
        item = self.item(1)
        self.assertEqual(item["status"], "claimed")
        self.assertIsNone(item["sending_at"])

    def test_done_from_sending_requires_evidence_via_cli(self):
        """F2 at the CLI: a send-unknown item refuses --done with
        only a reason; with REASON + EVIDENCE it closes and stores
        done_reason and done_evidence separately."""
        fetch = FakeFetch([msg_update(1, OWNER, "private", OWNER, "one")])
        payload = run_cli(self.path, [], fetch=fetch)
        token = payload["claim_token"]
        self.assertEqual(
            run_cli(self.path, ["--sending", "1", token]), {"ok": True}
        )
        self.assertEqual(
            run_cli(self.path, ["--done", "1", token, "reason-only"]),
            {"ok": False},
        )
        self.assertEqual(self.item(1)["status"], "sending")
        self.assertEqual(
            run_cli(
                self.path,
                ["--done", "1", token, "reconciled",
                 "history: reply found as message 900"],
            ),
            {"ok": True},
        )
        item = self.item(1)
        self.assertEqual(item["status"], "done")
        self.assertEqual(item["done_reason"], "reconciled")
        self.assertEqual(
            item["done_evidence"], "history: reply found as message 900"
        )


class PurgeExpiredTests(unittest.TestCase):
    """--purge-expired (Issue #17): the operator form of the
    pending-expiry sweep — same class TTLs and safety lines as the
    claim-time sweep, runnable on demand, with a dry run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])
        now = time.time()
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id="g-old",
                chat_id=GROUP, chat_type="group", text="old group",
                now=now - 3600,
                item_class=queue_core.CLASS_CONVERSATION,
            )
            queue_core.enqueue(
                state, platform="telegram", external_id="g-mid",
                chat_id=GROUP, chat_type="group", text="mid group",
                now=now - 300,
                item_class=queue_core.CLASS_CONVERSATION,
            )
            queue_core.enqueue(
                state, platform="telegram", external_id="dm-old",
                chat_id=OWNER, chat_type="private", text="old dm",
                now=now - 86400,
                item_class=queue_core.CLASS_REQUEST,
            )

    def tearDown(self):
        self.tmp.cleanup()

    def item(self, external_id):
        state = JsonStateStore(self.path).load()
        return next(
            i for i in state["queue"] if i["external_id"] == external_id
        )

    def test_purge_dry_run_changes_nothing(self):
        result = run_cli(self.path, ["--purge-expired", "--dry-run"])
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["dry_run"], True)
        self.assertEqual(result["expired"], 1)
        self.assertEqual(result["external_ids"], ["g-old"])
        for external_id in ("g-old", "g-mid", "dm-old"):
            item = self.item(external_id)
            self.assertEqual(item["status"], "pending")
            self.assertNotIn("done_reason", item)

    def test_purge_expires_old_group_items_only(self):
        result = run_cli(self.path, ["--purge-expired"])
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["dry_run"], False)
        self.assertEqual(result["expired"], 1)
        old = self.item("g-old")
        self.assertEqual(old["status"], "done")
        self.assertEqual(old["done_reason"], "expired")
        # The mid-age group item is under the default TTL, and the
        # day-old owner DM never expires by default.
        self.assertEqual(self.item("g-mid")["status"], "pending")
        self.assertEqual(self.item("dm-old")["status"], "pending")

    def test_purge_older_than_overrides_group_ttl(self):
        result = run_cli(
            self.path, ["--purge-expired", "--older-than", "60"]
        )
        self.assertEqual(result["expired"], 2)
        self.assertEqual(
            set(result["external_ids"]), {"g-old", "g-mid"}
        )
        # Even an aggressive threshold never touches the owner DM:
        # --older-than overrides the group class only.
        self.assertEqual(self.item("dm-old")["status"], "pending")

    def test_purge_rejects_bad_arguments(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = telegram.main(
                ["--purge-expired", "--older-than", "soon"],
                fetch=FakeFetch(),
                path=self.path,
            )
        self.assertEqual(rc, 2)
        self.assertFalse(json.loads(out.getvalue())["ok"])
        self.assertEqual(self.item("g-old")["status"], "pending")

    def test_purge_rejects_non_finite_older_than_without_mutation(self):
        """Finding 5 regression: float() parses "nan"/"inf", but a
        NaN threshold would expire every eligible item and an
        infinite one none — both are input errors, rejected with
        the usage error before any state changes."""
        for bad in ("nan", "inf", "-inf"):
            with self.subTest(older_than=bad):
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = telegram.main(
                        ["--purge-expired", "--older-than", bad],
                        fetch=FakeFetch(),
                        path=self.path,
                    )
                self.assertEqual(rc, 2)
                self.assertFalse(json.loads(out.getvalue())["ok"])
                for external_id in ("g-old", "g-mid", "dm-old"):
                    item = self.item(external_id)
                    self.assertEqual(item["status"], "pending")
                    self.assertNotIn("done_reason", item)


class IntakeClassificationTests(unittest.TestCase):
    """Classification protects owner/addressed/unknown input at intake."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def classes(self):
        state = JsonStateStore(self.path).load()
        return {
            i["external_id"]: i["item_class"] for i in state["queue"]
        }

    def test_intake_classification_rules(self):
        fetch = FakeFetch(
            [
                # The owner's group message adopts the group; the
                # sender being the owner makes it a request.
                msg_update(1, GROUP, "group", OWNER, "opening the room"),
                # A bot's unfamiliar statement stays protected.
                msg_update(2, GROUP, "group", 222, "plain agent chatter",
                           "kai_bot", True),
                # A mention of the bot (any case) is a request.
                msg_update(3, GROUP, "group", 333, "hey @ExampleBot check this"),
                # A bot command is a request.
                msg_update(4, GROUP, "group", 333, "/status please"),
                # A bot addressing this bot is a request, exactly
                # like a human addressing it.
                msg_update(5, GROUP, "group", 222, "@ExampleBot take this",
                           "kai_bot", True),
                # The owner's DM is a request.
                msg_update(6, OWNER, "private", OWNER, "a dm ask"),
            ]
        )
        run_cli(self.path, [], fetch=fetch)
        self.assertEqual(
            self.classes(),
            {
                "1": queue_core.CLASS_REQUEST,
                "2": queue_core.CLASS_REQUEST,
                "3": queue_core.CLASS_REQUEST,
                "4": queue_core.CLASS_REQUEST,
                "5": queue_core.CLASS_REQUEST,
                "6": queue_core.CLASS_REQUEST,
            },
        )


class VoiceIngestTests(unittest.TestCase):
    """A voice message is downloaded and queued, never silently dropped."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_voice_dm_is_queued_with_marker(self):
        fake_audio = Path(self.tmp.name) / "note.ogg"
        with mock.patch.object(
            telegram, "download_voice_file", return_value=fake_audio
        ) as dl:
            fetch = FakeFetch([voice_update(1, OWNER, "private", OWNER, duration=12)])
            run_cli(self.path, [], fetch=fetch)
        dl.assert_called_once()
        state = JsonStateStore(self.path).load()
        self.assertEqual(len(state["queue"]), 1)
        item = state["queue"][0]
        self.assertEqual(item["item_class"], queue_core.CLASS_REQUEST)
        self.assertTrue(item["text"].startswith("[voice message, 12s]"))
        self.assertIn(str(fake_audio), item["text"])

    def test_voice_download_failure_is_queued_not_dropped(self):
        with mock.patch.object(telegram, "download_voice_file", return_value=None):
            fetch = FakeFetch([voice_update(1, OWNER, "private", OWNER, duration=8)])
            run_cli(self.path, [], fetch=fetch)
        state = JsonStateStore(self.path).load()
        self.assertEqual(len(state["queue"]), 1)
        item = state["queue"][0]
        self.assertEqual(item["item_class"], queue_core.CLASS_REQUEST)
        self.assertIn("could not download", item["text"])


class IntakeProtectionClassificationTests(unittest.TestCase):
    """Only positive greetings in an idle lane may expire; unknown work survives."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        # These tests call telegram.ingest directly, which ensures
        # adapter state and now needs the bot username (no default).
        self._env = mock.patch.dict(os.environ, {
            telegram.BOT_USERNAME_ENV: "ExampleBot"})
        self._env.start()
        self.addCleanup(self._env.stop)
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def classes(self):
        state = JsonStateStore(self.path).load()
        return {
            i["external_id"]: i["item_class"] for i in state["queue"]
        }

    def item(self, external_id):
        state = JsonStateStore(self.path).load()
        return next(
            i for i in state["queue"] if i["external_id"] == str(external_id)
        )

    def test_protection_signals_and_unknown_talk_survive_purge(self):
        # Poll 1 claims the owner's opening message, so poll 2's
        # arrivals stay pending for the purge below.
        first = run_cli(
            self.path, [],
            fetch=FakeFetch(
                [msg_update(1, GROUP, "group", OWNER, "opening the room")]
            ),
        )
        self.assertIsNotNone(first)
        arrivals = [
            # A non-owner's reply to a bot's message: a follow-up
            # directed at earlier work — protected by the reply
            # alone, though its text is plain.
            msg_update(2, GROUP, "group", 222, "了解、その通りだね",
                       "kai_bot", True, reply_to=900),
            # A mention of ANOTHER participant (not this bot).
            msg_update(3, GROUP, "group", 333,
                       "@chiisamabot その案でいいと思う"),
            # A standalone correction, mentioning nobody.
            msg_update(4, GROUP, "group", 333, "さっきの件、やっぱりなしで"),
            # A standalone cancellation from a bot.
            msg_update(5, GROUP, "group", 222, "その作業は中止して",
                       "kai_bot", True),
            # A question-form follow-up to ongoing work.
            msg_update(6, GROUP, "group", 333, "それ、どうなった？"),
            # A request marker ("tell me").
            msg_update(7, GROUP, "group", 222, "あとで結果を教えて",
                       "kai_bot", True),
            # A plain statement is not evidence of ordinary talk.
            msg_update(8, GROUP, "group", 222, "今日のメモをここに置いておく",
                       "kai_bot", True),
        ]
        self.assertIsNone(
            run_cli(self.path, [], fetch=FakeFetch(arrivals))
        )
        self.assertEqual(
            self.classes(),
            {
                "1": queue_core.CLASS_REQUEST,
                "2": queue_core.CLASS_REQUEST,
                "3": queue_core.CLASS_REQUEST,
                "4": queue_core.CLASS_REQUEST,
                "5": queue_core.CLASS_REQUEST,
                "6": queue_core.CLASS_REQUEST,
                "7": queue_core.CLASS_REQUEST,
                "8": queue_core.CLASS_REQUEST,
            },
        )
        # A legacy record with no classification is protected too.
        now = time.time()
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id="legacy-1",
                chat_id=GROUP, chat_type="group", text="legacy record",
                now=now - 7200,
            )
            # Age every pending arrival well past the group TTL.
            for uid in range(2, 9):
                queue_core.find_item(state, "telegram", uid)[
                    "received_at"
                ] -= 7200
        self.assertEqual(
            run_cli(self.path, ["--done", "1", first["claim_token"]]),
            {"ok": True},
        )
        result = run_cli(self.path, ["--purge-expired"])
        self.assertEqual(result["expired"], 0)
        self.assertEqual(result["external_ids"], [])
        # The expiry purge itself expired nothing: the protection
        # holds. The human items and the legacy record stay pending.
        for uid in (4, 6, "legacy-1"):
            self.assertEqual(self.item(uid)["status"], "pending")
        # v2: the bot messages and the mention-of-another-bot are
        # closed by the preclaim filter as noise (not by expiry) —
        # their text stays in the group buffer as context.
        for uid in (2, 3, 5, 7, 8):
            self.assertEqual(self.item(uid)["status"], "done")
            self.assertEqual(self.item(uid)["done_reason"], "not-for-me")

    def test_exact_greetings_only_and_protection_overrides(self):
        for text in ("hello", "  HELLO  ", "こんにちは", "こんばんは"):
            with self.subTest(text=text):
                self.assertEqual(telegram.classify_message(
                    text, sender_is_owner=False), queue_core.CLASS_CONVERSATION)
                for signal in ({"sender_is_owner": True}, {"is_reply": True},
                               {"has_active_work": True}):
                    options = {"sender_is_owner": False, **signal}
                    self.assertEqual(telegram.classify_message(text, **options),
                                     queue_core.CLASS_REQUEST)
        # Unseen expressions and greeting prefixes cannot inherit eligibility.
        for text in ("その処理は保留", "対象をBに変更", "Please stop the job",
                     "Switch to the second target", "Wait for the green light",
                     "それは後回し", "Proceed with option C", "OK", "thanks",
                     "hello; pause the task", "hello\nuse B", "hi?", "", "🛑"):
            with self.subTest(text=text):
                self.assertEqual(telegram.classify_message(
                    text, sender_is_owner=False), queue_core.CLASS_REQUEST)

    def test_unknown_intake_survives_purge_and_claim_sweep(self):
        texts = ["その処理は保留", "対象をBに変更", "Please stop the job",
                 "Switch to the second target", "Wait for the green light"]
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            telegram._group_record(state, GROUP, create=True)
            telegram.ingest(state, {"ok": True, "result": [
                msg_update(uid, GROUP, "group", 222, text)
                for uid, text in enumerate(texts, 20)
            ]})
            for item in state["queue"]:
                item["received_at"] -= 7200
        self.assertTrue(all(c == queue_core.CLASS_REQUEST
                            for c in self.classes().values()))
        self.assertEqual(run_cli(self.path, ["--purge-expired"])["expired"], 0)
        payload = run_cli(self.path, [])
        self.assertEqual([item["text"] for item in payload["items"]], texts)
        self.assertTrue(all(item["status"] == "claimed"
                            for item in store.load()["queue"]))

    def test_idle_greeting_expires_but_active_work_greeting_survives(self):
        store = JsonStateStore(self.path)
        # v2 receipt dedupe: each subTest must use a distinct update
        # id. The loop clears the queue but the ingest receipt for
        # an update id persists 24 h, so re-ingesting the same id
        # is (correctly) skipped as a duplicate delivery.
        for index, work_class in enumerate((queue_core.CLASS_REQUEST, None,
                           queue_core.CLASS_CONVERSATION)):
            uid = 91 + 100 * index
            with self.subTest(work_class=work_class):
                with store.transaction() as state:
                    state["queue"] = []
                    telegram._group_record(state, GROUP, create=True)
                    queue_core.enqueue(
                        state, platform="telegram", external_id=90,
                        chat_id=GROUP, chat_type="group", text="work",
                        item_class=work_class)
                    # Even previously claimed ordinary talk is active work.
                    if work_class == queue_core.CLASS_CONVERSATION:
                        queue_core.claim_batch(state, platform="telegram")
                    telegram.ingest(state, {"ok": True, "result": [
                        msg_update(uid, GROUP, "group", 222, "hello")
                    ]})
                    queue_core.find_item(state, "telegram", uid)["received_at"] -= 7200
                self.assertEqual(self.item(uid)["item_class"], queue_core.CLASS_REQUEST)
                self.assertEqual(run_cli(self.path, ["--purge-expired"])["expired"], 0)
        with store.transaction() as state:
            for item in state["queue"]:
                item["status"] = queue_core.DONE
            # Work in a different chat does not block idle-room greetings.
            queue_core.enqueue(state, platform="telegram", external_id=92,
                               chat_id=OWNER, text="other lane work")
            telegram.ingest(state, {"ok": True, "result": [
                msg_update(93, GROUP, "group", 222, "hello")
            ]})
            queue_core.find_item(state, "telegram", 93)["received_at"] -= 7200
        self.assertEqual(self.item(93)["item_class"], queue_core.CLASS_CONVERSATION)
        self.assertEqual(run_cli(self.path, ["--purge-expired"])["external_ids"], ["93"])


class ExpiredContextTests(unittest.TestCase):
    """Finding 4 regression: group buffer entries carry their
    queue record's external_id, and an expired record's text is
    excluded from the actionable context of later wakes — expired
    work cannot re-enter through a fresh item's context."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        # This test calls telegram.ingest directly, which ensures
        # adapter state and now needs the bot username (no default).
        self._env = mock.patch.dict(os.environ, {
            telegram.BOT_USERNAME_ENV: "ExampleBot"})
        self._env.start()
        self.addCleanup(self._env.stop)
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def item(self, external_id):
        state = JsonStateStore(self.path).load()
        return next(
            i for i in state["queue"] if i["external_id"] == str(external_id)
        )

    def test_expired_item_is_absent_from_later_wake_context(self):
        # Finish the opener, then ingest ordinary talk without claiming it.
        first = run_cli(
            self.path, [],
            fetch=FakeFetch(
                [msg_update(10, GROUP, "group", OWNER, "room open")]
            ),
        )
        self.assertIsNotNone(first)
        run_cli(self.path, ["--done", "10", first["claim_token"]])
        with JsonStateStore(self.path).transaction() as state:
            telegram.ingest(state, {"ok": True, "result": [
                msg_update(11, GROUP, "group", 222, "こんにちは", "kai_bot", True),
                msg_update(12, GROUP, "group", 222, "こんばんは", "kai_bot", True),
            ]})
        # Buffer entries are tied to their queue records.
        state = JsonStateStore(self.path).load()
        by_text = {e["text"]: e for e in state["groups"][str(GROUP)]["buffer"]}
        self.assertEqual(by_text["こんにちは"]["external_id"], "11")
        self.assertEqual(by_text["こんばんは"]["external_id"], "12")
        # Age only the first arrival, then purge: it expires; the
        # fresh one stays pending.
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            queue_core.find_item(state, "telegram", 11)[
                "received_at"
            ] -= 7200
        result = run_cli(self.path, ["--purge-expired"])
        self.assertEqual(result["expired"], 1)
        self.assertEqual(result["external_ids"], ["11"])
        self.assertEqual(self.item(11)["status"], "done")
        self.assertEqual(self.item(11)["done_reason"], "expired")
        self.assertEqual(self.item(12)["status"], "pending")
        # A fresh arrival's wake keeps live context and drops expired talk.
        # v2: the live greeting is itself a bot message, so the
        # preclaim closes it as noise and it is not a wake item —
        # but its text still rides in the wake's context, while the
        # expired greeting is gone.
        payload = run_cli(
            self.path, [],
            fetch=FakeFetch(
                [msg_update(13, GROUP, "group", OWNER, "fresh question")]
            ),
        )
        self.assertEqual(payload["count"], 1)
        texts = [entry["text"] for entry in payload["items"][0]["context"]]
        self.assertIn("room open", texts)
        self.assertIn("こんばんは", texts)
        self.assertIn("fresh question", texts)
        self.assertNotIn("こんにちは", texts)


class NumericInputValidationTests(unittest.TestCase):
    """Finding 5 regression at the adapter layer: non-finite TTL /
    max-reply-age environment values fall back to the documented
    defaults before they can reach any mutation, while a finite
    negative keeps the disable convention."""

    def test_env_ttls_reject_non_finite_values(self):
        for bad in ("nan", "inf", "-inf"):
            with self.subTest(value=bad):
                with mock.patch.dict(
                    os.environ, {telegram.GROUP_PENDING_TTL_ENV: bad}
                ):
                    group_ttl, private_ttl = telegram._expiry_ttls()
                self.assertEqual(
                    group_ttl, queue_core.DEFAULT_GROUP_PENDING_TTL
                )
                self.assertIsNone(private_ttl)
                with mock.patch.dict(
                    os.environ, {telegram.MAX_REPLY_AGE_ENV: bad}
                ):
                    self.assertEqual(
                        telegram._max_reply_age(),
                        telegram.DEFAULT_MAX_REPLY_AGE,
                    )

    def test_env_ttls_keep_finite_and_negative_conventions(self):
        with mock.patch.dict(
            os.environ, {telegram.GROUP_PENDING_TTL_ENV: "120"}
        ):
            self.assertEqual(telegram._expiry_ttls()[0], 120.0)
        with mock.patch.dict(
            os.environ, {telegram.GROUP_PENDING_TTL_ENV: "-5"}
        ):
            self.assertIsNone(telegram._expiry_ttls()[0])
        with mock.patch.dict(
            os.environ, {telegram.PRIVATE_PENDING_TTL_ENV: "nan"}
        ):
            self.assertIsNone(telegram._expiry_ttls()[1])


class PayloadIdentityTests(unittest.TestCase):
    """Fix 2/4: the wake payload carries sender_id and is_owner, and
    other bots' messages are marked as third-party content in context."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_payload_carries_sender_id_and_is_owner(self):
        fetch = FakeFetch(
            [
                msg_update(1, GROUP, "group", OWNER, "owner speaks", "boss"),
                msg_update(2, GROUP, "group", 222, "stranger speaks"),
            ]
        )
        payload = run_cli(self.path, [], fetch=fetch)
        by_id = {item["update_id"]: item for item in payload["items"]}
        self.assertEqual(by_id[1]["sender_id"], OWNER)
        self.assertTrue(by_id[1]["is_owner"])
        self.assertEqual(by_id[2]["sender_id"], 222)
        self.assertFalse(by_id[2]["is_owner"])
        # Legacy top-level fields mirror the first item, including identity.
        self.assertEqual(payload["sender_id"], OWNER)
        self.assertTrue(payload["is_owner"])

    def test_bot_context_entries_are_marked_third_party(self):
        fetch = FakeFetch(
            [
                msg_update(1, GROUP, "group", OWNER, "room open", "boss"),
                msg_update(2, GROUP, "group", 222, "bot chatter", "kai_bot", True),
                msg_update(3, GROUP, "group", 333, "human chatter"),
            ]
        )
        payload = run_cli(self.path, [], fetch=fetch)
        context = payload["items"][0]["context"]
        by_text = {e["text"]: e for e in context}
        self.assertEqual(
            by_text["bot chatter"].get("note"), "third-party bot message"
        )
        self.assertNotIn("note", by_text["human chatter"])
        self.assertNotIn("note", by_text["room open"])


class VoiceCapTests(unittest.TestCase):
    """Fix 8: long voice notes are not downloaded; large downloads are capped."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_long_voice_note_is_not_downloaded(self):
        with mock.patch.object(
            telegram, "download_voice_file"
        ) as dl:
            fetch = FakeFetch(
                [voice_update(1, OWNER, "private", OWNER, duration=301)]
            )
            run_cli(self.path, [], fetch=fetch)
        dl.assert_not_called()
        state = JsonStateStore(self.path).load()
        item = state["queue"][0]
        self.assertIn("longer than 5 minutes", item["text"])
        self.assertIn("shorter voice note", item["text"])

    def test_voice_note_at_the_cap_is_downloaded(self):
        fake_audio = Path(self.tmp.name) / "note.ogg"
        with mock.patch.object(
            telegram, "download_voice_file", return_value=fake_audio
        ) as dl:
            fetch = FakeFetch(
                [voice_update(1, OWNER, "private", OWNER, duration=300)]
            )
            run_cli(self.path, [], fetch=fetch)
        dl.assert_called_once()


class FakeVoiceResponse:
    """A fake urllib response that yields a fixed number of bytes."""

    def __init__(self, total_bytes, chunk=64 * 1024):
        self.remaining = total_bytes
        self.chunk = chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n=-1):
        if self.remaining <= 0:
            return b""
        size = min(n if n > 0 else self.remaining, self.remaining)
        self.remaining -= size
        return b"x" * size


class VoiceDownloadTests(unittest.TestCase):
    """Fix 7/8/10/14: file_path validation, NoRedirect, streaming size
    cap, 0600 file mode, 0700 voice dir, voice_dir failure handling."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.voice = self.dir / "voice"

    def tearDown(self):
        self.tmp.cleanup()

    def _download(self, total_bytes, file_path="voice/file_1.oga"):
        with mock.patch.object(
            telegram,
            "api",
            return_value={"ok": True, "result": {"file_path": file_path}},
        ):
            with mock.patch.object(
                telegram,
                "_urlopen_no_redirect",
                return_value=FakeVoiceResponse(total_bytes),
            ):
                with mock.patch.dict(
                    os.environ, {telegram.VOICE_DIR_ENV: str(self.voice)}
                ):
                    return telegram.download_voice_file("voice-file-1", 99)

    def test_download_refuses_oversize_file(self):
        result = self._download(telegram.MAX_VOICE_BYTES + 1)
        self.assertIsNone(result)
        self.assertFalse((self.voice / "99.oga").exists())

    def test_download_writes_file_mode_0600_and_dir_0700(self):
        result = self._download(100)
        self.assertIsNotNone(result)
        self.assertEqual(result, self.voice / "99.oga")
        self.assertEqual(oct(result.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(self.voice.stat().st_mode & 0o777), "0o700")

    def test_download_rejects_bad_file_path(self):
        for bad in (
            "../etc/passwd",
            "voice/../../etc/passwd",
            "/abs/path.oga",
            "voice/file name.oga",
            "voice/file.oga?x=1",
            "voice/file|1.oga",
        ):
            with self.subTest(path=bad):
                self.assertIsNone(self._download(100, file_path=bad))

    def test_valid_file_paths(self):
        for good in (
            "voice/file_1.oga",
            "audio/file_2.mp3",
            "photos/file_3.jpg",
            "voice/file-4.ogg",
        ):
            with self.subTest(path=good):
                self.assertTrue(telegram._valid_file_path(good))

    def test_invalid_file_paths(self):
        for bad in (
            "../etc/passwd",
            "voice/../../etc/passwd",
            "/abs/path.oga",
            "voice/file name.oga",
            "voice/file.oga?x=1",
            "",
            None,
            "voice/file|1.oga",
            "..",
            "voice/..",
        ):
            with self.subTest(path=bad):
                self.assertFalse(telegram._valid_file_path(bad))

    def test_voice_dir_returns_none_on_mkdir_failure(self):
        with mock.patch.object(
            Path, "mkdir", side_effect=OSError("disk full")
        ):
            self.assertIsNone(telegram.voice_dir())

    def test_voice_dir_creates_with_mode_0700(self):
        with mock.patch.dict(
            os.environ, {telegram.VOICE_DIR_ENV: str(self.voice)}
        ):
            result = telegram.voice_dir()
        self.assertEqual(result, self.voice)
        self.assertEqual(oct(self.voice.stat().st_mode & 0o777), "0o700")

    def test_prune_removes_old_voice_files_and_keeps_recent(self):
        self.voice.mkdir()
        old = self.voice / "old.oga"
        old.write_bytes(b"x")
        recent = self.voice / "recent.oga"
        recent.write_bytes(b"x")
        old_time = time.time() - (telegram.VOICE_RETENTION_SECONDS + 100)
        os.utime(old, (old_time, old_time))
        with mock.patch.dict(
            os.environ, {telegram.VOICE_DIR_ENV: str(self.voice)}
        ):
            removed = telegram.prune_old_voice_files()
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertEqual(oct(self.voice.stat().st_mode & 0o777), "0o700")

    def test_no_redirect_handler_refuses_redirect(self):
        handler = telegram.NoRedirect()
        with self.assertRaises(RuntimeError):
            handler.redirect_request("req", "fp", "code", "msg", "headers", "newurl")

    def test_urlopen_no_redirect_uses_no_redirect_handler(self):
        opener = mock.MagicMock()
        with mock.patch.object(
            urllib.request, "build_opener", return_value=opener
        ) as build:
            telegram._urlopen_no_redirect(mock.MagicMock(), 5)
        args, _ = build.call_args
        self.assertTrue(
            any(isinstance(h, telegram.NoRedirect) for h in args)
        )
        opener.open.assert_called_once()


class ThrottleDelayTests(unittest.TestCase):
    """Round 2 item 1: rate limiting is a DELAY, never a drop.

    A sender over 5 messages in 60s, or a chat over 60 wakes in an
    hour, gets a wake_after timestamp and stays pending; a later
    poll claims it once its time comes. Nothing is ever closed for
    throttling.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def adopt_group(self):
        run_cli(
            self.path, [],
            fetch=FakeFetch([msg_update(1, GROUP, "group", OWNER, "adopt")]),
        )
        run_cli(self.path, ["--done", "1", self._claim_token(1)])

    def _claim_token(self, external_id):
        state = JsonStateStore(self.path).load()
        item = next(i for i in state["queue"] if i["external_id"] == str(external_id))
        return item["claim_token"]

    def item(self, external_id):
        state = JsonStateStore(self.path).load()
        return next(i for i in state["queue"] if i["external_id"] == str(external_id))

    def test_sender_over_five_gets_delayed_not_dropped(self):
        self.adopt_group()
        # Six messages from one non-owner sender in one fetch.
        updates = [
            msg_update(2 + i, GROUP, "group", 333, f"flood {i}")
            for i in range(6)
        ]
        payload = run_cli(self.path, [], fetch=FakeFetch(updates))
        # The batch takes the first 5 group items (the lane cap);
        # the 6th is still pending, delayed, NOT dropped.
        self.assertEqual(payload["count"], 5)
        self.assertEqual(
            [i["update_id"] for i in payload["items"]], [2, 3, 4, 5, 6]
        )
        sixth = self.item(7)
        self.assertEqual(sixth["status"], "pending")
        self.assertIsNotNone(sixth.get("wake_after"))
        self.assertGreater(sixth["wake_after"], time.time())
        # Close the first batch; the delayed 6th is NOT claimed yet.
        for uid in (2, 3, 4, 5, 6):
            run_cli(self.path, ["--done", str(uid), payload["claim_token"]])
        self.assertIsNone(run_cli(self.path, [], fetch=FakeFetch()))
        self.assertEqual(self.item(7)["status"], "pending")
        # Simulate the 60s window sliding past: age the sender's
        # arrivals out of the window and the wake_after into the
        # past. Then the delayed item IS claimed.
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            for item in state["queue"]:
                if item.get("sender_id") == 333:
                    item["received_at"] = time.time() - 61
            item = next(i for i in state["queue"] if i["external_id"] == "7")
            item["wake_after"] = time.time() - 1
        payload = run_cli(self.path, [], fetch=FakeFetch())
        self.assertIsNotNone(payload)
        self.assertEqual(
            [i["update_id"] for i in payload["items"]], [7]
        )

    def test_owner_is_never_delayed(self):
        self.adopt_group()
        updates = [
            msg_update(2 + i, GROUP, "group", OWNER, f"owner {i}")
            for i in range(9)
        ]
        run_cli(self.path, [], fetch=FakeFetch(updates))
        state = JsonStateStore(self.path).load()
        for item in state["queue"]:
            self.assertIsNone(item.get("wake_after"))

    def test_chat_cap_delays_items(self):
        self.adopt_group()
        # Pre-seed 60 wakes for the chat in the last hour.
        now = time.time()
        rate_path = Path(self.path).parent / "rate.json"
        rate_path.write_text(json.dumps({
            "senders": {},
            "chats": {str(GROUP): [now - 10] * 60},
        }), encoding="utf-8")
        payload = run_cli(
            self.path, [],
            fetch=FakeFetch([msg_update(2, GROUP, "group", 333, "one more")]),
        )
        # The item is delayed (wake_after set) and stays pending;
        # nothing wakes for it on this poll or the next.
        self.assertIsNone(payload)
        item = self.item(2)
        self.assertEqual(item["status"], "pending")
        self.assertIsNotNone(item.get("wake_after"))
        self.assertGreater(item["wake_after"], now)
        self.assertIsNone(run_cli(self.path, [], fetch=FakeFetch()))
        # Simulate the hour window sliding past: age the recorded
        # wakes out of the window and the wake_after into the past.
        # Then the delayed item IS claimed.
        rate_path.write_text(json.dumps({
            "senders": {},
            "chats": {str(GROUP): [now - 3601] * 60},
        }), encoding="utf-8")
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            item = next(i for i in state["queue"] if i["external_id"] == "2")
            item["wake_after"] = time.time() - 1
        payload = run_cli(self.path, [], fetch=FakeFetch())
        self.assertIsNotNone(payload)
        self.assertEqual(
            [i["update_id"] for i in payload["items"]], [2]
        )


class LongPollTests(unittest.TestCase):
    """Round 2 long polling: fetch_updates holds the getUpdates call
    open so Telegram returns the moment a message arrives."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_fetch_updates_uses_long_poll_timeout_and_new_types(self):
        seen = {}

        def fake_api(method, params, timeout=20.0):
            seen["method"] = method
            seen["params"] = params
            seen["timeout"] = timeout
            return {"ok": True, "result": []}

        with mock.patch.object(telegram, "api", fake_api):
            telegram.fetch_updates(0)
        self.assertEqual(seen["method"], "getUpdates")
        self.assertEqual(seen["params"]["timeout"], 30)
        self.assertEqual(seen["params"]["offset"], 0)
        self.assertEqual(
            seen["params"]["allowed_updates"],
            ["message", "my_chat_member", "callback_query",
             "edited_message", "message_reaction"],
        )
        # HTTP timeout is the long-poll timeout plus a margin.
        self.assertEqual(seen["timeout"], 45.0)

    def test_fetch_updates_respects_env_timeout(self):
        seen = {}

        def fake_api(method, params, timeout=20.0):
            seen["params"] = params
            seen["timeout"] = timeout
            return {"ok": True, "result": []}

        with mock.patch.dict(os.environ, {"TELEGRAM_BRIDGE_LONG_POLL_SECONDS": "45"}):
            with mock.patch.object(telegram, "api", fake_api):
                telegram.fetch_updates(0)
        self.assertEqual(seen["params"]["timeout"], 45)
        self.assertEqual(seen["timeout"], 60.0)

    def test_fetch_updates_clamps_timeout_to_fifty(self):
        seen = {}

        def fake_api(method, params, timeout=20.0):
            seen["params"] = params
            return {"ok": True, "result": []}

        with mock.patch.dict(os.environ, {"TELEGRAM_BRIDGE_LONG_POLL_SECONDS": "100"}):
            with mock.patch.object(telegram, "api", fake_api):
                telegram.fetch_updates(0)
        self.assertEqual(seen["params"]["timeout"], 50)

    def test_run_once_returns_none_when_poll_lock_held(self):
        import fcntl

        # run_cli points the poll lock at this temp file (see
        # POLL_LOCK_PATH_ENV): hold it, and the poll bows out.
        lock_path = Path(self.path).parent / "poll.lock"
        fetch = FakeFetch([msg_update(1, OWNER, "private", OWNER, "hi")])
        with open(lock_path, "w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIsNone(run_cli(self.path, [], fetch=fetch))
        # Nothing was fetched or ingested while the lock was held.
        state = JsonStateStore(self.path).load()
        self.assertEqual(state["offset"], 0)
        self.assertEqual(state["queue"], [])


class HelpTests(unittest.TestCase):
    """--help prints usage and exits without polling: a probe must
    never become a second getUpdates long-poll (Telegram answers
    the race with HTTP 409). An unknown flag is refused the same
    way instead of silently falling through to the poller."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, argv):
        fetch = mock.Mock(side_effect=AssertionError("must not poll"))
        out = io.StringIO()
        with redirect_stdout(out):
            rc = telegram.main(argv, fetch=fetch, path=self.path)
        fetch.assert_not_called()
        return rc, out.getvalue()

    def test_help_prints_usage_and_exits_zero(self):
        rc, text = self.run_main(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("--heartbeat", text)
        self.assertIn("--watchdog", text)

    def test_short_help_flag(self):
        rc, text = self.run_main(["-h"])
        self.assertEqual(rc, 0)
        self.assertIn("--heartbeat", text)

    def test_unknown_flag_refused_without_polling(self):
        rc, text = self.run_main(["--bogus"])
        self.assertEqual(rc, 2)
        self.assertFalse(json.loads(text)["ok"])


class MessageIdThreadingTests(unittest.TestCase):
    """Round 2 items 5 and 3: message_id is stored on the item and
    carried in the payload, and --send-and-close threads the reply
    under it in one call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "state.json"
        self.calls = self.dir / "send-calls.log"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def write_send_cmd(self, body):
        script = self.dir / "fake-send.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"with open({str(self.calls)!r}, 'a', encoding='utf-8') as fh:\n"
            "    fh.write('argv=' + repr(sys.argv[1:]) + '\\n')\n"
            "    fh.write('stdin=' + sys.stdin.read() + '\\n')\n"
            + body,
            encoding="utf-8",
        )
        script.chmod(0o755)
        return str(script)

    def test_payload_carries_message_id(self):
        fetch = FakeFetch([
            msg_update(1, OWNER, "private", OWNER, "hello", message_id=9001),
            msg_update(2, GROUP, "group", OWNER, "in group", message_id=9002),
        ])
        payload = run_cli(self.path, [], fetch=fetch)
        self.assertEqual(payload["items"][0]["message_id"], 9001)
        run_cli(self.path, ["--done", "1", payload["claim_token"]])
        payload = run_cli(self.path, [], fetch=FakeFetch())
        self.assertEqual(payload["items"][0]["message_id"], 9002)

    def test_send_and_close_threads_reply_and_closes_in_one_call(self):
        fetch = FakeFetch([
            msg_update(1, OWNER, "private", OWNER, "question", message_id=555)
        ])
        payload = run_cli(self.path, [], fetch=fetch)
        token = payload["claim_token"]
        cmd = self.write_send_cmd('print("send log")\nprint("4242")\n')
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--send-and-close", "1", token, str(OWNER), "555"],
                stdin_text="the answer",
            )
        self.assertEqual(result, {"ok": True, "message_id": "4242"})
        self.assertEqual(
            self.calls.read_text(encoding="utf-8").count("argv="), 1
        )
        calls = self.calls.read_text(encoding="utf-8")
        self.assertIn(f"argv=['{OWNER}', '--reply-to', '555']", calls)
        self.assertIn("stdin=the answer", calls)
        state = JsonStateStore(self.path).load()
        item = next(i for i in state["queue"] if i["external_id"] == "1")
        self.assertEqual(item["status"], "done")
        self.assertEqual(item["reply_message_id"], "4242")
        self.assertIsNotNone(item["replied_at"])

    def test_send_and_close_with_keyboard(self):
        # The keyboard goes only on command replies, so this sends
        # for a real /menu command, not plain chat text.
        fetch = FakeFetch([msg_update(1, OWNER, "private", OWNER, "/menu")])
        payload = run_cli(self.path, [], fetch=fetch)
        cmd = self.write_send_cmd('print("4242")\n')
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--send-and-close", "1", payload["claim_token"],
                 str(OWNER), "--keyboard"],
                stdin_text="here",
            )
        self.assertEqual(result, {"ok": True, "message_id": "4242"})
        calls = self.calls.read_text(encoding="utf-8")
        self.assertIn(f"argv=['{OWNER}', '--keyboard']", calls)

    def test_send_and_close_without_message_id_is_unknown(self):
        # A send whose output has no message id leaves the item in
        # "sending" and reports stage=unknown, never silently done.
        fetch = FakeFetch([msg_update(1, OWNER, "private", OWNER, "q")])
        payload = run_cli(self.path, [], fetch=fetch)
        cmd = self.write_send_cmd("")
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--send-and-close", "1", payload["claim_token"], str(OWNER)],
                stdin_text="answer",
            )
        # The send step itself is ambiguous (no message id), so the
        # combined call reports unknown, not record.
        self.assertEqual(result, {"ok": False, "stage": "unknown"})
        state = JsonStateStore(self.path).load()
        item = next(i for i in state["queue"] if i["external_id"] == "1")
        self.assertEqual(item["status"], "sending")


class CallbackQueryTests(unittest.TestCase):
    """Round 2 item 6: inline keyboard button taps arrive as
    callback_query updates; the spinner is dismissed promptly and
    the action is queued as the matching command."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def callback_update(self, uid, sender_id, data, chat_id=OWNER, chat_type="private", message_id=77):
        return {
            "update_id": uid,
            "callback_query": {
                "id": f"cb-{uid}",
                "from": {"id": sender_id, "is_bot": False},
                "message": {
                    "message_id": message_id,
                    "chat": {"id": chat_id, "type": chat_type},
                    "text": "old reply",
                },
                "data": data,
            },
        }

    def test_callback_queues_command_and_dismisses_spinner(self):
        api = FakeApi()
        payload = run_cli(
            self.path, [],
            fetch=FakeFetch([self.callback_update(1, OWNER, "cmd:pause")]),
            api_call=api,
        )
        self.assertIsNotNone(payload)
        self.assertEqual(payload["items"][0]["text"], "/pause")
        # A tap has no user message: callback["message"] is the bot's
        # own message, so the item carries message_id None and
        # source "callback_query" — its reply is standalone, never
        # threaded under the bot's own message.
        self.assertIsNone(payload["items"][0]["message_id"])
        self.assertEqual(payload["items"][0]["source"], "callback_query")
        # The spinner was dismissed via answerCallbackQuery.
        self.assertIn(
            ("answerCallbackQuery", {"callback_query_id": "cb-1"}),
            api.calls,
        )

    def test_callback_with_unknown_data_is_ignored(self):
        api = FakeApi()
        payload = run_cli(
            self.path, [],
            fetch=FakeFetch([self.callback_update(1, OWNER, "nonsense")]),
            api_call=api,
        )
        self.assertIsNone(payload)
        state = JsonStateStore(self.path).load()
        self.assertEqual(state["queue"], [])

    def test_callback_from_stranger_in_group_is_queued_but_not_privileged(self):
        # The bridge queues the tap; a stranger's /pause never runs.
        # v2: the preclaim filter closes it as an unauthorized
        # command before any wake, so there is no payload — the
        # gate moved earlier, the outcome is the same. The queue
        # record still exists, done as not-for-me, and the spinner
        # was still dismissed via answerCallbackQuery.
        payload = run_cli(
            self.path, [],
            fetch=FakeFetch([msg_update(1, GROUP, "group", OWNER, "adopt")]),
        )
        run_cli(self.path, ["--done", "1", payload["claim_token"]])
        api = FakeApi()
        payload = run_cli(
            self.path, [],
            fetch=FakeFetch([
                self.callback_update(2, 333, "cmd:pause",
                                     chat_id=GROUP, chat_type="group")
            ]),
            api_call=api,
        )
        self.assertIsNone(payload)
        state = JsonStateStore(self.path).load()
        queued = [i for i in state["queue"] if i["external_id"] == "2"]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["text"], "/pause")
        self.assertEqual(queued[0]["status"], "done")
        self.assertEqual(queued[0]["done_reason"], "not-for-me")
        self.assertIn(
            ("answerCallbackQuery", {"callback_query_id": "cb-2"}),
            api.calls,
        )


class EditedAndReactionTests(unittest.TestCase):
    """Round 2 item 11: edited_message updates a still-pending item;
    message_reaction never wakes and never creates an item."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def edited_update(self, uid, message_id, new_text, chat_id=OWNER, chat_type="private", sender_id=OWNER):
        return {
            "update_id": uid,
            "edited_message": {
                "message_id": message_id,
                "from": {"id": sender_id, "is_bot": False},
                "chat": {"id": chat_id, "type": chat_type},
                "text": new_text,
                "date": 1700000000 + uid,
                "edit_date": 1700000100 + uid,
            },
        }

    def reaction_update(self, uid, message_id, emoji="👍", chat_id=OWNER, chat_type="private", sender_id=OWNER):
        return {
            "update_id": uid,
            "message_reaction": {
                "message_id": message_id,
                "chat": {"id": chat_id, "type": chat_type},
                "user": {"id": sender_id, "is_bot": False},
                "old_reaction": [],
                "new_reaction": [{"type": "emoji", "emoji": emoji}],
            },
        }

    def test_edited_message_updates_pending_item(self):
        fetch = FakeFetch([msg_update(1, OWNER, "private", OWNER, "orig", message_id=500)])
        payload = run_cli(self.path, [], fetch=fetch)
        # The item is claimed; release it back to pending first.
        run_cli(self.path, ["--release", "1", payload["claim_token"]])
        # The edit updates the still-pending item's text, and the
        # same poll claims it with the corrected text.
        payload = run_cli(
            self.path, [],
            fetch=FakeFetch([self.edited_update(2, 500, "corrected")]),
        )
        self.assertIsNotNone(payload)
        self.assertEqual(payload["items"][0]["text"], "corrected")

    def test_edited_message_on_claimed_item_is_ignored(self):
        fetch = FakeFetch([msg_update(1, OWNER, "private", OWNER, "orig", message_id=500)])
        payload = run_cli(self.path, [], fetch=fetch)
        run_cli(
            self.path, [],
            fetch=FakeFetch([self.edited_update(2, 500, "too late")]),
        )
        state = JsonStateStore(self.path).load()
        item = next(i for i in state["queue"] if i["external_id"] == "1")
        self.assertEqual(item["text"], "orig")
        self.assertEqual(item["status"], "claimed")

    def test_edited_message_for_unknown_id_is_ignored(self):
        run_cli(
            self.path, [],
            fetch=FakeFetch([self.edited_update(1, 999, "ghost")]),
        )
        state = JsonStateStore(self.path).load()
        self.assertEqual(state["queue"], [])

    def test_message_reaction_never_wakes_and_creates_no_item(self):
        fetch = FakeFetch([self.reaction_update(1, 500, "👍")])
        self.assertIsNone(run_cli(self.path, [], fetch=fetch))
        state = JsonStateStore(self.path).load()
        self.assertEqual(state["queue"], [])
        self.assertEqual(state["offset"], 2)


class GroupAdminTests(unittest.TestCase):
    """Round 2 item 12: the group creator and admins can run
    commands; the admin list is fetched on adoption and cached."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_group_adoption_fetches_and_caches_admins(self):
        api = FakeApi(creator=OWNER, admins=[OWNER, 222])
        run_cli(
            self.path, [],
            fetch=FakeFetch([msg_update(1, GROUP, "group", OWNER, "adopt")]),
            api_call=api,
        )
        self.assertIn(
            ("getChatAdministrators", {"chat_id": GROUP}), api.calls
        )
        state = JsonStateStore(self.path).load()
        admins = state["groups"][str(GROUP)]["admins"]
        self.assertEqual(admins["chat_id"], GROUP)
        self.assertEqual(admins["creator_id"], OWNER)
        self.assertIn(222, admins["admin_ids"])
        self.assertIsNotNone(admins["fetched_at"])

    def test_payload_marks_group_admin(self):
        api = FakeApi(creator=OWNER, admins=[OWNER, 222])
        run_cli(
            self.path, [],
            fetch=FakeFetch([msg_update(1, GROUP, "group", OWNER, "adopt")]),
            api_call=api,
        )
        run_cli(self.path, ["--done", "1", self._claim_token(1)])
        payload = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(2, GROUP, "group", 222, "admin here"),
                msg_update(3, GROUP, "group", 333, "stranger here"),
            ]),
            api_call=api,
        )
        by_sender = {i["sender_id"]: i for i in payload["items"]}
        self.assertTrue(by_sender[222]["is_group_admin"])
        self.assertFalse(by_sender[333]["is_group_admin"])

    def test_command_from_unknown_sender_refreshes_admin_list_once(self):
        api = FakeApi(creator=OWNER, admins=[OWNER, 222])
        run_cli(
            self.path, [],
            fetch=FakeFetch([msg_update(1, GROUP, "group", OWNER, "adopt")]),
            api_call=api,
        )
        run_cli(self.path, ["--done", "1", self._claim_token(1)])
        # Age the cached admin list past the 60s refresh floor, so
        # a command from someone not in it triggers a re-fetch.
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            state["groups"][str(GROUP)]["admins"]["fetched_at"] = time.time() - 61
        # Sender 444 is not in the cached list; their command
        # triggers one re-fetch (which still does not list them).
        calls_before = len(api.calls)
        payload = run_cli(
            self.path, [],
            fetch=FakeFetch([msg_update(2, GROUP, "group", 444, "/pause")]),
            api_call=api,
        )
        self.assertEqual(len(api.calls), calls_before + 1)
        self.assertEqual(api.calls[-1][0], "getChatAdministrators")
        # v2: the stranger's /pause is closed by the preclaim filter
        # as an unauthorized command, so there is no wake payload —
        # the command never runs. The refresh still happened, which
        # is what this test protects.
        self.assertIsNone(payload)
        # A second command within 60s does NOT re-fetch again.
        calls_before = len(api.calls)
        payload = run_cli(
            self.path, [],
            fetch=FakeFetch([msg_update(3, GROUP, "group", 444, "/pause")]),
            api_call=api,
        )
        self.assertIsNone(payload)
        self.assertEqual(len(api.calls), calls_before)

    def test_refresh_admins_cli(self):
        api = FakeApi(creator=OWNER, admins=[OWNER, 222])
        run_cli(
            self.path, [],
            fetch=FakeFetch([msg_update(1, GROUP, "group", OWNER, "adopt")]),
            api_call=api,
        )
        result = run_cli(self.path, ["--refresh-admins"], api_call=api)
        self.assertTrue(result["ok"])
        record = result["group_admins"]
        self.assertEqual(record["chat_id"], GROUP)
        self.assertEqual(record["creator_id"], OWNER)
        self.assertIn(222, record["admin_ids"])

    def _claim_token(self, external_id):
        state = JsonStateStore(self.path).load()
        item = next(i for i in state["queue"] if i["external_id"] == str(external_id))
        return item["claim_token"]


class ActiveSpeakersTests(unittest.TestCase):
    """Round 2 item 8: /active lists who spoke recently, from the
    group_buffer the bridge already keeps."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_recent_speakers_from_group_buffer(self):
        now = time.time()
        buffer = [
            {"sender_name": "boss", "date": now - 60},
            {"sender_name": "kai", "date": now - 120},
            {"sender_name": "kai", "date": now - 180},
            {"sender_name": "old", "date": now - 7200},
        ]
        speakers = telegram.recent_speakers(buffer, now)
        self.assertEqual(
            [(s["sender_name"], s["messages"]) for s in speakers],
            [("boss", 1), ("kai", 2)],
        )
        # last_seen is the epoch of their most recent message.
        self.assertAlmostEqual(speakers[0]["last_seen"], now - 60, delta=2)
        self.assertAlmostEqual(speakers[1]["last_seen"], now - 120, delta=2)

    def test_active_cli_returns_recent_speakers(self):
        now = int(time.time())
        run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(1, GROUP, "group", OWNER, "adopt", username="boss", date=now - 60),
                msg_update(2, GROUP, "group", 222, "hi", username="kai", date=now - 30),
            ]),
        )
        result = run_cli(self.path, ["--active"])
        self.assertTrue(result["ok"])
        names = {s["sender_name"]: s for s in result["speakers"]}
        self.assertIn("@boss", names)
        self.assertIn("@kai", names)
        self.assertEqual(names["@kai"]["messages"], 1)


class DoneDefaultReasonTests(unittest.TestCase):
    """--done with no reply and no reason defaults the reason.

    Regression (2026-11-12 turns-dying bug): items 344786942-945
    were closed done with replied_at None and done_reason None —
    the owning turn closed the @mention work items with no reason
    recorded (a stale turn cannot --done: a wrong token is a
    no-op in mark_done). The CLI now defaults the reason on the
    operator path (queue_core.mark_done itself stays reason-optional;
    its tests pin that), so every silent close is auditable.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _claimed(self, external_id, item_class):
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id=external_id,
                chat_id=GROUP, chat_type="group", sender_id=OWNER,
                text="hello", message_id=9000 + external_id,
                item_class=item_class, now=1000.0,
            )
            batch = queue_core.claim_batch(state, platform="telegram", now=1000.0)
        return batch["claim_token"]

    def _item(self, external_id):
        return JsonStateStore(self.path).load()["queue"][0]

    def test_done_request_without_reason_defaults_to_no_reply_request(self):
        token = self._claimed(1, queue_core.CLASS_REQUEST)
        self.assertEqual(run_cli(self.path, ["--done", "1", token]), {"ok": True})
        item = self._item(1)
        self.assertEqual(item["status"], "done")
        self.assertIsNone(item["replied_at"])
        self.assertEqual(item["done_reason"], "closed-no-reply-request")

    def test_done_conversation_without_reason_defaults_to_no_reply(self):
        token = self._claimed(2, queue_core.CLASS_CONVERSATION)
        self.assertEqual(run_cli(self.path, ["--done", "2", token]), {"ok": True})
        self.assertEqual(self._item(2)["done_reason"], "closed-no-reply")

    def test_done_explicit_reason_is_preserved(self):
        token = self._claimed(3, queue_core.CLASS_REQUEST)
        self.assertEqual(
            run_cli(self.path, ["--done", "3", token, "silence-guarded"]),
            {"ok": True},
        )
        self.assertEqual(self._item(3)["done_reason"], "silence-guarded")


if __name__ == "__main__":
    unittest.main()


class BotUsernameRequiredTests(unittest.TestCase):
    """No default bot username any more: _bot_username() must fail
    loudly (unset or blank) instead of silently identifying as some
    other bot."""

    def test_unset_bot_username_fails_loudly(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                RuntimeError, "TELEGRAM_BRIDGE_BOT_USERNAME"
            ):
                telegram._bot_username()

    def test_blank_bot_username_fails_loudly(self):
        with mock.patch.dict(
            os.environ, {telegram.BOT_USERNAME_ENV: "   "}
        ):
            with self.assertRaisesRegex(
                RuntimeError, "TELEGRAM_BRIDGE_BOT_USERNAME"
            ):
                telegram._bot_username()
