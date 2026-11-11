"""Offline tests for Telegram media, both directions.

Inbound: photos and documents are downloaded and queued with a
marker carrying the saved file path — a photo with no caption is
queued, never silently dropped. Oversize or failed downloads are
queued with a note instead.
Outbound: --send-photo-and-close / --send-document-and-close send
one fenced media reply, record it and close the item, never
resending a confirmed send.

The Bot API fetch is stubbed; no network or credentials are used
except the local credential lookup inside the download helpers,
which the voice download tests already rely on.
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


def msg_update(uid, chat_id, chat_type, sender_id, text, username=None,
               is_bot=False, message_id=None):
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
    return {"update_id": uid, "message": message}


def photo_update(uid, chat_id, chat_type, sender_id, caption=None):
    message = {
        "message_id": uid,
        "from": {"id": sender_id, "is_bot": False},
        "chat": {"id": chat_id, "type": chat_type},
        "photo": [
            {"file_id": "photo-small", "width": 90, "height": 90,
             "file_size": 1000},
            {"file_id": "photo-mid", "width": 320, "height": 320,
             "file_size": 20000},
            {"file_id": "photo-big", "width": 1280, "height": 1280,
             "file_size": 300000},
        ],
        "date": 1700000000 + uid,
    }
    if caption is not None:
        message["caption"] = caption
    return {"update_id": uid, "message": message}


def document_update(uid, chat_id, chat_type, sender_id, caption=None,
                    file_name="report.pdf", file_id="doc-1"):
    message = {
        "message_id": uid,
        "from": {"id": sender_id, "is_bot": False},
        "chat": {"id": chat_id, "type": chat_type},
        "document": {"file_id": file_id, "file_name": file_name,
                     "mime_type": "application/pdf", "file_size": 5000},
        "date": 1700000000 + uid,
    }
    if caption is not None:
        message["caption"] = caption
    return {"update_id": uid, "message": message}


class FakeFetch:
    def __init__(self, updates=()):
        self.updates = list(updates)

    def __call__(self, offset):
        return {"ok": True, "result": list(self.updates)}


class FakeApi:
    def __call__(self, method, params, timeout=20.0):
        if method == "answerCallbackQuery":
            return {"ok": True, "result": True}
        raise AssertionError(f"unexpected Bot API call in test: {method}")


def run_cli(path, argv, fetch=None, stdin_text="", api_call=None,
            extra_env=None):
    out = io.StringIO()
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin_text)
    rate_path = Path(path).parent / "rate.json"
    env = {telegram.RATE_PATH_ENV: str(rate_path)}
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


class FakeDownloadResponse:
    """A fake urllib response that yields a fixed number of bytes."""

    def __init__(self, total_bytes, chunk=64 * 1024):
        self.remaining = total_bytes
        self.chunk = chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        if self.remaining <= 0:
            return b""
        take = min(self.remaining, self.chunk if size < 0 else size)
        self.remaining -= take
        return b"x" * take


class InboundMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "state.json"
        self.media = self.dir / "media"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def queue(self):
        return JsonStateStore(self.path).load()["queue"]

    def test_photo_with_no_caption_is_queued_not_dropped(self):
        fake_path = self.media / "1" / "photo.jpg"
        with mock.patch.object(
            telegram, "download_media_file",
            return_value=(fake_path, None),
        ) as dl:
            payload = run_cli(
                self.path, [],
                fetch=FakeFetch([photo_update(1, OWNER, "private", OWNER)]),
                extra_env={telegram.MEDIA_DIR_ENV: str(self.media)},
            )
        self.assertIsNotNone(payload)
        dl.assert_called_once_with("photo-big", 1)
        item = self.queue()[0]
        self.assertEqual(item["item_class"], queue_core.CLASS_REQUEST)
        self.assertTrue(item["text"].startswith("[photo]"))
        self.assertIn(str(fake_path), item["text"])
        # The largest size was downloaded, not a thumbnail.
        self.assertNotIn("photo-small", item["text"])

    def test_photo_with_caption_keeps_the_caption(self):
        fake_path = self.media / "1" / "photo.jpg"
        with mock.patch.object(
            telegram, "download_media_file",
            return_value=(fake_path, None),
        ):
            run_cli(
                self.path, [],
                fetch=FakeFetch([
                    photo_update(1, OWNER, "private", OWNER,
                                 caption="look at this"),
                ]),
                extra_env={telegram.MEDIA_DIR_ENV: str(self.media)},
            )
        item = self.queue()[0]
        self.assertTrue(item["text"].startswith("[photo]"))
        self.assertIn("look at this", item["text"])

    def test_document_is_queued_with_path_and_name(self):
        fake_path = self.media / "1" / "report.pdf"
        with mock.patch.object(
            telegram, "download_media_file",
            return_value=(fake_path, None),
        ) as dl:
            run_cli(
                self.path, [],
                fetch=FakeFetch([
                    document_update(1, OWNER, "private", OWNER),
                ]),
                extra_env={telegram.MEDIA_DIR_ENV: str(self.media)},
            )
        dl.assert_called_once_with("doc-1", 1, filename_hint="report.pdf")
        item = self.queue()[0]
        self.assertEqual(item["item_class"], queue_core.CLASS_REQUEST)
        self.assertTrue(item["text"].startswith("[document report.pdf]"))
        self.assertIn(str(fake_path), item["text"])

    def test_oversize_photo_is_queued_with_a_note_not_dropped(self):
        with mock.patch.object(
            telegram, "download_media_file",
            return_value=(None, "too-big"),
        ):
            run_cli(
                self.path, [],
                fetch=FakeFetch([photo_update(1, OWNER, "private", OWNER)]),
                extra_env={telegram.MEDIA_DIR_ENV: str(self.media)},
            )
        item = self.queue()[0]
        self.assertIn("larger than 20MB", item["text"])
        self.assertIn("smaller", item["text"])

    def test_failed_download_is_queued_not_dropped(self):
        with mock.patch.object(
            telegram, "download_media_file",
            return_value=(None, "failed"),
        ):
            run_cli(
                self.path, [],
                fetch=FakeFetch([
                    document_update(1, OWNER, "private", OWNER),
                ]),
                extra_env={telegram.MEDIA_DIR_ENV: str(self.media)},
            )
        item = self.queue()[0]
        self.assertIn("could not download", item["text"])

    def test_largest_photo_size_prefers_file_size(self):
        sizes = [
            {"file_id": "a", "width": 100, "height": 100, "file_size": 5},
            {"file_id": "b", "width": 50, "height": 50, "file_size": 900},
            {"file_id": "c", "width": 2000, "height": 2000},
        ]
        self.assertEqual(
            telegram._largest_photo_size(sizes)["file_id"], "b"
        )
        self.assertIsNone(telegram._largest_photo_size([]))


class MediaDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.media = self.dir / "media"

    def tearDown(self):
        self.tmp.cleanup()

    def _download(self, total_bytes, file_path="photos/file_1.jpg",
                  file_size=None, file_id="file-1", hint=None):
        result = {"ok": True, "result": {"file_path": file_path}}
        if file_size is not None:
            result["result"]["file_size"] = file_size
        with mock.patch.object(telegram, "api", return_value=result):
            with mock.patch.object(
                telegram, "_urlopen_no_redirect",
                return_value=FakeDownloadResponse(total_bytes),
            ):
                with mock.patch.dict(
                    os.environ, {telegram.MEDIA_DIR_ENV: str(self.media)}
                ):
                    return telegram.download_media_file(
                        file_id, 99, filename_hint=hint
                    )

    def test_download_refuses_oversize_file_by_reported_size(self):
        path, error = self._download(
            100, file_size=telegram.MAX_MEDIA_BYTES + 1
        )
        self.assertIsNone(path)
        self.assertEqual(error, "too-big")
        self.assertFalse((self.media / "99").exists())

    def test_download_refuses_oversize_stream(self):
        path, error = self._download(telegram.MAX_MEDIA_BYTES + 1)
        self.assertIsNone(path)
        self.assertEqual(error, "too-big")
        self.assertFalse((self.media / "99" / "file_1.jpg").exists())

    def test_download_writes_file_mode_0600_and_dirs_0700(self):
        # Unix permission bits only exist on POSIX. On Windows,
        # os.chmod does not set them, so this assertion cannot pass
        # there even though the download code is correct (the download
        # itself, the oversize refusal, and the filename sanitising are
        # covered by the other tests on every platform).
        if os.name != "posix":
            self.skipTest("Unix file-mode bits are POSIX-only")
        path, error = self._download(100)
        self.assertIsNone(error)
        self.assertEqual(path, self.media / "99" / "file_1.jpg")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(self.media.stat().st_mode & 0o777), "0o700")
        self.assertEqual(
            oct((self.media / "99").stat().st_mode & 0o777), "0o700"
        )

    def test_download_rejects_bad_file_path(self):
        for bad in (
            "../etc/passwd",
            "photos/../../etc/passwd",
            "/abs/path.jpg",
            "photos/file name.jpg",
            "photos/file.jpg?x=1",
        ):
            with self.subTest(path=bad):
                path, error = self._download(100, file_path=bad)
                self.assertIsNone(path)
                self.assertEqual(error, "failed")

    def test_document_filename_is_sanitized(self):
        path, error = self._download(
            100, file_path="documents/file_1.pdf",
            hint="../../etc/passwd",
        )
        self.assertIsNone(error)
        self.assertEqual(path.parent, self.media / "99")
        self.assertNotIn("..", str(path))
        self.assertTrue(path.exists())

    def test_safe_filename_falls_back_to_file_id(self):
        self.assertEqual(
            telegram._safe_media_filename("", "abc123", ".jpg"),
            "abc123.jpg",
        )
        self.assertEqual(
            telegram._safe_media_filename("a/b/c.pdf", "x"), "c.pdf"
        )


class OutboundMediaTests(unittest.TestCase):
    """Fenced media sends: same fence as text, one send, closed."""

    def setUp(self):
        # The media file must live under the (real) home directory
        # for the path guard, so this class's temp dir is made there.
        self.tmp = tempfile.TemporaryDirectory(dir=str(Path.home()))
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "state.json"
        self.calls = self.dir / "send-calls.log"
        self.photo = self.dir / "pic.jpg"
        self.photo.write_bytes(b"fake-jpeg-bytes")
        self.doc = self.dir / "notes.pdf"
        self.doc.write_bytes(b"fake-pdf-bytes")
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

    def claim(self, text="look at this", uid=1):
        payload = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(uid, OWNER, "private", OWNER, text),
            ]),
        )
        self.assertIsNotNone(payload)
        return payload

    def item(self, external_id):
        return next(
            i for i in JsonStateStore(self.path).load()["queue"]
            if i["external_id"] == str(external_id)
        )

    def test_photo_send_closes_the_item_in_one_call(self):
        payload = self.claim()
        cmd = self.write_send_cmd()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--send-photo-and-close", "1", payload["claim_token"],
                 str(OWNER), str(self.photo)],
                stdin_text="here it is",
            )
        self.assertEqual(
            result, {"ok": True, "message_id": "4242", "covered": []}
        )
        calls = self.calls.read_text(encoding="utf-8")
        self.assertEqual(calls.count("argv="), 1)
        self.assertIn(
            f"argv=['--photo', '{OWNER}', '{self.photo}']", calls
        )
        self.assertIn("stdin=here it is", calls)
        item = self.item(1)
        self.assertEqual(item["status"], "done")
        self.assertEqual(item["reply_message_id"], "4242")

    def test_document_send_with_cover_closes_both(self):
        first = run_cli(
            self.path, [],
            fetch=FakeFetch([
                msg_update(1, OWNER, "private", OWNER, "send the file"),
                msg_update(2, OWNER, "private", OWNER, "please"),
            ]),
        )
        cmd = self.write_send_cmd()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            result = run_cli(
                self.path,
                ["--send-document-and-close", "1", first["claim_token"],
                 str(OWNER), str(self.doc), "--cover", "2"],
                stdin_text="",
            )
        self.assertEqual(
            result, {"ok": True, "message_id": "4242", "covered": [2]}
        )
        self.assertEqual(self.calls.read_text(encoding="utf-8").count("argv="), 1)
        for external_id in ("1", "2"):
            self.assertEqual(self.item(external_id)["status"], "done")

    def test_media_send_refuses_wrong_chat_and_reply_target(self):
        payload = self.claim()
        cmd = self.write_send_cmd()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd}):
            with mock.patch.object(
                telegram, "_run_send_command"
            ) as sender:
                result = telegram.send_media_fenced(
                    JsonStateStore(self.path), 1, payload["claim_token"],
                    "999", "photo", str(self.photo), "cap",
                )
                self.assertEqual(result, {"ok": False, "stage": "fence"})
                result = telegram.send_media_fenced(
                    JsonStateStore(self.path), 1, payload["claim_token"],
                    str(OWNER), "photo", str(self.photo), "cap",
                    reply_to=14,
                )
                self.assertEqual(result, {"ok": False, "stage": "fence"})
                sender.assert_not_called()
        self.assertEqual(self.item(1)["status"], "claimed")

    def test_media_send_refuses_keyboard_on_plain_chat(self):
        payload = self.claim()
        with mock.patch.dict(
            os.environ, {telegram.SEND_CMD_ENV: "/fake/sender"}
        ):
            with mock.patch.object(telegram, "_run_send_command") as sender:
                result = telegram.send_media_fenced(
                    JsonStateStore(self.path), 1, payload["claim_token"],
                    str(OWNER), "photo", str(self.photo), "cap",
                    keyboard=True,
                )
        self.assertEqual(result, {"ok": False, "stage": "keyboard"})
        sender.assert_not_called()
        self.assertEqual(self.item(1)["status"], "claimed")

    def test_media_send_refuses_path_outside_home_and_missing_file(self):
        payload = self.claim()
        with mock.patch.object(telegram, "_run_send_command") as sender:
            for bad_path in ("/etc/passwd", str(self.dir / "nope.jpg")):
                result = telegram.send_media_fenced(
                    JsonStateStore(self.path), 1, payload["claim_token"],
                    str(OWNER), "photo", bad_path, "cap",
                )
                self.assertEqual(result, {"ok": False, "stage": "input"})
            sender.assert_not_called()
        self.assertEqual(self.item(1)["status"], "claimed")

    def test_media_send_refuses_oversize_caption(self):
        payload = self.claim()
        with mock.patch.object(telegram, "_run_send_command") as sender:
            result = telegram.send_media_fenced(
                JsonStateStore(self.path), 1, payload["claim_token"],
                str(OWNER), "photo", str(self.photo),
                "x" * (telegram.MAX_MEDIA_CAPTION_CHARS + 1),
            )
        self.assertEqual(result, {"ok": False, "stage": "input"})
        sender.assert_not_called()


if __name__ == "__main__":
    unittest.main()
