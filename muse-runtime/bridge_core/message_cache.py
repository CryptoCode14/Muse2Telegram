"""Disk-backed Telegram message cache.

Pattern source: OpenClaw ``extensions/telegram/src/message-cache.ts``
(bounded in-memory map + plugin-state persistence, no TTL, size-bounded,
keyed by chat + message id). This is our own Python implementation for
the Muse2Telegram hook bridge; it does not import OpenClaw code and it
never talks to Telegram.

Why it exists: the Bot API has no arbitrary ``getMessage``, so reply
context can only come from messages this bridge has itself observed.
Recording every observed message lets later turns rebuild reply chains
and group windows without re-reading Telegram.

Persistence mirrors ``store.py``: mode 0600 temp file plus atomic
``os.replace``. Unlike the state store, a corrupt cache file is *not*
fatal: the cache is derived data that re-accumulates from observed
messages, so a bad file starts empty instead of wedging the bridge.

All methods are pure/deterministic over recorded data; nothing here
touches the network.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

DEFAULT_MAX_MEMORY = 5000
DEFAULT_MAX_PERSISTENT = 3000
REPLY_CHAIN_MAX_DEPTH = 4
DM_HISTORY_LIMIT = 10
GROUP_WINDOW_LIMIT = 50


def _as_int(value):
    """Coerce a Telegram id to int when it looks like one, else keep it."""
    if isinstance(value, bool):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _format_line(record: dict) -> str:
    sender = record.get("sender_id")
    sender_text = "unknown" if sender is None else str(sender)
    return f"[{record['message_id']}] {sender_text}: {record.get('text', '')}"


class MessageCache:
    """Bounded in-memory + JSON-persisted cache of observed messages.

    ``store_path`` is the JSON file. In-memory cap ``max_memory``
    (default 5000, OpenClaw's in-memory bound) and persistent cap
    ``max_persistent`` (default 3000, OpenClaw's persisted bound);
    both evict oldest first.
    """

    def __init__(self, store_path: str | Path, max_memory: int = DEFAULT_MAX_MEMORY,
                 max_persistent: int = DEFAULT_MAX_PERSISTENT):
        self.store_path = Path(store_path)
        self.store_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_memory = max_memory
        self.max_persistent = max_persistent
        # Insertion-ordered: dict preserves first-insertion order, and a
        # re-record updates in place without moving the entry.
        self._by_key: dict[tuple, dict] = {}
        self._load()

    # -- persistence ---------------------------------------------------

    def _load(self) -> None:
        if not self.store_path.exists():
            return
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Derived data: a corrupt cache starts empty, it never wedges
            # the bridge the way corrupt queue state would.
            return
        if isinstance(raw, dict):
            records = raw.get("messages", [])
        elif isinstance(raw, list):
            records = raw
        else:
            return
        if not isinstance(records, list):
            return
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                key = (_as_int(record["chat_id"]), _as_int(record["message_id"]))
            except KeyError:
                continue
            self._by_key[key] = record
        self._trim_memory()

    def _save(self) -> None:
        # Persist only the newest max_persistent records.
        records = list(self._by_key.values())[-self.max_persistent:] if self.max_persistent > 0 else []
        raw = json.dumps({"version": 1, "messages": records}, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(
            dir=self.store_path.parent, prefix=f"{self.store_path.name}.tmp."
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.store_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # -- internals -----------------------------------------------------

    def _trim_memory(self) -> None:
        while len(self._by_key) > self.max_memory:
            oldest = next(iter(self._by_key))
            del self._by_key[oldest]

    def _chat_records(self, chat_id) -> list[dict]:
        chat_id = _as_int(chat_id)
        return [r for (c, _), r in self._by_key.items() if c == chat_id]

    # -- public API ------------------------------------------------------

    def record(self, chat_id, message_id, text, *, sender_id=None,
               reply_to_message_id=None, timestamp=None,
               chat_type: str = "private") -> dict:
        """Record one observed message; returns the stored record.

        Re-recording the same (chat_id, message_id) updates it in place
        and keeps its original received position.
        """
        key = (_as_int(chat_id), _as_int(message_id))
        record = {
            "chat_id": key[0],
            "message_id": key[1],
            "text": text,
            "sender_id": sender_id,
            "reply_to_message_id": (
                _as_int(reply_to_message_id)
                if reply_to_message_id is not None else None
            ),
            "timestamp": time.time() if timestamp is None else timestamp,
            "chat_type": chat_type,
        }
        self._by_key[key] = record
        self._trim_memory()
        self._save()
        return dict(record)

    def get(self, chat_id, message_id) -> dict | None:
        """Return a copy of the cached record, or None if never observed."""
        record = self._by_key.get((_as_int(chat_id), _as_int(message_id)))
        return dict(record) if record is not None else None

    def reply_chain(self, chat_id, message_id, depth: int = REPLY_CHAIN_MAX_DEPTH) -> list[dict]:
        """Ancestors of ``message_id`` following ``reply_to`` links.

        Oldest first, at most ``depth`` (default 4, OpenClaw's
        ``TELEGRAM_REPLY_CHAIN_MAX_DEPTH``). The message itself is not
        included; a missing or uncached link ends the chain. Cycle-safe.
        """
        chain: list[dict] = []
        visited: set = set()
        current = self.get(chat_id, message_id)
        while current is not None and len(chain) < depth:
            parent_id = current.get("reply_to_message_id")
            if parent_id is None or parent_id in visited:
                break
            visited.add(parent_id)
            parent = self.get(chat_id, parent_id)
            if parent is None:
                break
            chain.append(parent)
            current = parent
        chain.reverse()
        return [dict(r) for r in chain]

    def group_window(self, chat_id, *, limit: int = GROUP_WINDOW_LIMIT,
                     watermark=None) -> list[dict]:
        """Messages for a group chat after ``watermark``, received order.

        ``watermark`` is a timestamp (seconds since epoch); only messages
        with a strictly greater timestamp are returned. With no watermark
        the whole recorded window is eligible. At most the newest
        ``limit`` (default 50) are returned, in received order.
        """
        records = self._chat_records(chat_id)
        if watermark is not None:
            records = [r for r in records if r.get("timestamp", 0) > watermark]
        return [dict(r) for r in records[-limit:]] if limit > 0 else []

    def dm_history(self, chat_id, limit: int = DM_HISTORY_LIMIT) -> list[dict]:
        """Last ``limit`` messages for a DM (default 10), received order."""
        records = self._chat_records(chat_id)
        return [dict(r) for r in records[-limit:]] if limit > 0 else []

    def prune(self) -> int:
        """Enforce the memory/persistent caps; returns records dropped."""
        before = len(self._by_key)
        self._trim_memory()
        dropped = before - len(self._by_key)
        self._save()
        return dropped

    def build_context(self, chat_id, *, reply_to_message_id=None,
                      chat_type: str = "private") -> str:
        """Assemble ``[Reply chain]`` + recent-window text for one chat.

        The reply-chain section holds the reply target plus up to 4
        ancestors, oldest first. The window is the DM history (last 10)
        for private chats and the group window (last 50) otherwise.
        Messages already shown in the chain are not repeated in the
        window. Returns "" when there is nothing to show.
        """
        sections: list[str] = []
        chain_ids: set = set()
        if reply_to_message_id is not None:
            chain = self.reply_chain(chat_id, reply_to_message_id)
            target = self.get(chat_id, reply_to_message_id)
            nodes = chain + ([target] if target is not None else [])
            if nodes:
                chain_ids = {r["message_id"] for r in nodes}
                lines = "\n".join(_format_line(r) for r in nodes)
                sections.append(f"[Reply chain]\n{lines}")
        if chat_type == "private":
            window = self.dm_history(chat_id)
        else:
            window = self.group_window(chat_id)
        window = [r for r in window if r["message_id"] not in chain_ids]
        if window:
            lines = "\n".join(_format_line(r) for r in window)
            sections.append(f"[Recent messages]\n{lines}")
        return "\n".join(sections)
