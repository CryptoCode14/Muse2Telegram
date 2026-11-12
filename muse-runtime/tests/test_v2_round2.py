"""Round-2 redo tests (2026-11-12): the three-way sender contract,
queue-held resume, ack reservation, fail-closed reply, heartbeat
horizon, poison updates, and the stretch minors (m5-m9).

Each class names the item it covers (B1/B2/M1/M2/M3/M4/M5/m5-m9).
No Muse credentials, Telegram calls, or external sender are used:
the sender subprocess is mocked or a fake script, and the store is
a temp dir.
"""
import io
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

from bridge_core import queue as queue_core
from bridge_core import telegram
from bridge_core.store import JsonStateStore

OWNER = 111
GROUP = -777


def load_sender_script():
    """Load bin/telegram-sender as a module (no .py suffix, so the
    loader must be explicit)."""
    here = Path(__file__).resolve()
    sender_path = here.parent.parent / "bin" / "telegram-sender"
    loader = SourceFileLoader("telegram_sender_round2", str(sender_path))
    spec = importlib.util.spec_from_loader(
        "telegram_sender_round2", loader
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def http_error(code, retry_after=None):
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return urllib.error.HTTPError("url", code, "error", headers, None)


class SenderThreeWayTests(unittest.TestCase):
    """B1: bin/telegram-sender exit codes 0/2/3."""

    def setUp(self):
        self.sender = load_sender_script()

    def test_403_maps_to_definitive(self):
        with mock.patch.object(
            self.sender, "_post",
            side_effect=http_error(403),
        ):
            with self.assertRaises(self.sender.DefinitiveSendError) as ctx:
                self.sender.send_message(111, "hi")
        self.assertEqual(ctx.exception.code, 403)

    def test_404_maps_to_definitive(self):
        with mock.patch.object(
            self.sender, "_post",
            side_effect=http_error(404),
        ):
            with self.assertRaises(self.sender.DefinitiveSendError) as ctx:
                self.sender.send_message(111, "hi")
        self.assertEqual(ctx.exception.code, 404)

    def test_400_on_all_variants_maps_to_definitive(self):
        # Every variant 400: the send provably did not go out.
        # reply_to + HTML text keep all three variants distinct
        # (plain text with no threading would dedupe otherwise).
        calls = {"n": 0}

        def _post(method, payload):
            calls["n"] += 1
            raise http_error(400)

        with mock.patch.object(self.sender, "_post", side_effect=_post):
            with self.assertRaises(self.sender.DefinitiveSendError) as ctx:
                self.sender.send_message(
                    111, "hi <b>there</b>", reply_to=55)
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(calls["n"], 3)

    def test_429_sleeps_capped_retry_after_and_retries_once(self):
        # Retry-After 120 -> sleep min(120, 60) = 60, one retry,
        # then retryable with the raw value carried.
        calls = {"n": 0}

        def _post(method, payload):
            calls["n"] += 1
            raise http_error(429, retry_after=120)

        with mock.patch.object(self.sender, "_post", side_effect=_post), \
                mock.patch.object(self.sender.time, "sleep") as slept:
            with self.assertRaises(self.sender.RetryableSendError) as ctx:
                self.sender.send_message(111, "hi")
        self.assertEqual(ctx.exception.code, 429)
        self.assertEqual(ctx.exception.retry_after, 120)
        self.assertEqual(calls["n"], 2)
        slept.assert_called_once_with(60)

    def test_429_without_retry_after_retries_at_once(self):
        calls = {"n": 0}

        def _post(method, payload):
            calls["n"] += 1
            raise http_error(429)

        with mock.patch.object(self.sender, "_post", side_effect=_post), \
                mock.patch.object(self.sender.time, "sleep") as slept:
            with self.assertRaises(self.sender.RetryableSendError) as ctx:
                self.sender.send_message(111, "hi")
        self.assertEqual(ctx.exception.code, 429)
        self.assertIsNone(ctx.exception.retry_after)
        self.assertEqual(calls["n"], 2)
        slept.assert_not_called()

    def test_429_then_success_returns_message_id(self):
        calls = {"n": 0}

        def _post(method, payload):
            calls["n"] += 1
            if calls["n"] == 1:
                raise http_error(429, retry_after=5)
            return {"ok": True, "result": {"message_id": 55}}

        with mock.patch.object(self.sender, "_post", side_effect=_post), \
                mock.patch.object(self.sender.time, "sleep") as slept:
            self.assertEqual(self.sender.send_message(111, "hi"), 55)
        self.assertEqual(calls["n"], 2)
        slept.assert_called_once_with(5)

    def test_500_maps_to_retryable_without_retry_after(self):
        with mock.patch.object(
            self.sender, "_post",
            side_effect=http_error(500),
        ):
            with self.assertRaises(self.sender.RetryableSendError) as ctx:
                self.sender.send_message(111, "hi")
        self.assertEqual(ctx.exception.code, 500)
        self.assertIsNone(ctx.exception.retry_after)

    def test_media_429_retries_once_per_send_not_per_variant(self):
        # The 429 retry is once per send call, shared across the
        # format variants: variant 1 429s, the retry is spent, and
        # a 400 on the retried variant moves on to variant 2
        # instead of retrying the 429 again.
        calls = {"n": 0}

        def _post_multipart(method, fields, file_field, filename, data, content_type):
            calls["n"] += 1
            if calls["n"] == 1:
                raise http_error(429, retry_after=1)
            raise http_error(400)

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        tmp.write(b"x")
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        with mock.patch.object(
            self.sender, "_post_multipart", side_effect=_post_multipart
        ), mock.patch.object(self.sender.time, "sleep"):
            with self.assertRaises(self.sender.DefinitiveSendError):
                self.sender.send_photo(
                    111, tmp.name, caption="c <b>bold</b>", reply_to=55)
        # variant 1 (429, retried) + variant 1 again (400) +
        # variant 2 (400) + variant 3 (400): the 429 retry was
        # spent once, not once per variant.
        self.assertEqual(calls["n"], 4)

    def _run_main(self, argv, stdin_text="hi\n"):
        # main() takes no arguments: it reads sys.argv itself.
        err = io.StringIO()
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin_text)
        try:
            with redirect_stderr(err):
                with mock.patch.object(sys, "argv", argv):
                    return self.sender.main(), err.getvalue()
        finally:
            sys.stdin = old_stdin

    def test_main_returns_2_on_definitive(self):
        with mock.patch.object(
            self.sender, "send_message",
            side_effect=self.sender.DefinitiveSendError(403),
        ):
            code, err = self._run_main(["telegram-sender", "111"])
        self.assertEqual(code, 2)
        self.assertIn("telegram rejected", err)

    def test_main_returns_3_on_retryable_with_retry_after_line(self):
        with mock.patch.object(
            self.sender, "send_message",
            side_effect=self.sender.RetryableSendError(429, 120),
        ):
            code, err = self._run_main(["telegram-sender", "111"])
        self.assertEqual(code, 3)
        self.assertIn("telegram HTTP 429 retry-after=120", err)

    def test_main_returns_3_on_5xx_without_retry_after_line(self):
        with mock.patch.object(
            self.sender, "send_message",
            side_effect=self.sender.RetryableSendError(500),
        ):
            code, err = self._run_main(["telegram-sender", "111"])
        self.assertEqual(code, 3)
        self.assertIn("telegram HTTP 500", err)
        self.assertNotIn("retry-after", err)


def _fenced_store(tmp):
    """Temp store with one claimed DM item; returns (store, token)."""
    path = Path(tmp) / "state.json"
    store = JsonStateStore(path)
    with store.transaction() as state:
        queue_core.enqueue(
            state, platform="telegram", external_id=101,
            chat_id=OWNER, chat_type="private", sender_id=OWNER,
            text="hello", message_id=55,
        )
        token = queue_core.claim_batch(
            state, platform="telegram", now=1000.0)["claim_token"]
    return store, token


class SendFencedThreeWayTests(unittest.TestCase):
    """B1: send_fenced / send_media_fenced map the 3-way contract."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store, self.token = _fenced_store(self.temp.name)
        self.env = mock.patch.dict(os.environ, {
            telegram.SEND_CMD_ENV: "/fake/sender",
            telegram.BOT_USERNAME_ENV: "ExampleBot"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def item(self, external_id=101):
        return queue_core.find_item(self.store.load(), "telegram", external_id)

    def test_rejected_returns_item_to_pending(self):
        with mock.patch.object(
            telegram, "_run_send_command", return_value=("rejected", None)
        ):
            result = telegram.send_fenced(
                self.store, 101, self.token, OWNER, "the reply", now=1000.0)
        self.assertEqual(result, {"ok": False, "stage": "rejected"})
        item = self.item()
        self.assertEqual(item["status"], "pending")
        self.assertIsNone(item["claim_token"])

    def test_retryable_returns_item_pending_with_delay(self):
        with mock.patch.object(
            telegram, "_run_send_command",
            return_value=telegram._SendResult("retryable", None, 120),
        ):
            result = telegram.send_fenced(
                self.store, 101, self.token, OWNER, "the reply", now=1000.0)
        self.assertEqual(result, {
            "ok": False, "stage": "retryable", "retry_after": 120.0})
        item = self.item()
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["wake_after"], 1120.0)

    def test_retryable_without_retry_after_uses_60s_default(self):
        # A plain 2-tuple from a mocked sender carries no delay:
        # the 60 s default applies (see _retryable_delay_seconds).
        with mock.patch.object(
            telegram, "_run_send_command", return_value=("retryable", None)
        ):
            result = telegram.send_fenced(
                self.store, 101, self.token, OWNER, "the reply", now=1000.0)
        self.assertEqual(result, {
            "ok": False, "stage": "retryable", "retry_after": 60.0})
        self.assertEqual(self.item()["wake_after"], 1060.0)

    def test_retryable_delay_capped_at_300(self):
        with mock.patch.object(
            telegram, "_run_send_command",
            return_value=telegram._SendResult("retryable", None, 900),
        ):
            result = telegram.send_fenced(
                self.store, 101, self.token, OWNER, "the reply", now=1000.0)
        self.assertEqual(result["retry_after"], 300.0)
        self.assertEqual(self.item()["wake_after"], 1300.0)

    def test_media_rejected_returns_item_to_pending(self):
        media = Path(self.temp.name) / "photo.jpg"
        media.write_bytes(b"x")
        with mock.patch.object(Path, "home", return_value=Path(self.temp.name)), \
                mock.patch.object(
                    telegram, "_run_send_command",
                    return_value=("rejected", None)):
            result = telegram.send_media_fenced(
                self.store, 101, self.token, OWNER, "photo", str(media),
                "caption", now=1000.0)
        self.assertEqual(result, {"ok": False, "stage": "rejected"})
        self.assertEqual(self.item()["status"], "pending")

    def test_media_retryable_returns_item_pending_with_delay(self):
        media = Path(self.temp.name) / "photo.jpg"
        media.write_bytes(b"x")
        with mock.patch.object(Path, "home", return_value=Path(self.temp.name)), \
                mock.patch.object(
                    telegram, "_run_send_command",
                    return_value=telegram._SendResult("retryable", None, 45),
                ):
            result = telegram.send_media_fenced(
                self.store, 101, self.token, OWNER, "photo", str(media),
                "caption", now=1000.0)
        self.assertEqual(result, {
            "ok": False, "stage": "retryable", "retry_after": 45.0})
        item = self.item()
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["wake_after"], 1045.0)


# --- --reply CLI harness (small copy of test_telegram_reply's) ---

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


_FAKE_SEND_BODY = """\
#!/usr/bin/env python3
import sys, json
with open({log!r}, 'a', encoding='utf-8') as fh:
    fh.write('argv=' + repr(sys.argv[1:]) + '\\n')
if sys.argv[1:2] and not sys.argv[1].startswith('--'):
    with open({log!r}, 'a', encoding='utf-8') as fh:
        fh.write('stdin=' + sys.stdin.read() + '\\n')
{probe}
{fail}
print("4242")
"""


class ReplyHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "state.json"
        self.calls = self.dir / "send-calls.log"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def write_send_cmd(self, fail_exit=None, probe=""):
        """A fake send command. ``fail_exit`` makes the text send
        exit with that code instead of 0; ``probe`` is extra code
        run on the text-send path (e.g. dump the queue state).
        Every invocation is logged."""
        fail = ""
        if fail_exit is not None:
            fail = (
                "if sys.argv[1:2] and not sys.argv[1].startswith('--'):\n"
                f"    raise SystemExit({fail_exit})\n"
            )
        script = self.dir / "fake-send.py"
        script.write_text(
            _FAKE_SEND_BODY.format(
                log=str(self.calls), fail=fail, probe=probe),
            encoding="utf-8",
        )
        script.chmod(0o755)
        return str(script)

    def send_env(self, cmd):
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


class ReserveAllBeforeSendTests(ReplyHarness):
    """B2: primary + extras are reserved SENDING before the send."""

    def test_extras_are_sending_before_the_network_send(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "one", message_id=9001),
            msg_update(2, OWNER, "private", OWNER, "two", message_id=9002),
        ])
        probe = (
            "    state = json.load(open(" + repr(str(self.path)) + "))\n"
            "    statuses = {i['external_id']: i['status'] for i in state['queue']}\n"
            "    with open(" + repr(str(self.calls)) + ", 'a') as fh:\n"
            "        fh.write('at-send=' + repr(statuses) + '\\n')\n"
        )
        cmd = self.write_send_cmd(probe=probe)
        with self.send_env(cmd):
            result = run_cli(
                self.path, ["--reply", payload["claim_token"]],
                stdin_text="one reply for both",
            )
        self.assertTrue(result["ok"])
        # At send time both batch items were already "sending" —
        # the reservation happened before the network call, not
        # after a successful send.
        self.assertIn("at-send={'1': 'sending', '2': 'sending'}",
                      self.call_log())

    def test_rejected_send_leaves_extras_pending(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "one", message_id=9001),
            msg_update(2, OWNER, "private", OWNER, "two", message_id=9002),
        ])
        cmd = self.write_send_cmd(fail_exit=2)
        with self.send_env(cmd):
            result = run_cli(
                self.path, ["--reply", payload["claim_token"]],
                stdin_text="one reply for both",
            )
        self.assertEqual(result, {
            "ok": False, "stage": "rejected", "message_id": None,
            "chat_id": OWNER, "replied_update_ids": [],
            "closed_update_ids": [], "ack_deleted": False,
            "next_batch": None,
        })
        # A definitive rejection returns the whole batch to
        # pending: nothing is stranded in "sending".
        self.assertEqual(self.item(1)["status"], "pending")
        self.assertEqual(self.item(2)["status"], "pending")


class WarmBatonTests(unittest.TestCase):
    """M1: warm baton is 120 s, not 30 s."""

    def test_warm_claim_recycles_at_121s_not_31s(self):
        state = {"queue": []}
        queue_core.enqueue(
            state, platform="telegram", external_id=1,
            chat_id=OWNER, chat_type="private", sender_id=OWNER,
            text="hi", message_id=1,
        )
        batch = queue_core.claim_batch(
            state, platform="telegram", now=1000.0)
        self.assertIsNotNone(batch)
        item = queue_core.find_item(state, "telegram", 1)
        item["warm_claimed"] = True
        item["heartbeat_at"] = 1000.0
        item["turn_started_at"] = None
        # 31 s in: the old 30 s baton would already have recycled.
        self.assertEqual(queue_core.stale_claims(state, now=1031.0), [])
        # 121 s in: past the 120 s baton, recyclable.
        stale = queue_core.stale_claims(state, now=1121.0)
        self.assertEqual([i["external_id"] for i in stale], ["1"])

    def test_warm_baton_constant_is_120(self):
        self.assertEqual(queue_core.WARM_BATON_TIMEOUT_SECONDS, 120.0)


class ReplyFailsClosedTests(ReplyHarness):
    """M3: --reply fails closed when a sibling is SENDING."""

    def _claim_two(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "one", message_id=9001),
            msg_update(2, OWNER, "private", OWNER, "two", message_id=9002),
        ])
        return payload["claim_token"]

    def test_reply_with_sending_sibling_fails_closed(self):
        token = self._claim_two()
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            self.assertTrue(queue_core.mark_sending(
                state, "telegram", 1, token, now=1000.0))
        cmd = self.write_send_cmd()
        with self.send_env(cmd):
            result = run_cli(
                self.path, ["--reply", token], stdin_text="answer",
            )
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["stage"], "fence")
        # Nothing was sent at all: no send attempt for either item.
        self.assertNotIn("stdin=", self.call_log())
        self.assertEqual(self.item(1)["status"], "sending")
        self.assertEqual(self.item(2)["status"], "claimed")

    def test_reply_with_sending_replied_sibling_falls_through(self):
        # The whole batch is one SENDING item whose answer already
        # went out: it is not a fence — it falls through to the
        # existing stale-token path, and nothing is sent.
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "one", message_id=9001),
        ])
        token = payload["claim_token"]
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            self.assertTrue(queue_core.mark_sending(
                state, "telegram", 1, token, now=1000.0))
            item = queue_core.find_item(state, "telegram", 1)
            item["replied_at"] = 1001.0
        cmd = self.write_send_cmd()
        with self.send_env(cmd):
            result = run_cli(
                self.path, ["--reply", token], stdin_text="answer",
            )
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["stage"], "stale-token")
        self.assertNotIn("stdin=", self.call_log())


class WatchdogStuckSendingTests(unittest.TestCase):
    """B1: the sweep surfaces items stuck in 'sending' past 1 h."""

    def _sending_state(self, sending_at):
        state = {"queue": []}
        queue_core.enqueue(
            state, platform="telegram", external_id=1,
            chat_id=OWNER, chat_type="private", sender_id=OWNER,
            text="hi", message_id=1,
        )
        queue_core.claim_batch(state, platform="telegram", now=1000.0)
        token = queue_core.find_item(state, "telegram", 1)["claim_token"]
        self.assertTrue(queue_core.mark_sending(
            state, "telegram", 1, token, now=1000.0))
        item = queue_core.find_item(state, "telegram", 1)
        if sending_at is None:
            item.pop("sending_at", None)
        else:
            item["sending_at"] = sending_at
        return state

    def test_stuck_sending_past_1h_is_reported(self):
        state = self._sending_state(1000.0)
        report = queue_core.watchdog_sweep(state, now=4601.0)
        self.assertEqual(report["stuck_sending"], ["1"])

    def test_fresh_sending_item_is_not_stuck(self):
        state = self._sending_state(1000.0)
        report = queue_core.watchdog_sweep(state, now=1060.0)
        self.assertEqual(report["stuck_sending"], [])

    def test_sending_without_timestamp_counts_as_stuck(self):
        # Legacy / hand-edited state with no sending_at: outcome
        # unknown for an unbounded time, so it counts as stuck.
        state = self._sending_state(None)
        report = queue_core.watchdog_sweep(state, now=1060.0)
        self.assertEqual(report["stuck_sending"], ["1"])


class WatchdogHeartbeatWiringTests(unittest.TestCase):
    """M4: the watchdog passes the 600 s heartbeat horizon."""

    def _claimed_state(self):
        state = {"queue": []}
        queue_core.enqueue(
            state, platform="telegram", external_id=1,
            chat_id=OWNER, chat_type="private", sender_id=OWNER,
            text="hi", message_id=1,
        )
        queue_core.claim_batch(state, platform="telegram", now=1000.0)
        item = queue_core.find_item(state, "telegram", 1)
        item["claimed_at"] = 1000.0
        item["heartbeat_at"] = 1000.0
        item["turn_started_at"] = None
        return state

    def test_watchdog_recycles_never_started_claim_at_601s_not_599s(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = JsonStateStore(path)
            with store.transaction() as state:
                state.update(self._claimed_state())
            with mock.patch.dict(os.environ, {
                    telegram.BOT_USERNAME_ENV: "ExampleBot"}):
                # 599 s in: not yet recyclable under the 600 s horizon.
                report = telegram.watchdog_sweep(store, now=1599.0)
                self.assertEqual(report["recycled"], [])
                # 601 s in: recyclable.
                report = telegram.watchdog_sweep(store, now=1601.0)
                self.assertEqual(report["recycled"], ["1"])


class QueueHoldTests(unittest.TestCase):
    """M2: hold / unhold / held_items / claim_ids in the queue."""

    def _state(self):
        state = {"queue": []}
        queue_core.enqueue(
            state, platform="telegram", external_id=1,
            chat_id=OWNER, chat_type="private", sender_id=OWNER,
            text="one", message_id=1,
        )
        queue_core.enqueue(
            state, platform="telegram", external_id=2,
            chat_id=OWNER, chat_type="private", sender_id=OWNER,
            text="two", message_id=2,
        )
        queue_core.enqueue(
            state, platform="telegram", external_id=3,
            chat_id=GROUP, chat_type="group", sender_id=OWNER,
            text="three", message_id=3,
        )
        return state

    def test_hold_pending_item_sets_wake_after_24h(self):
        state = {"queue": []}
        queue_core.enqueue(
            state, platform="telegram", external_id=1,
            chat_id=OWNER, chat_type="private", sender_id=OWNER,
            text="one", message_id=1,
        )
        self.assertTrue(queue_core.hold_item(
            state, "telegram", 1, now=1000.0))
        item = queue_core.find_item(state, "telegram", 1)
        self.assertEqual(item["wake_after"], 1000.0 + 86400.0)
        self.assertTrue(item["held"])
        # Unclaimable until the 24 h bound: the hold self-heals.
        self.assertIsNone(queue_core.claim_batch(
            state, platform="telegram", now=1000.0))
        batch = queue_core.claim_batch(
            state, platform="telegram", now=1000.0 + 86400.0 + 1)
        self.assertIsNotNone(batch)
        self.assertEqual(
            [i["external_id"] for i in batch["items"]], ["1"])

    def test_hold_claimed_item_requires_token_match(self):
        state = self._state()
        batch = queue_core.claim_batch(state, platform="telegram", now=1000.0)
        token = batch["claim_token"]
        self.assertFalse(queue_core.hold_item(
            state, "telegram", 1, "wrong-token", now=1000.0))
        self.assertTrue(queue_core.hold_item(
            state, "telegram", 1, token, now=1000.0))
        # The claim is left intact: still claimed under its token.
        item = queue_core.find_item(state, "telegram", 1)
        self.assertEqual(item["status"], "claimed")
        self.assertEqual(item["claim_token"], token)

    def test_hold_refuses_replied_item(self):
        state = self._state()
        batch = queue_core.claim_batch(state, platform="telegram", now=1000.0)
        item = queue_core.find_item(state, "telegram", 1)
        item["replied_at"] = 1001.0
        self.assertFalse(queue_core.hold_item(
            state, "telegram", 1, batch["claim_token"], now=1000.0))

    def test_unhold_clears_only_held_items(self):
        state = self._state()
        # A throttle wake_after (not held) is never cleared.
        item = queue_core.find_item(state, "telegram", 1)
        item["wake_after"] = 5000.0
        self.assertFalse(queue_core.unhold_item(state, "telegram", 1))
        self.assertEqual(item["wake_after"], 5000.0)
        # A held item releases cleanly.
        self.assertTrue(queue_core.hold_item(
            state, "telegram", 2, now=1000.0))
        self.assertTrue(queue_core.unhold_item(state, "telegram", 2))
        item = queue_core.find_item(state, "telegram", 2)
        self.assertIsNone(item["wake_after"])
        self.assertNotIn("held", item)

    def test_held_items_lists_in_queue_order(self):
        state = self._state()
        queue_core.hold_item(state, "telegram", 2, now=1000.0)
        queue_core.hold_item(state, "telegram", 1, now=1000.0)
        held = queue_core.held_items(state, platform="telegram")
        self.assertEqual(
            [i["external_id"] for i in held], ["1", "2"])
        self.assertEqual(queue_core.held_items(state), held)

    def test_claim_ids_claims_named_items_in_one_lane(self):
        state = self._state()
        batch = queue_core.claim_ids(
            state, platform="telegram", external_ids=[1, 2, 3], now=1000.0)
        self.assertIsNotNone(batch)
        # The batch takes the lane of the first claimable named
        # item (the DM); the group item stays pending for the
        # intake's next poll.
        self.assertEqual(
            [i["external_id"] for i in batch["items"]], ["1", "2"])
        self.assertEqual(
            queue_core.find_item(state, "telegram", 3)["status"], "pending")

    def test_claim_ids_skips_items_with_future_wake_after(self):
        state = self._state()
        queue_core.hold_item(state, "telegram", 1, now=1000.0)
        batch = queue_core.claim_ids(
            state, platform="telegram", external_ids=[1], now=1000.0)
        self.assertIsNone(batch)


class HoldResumeCliTests(ReplyHarness):
    """M2: --held / --hold / --unhold through the CLI."""

    def test_held_lists_held_items(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello", message_id=9001),
        ])
        token = payload["claim_token"]
        self.assertEqual(
            run_cli(self.path, ["--hold", "1", token]), {"ok": True})
        held = run_cli(self.path, ["--held"])
        self.assertEqual(held["ok"], True)
        self.assertEqual(len(held["held"]), 1)
        self.assertEqual(held["held"][0]["update_id"], 1)
        self.assertEqual(held["held"][0]["chat_id"], OWNER)
        self.assertEqual(held["held"][0]["status"], "claimed")

    def test_unhold_reclaims_with_fresh_token(self):
        payload = self.claim([
            msg_update(1, OWNER, "private", OWNER, "hello", message_id=9001),
        ])
        old_token = payload["claim_token"]
        self.assertEqual(
            run_cli(self.path, ["--hold", "1", old_token]), {"ok": True})
        # The claim dies (released here; in production the 600 s
        # heartbeat horizon recycles it). The hold's wake_after
        # survives the release, so the item is unclaimable until
        # --unhold.
        self.assertEqual(
            run_cli(self.path, ["--release", "1", old_token]),
            {"ok": True})
        self.assertIsNone(
            run_cli(self.path, [], fetch=FakeFetch([
                msg_update(1, OWNER, "private", OWNER, "hello",
                           message_id=9001),
            ])))
        resumed = run_cli(self.path, ["--unhold", "1"])
        self.assertEqual(resumed["ok"], True)
        self.assertEqual(resumed["unheld"], [1])
        new_token = resumed["claim_token"]
        self.assertIsNotNone(new_token)
        self.assertNotEqual(new_token, old_token)
        self.assertEqual(len(resumed["items"]), 1)
        self.assertEqual(resumed["items"][0]["update_id"], 1)
        # The held token is dead: --reply with it fails stale-token.
        cmd = self.write_send_cmd()
        with self.send_env(cmd):
            stale = run_cli(
                self.path, ["--reply", old_token], stdin_text="answer",
            )
        self.assertEqual(stale["stage"], "stale-token")
        self.assertNotIn("stdin=", self.call_log())
        # The fresh token answers.
        with self.send_env(cmd):
            result = run_cli(
                self.path, ["--reply", new_token], stdin_text="answer",
            )
        self.assertTrue(result["ok"])

    def test_held_update_is_never_held_twice(self):
        # M2 verification: the offset advances in the same
        # transaction as the enqueue, and the receipt dedupes a
        # re-delivery — so a held update can never be held twice.
        update = msg_update(
            5, OWNER, "private", OWNER, "hello", message_id=9005)
        payload = self.claim([update])
        token = payload["claim_token"]
        self.assertEqual(
            run_cli(self.path, ["--hold", "5", token]), {"ok": True})
        # The same update delivered again: deduped, nothing new.
        self.assertIsNone(
            run_cli(self.path, [], fetch=FakeFetch([update])))
        self.assertEqual(
            [i["external_id"] for i in self.items()], ["5"])
        held = run_cli(self.path, ["--held"])
        self.assertEqual(len(held["held"]), 1)


class AckReservationTests(unittest.TestCase):
    """M5: ack reservation inside txn 1, cleared on failure."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.store = JsonStateStore(self.path)
        with self.store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id=101,
                chat_id=OWNER, chat_type="private", sender_id=OWNER,
                text="please research this thoroughly", message_id=55,
            )
            queue_core.enqueue(
                state, platform="telegram", external_id=102,
                chat_id=OWNER, chat_type="private", sender_id=OWNER,
                text="and this too", message_id=56,
            )
            self.token = queue_core.claim_batch(
                state, platform="telegram")["claim_token"]

    def item(self, external_id=101):
        return queue_core.find_item(self.store.load(), "telegram", external_id)

    def ack(self, target=101, tok=None, sender_result=("sent", "777")):
        if tok is None:
            tok = self.token
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_send_command", return_value=sender_result
            ) as sender:
                result = telegram.send_ack(
                    self.store, target, tok, OWNER, reply_to=55)
        return result, sender

    def test_concurrent_acks_send_only_once(self):
        # Two threads, one slow mocked sender: exactly one ack is
        # sent, the other is already-acked, the sender runs once.
        # Thread A reserves the ack and enters its slow send; only
        # then does thread B run, so B must see A's reservation.
        calls = {"n": 0}
        entered = threading.Event()

        def slow_send(send_cmd, chat_id, text, reply_to=None, keyboard=False):
            calls["n"] += 1
            entered.set()
            time.sleep(0.3)
            return ("sent", "777")

        results = []
        with mock.patch.dict(os.environ, {
                telegram.SEND_CMD_ENV: "/fake/sender",
                telegram.BOT_USERNAME_ENV: "ExampleBot"}):
            with mock.patch.object(
                telegram, "_run_send_command", side_effect=slow_send):
                def _ack():
                    results.append(telegram.send_ack(
                        self.store, 101, self.token, OWNER, reply_to=55))
                thread_a = threading.Thread(target=_ack)
                thread_a.start()
                self.assertTrue(entered.wait(timeout=10))
                thread_b = threading.Thread(target=_ack)
                thread_b.start()
                thread_a.join(timeout=10)
                thread_b.join(timeout=10)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(len(results), 2)
        self.assertIn({"ok": True, "message_id": "777"}, results)
        self.assertIn({"ok": False, "stage": "already-acked"}, results)
        self.assertEqual(self.item()["ack_message_id"], "777")

    def test_sweep_clears_tentative_ack_keeps_completed(self):
        with self.store.transaction() as state:
            self.assertTrue(queue_core.reserve_ack(
                state, "telegram", 101, self.token, now=1000.0))
            self.assertTrue(queue_core.mark_ack(
                state, "telegram", 102, "888", self.token, now=1000.0))
            # Age both claims past the heartbeat horizon.
            for eid in (101, 102):
                item = queue_core.find_item(state, "telegram", eid)
                item["heartbeat_at"] = 0.0
                item["claimed_at"] = 0.0
        with self.store.transaction() as state:
            report = queue_core.watchdog_sweep(state, now=10000.0)
        self.assertEqual(sorted(report["recycled"]), ["101", "102"])
        tentative = self.item(101)
        self.assertIsNone(tentative["ack_sent_at"])
        self.assertIsNone(tentative.get("ack_message_id"))
        completed = self.item(102)
        self.assertEqual(completed["ack_message_id"], "888")

    def test_clear_ack_refuses_when_message_id_recorded(self):
        with self.store.transaction() as state:
            self.assertTrue(queue_core.mark_ack(
                state, "telegram", 101, "777", self.token, now=1000.0))
            self.assertFalse(queue_core.clear_ack(
                state, "telegram", 101, self.token))
        self.assertEqual(self.item()["ack_message_id"], "777")

    def test_mark_ack_completes_tentative_reservation(self):
        with self.store.transaction() as state:
            self.assertTrue(queue_core.reserve_ack(
                state, "telegram", 101, self.token, now=1000.0))
            # A tentative reservation does NOT refuse mark_ack:
            # completing it is exactly what mark_ack does.
            self.assertTrue(queue_core.mark_ack(
                state, "telegram", 101, "777", self.token, now=1001.0))
        self.assertEqual(self.item()["ack_message_id"], "777")

    def test_send_ack_clears_reservation_on_rejected(self):
        result, sender = self.ack(sender_result=("rejected", None))
        self.assertEqual(result, {"ok": False, "stage": "rejected"})
        self.assertIsNone(self.item()["ack_sent_at"])
        self.assertIsNone(self.item().get("ack_message_id"))
        # The retry is safe: the ack sends on the second attempt.
        result, _sender = self.ack(sender_result=("sent", "777"))
        self.assertEqual(result, {"ok": True, "message_id": "777"})

    def test_send_ack_clears_reservation_on_retryable_and_unknown(self):
        result, _sender = self.ack(
            sender_result=telegram._SendResult("retryable", None, 30))
        self.assertEqual(result, {
            "ok": False, "stage": "retryable", "retry_after": 30.0})
        self.assertIsNone(self.item()["ack_sent_at"])
        self.assertEqual(self.item()["status"], "claimed")
        result, _sender = self.ack(sender_result=("unknown", None))
        self.assertEqual(result, {"ok": False, "stage": "unknown"})
        self.assertIsNone(self.item()["ack_sent_at"])


RUNTIME = Path(__file__).resolve().parent.parent


class IntakeDoneGuardTests(unittest.TestCase):
    """m6: the intake.sh done branch never passes "done" as an id."""

    def test_done_branch_guards_bare_done(self):
        script = (RUNTIME / "hooks" / "telegram-bridge-intake.sh").read_text()
        done_branch = script.split("done)")[1].split(
            'silent "telegram-not-for-me"')[0]
        self.assertIn('[ -n "$ids" ]', done_branch)
        self.assertIn('[ "$ids" != "done" ]', done_branch)
        # And the hold branch really holds in the queue now.
        hold_branch = script.split("hold)")[1].split("done)")[0]
        self.assertIn("--hold", hold_branch)
        self.assertNotIn("held.jsonl", hold_branch)

    def test_done_guard_simulation_all_three_shapes(self):
        # Simulate the script's case dispatch + guard for the three
        # decision shapes the decider can print.
        script = (
            'decision="$1"\n'
            'case "$decision" in\n'
            '  done*)\n'
            '    ids=${decision#done }\n'
            '    if [ -n "$ids" ] && [ "$ids" != "done" ]; then\n'
            '      echo "call:$ids"\n'
            '    else\n'
            '      echo "skip"\n'
            '    fi\n'
            '    ;;\n'
            '  *) echo "other:$decision" ;;\n'
            'esac\n'
        )
        for decision, expected in (
            ("done", "skip"),
            ("done 1 2", "call:1 2"),
            ("wake", "other:wake"),
        ):
            proc = subprocess.run(
                ["bash", "-c", script, "sim", decision],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), expected, decision)

    def test_both_hook_scripts_pass_bash_n(self):
        for name in ("telegram-bridge-intake.sh",
                     "telegram-bridge-watchdog.sh"):
            proc = subprocess.run(
                ["bash", "-n", str(RUNTIME / "hooks" / name)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)


class PoisonUpdateTests(unittest.TestCase):
    """m7: a poison update pins the offset until the 3rd failure."""

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {
            telegram.BOT_USERNAME_ENV: "ExampleBot"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_offset_pinned_for_two_polls_advances_on_third(self):
        # my_chat_member with a chat but no id: _ingest_update
        # raises KeyError 'id', and no minimal hold is possible
        # (no message/sender), so it lands in intake_unanswered
        # and the offset does NOT advance — until the 3rd failure.
        poison = {"update_id": 5,
                  "my_chat_member": {"chat": {"type": "group"}}}
        state = {"owner_id": OWNER, "offset": 0}
        telegram._ensure_adapter_state(state)

        def _lenient_api(method, params, timeout=20.0):
            if method in ("getChatAdministrators", "getChatMemberCount"):
                return {"ok": True, "result": []}
            if method == "answerCallbackQuery":
                return {"ok": True, "result": True}
            return {"ok": True, "result": []}

        for poll in (1, 2):
            telegram.ingest(
                state, {"ok": True, "result": [poison]},
                now=1700000000 + poll, api_call=_lenient_api,
            )
            self.assertEqual(state["offset"], 0, f"poll {poll}")
        telegram.ingest(
            state, {"ok": True, "result": [poison]},
            now=1700000003, api_call=_lenient_api,
        )
        # 3rd failure: the offset advances past it and a receipt
        # is recorded, so a later re-delivery is deduped. (The
        # receipt key lowercases the bot id.)
        self.assertEqual(state["offset"], 6)
        self.assertIn(
            queue_core.receipt_key("examplebot", 5),
            state.get("receipts", {}),
        )
        # The unanswered records stay visible (3 kept).
        self.assertEqual(len(state.get("intake_unanswered", [])), 3)
        # 4th delivery: deduped — no new record, offset unchanged.
        telegram.ingest(
            state, {"ok": True, "result": [poison]},
            now=1700000004, api_call=_lenient_api,
        )
        self.assertEqual(state["offset"], 6)
        self.assertEqual(len(state.get("intake_unanswered", [])), 3)


class StoreDirFsyncTests(unittest.TestCase):
    """m8: _write_atomic fsyncs the directory after the replace."""

    def test_write_atomic_fsyncs_state_dir_for_main_and_snapshot(self):
        from bridge_core.store import JsonStateStore
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = JsonStateStore(path)
            dirs = []
            real = JsonStateStore._fsync_dir

            def spy(self, directory):
                dirs.append(Path(directory))
                return real(self, directory)

            with mock.patch.object(JsonStateStore, "_fsync_dir", spy):
                with store.transaction() as state:
                    state["x"] = 1
            # One commit writes the main file and the .good
            # snapshot: the directory is fsynced for each.
            self.assertEqual(dirs, [Path(tmp), Path(tmp)])
            self.assertEqual(JsonStateStore(path).load()["x"], 1)

    def test_failing_dir_fsync_does_not_fail_the_save(self):
        from bridge_core.store import JsonStateStore
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = JsonStateStore(path)
            with mock.patch.object(
                JsonStateStore, "_fsync_dir",
                side_effect=OSError("no dir fsync"),
            ):
                with store.transaction() as state:
                    state["x"] = 1
            # Degraded durability, not a failed save.
            self.assertEqual(JsonStateStore(path).load()["x"], 1)


class RateFileTests(unittest.TestCase):
    """m9: rate file uses a unique mkstemp + fsyncs, best-effort."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rate_path = Path(self.temp.name) / "rate.json"
        self.env = mock.patch.dict(os.environ, {
            telegram.RATE_PATH_ENV: str(self.rate_path)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_round_trip_no_tmp_leftovers_mode_0600(self):
        state = {"senders": {"111": [1.0, 2.0]}, "chats": {"-777": [3.0]}}
        telegram._save_rate_state(state)
        self.assertEqual(telegram._load_rate_state(), state)
        leftovers = [p for p in Path(self.temp.name).iterdir()
                     if ".tmp." in p.name]
        self.assertEqual(leftovers, [])
        mode = stat.S_IMODE(os.stat(self.rate_path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_save_failure_is_best_effort(self):
        with mock.patch.object(
            tempfile, "mkstemp", side_effect=OSError("no tmp")):
            # Must not raise: a failed rate save only disables
            # throttling, never the intake.
            telegram._save_rate_state({"senders": {}, "chats": {}})
        self.assertFalse(self.rate_path.exists())


if __name__ == "__main__":
    unittest.main()
