#!/usr/bin/env python3
"""A2A endpoint server v2 for Westin's bots (Westin Jr., Gus, Dorian, Quinn).

Stdlib-only, Python 3.12+. What v2 changes vs v0 (see README.md):

  * TLS HANDSHAKE FIX (the 2026-11-16 bus hang): the listening socket is
    NEVER TLS-wrapped. Each accepted connection is wrapped in its own
    handler thread with do_handshake_on_connect=False and a handshake
    timeout (default 10s, env A2A_HANDSHAKE_TIMEOUT). A client that opens
    TCP and never completes the handshake now kills one handler thread
    only; accept() keeps running. Same hang killed Dex's port-8767
    server. Also: request_queue_size=128, daemon_threads=True, 30s
    socket timeouts, `Connection: close` on every response, and
    scanner-noise tracebacks suppressed to one log line.
  * /health/deep (no auth): real round-trip probe — Redis SET/GET if a
    Redis is reachable, else a write+read+delete probe file in the data
    dir — plus tasks.json readability, push seq, uptime, thread count,
    listen-queue estimate, and signing/rejection counters. It does real
    work, so it cannot report ok while the process is wedged.
  * HMAC signing + replay protection on message/send (verified BEFORE
    anything is stored): sig / key_id / ts / counter, domain tag
    "muse-a2a-v1", HMAC-SHA256 over ts + id + canonical JSON of the
    message without the signing fields. Secrets in secrets/<id>.key
    (0600), rotation via <id>.key.next. Unsigned messages are still
    ACCEPTED in compat mode (marked signed:false) unless
    --require-signing is given. Failures -> JSON-RPC -32002, logged
    (metadata only, never the payload) to rejected.jsonl.
  * Typed frames / state_ref / deltas: frozen envelope (closed 7-type
    vocab, mandatory to/from/id/task_id/type, ISO-8601 UTC timestamps,
    unix ts also accepted), optional state_ref + changed (delta fields
    only). state/ key-value store: JSON-RPC state/set + state/get,
    backed by state.json and mirrored to Redis when available, plus
    GET /state/<key>. to_toon/from_toon helpers for uniform tables.
    Payloads > ~2000 chars are still stored, but the client warns and
    suggests state/set + state_ref instead.
  * A2A v1.0: Agent Card v1 shape (protocolVersion 1.0, capabilities
    incl. state, securitySchemes bearer + hmac, skills with tags and
    examples), A2A-Version: 1.0 response header (0.1 still accepted),
    REST binding GET /tasks, GET /tasks/<id>, POST /tasks,
    POST /tasks/<id>/cancel. The 8 task states are unchanged.
    NOT implemented: v1.1 RC, SendLiveMessage, streaming.
  * Audit chain: audit.jsonl entries carry seq, prev_hash, chain_hash
    (SHA-256 over prev_hash + canonical entry JSON) and payload_hash
    (SHA-256 of the payload, not the payload itself). Full verbatim
    messages go to audit-full.jsonl ONLY with --full-audit. Old
    unchained lines verify as "legacy" (see audit_verify.py).

Everything v0 did is kept: Agent Card, /agents, /health, /events
long-poll + /push/status, JSON-RPC message/send, tasks/get, tasks/list,
tasks/cancel, file-backed tasks.json + audit.jsonl, Bearer key auth,
Fix 9 push rule (no re-wake on redundant confirms of terminal tasks).

Run:  python3 a2a_server_v2.py [--port PORT] [--host HOST]
        [--data-dir DIR] [--tls-cert C --tls-key K]
        [--require-signing] [--full-audit] [--redis-url URL]
The Bearer key file is created on first run if missing (0600). Never
commit it, never share it in chat; give it to a new bot over a secure
channel. Per-sender HMAC secrets are created with gen_pair_secret.py,
never by this server, never in code.
"""
import argparse
import csv
import hashlib
import hmac as hmac_mod
import io
import json
import os
import re
import secrets
import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

HERE = Path(__file__).resolve().parent

PERFORMATIVES = {"request", "inform", "propose", "confirm", "refuse", "cancel", "decide"}
TASK_STATES = {"submitted", "working", "input-required", "auth-required",
               "completed", "failed", "canceled", "rejected"}
TERMINAL_STATES = {"completed", "failed", "canceled", "rejected"}

BOT_NAME = os.environ.get("A2A_BOT_NAME", "westin-jr")
BOT_VERSION = "2.0.0"
PROTOCOL_VERSION = "1.0"
SCHEME = "http"

SIGN_DOMAIN = "muse-a2a-v1"
SIGN_MAX_SKEW_SECONDS = 300
SEEN_MAX = 10000
PAYLOAD_WARN_CHARS = 2000
PUSH_MAX_EVENTS = 1000
PUSH_WAIT_SECONDS = 25
HANDSHAKE_TIMEOUT_DEFAULT = 10.0
SOCKET_TIMEOUT = 30.0


def utcnow_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def canonical_json(obj) -> str:
    """Canonical JSON: sorted keys, tight separators, no ascii escapes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_ts(value):
    """Accept a unix-seconds number or an ISO-8601 string; return int or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            return int(float(s))
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except Exception:
            return None
    return None


# ---------------------------------------------------------------- TOON ----
def to_toon(rows):
    """Render a uniform table (list of dicts, identical keys) as TOON.

    Minimal TOON: first line is the column keys (once), each following
    line is one row, cells CSV-quoted. Strings are written raw (no
    extra quoting); numbers/bools/null/containers are JSON-encoded so
    they parse back to their types. Caveat, documented in README.md:
    a string cell that itself parses as JSON (e.g. "42", "true") comes
    back as that JSON value — if exact string typing matters for such
    cells, use a plain JSON payload instead.
    Raises ValueError on non-uniform / empty input.
    """
    if not isinstance(rows, list) or not rows:
        raise ValueError("toon needs a non-empty list of rows")
    keys = None
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("toon rows must be objects")
        if keys is None:
            keys = list(row.keys())
        elif list(row.keys()) != keys:
            raise ValueError("toon rows must all have identical keys")
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(keys)
    for row in rows:
        cells = []
        for k in keys:
            v = row[k]
            cells.append(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
        w.writerow(cells)
    return buf.getvalue()


def from_toon(text):
    """Inverse of to_toon: TOON text -> list of dicts."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty toon data")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ValueError("empty toon data")
    keys = rows[0]
    out = []
    for line in rows[1:]:
        if len(line) != len(keys):
            raise ValueError("toon row has wrong number of cells")
        row = {}
        for k, cell in zip(keys, line):
            try:
                row[k] = json.loads(cell)
            except Exception:
                row[k] = cell
        out.append(row)
    return out


def is_uniform_table(value):
    """True if value is a non-empty list of dicts with identical keys."""
    if not isinstance(value, list) or not value:
        return False
    if not all(isinstance(r, dict) for r in value):
        return False
    keys = list(value[0].keys())
    return all(list(r.keys()) == keys for r in value)


# ------------------------------------------------------- signing helpers ----
def signing_input(msg_id, ts, unsigned_msg) -> bytes:
    """Exact bytes that are HMACed. Client and server MUST agree on this.

    domain + "\\n" + ts + "\\n" + id + "\\n" + canonical JSON of the
    message WITHOUT sig/key_id/ts/counter.
    """
    return "\n".join([SIGN_DOMAIN, str(ts), str(msg_id),
                      canonical_json(unsigned_msg)]).encode("utf-8")


def sign_message(msg: dict, secret: bytes, ts=None, counter=None) -> dict:
    """Return a copy of msg with sig/key_id/ts/counter added."""
    out = dict(msg)
    if ts is None:
        ts = int(time.time())
    out["ts"] = int(ts)
    if counter is not None:
        out["counter"] = int(counter)
    out["key_id"] = msg.get("from")
    unsigned = {k: v for k, v in out.items()
                if k not in ("sig", "key_id", "ts", "counter")}
    digest = hmac_mod.new(secret, signing_input(out["id"], out["ts"], unsigned),
                          hashlib.sha256).hexdigest()
    out["sig"] = digest
    return out


# ------------------------------------------------------------ Redis (RESP) ----
def _resp_encode(*args) -> bytes:
    parts = [f"*{len(args)}\r\n".encode()]
    for a in args:
        b = a if isinstance(a, bytes) else str(a).encode()
        parts.append(f"${len(b)}\r\n".encode() + b + b"\r\n")
    return b"".join(parts)


def _resp_readline(f) -> bytes:
    line = f.readline()
    if not line:
        raise ConnectionError("redis closed connection")
    return line


def _resp_read(f):
    line = _resp_readline(f)
    kind, rest = line[:1], line[1:].strip()
    if kind == b"+":
        return rest.decode()
    if kind == b"-":
        raise ConnectionError("redis error: " + rest.decode(errors="replace"))
    if kind == b":":
        return int(rest)
    if kind == b"$":
        n = int(rest)
        if n < 0:
            return None
        data = f.read(n + 2)[:n]
        return data.decode(errors="replace")
    if kind == b"*":
        n = int(rest)
        if n < 0:
            return None
        return [_resp_read(f) for _ in range(n)]
    raise ConnectionError("redis: unexpected reply " + repr(line[:20]))


def parse_redis_url(url):
    """redis://[:password@]host:port/db -> (host, port, password, db)."""
    from urllib.parse import urlparse as _up
    u = _up(url or "")
    host = u.hostname or "127.0.0.1"
    port = u.port or 6379
    password = u.password
    db = 0
    if u.path and u.path.strip("/") != "":
        try:
            db = int(u.path.strip("/"))
        except ValueError:
            db = 0
    return host, port, password, db


def redis_command(redis_url, *args, timeout=0.5):
    """One-shot RESP command over a fresh socket. No redis dependency."""
    host, port, password, db = parse_redis_url(redis_url)
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.settimeout(timeout)
        f = sock.makefile("rb")
        if password:
            sock.sendall(_resp_encode("AUTH", password))
            _resp_read(f)
        if db:
            sock.sendall(_resp_encode("SELECT", db))
            _resp_read(f)
        sock.sendall(_resp_encode(*args))
        return _resp_read(f)
    finally:
        try:
            sock.close()
        except Exception:
            pass


def redis_roundtrip(redis_url, timeout=0.5):
    """Real round-trip: PING, SET probe, GET probe, DEL probe.

    Returns (ok, ms, error_or_None)."""
    t0 = time.time()
    try:
        if redis_command(redis_url, "PING", timeout=timeout) != "PONG":
            return False, (time.time() - t0) * 1000, "PING did not return PONG"
        probe_key = f"a2a:health:probe:{os.getpid()}:{int(t0)}"
        probe_val = f"probe-{t0:.6f}"
        redis_command(redis_url, "SET", probe_key, probe_val, "EX", "60", timeout=timeout)
        got = redis_command(redis_url, "GET", probe_key, timeout=timeout)
        redis_command(redis_url, "DEL", probe_key, timeout=timeout)
        if got != probe_val:
            return False, (time.time() - t0) * 1000, "GET did not return SET value"
        return True, (time.time() - t0) * 1000, None
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        return False, (time.time() - t0) * 1000, f"{type(exc).__name__}: {exc}"

# ------------------------------------------------------------------ State ----
class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class State:
    """All mutable server state, kept off module globals so tests can
    run several servers in-process with separate temp data dirs."""

    def __init__(self, data_dir, require_signing=False, full_audit=False,
                 redis_url=None, handshake_timeout=HANDSHAKE_TIMEOUT_DEFAULT):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.require_signing = require_signing
        self.full_audit = full_audit
        self.redis_url = redis_url or os.environ.get(
            "A2A_REDIS_URL", "redis://127.0.0.1:6379/0")
        self.handshake_timeout = float(handshake_timeout)
        self.started_at = time.time()
        self.listen_port = 0  # filled in by create_server / main

        self.tasks_file = self.data_dir / "tasks.json"
        self.audit_file = self.data_dir / "audit.jsonl"
        self.audit_full_file = self.data_dir / "audit-full.jsonl"
        self.rejected_file = self.data_dir / "rejected.jsonl"
        self.agents_file = self.data_dir / "agents.json"
        self.key_file = self.data_dir / "key"
        self.push_state_file = self.data_dir / "push_state.json"
        self.secrets_dir = self.data_dir / "secrets"
        self.secrets_dir.mkdir(exist_ok=True)
        try:
            os.chmod(self.secrets_dir, 0o700)
        except Exception:
            pass
        self.seen_file = self.data_dir / "seen.json"
        self.counters_file = self.data_dir / "counters.json"
        self.state_file = self.data_dir / "state.json"

        self.tasks_lock = threading.Lock()
        self.audit_lock = threading.Lock()
        self.signing_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self._push_lock = threading.Lock()
        self._push_cond = threading.Condition(self._push_lock)
        self._push_events = []
        self._push_seq = 0

        self.api_key = self._load_key()
        self.secrets = {}          # sender id -> [secret bytes, ...] (current + next)
        self.load_secrets()
        self.seen = {}             # message id -> ts (unix)
        self.counters = {}         # sender id -> last counter seen
        self._load_seen_counters()
        self.signed_accepted = 0
        self.unsigned_accepted = 0
        self.rejected_total = 0
        self.rejected_by_reason = {}
        self._audit_seq = 0
        self._audit_prev_hash = "0" * 64
        self._load_audit_chain()
        self._load_push_state()

    # ------------------------------------------------------------ key ----
    def _load_key(self) -> str:
        if self.key_file.exists():
            return self.key_file.read_text().strip()
        key = "a2a-" + secrets.token_hex(16)
        self.key_file.write_text(key + "\n")
        os.chmod(self.key_file, 0o600)
        return key

    # --------------------------------------------------------- secrets ----
    def load_secrets(self):
        """(Re)load per-sender HMAC secrets. <id>.key is current,
        <id>.key.next is the rotation spare; either verifies."""
        found = {}
        for path in sorted(self.secrets_dir.glob("*.key")):
            sender = path.name[:-len(".key")]
            try:
                found.setdefault(sender, []).append(path.read_text().strip().encode())
            except Exception:
                continue
        for path in sorted(self.secrets_dir.glob("*.key.next")):
            sender = path.name[:-len(".key.next")]
            try:
                found.setdefault(sender, []).append(path.read_text().strip().encode())
            except Exception:
                continue
        with self.signing_lock:
            self.secrets = {k: v for k, v in found.items() if v and all(v)}

    # ------------------------------------------------- seen / counters ----
    def _load_seen_counters(self):
        try:
            data = json.loads(self.seen_file.read_text())
            self.seen = {str(k): int(v) for k, v in data.items()}
        except Exception:
            self.seen = {}
        try:
            data = json.loads(self.counters_file.read_text())
            self.counters = {str(k): int(v) for k, v in data.items()}
        except Exception:
            self.counters = {}
        self._prune_seen_locked()

    def _prune_seen_locked(self):
        now = int(time.time())
        self.seen = {k: v for k, v in self.seen.items()
                     if now - v <= SIGN_MAX_SKEW_SECONDS}
        if len(self.seen) > SEEN_MAX:
            keep = sorted(self.seen.items(), key=lambda kv: kv[1])[-SEEN_MAX:]
            self.seen = dict(keep)

    def _save_seen_counters_locked(self):
        try:
            tmp = self.seen_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.seen))
            os.replace(tmp, self.seen_file)
            tmp2 = self.counters_file.with_suffix(".json.tmp")
            tmp2.write_text(json.dumps(self.counters))
            os.replace(tmp2, self.counters_file)
        except Exception:
            pass

    # ------------------------------------------------------------ tasks ----
    def load_tasks(self) -> dict:
        if self.tasks_file.exists():
            try:
                return json.loads(self.tasks_file.read_text())
            except Exception:
                return {}
        return {}

    def save_tasks(self, tasks: dict) -> None:
        tmp = self.tasks_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(tasks, indent=2))
        os.replace(tmp, self.tasks_file)

    def tasks_readable(self):
        try:
            self.load_tasks()
            return True, len(self.load_tasks())
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    # ------------------------------------------------------------ audit ----
    def _load_audit_chain(self):
        """Resume seq / prev_hash from the last chained audit line.
        Legacy lines (no chain_hash) are skipped, not failed."""
        seq, prev = 0, "0" * 64
        try:
            with open(self.audit_file) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if entry.get("chain_hash") and isinstance(entry.get("seq"), int):
                        seq = max(seq, entry["seq"])
                        prev = entry["chain_hash"]
        except FileNotFoundError:
            pass
        except Exception:
            pass
        self._audit_seq = seq
        self._audit_prev_hash = prev

    def audit(self, entry: dict, full_msg=None) -> dict:
        """Append a chained audit entry. Default: payload_hash + envelope
        fields only, NOT the full payload (small, PII-light). With
        --full-audit the verbatim message also goes to audit-full.jsonl."""
        with self.audit_lock:
            core = dict(entry)
            core.pop("seq", None)
            core.pop("prev_hash", None)
            core.pop("chain_hash", None)
            core["logged_at"] = utcnow_iso()
            payload = core.get("payload", {})
            core["payload_hash"] = sha256_hex(
                canonical_json(payload if isinstance(payload, dict) else {"v": payload}).encode())
            core.pop("payload", None)
            self._audit_seq += 1
            # Chain over the entry WITHOUT seq/prev_hash/chain_hash, so a
            # verifier can recompute it from the stored line alone.
            chain = sha256_hex(
                (self._audit_prev_hash + canonical_json(core)).encode())
            core["seq"] = self._audit_seq
            core["prev_hash"] = self._audit_prev_hash
            core["chain_hash"] = chain
            self._audit_prev_hash = chain
            with open(self.audit_file, "a") as fh:
                fh.write(json.dumps(core) + "\n")
            if self.full_audit and full_msg is not None:
                with open(self.audit_full_file, "a") as fh:
                    fh.write(json.dumps({"seq": core["seq"], "message": full_msg}) + "\n")
            return core

    def reject(self, sender, msg_id, reason):
        """Log a rejected message: metadata only, NEVER the payload."""
        with self.signing_lock:
            self.rejected_total += 1
            self.rejected_by_reason[reason] = self.rejected_by_reason.get(reason, 0) + 1
            total = self.rejected_total
        line = {"at": utcnow_iso(), "sender": sender, "id": msg_id,
                "reason": reason, "rejected_total": total}
        try:
            with open(self.rejected_file, "a") as fh:
                fh.write(json.dumps(line) + "\n")
        except Exception:
            pass
        return line

    # ------------------------------------------------------------- push ----
    def _load_push_state(self):
        try:
            data = json.loads(self.push_state_file.read_text())
            self._push_seq = int(data.get("seq", 0))
            self._push_events = list(data.get("events", []))
        except Exception:
            self._push_seq = 0
            self._push_events = []

    def _save_push_state_locked(self):
        try:
            tmp = self.push_state_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"seq": self._push_seq,
                                       "events": self._push_events[-200:]}))
            os.replace(tmp, self.push_state_file)
        except Exception:
            pass

    def push_emit(self, task_id, frm, to, typ):
        with self._push_cond:
            self._push_seq += 1
            self._push_events.append({
                "seq": self._push_seq, "task_id": task_id, "from": frm,
                "to": to, "type": typ, "at": utcnow_iso(),
            })
            del self._push_events[: -PUSH_MAX_EVENTS]
            self._save_push_state_locked()
            self._push_cond.notify_all()
        return self._push_seq

    def push_events_for(self, bot, after):
        with self._push_lock:
            return [e for e in self._push_events if e["to"] == bot and e["seq"] > after]

    # ------------------------------------------------------------ state ----
    def load_state_store(self) -> dict:
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
        return {}

    def save_state_store(self, store: dict) -> None:
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(store, indent=2))
        os.replace(tmp, self.state_file)

    def state_set(self, key, value, task_id=None):
        with self.state_lock:
            store = self.load_state_store()
            store[key] = {"value": value, "task_id": task_id,
                          "updated_at": utcnow_iso()}
            self.save_state_store(store)
        # Mirror to Redis when available; best effort, never fail the set.
        try:
            redis_command(self.redis_url, "SET", f"a2a:state:{key}",
                          json.dumps(value), timeout=0.5)
        except Exception:
            pass
        return store[key]

    def state_get(self, key):
        with self.state_lock:
            store = self.load_state_store()
            entry = store.get(key)
        if entry is not None:
            return entry
        try:
            raw = redis_command(self.redis_url, "GET", f"a2a:state:{key}", timeout=0.5)
            if raw is not None:
                return {"value": json.loads(raw), "task_id": None,
                        "updated_at": None, "source": "redis"}
        except Exception:
            pass
        return None

    # ----------------------------------------------------------- signing ----
    def verify_signature(self, msg: dict):
        """Verify HMAC + replay protection. Returns (signed, reason).

        signed=True only on a fully valid signature. reason is None on
        success, else a short machine-readable string. On success the
        id is recorded in the seen-set and the counter advanced, so a
        replay of the same id/counter is rejected next time.
        Unsigned messages return (False, "unsigned") — the caller decides
        whether compat mode accepts them.
        """
        if "sig" not in msg:
            return False, "unsigned"
        sender = msg.get("from") or ""
        key_id = msg.get("key_id") or sender
        msg_id = msg.get("id") or ""
        ts = parse_ts(msg.get("ts"))
        if ts is None:
            return False, "missing/invalid ts"
        now = int(time.time())
        if abs(now - ts) > SIGN_MAX_SKEW_SECONDS:
            return False, "stale ts"
        with self.signing_lock:
            if msg_id in self.seen:
                return False, "replayed id"
            counter = msg.get("counter")
            if counter is not None:
                if not isinstance(counter, int) or isinstance(counter, bool):
                    return False, "bad counter"
                last = self.counters.get(sender)
                if last is not None and counter <= last:
                    return False, "counter replay"
            lookup = key_id if key_id in self.secrets else sender
            secret_list = self.secrets.get(lookup)
            if not secret_list:
                return False, "unknown sender key"
            unsigned = {k: v for k, v in msg.items()
                        if k not in ("sig", "key_id", "ts", "counter")}
            expected_input = signing_input(msg_id, ts, unsigned)
            sig = str(msg.get("sig") or "")
            ok = False
            for secret in secret_list:
                digest = hmac_mod.new(secret, expected_input,
                                      hashlib.sha256).hexdigest()
                if hmac_mod.compare_digest(digest, sig):
                    ok = True
                    break
            if not ok:
                return False, "bad signature"
            # Success: record id + counter (persisted, capped, TTL-pruned).
            self.seen[msg_id] = ts
            self._prune_seen_locked()
            if counter is not None:
                self.counters[sender] = counter
            self._save_seen_counters_locked()
            self.signed_accepted += 1
        return True, None

    # ------------------------------------------------------- health deep ----
    def listen_queue_estimate(self):
        """Best-effort accept-queue depth from /proc/net/tcp{,6} for our
        listening port. Returns int or None (unavailable / not Linux)."""
        try:
            port_hex = f"{self.listen_port:04X}"
            for path in ("/proc/net/tcp", "/proc/net/tcp6"):
                with open(path) as fh:
                    next(fh)
                    for line in fh:
                        parts = line.split()
                        if len(parts) < 5:
                            continue
                        local = parts[1]
                        if not local.endswith(":" + port_hex):
                            continue
                        if parts[3] != "0A":  # LISTEN
                            continue
                        tx_rx = parts[4].split(":")
                        if len(tx_rx) == 2:
                            return int(tx_rx[1], 16)
        except Exception:
            pass
        return None

    def health_deep(self, nonce=None):
        """Real checks, real work. ok=False unless the probes pass.

        nonce: if the caller passed ?nonce=<random>, it is echoed back
        in the response so the caller can prove this response was
        generated for THIS request (not cached/stale). If no nonce was
        passed, the server generates one. The proof field carries the
        actual round-trip evidence (redis ms / file probe)."""
        t0 = time.time()
        if nonce is None:
            nonce = secrets.token_hex(8)
        tasks_ok, tasks_count = self.tasks_readable()
        redis_ok, redis_ms, redis_err = redis_roundtrip(self.redis_url, timeout=0.5)
        file_probe = None
        if not redis_ok:
            probe_name = f".health-probe-{os.getpid()}-{int(t0 * 1000)}.tmp"
            probe_path = self.data_dir / probe_name
            fp0 = time.time()
            try:
                probe_path.write_text(f"probe-{t0:.6f}")
                back = probe_path.read_text()
                probe_path.unlink()
                file_probe = {"ok": back == f"probe-{t0:.6f}",
                              "ms": round((time.time() - fp0) * 1000, 1)}
            except Exception as exc:  # noqa: BLE001
                try:
                    probe_path.unlink(missing_ok=True)
                except Exception:
                    pass
                file_probe = {"ok": False, "ms": round((time.time() - fp0) * 1000, 1),
                              "error": str(exc)}
        probe_ok = redis_ok or (file_probe and file_probe["ok"])
        ok = bool(tasks_ok and probe_ok)
        with self.signing_lock:
            signing = {
                "require_signing": self.require_signing,
                "signed_accepted": self.signed_accepted,
                "unsigned_accepted": self.unsigned_accepted,
                "rejected_total": self.rejected_total,
                "rejected_by_reason": dict(self.rejected_by_reason),
                "seen_ids": len(self.seen),
                "senders_with_keys": sorted(self.secrets.keys()),
            }
        return {
            "ok": ok,
            "nonce": nonce,
            "proof": {"probe": "redis-set-get" if redis_ok else "file-probe",
                      "redis_roundtrip_ms": round(redis_ms, 1) if redis_ok else None,
                      "tasks_readable": tasks_ok},
            "bot": BOT_NAME,
            "version": BOT_VERSION,
            "protocolVersion": PROTOCOL_VERSION,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "threads": threading.active_count(),
            "tasks_file_readable": tasks_ok,
            "tasks_count": tasks_count if tasks_ok else 0,
            "push_seq": self._push_seq,
            "redis": {"reachable": redis_ok, "roundtrip_ms": round(redis_ms, 1),
                      "error": redis_err},
            "file_probe": file_probe,
            "listen_backlog_configured": 128,
            "listen_queue_estimate": self.listen_queue_estimate(),
            "signing": signing,
            "audit": {"seq": self._audit_seq,
                      "last_chain_hash": self._audit_prev_hash[:16] + "..."},
        }

# -------------------------------------------------------------- envelope ----
def validate_envelope(msg: dict):
    """Frozen v2 envelope. Return an error string, or None if valid.

    Mandatory: id, from, to, type, task_id. type is the closed 7-word
    performative vocabulary. Short IDs (<=128 chars). Timestamps may be
    ISO-8601 UTC strings or unix seconds. Optional state_ref (string)
    and changed (object of delta fields only). No prose is required in
    bot-to-bot fields — and prose is NOT rejected (compat); the rule is
    documented in README.md, not enforced here.
    """
    if not isinstance(msg, dict):
        return "message must be a JSON object"
    for field in ("id", "from", "to", "type", "task_id"):
        if not msg.get(field):
            return f"missing required field: {field}"
        if not isinstance(msg[field], str):
            return f"field must be a string: {field}"
        if len(msg[field]) > 128:
            return f"field too long (max 128 chars): {field}"
    if msg["type"] not in PERFORMATIVES:
        return f"type must be one of {sorted(PERFORMATIVES)}"
    if "payload" in msg and not isinstance(msg["payload"], dict):
        return "payload must be an object"
    if "state_ref" in msg and not isinstance(msg["state_ref"], str):
        return "state_ref must be a string"
    if "changed" in msg and not isinstance(msg["changed"], dict):
        return "changed must be an object"
    if "ts" in msg and parse_ts(msg.get("ts")) is None:
        return "ts must be unix seconds or an ISO-8601 timestamp"
    if "counter" in msg and (not isinstance(msg["counter"], int)
                             or isinstance(msg["counter"], bool)):
        return "counter must be an integer"
    for ts_field in ("timestamp", "deadline"):
        if ts_field in msg and msg[ts_field] is not None:
            v = msg[ts_field]
            if isinstance(v, (int, float)):
                continue
            if isinstance(v, str) and parse_ts(v) is not None:
                continue
            return f"{ts_field} must be an ISO-8601 timestamp or unix seconds"
    return None


def load_agents(state: State) -> dict:
    if state.agents_file.exists():
        try:
            return json.loads(state.agents_file.read_text())
        except Exception:
            return {"agents": []}
    return {"agents": []}


def agent_card(state: State, host: str, port: int) -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "Westin Jr.",
        "description": "Westin's personal Muse: research, numbers, tickets, chat. Speaks A2A so other bots can hand it work and get status back.",
        "version": BOT_VERSION,
        "url": f"{SCHEME}://{host}:{port}/",
        "capabilities": {"streaming": False, "pushNotifications": True,
                         "state": True, "extendedAgentCard": True},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {"id": "research", "name": "Research",
             "description": "Web research with sources, delivered simply.",
             "tags": ["research", "web"], "examples": ["research the best..."]},
            {"id": "tickets", "name": "Tickets",
             "description": "Find and compare event tickets.",
             "tags": ["tickets", "events"], "examples": ["find Avs tickets Dec 20"]},
            {"id": "chat", "name": "Chat",
             "description": "Plain chat and questions.",
             "tags": ["chat"], "examples": ["what is on my calendar?"]},
            {"id": "state", "name": "Shared state",
             "description": "Read/write shared task state via state/set and state/get.",
             "tags": ["state", "tasks"], "examples": ["state/get task-042"]},
        ],
        "securitySchemes": {
            "bearer": {"type": "http", "scheme": "bearer"},
            "hmac": {"type": "http", "scheme": "hmac",
                     "description": "HMAC-SHA256 per-sender signature. Envelope fields sig, key_id, ts (unix seconds), counter. Signing input is the domain tag 'muse-a2a-v1' + ts + id + canonical JSON of the message without sig/key_id/ts/counter. See README.md."},
        },
        "security": [{"bearer": []}, {"hmac": []}],
    }


# ------------------------------------------------------- message pipeline ----
def process_message(state: State, msg: dict):
    """Validate + verify signature + store + audit + push (Fix 9).

    Signature verification happens BEFORE anything is stored or reaches
    any LLM-facing logic. Returns (task, warning_or_None, signed_bool).
    Raises RpcError(-32602) for a bad envelope, RpcError(-32002) for a
    bad signature / replay / unsigned-when-required.
    """
    bad = validate_envelope(msg)
    if bad:
        raise RpcError(-32602, bad)
    signed, reason = state.verify_signature(msg)
    if not signed:
        if reason == "unsigned":
            if state.require_signing:
                state.reject(msg.get("from"), msg.get("id"), "unsigned")
                raise RpcError(-32002, "bad signature/replay: unsigned message rejected (require-signing on)")
            with state.signing_lock:
                state.unsigned_accepted += 1
        else:
            state.reject(msg.get("from"), msg.get("id"), reason)
            raise RpcError(-32002, f"bad signature/replay: {reason}")
    stored = dict(msg)
    stored["signed"] = bool(signed)
    stored["received_at"] = utcnow_iso()
    payload = msg.get("payload") or {}
    warning = None
    if len(json.dumps(payload)) > PAYLOAD_WARN_CHARS:
        warning = (f"payload is {len(json.dumps(payload))} chars (>~{PAYLOAD_WARN_CHARS}, "
                   "~500 tokens); prefer state/set + state_ref instead of re-sending bulk data")
    # Deltas: state_ref + changed applies the changed fields to the
    # shared state store, so the receiver reads current state instead
    # of replaying history.
    if msg.get("state_ref") and isinstance(msg.get("changed"), dict):
        current = state.state_get(msg["state_ref"])
        base = current["value"] if current and isinstance(current.get("value"), dict) else {}
        merged = dict(base)
        merged.update(msg["changed"])
        state.state_set(msg["state_ref"], merged, task_id=msg.get("task_id"))

    with state.tasks_lock:
        tasks = state.load_tasks()
        tid = msg["task_id"]
        task = tasks.get(tid)
        status_before = task.get("status") if task is not None else None
        if task is None:
            task = {"id": tid, "context_id": msg.get("context_id"),
                    "status": "submitted", "history": [],
                    "created_at": utcnow_iso()}
            tasks[tid] = task
        task["history"].append(stored)
        if msg["type"] == "request":
            task["status"] = "working"
        elif msg["type"] in ("confirm", "decide") and msg.get("state") in TASK_STATES:
            task["status"] = msg["state"]
        elif msg["type"] == "cancel":
            task["status"] = "canceled"
        elif msg["type"] == "refuse":
            task["status"] = "rejected"
        task["updated_at"] = utcnow_iso()
        state.save_tasks(tasks)
    # Audit chain entry: envelope fields + payload_hash, NOT the payload.
    state.audit({"kind": "message", "direction": "in",
                 "id": msg.get("id"), "from": msg.get("from"),
                 "to": msg.get("to"), "type": msg.get("type"),
                 "task_id": msg.get("task_id"), "state": msg.get("state"),
                 "state_ref": msg.get("state_ref"),
                 "signed": bool(signed), "payload": payload}, full_msg=stored)
    # Fix 9: no push for a non-request message on a task that was
    # already terminal before this message (redundant confirm ping-pong).
    if not (status_before in TERMINAL_STATES and msg.get("type") != "request"):
        state.push_emit(tid, msg.get("from"), msg.get("to"), msg.get("type"))
    return task, warning, bool(signed)


# ---------------------------------------------------------------- Handler ----
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- TLS fix: wrap per-connection, in this handler thread only. --
    # self.request is the raw accepted socket; StreamRequestHandler.setup()
    # (called via super()) builds rfile/wfile from self.request, so the
    # wrap must happen here, on self.request, BEFORE super().setup().
    # Never wrap the listening socket: accept() stays plain TCP.
    def setup(self):
        ctx = getattr(self.server, "tls_context", None)
        request = self.request
        try:
            request.settimeout(SOCKET_TIMEOUT)
            if ctx is not None:
                request.settimeout(
                    getattr(self.server, "handshake_timeout", HANDSHAKE_TIMEOUT_DEFAULT))
                request = ctx.wrap_socket(
                    request, server_side=True, do_handshake_on_connect=False)
                try:
                    request.do_handshake()
                except Exception:
                    try:
                        request.close()
                    except Exception:
                        pass
                    raise
                request.settimeout(SOCKET_TIMEOUT)
        except Exception:
            # setup() failed (e.g. hung/aborted handshake): abort this
            # handler only. The server's handle_error logs one line.
            raise
        self.request = request
        super().setup()

    def log_message(self, *args):
        pass

    def _send_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.send_header("A2A-Version", PROTOCOL_VERSION)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            self.close_connection = True

    def _authed(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return hmac_mod.compare_digest(auth[7:].strip(), self.server.state.api_key)

    def _unauthorized(self):
        self._send_json(200, {"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32001, "message": "unauthorized"}})

    def _host_port(self):
        host = self.headers.get("Host", f"localhost:{self.server.server_port}")
        return host.split(":")[0], self.server.server_port

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length else b""

    # ------------------------------------------------------------- GET ----
    def do_GET(self):
        state = self.server.state
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/.well-known/agent-card.json":
            host, port = self._host_port()
            self._send_json(200, agent_card(state, host, port))
            return
        if path == "/agents":
            self._send_json(200, load_agents(state))
            return
        if path == "/health":
            self._send_json(200, {"ok": True, "bot": BOT_NAME, "version": BOT_VERSION})
            return
        if path == "/health/deep":
            qs = parse_qs(parsed.query)
            nonce = (qs.get("nonce") or [None])[0]
            if nonce is not None:
                nonce = str(nonce)[:64]
            self._send_json(200, state.health_deep(nonce=nonce))
            return
        if path == "/push/status":
            if not self._authed():
                self._unauthorized()
                return
            with state._push_lock:
                self._send_json(200, {
                    "push": True, "seq": state._push_seq,
                    "events_kept": len(state._push_events),
                    "last_event": state._push_events[-1] if state._push_events else None,
                })
            return
        if path == "/events":
            if not self._authed():
                self._unauthorized()
                return
            qs = parse_qs(parsed.query)
            bot = (qs.get("bot") or [""])[0]
            try:
                after = int((qs.get("after") or ["0"])[0])
            except ValueError:
                after = 0
            if not bot:
                self._send_json(400, {"error": "bot parameter required"})
                return
            deadline = time.time() + PUSH_WAIT_SECONDS
            resp_events, resp_seq = None, 0
            with state._push_cond:
                while True:
                    events = [e for e in state._push_events
                              if e["to"] == bot and e["seq"] > after]
                    if events:
                        resp_events, resp_seq = events, state._push_seq
                        break
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        resp_events, resp_seq = [], state._push_seq
                        break
                    state._push_cond.wait(timeout=min(remaining, 5))
            self._send_json(200, {"bot": bot, "after": after,
                                  "seq": resp_seq, "events": resp_events})
            return
        if path == "/tasks":
            if not self._authed():
                self._unauthorized()
                return
            qs = parse_qs(parsed.query)
            status = (qs.get("status") or [None])[0]
            with state.tasks_lock:
                items = list(state.load_tasks().values())
            if status:
                items = [t for t in items if t["status"] == status]
            self._send_json(200, {"tasks": items, "states": sorted(TASK_STATES)})
            return
        if path.startswith("/tasks/"):
            if not self._authed():
                self._unauthorized()
                return
            tid = unquote(path[len("/tasks/"):].strip("/"))
            with state.tasks_lock:
                task = state.load_tasks().get(tid)
            if task is None:
                self._send_json(404, {"error": "task not found"})
                return
            self._send_json(200, {"task": task})
            return
        if path.startswith("/state/"):
            if not self._authed():
                self._unauthorized()
                return
            key = unquote(path[len("/state/"):].strip("/"))
            entry = state.state_get(key)
            if entry is None:
                self._send_json(404, {"error": "key not found"})
                return
            self._send_json(200, {"key": key, **entry})
            return
        self._send_json(404, {"error": "not found"})

    # ------------------------------------------------------------ POST ----
    def do_POST(self):
        state = self.server.state
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/tasks":
            # REST binding: POST /tasks, body = envelope (or {"message": ...}).
            if not self._authed():
                self._unauthorized()
                return
            try:
                body = json.loads(self._read_body().decode() or "{}")
            except Exception:
                self._send_json(200, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32700, "message": "parse error"}})
                return
            msg = body.get("message") if isinstance(body, dict) else None
            if not isinstance(msg, dict):
                msg = body
            try:
                task, warning, signed = process_message(state, msg)
            except RpcError as exc:
                self._send_json(200, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": exc.code, "message": exc.message}})
                return
            result = {"task": task, "signed": signed}
            if warning:
                result["warning"] = warning
            self._send_json(200, result)
            return
        if path.startswith("/tasks/") and path.endswith("/cancel"):
            if not self._authed():
                self._unauthorized()
                return
            tid = unquote(path[len("/tasks/"):-len("/cancel")].strip("/"))
            with state.tasks_lock:
                tasks = state.load_tasks()
                task = tasks.get(tid)
                if task is None:
                    self._send_json(404, {"error": "task not found"})
                    return
                task["status"] = "canceled"
                task["updated_at"] = utcnow_iso()
                state.save_tasks(tasks)
            state.audit({"kind": "cancel", "task_id": tid, "payload": {"task_id": tid}})
            self._send_json(200, {"task": task})
            return
        if path != "/":
            self._send_json(404, {"error": "not found"})
            return
        try:
            req = json.loads(self._read_body().decode() or "{}")
        except Exception:
            self._send_json(200, {"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": "parse error"}})
            return
        rpc_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        def err(code: int, message: str):
            self._send_json(200, {"jsonrpc": "2.0", "id": rpc_id,
                                  "error": {"code": code, "message": message}})

        def ok(result: dict):
            self._send_json(200, {"jsonrpc": "2.0", "id": rpc_id, "result": result})

        if method in ("message/send", "tasks/cancel", "state/set"):
            if not self._authed():
                err(-32001, "unauthorized: Bearer key required")
                return
        if method == "message/send":
            msg = params.get("message") or params
            try:
                task, warning, signed = process_message(state, msg)
            except RpcError as exc:
                err(exc.code, exc.message)
                return
            result = {"task": task, "signed": signed}
            if warning:
                result["warning"] = warning
            ok(result)
            return
        if method == "tasks/get":
            with state.tasks_lock:
                task = state.load_tasks().get(params.get("id") or params.get("task_id", ""))
            if task is None:
                err(-32602, "task not found")
                return
            ok({"task": task})
            return
        if method == "tasks/list":
            with state.tasks_lock:
                items = list(state.load_tasks().values())
            if params.get("status"):
                items = [t for t in items if t["status"] == params["status"]]
            ok({"tasks": items, "states": sorted(TASK_STATES)})
            return
        if method == "tasks/cancel":
            tid = params.get("id") or params.get("task_id", "")
            with state.tasks_lock:
                tasks = state.load_tasks()
                task = tasks.get(tid)
                if task is None:
                    err(-32602, "task not found")
                    return
                task["status"] = "canceled"
                task["updated_at"] = utcnow_iso()
                state.save_tasks(tasks)
            state.audit({"kind": "cancel", "task_id": tid, "payload": {"task_id": tid}})
            ok({"task": task})
            return
        if method == "state/set":
            key = params.get("key")
            if not key or not isinstance(key, str):
                err(-32602, "state/set needs a string key")
                return
            if "value" not in params:
                err(-32602, "state/set needs a value")
                return
            entry = state.state_set(key, params["value"], task_id=params.get("task_id"))
            ok({"key": key, **entry})
            return
        if method == "state/get":
            key = params.get("key")
            if not key or not isinstance(key, str):
                err(-32602, "state/get needs a string key")
                return
            entry = state.state_get(key)
            if entry is None:
                err(-32602, "key not found")
                return
            ok({"key": key, **entry})
            return
        err(-32601, f"method not found: {method}")


# ---------------------------------------------------------------- Server ----
class A2AServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, state, tls_context=None, handshake_timeout=None):
        self.state = state
        self.tls_context = tls_context
        self.handshake_timeout = float(
            handshake_timeout if handshake_timeout is not None
            else os.environ.get("A2A_HANDSHAKE_TIMEOUT", HANDSHAKE_TIMEOUT_DEFAULT))
        super().__init__(address, Handler)
        state.listen_port = self.server_address[1]

    def handle_error(self, request, client_address):
        # Scanner / probe noise: one line, no traceback. A hung or
        # aborted TLS handshake lands here and must never wedge the bus.
        try:
            print(f"[a2a-v2] handler error from "
                  f"{client_address[0]}:{client_address[1]}", flush=True)
        except Exception:
            pass


def create_server(host, port, data_dir, tls_cert=None, tls_key=None,
                  require_signing=False, full_audit=False, redis_url=None,
                  handshake_timeout=None):
    global SCHEME
    state = State(data_dir, require_signing=require_signing,
                  full_audit=full_audit, redis_url=redis_url,
                  handshake_timeout=handshake_timeout
                  if handshake_timeout is not None
                  else float(os.environ.get("A2A_HANDSHAKE_TIMEOUT",
                                            HANDSHAKE_TIMEOUT_DEFAULT)))
    tls_context = None
    if tls_cert and tls_key:
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.load_cert_chain(certfile=tls_cert, keyfile=tls_key)
        SCHEME = "https"
    server = A2AServer((host, port), state, tls_context=tls_context,
                       handshake_timeout=state.handshake_timeout)
    return server, state


def main() -> None:
    global SCHEME
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data-dir", default=str(HERE),
                    help="directory for tasks.json, audit.jsonl, key, secrets/")
    ap.add_argument("--tls-cert", default=None)
    ap.add_argument("--tls-key", default=None)
    ap.add_argument("--require-signing", action="store_true",
                    help="reject unsigned messages (compat mode is default: accept + mark signed:false)")
    ap.add_argument("--full-audit", action="store_true",
                    help="also write verbatim messages to audit-full.jsonl")
    ap.add_argument("--redis-url", default=None,
                    help="e.g. redis://127.0.0.1:6379/0 (default tries localhost, reports redis:false if absent)")
    ap.add_argument("--handshake-timeout", type=float, default=None,
                    help="seconds to allow a TLS handshake per connection (default 10, env A2A_HANDSHAKE_TIMEOUT)")
    args = ap.parse_args()
    server, state = create_server(
        args.host, args.port, args.data_dir,
        tls_cert=args.tls_cert, tls_key=args.tls_key,
        require_signing=args.require_signing, full_audit=args.full_audit,
        redis_url=args.redis_url, handshake_timeout=args.handshake_timeout)
    if not state.agents_file.exists():
        state.agents_file.write_text(json.dumps({
            "agents": [
                {"id": "westin-jr", "name": "Westin Jr.", "role": "personal assistant (research/tickets/chat)",
                 "url": f"{SCHEME}://{args.host}:{server.server_address[1]}/", "status": "online"},
                {"id": "gus", "name": "Gus", "role": "Grand Valley Garage Doors (ServiceTitan/estimates/scheduling)",
                 "url": None, "status": "pending"},
                {"id": "dorian", "name": "Dorian", "role": "Westin's other Muse (Windows)",
                 "url": None, "status": "pending"},
            ]}, indent=2))
    print(f"A2A server v2 up: {SCHEME}://{args.host}:{server.server_address[1]}/  (bot={BOT_NAME})", flush=True)
    print(f"Agent Card: {SCHEME}://{args.host}:{server.server_address[1]}/.well-known/agent-card.json", flush=True)
    print(f"Key file: {state.key_file} (0600, do not commit or share in chat)", flush=True)
    print(f"Signing: {'required' if state.require_signing else 'compat mode (unsigned accepted, marked signed:false)'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
