#!/usr/bin/env python3
"""Tiny A2A client wrapper v2 for Westin's bots (copy of the v0 client,
plus signing, TOON, state store, and the v1.0 REST binding).

Usage:
  python3 a2a_client_v2.py --url http://127.0.0.1:8765/ --key-file key \
      send --from dorian --to westin-jr --type request --task task-001 \
      --payload '{"action":"hello"}'
  python3 a2a_client_v2.py --url ... --key-file key \
      send --from dorian --to westin-jr --task task-001 \
      --payload '{"table":[{"a":1,"b":2},{"a":3,"b":4}]}' --toon
  python3 a2a_client_v2.py --url ... --key-file key \
      send --from dorian --to westin-jr --task task-001 \
      --payload '{"action":"hi"}' --secret-file secrets/dorian.key
  python3 a2a_client_v2.py --url ... --key-file key get --task task-001
  python3 a2a_client_v2.py --url ... --key-file key list
  python3 a2a_client_v2.py --url ... --key-file key state-set --key task-001 --value '{"status":"done"}' [--task task-001]
  python3 a2a_client_v2.py --url ... --key-file key state-get --key task-001
  python3 a2a_client_v2.py --url ... card        (no key needed)
  python3 a2a_client_v2.py --url ... agents      (no key needed)
  python3 a2a_client_v2.py --url ... health
  python3 a2a_client_v2.py --url ... health-deep
  python3 a2a_client_v2.py toon-decode --data 'a,b\n"1",2\n'

Signing (--secret-file): adds sig/key_id/ts/counter, HMAC-SHA256 with
the per-sender secret, domain "muse-a2a-v1", exactly as the v2 server
verifies it. The counter is kept in <secret-file>.counter and auto-
increments per signed send (strictly increasing per sender). The key
file and the secret file are read from disk; never put either on a
command line, in chat, or in git.

TOON (--toon): if the payload is a uniform table (a list of dicts with
identical keys, or {"table": [...]}), it is rendered as TOON (keys once,
rows as CSV-like lines) and sent as payload {"format":"toon","data":...}.
The server stores it verbatim; from_toon() here (and in the server)
converts it back.

Payload size rule: payloads over ~2000 chars (~500 tokens) are still
sent, but this client warns and suggests state/set + state_ref instead
of re-sending bulk data. The server stores the payload either way.
"""
import argparse
import csv
import hashlib
import hmac as hmac_mod
import io
import json
import secrets
import sys
import time
import urllib.request
import urllib.error

PAYLOAD_WARN_CHARS = 2000
SIGN_DOMAIN = "muse-a2a-v1"


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def to_toon(rows):
    """Uniform table -> TOON text (keys once, rows CSV-like, JSON cells).

    Strings are written raw; numbers/bools/null/containers are
    JSON-encoded so they parse back to their types. Caveat: a string
    cell that itself parses as JSON (e.g. "42") comes back as that
    JSON value — use plain JSON if that typing matters.
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
    """TOON text -> list of dicts."""
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
    if not isinstance(value, list) or not value:
        return False
    if not all(isinstance(r, dict) for r in value):
        return False
    keys = list(value[0].keys())
    return all(list(r.keys()) == keys for r in value)


def sign_message(msg, secret: bytes, counter=None):
    """Add sig/key_id/ts/counter. Must match a2a_server_v2 exactly."""
    out = dict(msg)
    ts = int(time.time())
    out["ts"] = ts
    if counter is not None:
        out["counter"] = int(counter)
    out["key_id"] = msg.get("from")
    unsigned = {k: v for k, v in out.items()
                if k not in ("sig", "key_id", "ts", "counter")}
    signing_input = "\n".join([SIGN_DOMAIN, str(ts), str(out["id"]),
                               canonical_json(unsigned)]).encode("utf-8")
    out["sig"] = hmac_mod.new(secret, signing_input, hashlib.sha256).hexdigest()
    return out


def next_counter(secret_file):
    """Strictly increasing per-sender counter, kept beside the secret."""
    counter_file = secret_file + ".counter"
    try:
        n = int(open(counter_file).read().strip())
    except Exception:
        n = 0
    n += 1
    with open(counter_file, "w") as fh:
        fh.write(str(n))
    return n


import ssl as _ssl

INSECURE = False  # --insecure: skip TLS verification (localhost/test only)


def _ctx():
    return _ssl._create_unverified_context() if INSECURE else None


def rpc(url: str, method: str, params: dict, key: str | None, rpc_id: int = 1):
    body = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("A2A-Version", "1.0")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ctx()) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"error": {"code": exc.code, "message": exc.read().decode()[:300]}}


def get(url: str, path: str, key: str | None = None):
    req = urllib.request.Request(url.rstrip("/") + path, method="GET")
    req.add_header("A2A-Version", "1.0")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=30, context=_ctx()) as resp:
        return json.loads(resp.read().decode())


def post_rest(url: str, path: str, body: dict, key: str | None):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("A2A-Version", "1.0")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ctx()) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"error": {"code": exc.code, "message": exc.read().decode()[:300]}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765/")
    ap.add_argument("--key-file", default="key")
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS verification (localhost/test only; the live cert is for your A2A hostname, so 127.0.0.1 fails verification)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("--from", dest="frm", required=True)
    s.add_argument("--to", required=True)
    s.add_argument("--type", default="request")
    s.add_argument("--task", required=True)
    s.add_argument("--payload", default="{}")
    s.add_argument("--state", default=None)
    s.add_argument("--deadline", default=None)
    s.add_argument("--state-ref", default=None)
    s.add_argument("--changed", default=None,
                   help='JSON object of delta fields, sent with --state-ref')
    s.add_argument("--toon", action="store_true",
                   help="render a uniform table payload as TOON")
    s.add_argument("--secret-file", default=None,
                   help="per-sender HMAC secret file; signs the message")
    s.add_argument("--rest", action="store_true",
                   help="send via REST POST /tasks instead of JSON-RPC")
    g = sub.add_parser("get")
    g.add_argument("--task", required=True)
    sub.add_parser("list")
    sub.add_parser("card")
    sub.add_parser("agents")
    sub.add_parser("health")
    sub.add_parser("health-deep")
    c = sub.add_parser("cancel")
    c.add_argument("--task", required=True)
    ss = sub.add_parser("state-set")
    ss.add_argument("--key", required=True)
    ss.add_argument("--value", required=True, help="JSON value")
    ss.add_argument("--task", default=None)
    sg = sub.add_parser("state-get")
    sg.add_argument("--key", required=True)
    td = sub.add_parser("toon-decode")
    td.add_argument("--data", required=True)
    args = ap.parse_args()
    global INSECURE
    INSECURE = bool(args.insecure)

    if args.cmd == "toon-decode":
        print(json.dumps(from_toon(args.data), indent=2))
        return
    if args.cmd in ("card",):
        print(json.dumps(get(args.url, "/.well-known/agent-card.json"), indent=2))
        return
    if args.cmd == "agents":
        print(json.dumps(get(args.url, "/agents"), indent=2))
        return
    if args.cmd == "health":
        print(json.dumps(get(args.url, "/health"), indent=2))
        return
    if args.cmd == "health-deep":
        print(json.dumps(get(args.url, "/health/deep"), indent=2))
        return
    try:
        key = open(args.key_file).read().strip()
    except OSError:
        print("key file not found: run the server once to generate it", file=sys.stderr)
        sys.exit(1)
    if args.cmd == "send":
        payload = json.loads(args.payload)
        if args.toon:
            table = payload.get("table") if isinstance(payload, dict) else payload
            if not is_uniform_table(table):
                print("error: --toon needs a uniform table "
                      "(list of dicts, identical keys)", file=sys.stderr)
                sys.exit(2)
            payload = {"format": "toon", "data": to_toon(table)}
        payload_chars = len(json.dumps(payload))
        if payload_chars > PAYLOAD_WARN_CHARS:
            print(f"warning: payload is {payload_chars} chars "
                  f"(>{PAYLOAD_WARN_CHARS}, ~500 tokens); consider state/set "
                  "+ state_ref instead of re-sending bulk data", file=sys.stderr)
        msg = {"id": f"msg-{int(time.time())}-{secrets.token_hex(4)}",
               "from": args.frm, "to": args.to,
               "type": args.type, "task_id": args.task, "payload": payload}
        if args.state:
            msg["state"] = args.state
        if args.deadline:
            msg["deadline"] = args.deadline
        if args.state_ref:
            msg["state_ref"] = args.state_ref
        if args.changed:
            msg["changed"] = json.loads(args.changed)
        if args.secret_file:
            try:
                secret = open(args.secret_file).read().strip().encode()
            except OSError:
                print(f"secret file not found: {args.secret_file}", file=sys.stderr)
                sys.exit(1)
            msg = sign_message(msg, secret, counter=next_counter(args.secret_file))
        if args.rest:
            print(json.dumps(post_rest(args.url, "/tasks", msg, key), indent=2))
        else:
            print(json.dumps(rpc(args.url, "message/send", {"message": msg}, key), indent=2))
    elif args.cmd == "get":
        print(json.dumps(rpc(args.url, "tasks/get", {"id": args.task}, key), indent=2))
    elif args.cmd == "list":
        print(json.dumps(rpc(args.url, "tasks/list", {}, key), indent=2))
    elif args.cmd == "cancel":
        print(json.dumps(rpc(args.url, "tasks/cancel", {"id": args.task}, key), indent=2))
    elif args.cmd == "state-set":
        print(json.dumps(rpc(args.url, "state/set",
                             {"key": args.key, "value": json.loads(args.value),
                              "task_id": args.task}, key), indent=2))
    elif args.cmd == "state-get":
        print(json.dumps(rpc(args.url, "state/get", {"key": args.key}, key), indent=2))


if __name__ == "__main__":
    main()
