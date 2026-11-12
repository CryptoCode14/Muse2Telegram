"""SQLite delivery ledger: at-least-once delivery with evidence.

Pattern source: Hermes ``gateway/delivery_ledger.py`` (durable
delivery-obligation ledger in ``state.db``: ``pending`` before any
send, ``attempting`` right before the await, ``delivered`` only on
success, ``failed`` on definitive rejection; crash semantics are
explicitly at-least-once; ``attempting`` rows are redelivered with a
visible recovered marker). This is our own Python implementation
(stdlib ``sqlite3`` only); it does not import Hermes code.

The ledger records evidence, it does not decide delivery. It NEVER
auto-DONEs a sending item: a row left in ``attempting`` by a crashed
process stays ``attempting`` until a sweep reclaims it (dead owner
only) and the caller re-sends and marks the outcome itself.

Sweep rules (``sweep_recoverable``):
- A row owned by a LIVE pid is never claimed, whatever its age.
- A row owned by a dead pid is recoverable (attempts < max_attempts).
- A pid that has been recycled is detected by comparing the recorded
  ``owner_started_at`` against the live process start time (Linux
  ``/proc``); a mismatch means the recorded owner is gone.
- When no pid information is available at all (no owner recorded, or
  ``/proc`` unavailable), a row is recoverable only if it is
  ``pending`` with attempts left, or ``attempting`` and older than
  ``stale_after_seconds``.
- Each swept row gets ``attempts + 1`` and ``"recovered": True`` in the
  returned dict. The caller is responsible for sending it with the
  ``RECOVERED_MARKER`` prefix so the recipient can see it may be a
  repeat.
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid
from pathlib import Path

MAX_ATTEMPTS = 3
STALE_AFTER_SECONDS = 24 * 60 * 60  # 24 h, Hermes default
RETENTION_SECONDS = 7 * 24 * 60 * 60  # delivered rows kept 7 days
MAX_ROWS = 500

RECOVERED_MARKER = (
    "Recovered reply \u2014 the bridge restarted while sending; "
    "this may be a repeat."
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_obligations (
    obligation_id TEXT PRIMARY KEY,
    session_key TEXT,
    platform TEXT,
    chat_id INTEGER,
    thread_id INTEGER,
    content TEXT,
    state TEXT,
    attempts INTEGER,
    created_at REAL,
    updated_at REAL,
    owner_pid INTEGER,
    owner_started_at REAL,
    last_error TEXT,
    adapter_profile TEXT
)
"""


def _clock_ticks_per_second() -> int:
    try:
        return os.sysconf("SC_CLK_TCK")
    except (AttributeError, ValueError, OSError):
        return 100


def _boot_time() -> float | None:
    """Seconds-since-epoch the machine booted, from /proc/stat (Linux)."""
    try:
        with open("/proc/stat", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except OSError:
        return None
    return None


def process_start_time(pid: int) -> float | None:
    """Start time (epoch seconds) of ``pid``, or None if unavailable.

    Linux only, via /proc/<pid>/stat field 22 (starttime, clock ticks
    since boot) plus the boot time. Returns None on any other platform
    or when the process does not exist.
    """
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8") as handle:
            stat = handle.read()
    except (OSError, ValueError):
        return None
    # comm may contain spaces/parens; starttime is the field after the
    # closing paren, index 19 in the remainder (field 22 overall).
    try:
        rest = stat[stat.rindex(")") + 1:].split()
        start_ticks = int(rest[19])
    except (ValueError, IndexError):
        return None
    boot = _boot_time()
    if boot is None:
        return None
    return boot + start_ticks / _clock_ticks_per_second()


def pid_alive(pid: int | None) -> bool:
    """True if ``pid`` exists (os.kill(pid, 0) probe)."""
    if pid is None:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Exists, just not ours.
    except (OSError, ValueError, OverflowError):
        return False
    return True


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


class DeliveryLedger:
    """Single-writer SQLite ledger of delivery obligations (WAL mode)."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- lifecycle -------------------------------------------------------

    def record_obligation(self, *, session_key: str, platform: str,
                          chat_id: int, thread_id: int | None = None,
                          content: str, adapter_profile: str | None = None,
                          obligation_id: str | None = None,
                          created_at: float | None = None) -> str:
        """Insert a ``pending`` obligation BEFORE any send; returns its id."""
        now = time.time() if created_at is None else created_at
        oid = obligation_id or uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO delivery_obligations "
            "(obligation_id, session_key, platform, chat_id, thread_id, "
            " content, state, attempts, created_at, updated_at, "
            " owner_pid, owner_started_at, last_error, adapter_profile) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, NULL, NULL, NULL, ?)",
            (oid, session_key, platform, int(chat_id), thread_id, content,
             now, now, adapter_profile),
        )
        self._conn.commit()
        return oid

    def get(self, obligation_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM delivery_obligations WHERE obligation_id = ?",
            (obligation_id,),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def mark_attempting(self, obligation_id: str, *, owner_pid: int | None = None,
                        owner_started_at: float | None = None) -> bool:
        """Mark right before the send await. Defaults to this process."""
        pid = os.getpid() if owner_pid is None else owner_pid
        started = (
            process_start_time(pid)
            if owner_started_at is None else owner_started_at
        )
        cur = self._conn.execute(
            "UPDATE delivery_obligations SET state='attempting', "
            "owner_pid=?, owner_started_at=?, updated_at=? "
            "WHERE obligation_id=?",
            (pid, started, time.time(), obligation_id),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def mark_delivered(self, obligation_id: str) -> bool:
        """Mark only on confirmed send success."""
        cur = self._conn.execute(
            "UPDATE delivery_obligations SET state='delivered', "
            "owner_pid=NULL, owner_started_at=NULL, updated_at=? "
            "WHERE obligation_id=?",
            (time.time(), obligation_id),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def mark_failed(self, obligation_id: str, error: str = "") -> bool:
        """Mark on definitive rejection (not on timeout/unknown outcome)."""
        cur = self._conn.execute(
            "UPDATE delivery_obligations SET state='failed', last_error=?, "
            "owner_pid=NULL, owner_started_at=NULL, updated_at=? "
            "WHERE obligation_id=?",
            (str(error), time.time(), obligation_id),
        )
        self._conn.commit()
        return cur.rowcount == 1

    # -- recovery --------------------------------------------------------

    def sweep_recoverable(self, now: float | None = None, *,
                          max_attempts: int = MAX_ATTEMPTS,
                          stale_after_seconds: int = STALE_AFTER_SECONDS) -> list[dict]:
        """Claim rows safe to redeliver. Never claims a live owner's row.

        Each returned dict is the row plus ``"recovered": True``; the
        row's ``attempts`` is incremented and its owner cleared, its
        ``state`` left untouched so the caller can see whether it was
        ``pending`` (never started, redeliver plainly) or ``attempting``
        (may have delivered, redeliver WITH ``RECOVERED_MARKER``).
        Rows at/past ``max_attempts`` are left alone (exhausted).
        """
        now = time.time() if now is None else now
        rows = self._conn.execute(
            "SELECT * FROM delivery_obligations "
            "WHERE state IN ('pending', 'attempting') ORDER BY created_at"
        ).fetchall()
        recovered: list[dict] = []
        for row in rows:
            data = _row_to_dict(row)
            if data["attempts"] >= max_attempts:
                continue  # Exhausted; operator evidence, not auto-retried.
            owner_pid = data["owner_pid"]
            if owner_pid is not None:
                if pid_alive(owner_pid):
                    recorded = data["owner_started_at"]
                    actual = process_start_time(owner_pid) if recorded is not None else None
                    if actual is None or abs(actual - recorded) <= 2.0:
                        continue  # Live owner (or start time unverifiable).
                    # Pid recycled: the recorded owner is gone.
                # Dead owner: recoverable.
            else:
                # No pid information: only the conservative rule.
                if data["state"] == "attempting" and (
                    now - data["updated_at"] <= stale_after_seconds
                ):
                    continue
                # state == "pending" (never started) is recoverable.
            self._conn.execute(
                "UPDATE delivery_obligations SET attempts=attempts+1, "
                "owner_pid=NULL, owner_started_at=NULL, updated_at=? "
                "WHERE obligation_id=?",
                (now, data["obligation_id"]),
            )
            data["attempts"] += 1
            data["owner_pid"] = None
            data["owner_started_at"] = None
            data["updated_at"] = now
            data["recovered"] = True
            recovered.append(data)
        self._conn.commit()
        return recovered

    def prune(self, now: float | None = None, *,
              retention_seconds: int = RETENTION_SECONDS,
              max_rows: int = MAX_ROWS) -> int:
        """Delete delivered rows older than retention, then cap the table.

        Only ``delivered`` rows are ever deleted; pending/attempting/
        failed rows are evidence and are kept even past ``max_rows``.
        Returns the number of rows deleted.
        """
        now = time.time() if now is None else now
        cur = self._conn.execute(
            "DELETE FROM delivery_obligations WHERE state='delivered' "
            "AND updated_at < ?",
            (now - retention_seconds,),
        )
        deleted = cur.rowcount
        total = self._conn.execute(
            "SELECT COUNT(*) FROM delivery_obligations"
        ).fetchone()[0]
        while total > max_rows:
            victim = self._conn.execute(
                "SELECT obligation_id FROM delivery_obligations "
                "WHERE state='delivered' ORDER BY updated_at ASC LIMIT 1"
            ).fetchone()
            if victim is None:
                break
            self._conn.execute(
                "DELETE FROM delivery_obligations WHERE obligation_id=?",
                (victim[0],),
            )
            deleted += 1
            total -= 1
        self._conn.commit()
        return deleted
