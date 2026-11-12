"""SQLite outbox for the P1.5 dispatcher (OUTBOX-FIRST delivery).

The agent writes its reply here, dispatches it itself with
``--dispatch-outbox CLAIM_TOKEN`` so it goes out in the turn,
and closes the turn; the poll script
(``bridge_core.telegram.dispatch_outbox``, called from
``run_once``) is the backstop that delivers any entry the
turn never dispatched, retries, and records
``reply_message_id`` before marking the queue item done.
Delivery never needs a turn.

Path: next to state.json — ``<state stem>.outbox.sqlite3`` (see
``telegram.outbox_path``; ``TELEGRAM_BRIDGE_OUTBOX_PATH`` overrides
it for tests). The file holds reply text (private conversations),
so it is created mode 0600 in the state directory (already 0700).

Schema (table ``outbox``):

  id                  INTEGER PRIMARY KEY AUTOINCREMENT — entry id,
                      returned by --outbox
  claim_token         TEXT NOT NULL — the claim token that fenced
                      the write (same rules as --reply: stale token
                      or already-done items refuse the write)
  target_update_id    INTEGER NOT NULL — primary queue item
                      (external_id)
  chat_id             INTEGER NOT NULL — destination chat, bound at
                      write time to the queued item's chat
  reply_to_message_id INTEGER — thread target, the primary item's
                      own message_id, or NULL for a standalone reply
  keyboard            INTEGER NOT NULL DEFAULT 0 — attach the
                      command keyboard (command replies only)
  text                TEXT NOT NULL — the agent's reply text (1-4096
                      chars, validated at write time)
  extra_update_ids    TEXT NOT NULL DEFAULT '[]' — JSON list of the
                      other batch items this one reply covers
  status              TEXT NOT NULL DEFAULT 'pending' — pending |
                      sending | delivered | failed | unknown |
                      superseded (see below)
  attempts            INTEGER NOT NULL DEFAULT 0 — delivery attempts
                      (incremented when an attempt starts)
  created_at          REAL NOT NULL — epoch seconds of the write
  next_attempt_at     REAL — earliest next delivery attempt
                      (retryable backoff); NULL means due now
  delivered_at        REAL — epoch seconds the send was confirmed
  reply_message_id    TEXT — Telegram message id, recorded BEFORE
                      the queue item is marked done
  last_error          TEXT — short reason for the last failed /
                      superseded / unknown outcome
  retry_after         REAL — last Retry-After seconds the sender
                      reported (429 only)

Statuses:

  pending — queued, due when ``next_attempt_at`` is NULL or past.
  sending — a dispatcher claimed this attempt (CAS on
    status='pending'); another dispatcher that finds it here
    leaves it alone.
  delivered — the send was confirmed; ``reply_message_id`` and
    ``delivered_at`` are set. Terminal.
  failed — the sender definitively rejected the send (exit 2:
    400/403/404 — provably not delivered), the entry was refused
    locally (keyboard rule), or the operator dropped it
    (--outbox-drop). Terminal, never retried.
  unknown — the send outcome is unknown (sender exit 1, timeout,
    exit 0 without a message id): the send may have gone out, so
    it is NEVER resent blindly. Terminal, for the operator to
    reconcile (the queue item stays in "sending" the same way).
  superseded — the queued reply is no longer needed: the queue
    item was already answered, closed, or re-claimed under another
    token, or the reply aged past the fence. Terminal; the item's
    own fate decides what happens next (a recycled item is
    re-woken and answered by a fresh turn).

Only the dispatcher writes delivery outcomes; only the agent's
--outbox write inserts rows. Every status transition is a single
SQLite statement guarded on the expected prior status, so two
dispatchers can never deliver the same entry twice.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

#: Live statuses: an entry in one of these blocks a second write
#: for the same queue item (see enqueue). Terminal statuses do
#: not block: a failed entry's item is pending again for a fresh
#: turn, and a delivered/unknown/superseded entry's item is
#: already answered, reconciled, or re-owned.
LIVE_STATUSES = ("pending", "sending")

#: Terminal statuses: the entry will never be attempted again.
TERMINAL_STATUSES = ("delivered", "failed", "unknown", "superseded")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  claim_token TEXT NOT NULL,
  target_update_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  reply_to_message_id INTEGER,
  keyboard INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL,
  extra_update_ids TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  next_attempt_at REAL,
  delivered_at REAL,
  reply_message_id TEXT,
  last_error TEXT,
  retry_after REAL
);
CREATE INDEX IF NOT EXISTS outbox_due_idx
  ON outbox (status, next_attempt_at, created_at);
"""


def _row_to_entry(row: sqlite3.Row) -> dict:
    entry = dict(row)
    entry["keyboard"] = bool(entry["keyboard"])
    try:
        extras = json.loads(entry["extra_update_ids"] or "[]")
    except ValueError:
        extras = []
    entry["extra_update_ids"] = [
        int(value) for value in extras if isinstance(value, int)
    ] if isinstance(extras, list) else []
    return entry


class OutboxStore:
    """One SQLite outbox file. Connections are short-lived (one
    per operation) with autocommit; the multi-statement
    transitions run inside explicit BEGIN IMMEDIATE so the
    check-and-write is atomic across processes."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        existed = self.path.exists()
        conn = sqlite3.connect(
            str(self.path), timeout=5.0, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        if not existed:
            # Reply text is private conversation: the file is
            # created mode 0600 like the state store's temp files.
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        return conn

    def enqueue(self, entry: dict) -> int | None:
        """Insert one entry. Returns the new row id, or None when
        a live entry (pending/sending) already covers any of the
        same queue items — the second write is refused, never
        queued alongside, so one batch can never be delivered
        twice. The check and the insert run in one BEGIN
        IMMEDIATE transaction."""
        target = int(entry["target_update_id"])
        extras = [int(v) for v in entry.get("extra_update_ids") or []]
        covered = [target] + [v for v in extras if v != target]
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                live = conn.execute(
                    "SELECT target_update_id, extra_update_ids FROM outbox "
                    "WHERE status IN ('pending', 'sending')"
                ).fetchall()
                for row in live:
                    other = {int(row["target_update_id"])}
                    try:
                        parsed = json.loads(row["extra_update_ids"] or "[]")
                    except ValueError:
                        parsed = []
                    if isinstance(parsed, list):
                        other.update(
                            int(v) for v in parsed if isinstance(v, int)
                        )
                    if other & set(covered):
                        conn.execute("ROLLBACK")
                        return None
                cursor = conn.execute(
                    "INSERT INTO outbox (claim_token, target_update_id, "
                    "chat_id, reply_to_message_id, keyboard, text, "
                    "extra_update_ids, status, attempts, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)",
                    (
                        entry["claim_token"],
                        target,
                        int(entry["chat_id"]),
                        entry.get("reply_to_message_id"),
                        1 if entry.get("keyboard") else 0,
                        entry["text"],
                        json.dumps(covered[1:]),
                        float(entry["created_at"]),
                    ),
                )
                conn.execute("COMMIT")
                return int(cursor.lastrowid)
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        finally:
            conn.close()

    def due_entries(self, now: float, limit: int,
                    claim_token: str | None = None) -> list:
        """Pending entries due for delivery (``next_attempt_at``
        NULL or past), oldest first, at most ``limit``. With a
        claim token, only that token's entries: the agent's
        self-dispatch after an --outbox write delivers its own
        reply in the turn, never another batch's."""
        conn = self._connect()
        try:
            if claim_token is None:
                rows = conn.execute(
                    "SELECT * FROM outbox WHERE status = 'pending' "
                    "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                    "ORDER BY created_at ASC, id ASC LIMIT ?",
                    (float(now), int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM outbox WHERE status = 'pending' "
                    "AND claim_token = ? "
                    "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                    "ORDER BY created_at ASC, id ASC LIMIT ?",
                    (str(claim_token), float(now), int(limit)),
                ).fetchall()
            return [_row_to_entry(row) for row in rows]
        finally:
            conn.close()

    def live_entries(self) -> list:
        """All pending/sending entries, oldest first (the
        dispatcher and the write fence read these)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM outbox "
                "WHERE status IN ('pending', 'sending') "
                "ORDER BY created_at ASC, id ASC"
            ).fetchall()
            return [_row_to_entry(row) for row in rows]
        finally:
            conn.close()

    def get(self, entry_id: int) -> dict | None:
        """One entry by id, or None when there is no such entry."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM outbox WHERE id = ?", (int(entry_id),)
            ).fetchone()
            return _row_to_entry(row) if row is not None else None
        finally:
            conn.close()

    def list_entries(self, limit: int = 100) -> list:
        """All entries, newest first (the --outbox-status view)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM outbox "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [_row_to_entry(row) for row in rows]
        finally:
            conn.close()

    def mark_sending(self, entry_id: int) -> bool:
        """Claim one delivery attempt: pending -> sending, with
        the attempt count incremented. The CAS on status='pending'
        means exactly one dispatcher owns the attempt; False when
        another dispatcher (or a terminal transition) got there
        first."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    "UPDATE outbox SET status = 'sending', "
                    "attempts = attempts + 1 WHERE id = ? "
                    "AND status = 'pending'",
                    (int(entry_id),),
                )
                if cursor.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return False
                conn.execute("COMMIT")
                return True
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        finally:
            conn.close()

    def mark_delivered(self, entry_id: int, message_id, now: float) -> bool:
        """Record a confirmed send: sending -> delivered, with
        the Telegram message id. The dispatcher calls this BEFORE
        marking the queue item done, so a crash between the two
        leaves the reply recoverable from the outbox instead of
        lost."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE outbox SET status = 'delivered', "
                "reply_message_id = ?, delivered_at = ? "
                "WHERE id = ? AND status = 'sending'",
                (str(message_id), float(now), int(entry_id)),
            )
            return cursor.rowcount == 1
        finally:
            conn.close()

    def mark_failed(self, entry_id: int, error: str) -> bool:
        """Terminal failure: the send provably did not go out
        (definitive rejection, keyboard refusal, operator drop).
        Never retried. True when the entry was live."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE outbox SET status = 'failed', last_error = ? "
                "WHERE id = ? AND status IN ('pending', 'sending')",
                (str(error)[:500], int(entry_id)),
            )
            return cursor.rowcount == 1
        finally:
            conn.close()

    def mark_unknown(self, entry_id: int, error: str) -> bool:
        """Terminal unknown outcome: the send may have gone out,
        so it is never resent blindly. True when the entry was
        live."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE outbox SET status = 'unknown', last_error = ? "
                "WHERE id = ? AND status IN ('pending', 'sending')",
                (str(error)[:500], int(entry_id)),
            )
            return cursor.rowcount == 1
        finally:
            conn.close()

    def mark_superseded(self, entry_id: int, error: str) -> bool:
        """Terminal supersede: the queued reply is no longer
        needed (already answered, closed, or re-owned). True when
        the entry was live."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE outbox SET status = 'superseded', last_error = ? "
                "WHERE id = ? AND status IN ('pending', 'sending')",
                (str(error)[:500], int(entry_id)),
            )
            return cursor.rowcount == 1
        finally:
            conn.close()

    def requeue(self, entry_id: int, next_attempt_at: float,
                retry_after, error: str) -> bool:
        """Back to pending for a later attempt: sending ->
        pending with the next-attempt time (retryable backoff, or
        the short defer delay). True when the entry was sending."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE outbox SET status = 'pending', "
                "next_attempt_at = ?, retry_after = ?, last_error = ? "
                "WHERE id = ? AND status = 'sending'",
                (
                    float(next_attempt_at),
                    None if retry_after is None else float(retry_after),
                    str(error)[:500],
                    int(entry_id),
                ),
            )
            return cursor.rowcount == 1
        finally:
            conn.close()
