"""Fake Telegram Bot API (in-memory, no network).

Used by the Muse2Telegram v2 simulation harness. Nothing here touches
api.telegram.org or any real token: all state lives in this process
(plus, optionally, a JSONL sends log written by the fake sender
subprocess — see fake_harness.FakeTurn).

Duplicate-send tracking: every send records the ``update_id`` it was
answering when the caller supplies one. ``duplicate_sends()`` counts
sends beyond the first for the same (chat_id, text, update_id) —
i.e. the same inbound message answered twice.
"""
from __future__ import annotations

import itertools
import json
import time
from collections import Counter
from pathlib import Path


class FakeTelegramError(RuntimeError):
    """Raised when a test wants the exception form of a Bot API error."""


def _bot_error(code, description):
    return {"ok": False, "error_code": code, "description": description}


class FakeTelegram:
    """In-memory stand-in for the Telegram Bot API.

    Message ids and update ids are sequential integers starting at 1
    (unless the caller seeds them). ``sends`` is the append-only
    record of every sendMessage call, in call order.
    """

    def __init__(self, *, first_message_id=1, first_update_id=1):
        self._next_message_id = itertools.count(first_message_id)
        self._next_update_id = itertools.count(first_update_id)
        self._next_injected_message_id = itertools.count(100000)
        self.updates = []          # injected updates, in injection order
        self.sends = []            # {"message_id","chat_id","text","reply_to","update_id","at"}
        self.messages = {}         # (chat_id, message_id) -> text (live, editable)
        self.typing_calls = []     # {"chat_id","action","at"}
        self.delete_calls = []     # {"chat_id","message_id","at"}
        self.api_calls = []        # (method, params) for every api_call dispatch
        self.get_updates_calls = []  # offsets requested

    # -- injection -----------------------------------------------------

    def inject_message(self, chat_id, text, *, sender_id, chat_type="private",
                       update_id=None, message_id=None, sender_name=None,
                       date=None):
        """Build + store one inbound message update; return the update dict."""
        uid = update_id if update_id is not None else next(self._next_update_id)
        mid = message_id if message_id is not None else next(self._next_injected_message_id)
        user = {"id": sender_id, "is_bot": False}
        if sender_name:
            user["first_name"] = sender_name
        update = {
            "update_id": uid,
            "message": {
                "message_id": mid,
                "from": user,
                "chat": {"id": chat_id, "type": chat_type},
                "text": text,
                "date": int(date if date is not None else time.time()),
            },
        }
        self.updates.append(update)
        return update

    def inject_update(self, update):
        """Store a caller-built update dict verbatim; return it."""
        self.updates.append(update)
        return update

    # -- Bot API methods -------------------------------------------------

    def getUpdates(self, offset=0, timeout=0):
        """Return updates with update_id >= offset, in order.

        ``timeout`` is accepted for signature parity with long
        polling but never sleeps: the harness drives time with a
        fake clock, so a real wait would only slow the suite.
        """
        self.get_updates_calls.append(offset)
        result = [u for u in self.updates
                  if isinstance(u.get("update_id"), int) and u["update_id"] >= offset]
        return {"ok": True, "result": result}

    def sendMessage(self, chat_id, text, reply_to=None, update_id=None):
        message_id = next(self._next_message_id)
        record = {
            "message_id": message_id,
            "chat_id": chat_id,
            "text": text,
            "reply_to": reply_to,
            "update_id": update_id,
            "at": time.time(),
        }
        self.sends.append(record)
        self.messages[(chat_id, message_id)] = text
        return {"ok": True, "result": {"message_id": message_id}}

    def editMessageText(self, chat_id, message_id, text):
        """Edit a sent message, with Telegram's real 400 behaviours.

        - Same text as the current text -> 400 "message is not
          modified" (the delivered evidence: the message exists and
          already carries this text, so a send DID go out).
        - Unknown message_id -> 400 "message to edit not found"
          (the never-sent evidence: nothing was ever delivered
          under that id).
        - Otherwise the text is replaced and ok is returned.
        """
        key = (chat_id, message_id)
        if key not in self.messages:
            return _bot_error(400, "Bad Request: message to edit not found")
        if self.messages[key] == text:
            return _bot_error(400, "Bad Request: message is not modified")
        self.messages[key] = text
        return {"ok": True, "result": True}

    def sendChatAction(self, chat_id, action="typing"):
        self.typing_calls.append(
            {"chat_id": chat_id, "action": action, "at": time.time()})
        return {"ok": True, "result": True}

    def deleteMessage(self, chat_id, message_id):
        self.delete_calls.append(
            {"chat_id": chat_id, "message_id": message_id, "at": time.time()})
        key = (chat_id, message_id)
        if key not in self.messages:
            return _bot_error(
                400, "Bad Request: message to delete not found")
        del self.messages[key]
        return {"ok": True, "result": True}

    # -- api_call adapter -------------------------------------------------
    # bridge_core.telegram functions take api_call(method, params,
    # timeout=...). This dispatches those calls onto the fake, so the
    # REAL ingest code can run against it.

    def api_call(self, method, params=None, timeout=20.0):
        params = dict(params or {})
        self.api_calls.append((method, params))
        if method == "getUpdates":
            return self.getUpdates(params.get("offset", 0),
                                   params.get("timeout", 0))
        if method == "sendMessage":
            return self.sendMessage(params["chat_id"], params.get("text", ""),
                                    reply_to=params.get("reply_to_message_id"))
        if method == "editMessageText":
            return self.editMessageText(params["chat_id"],
                                        params["message_id"], params["text"])
        if method == "sendChatAction":
            return self.sendChatAction(params["chat_id"],
                                       params.get("action", "typing"))
        if method == "deleteMessage":
            return self.deleteMessage(params["chat_id"], params["message_id"])
        if method == "getChatAdministrators":
            # The harness owner is the creator; no other admins.
            owner = params.get("_fake_owner_id", 111)
            return {"ok": True, "result": [
                {"user": {"id": owner}, "status": "creator"}]}
        if method == "getChatMemberCount":
            return {"ok": True, "result": 2}
        if method == "answerCallbackQuery":
            return {"ok": True, "result": True}
        raise FakeTelegramError(f"FakeTelegram: no stub for {method!r}")

    # -- inspection helpers ------------------------------------------------

    def sends_for(self, chat_id):
        return [s for s in self.sends if s["chat_id"] == chat_id]

    def find_send(self, chat_id, text):
        """First send matching (chat_id, text), or None."""
        for send in self.sends:
            if send["chat_id"] == chat_id and send["text"] == text:
                return send
        return None

    def duplicate_sends(self, update_id=None):
        """Count sends beyond the first per (chat_id, text, update_id).

        With update_id given, only that inbound message is counted.
        Only sends that were recorded with an update_id participate:
        a send with update_id=None cannot be attributed to one
        inbound message, so it is excluded rather than guessed at.
        """
        keyed = [s for s in self.sends if s.get("update_id") is not None]
        if update_id is not None:
            keyed = [s for s in keyed if s["update_id"] == update_id]
        counts = Counter((s["chat_id"], s["text"], s["update_id"])
                         for s in keyed)
        return sum(n - 1 for n in counts.values() if n > 1)

    def duplicate_sends_unattributed(self):
        """Sends beyond the first per (chat_id, text), ignoring update_id.

        Coarser check for callers that never pass update_id (e.g.
        the subprocess sender path, where the update id is not on
        the argv). Two genuinely different replies with identical
        text to the same chat count as a duplicate here — that is
        the conservative reading, and the scenarios that use it say
        so in their notes.
        """
        counts = Counter((s["chat_id"], s["text"]) for s in self.sends)
        return sum(n - 1 for n in counts.values() if n > 1)

    def reload_from_log(self, log_path):
        """Merge sends recorded by the fake sender subprocess.

        The subprocess writes one JSON object per line to ``log_path``
        (see fake_harness.write_fake_sender). Records already present
        in ``self.sends`` (matched by message_id) are skipped, so
        calling this repeatedly is safe. Returns new records merged.
        """
        path = Path(log_path)
        if not path.exists():
            return []
        known = {s["message_id"] for s in self.sends}
        merged = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") == "send" and rec.get("message_id") not in known:
                send = {
                    "message_id": rec["message_id"],
                    "chat_id": rec["chat_id"],
                    "text": rec["text"],
                    "reply_to": rec.get("reply_to"),
                    "update_id": rec.get("update_id"),
                    "at": rec.get("at", time.time()),
                }
                self.sends.append(send)
                self.messages[(send["chat_id"], send["message_id"])] = send["text"]
                known.add(send["message_id"])
                merged.append(send)
            elif rec.get("kind") == "typing":
                self.typing_calls.append(
                    {"chat_id": rec["chat_id"], "action": "typing",
                     "at": rec.get("at", time.time())})
        return merged
