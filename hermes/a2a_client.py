#!/usr/bin/env python3
"""Tiny A2A client wrapper for Westin's bots.

Usage:
  python3 a2a_client.py --url http://127.0.0.1:8765/ --key-file key \
      send --from westin-jr --to dorian --type request --task task-001 \
      --payload '{"action":"hello"}'
  python3 a2a_client.py --url ... --key-file key get --task task-001
  python3 a2a_client.py --url ... --key-file key list
  python3 a2a_client.py --url ... card        (no key needed)
  python3 a2a_client.py --url ... agents      (no key needed)

The key is read from the key file (0600). Never put it on a command
line, in chat, or in git. A new bot gets the key over a secure channel.
"""
import argparse
import json
import sys
import time
import urllib.request
import urllib.error


def rpc(url: str, method: str, params: dict, key: str | None, rpc_id: int = 1):
    body = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"error": {"code": exc.code, "message": exc.read().decode()[:300]}}


def get(url: str, path: str):
    with urllib.request.urlopen(url.rstrip("/") + path, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765/")
    ap.add_argument("--key-file", default="key")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("--from", dest="frm", required=True)
    s.add_argument("--to", required=True)
    s.add_argument("--type", default="request")
    s.add_argument("--task", required=True)
    s.add_argument("--payload", default="{}")
    s.add_argument("--state", default=None)
    s.add_argument("--deadline", default=None)
    g = sub.add_parser("get")
    g.add_argument("--task", required=True)
    sub.add_parser("list")
    sub.add_parser("card")
    sub.add_parser("agents")
    c = sub.add_parser("cancel")
    c.add_argument("--task", required=True)
    args = ap.parse_args()

    if args.cmd in ("card",):
        print(json.dumps(get(args.url, "/.well-known/agent-card.json"), indent=2))
        return
    if args.cmd == "agents":
        print(json.dumps(get(args.url, "/agents"), indent=2))
        return
    try:
        key = open(args.key_file).read().strip()
    except OSError:
        print("key file not found: run the server once to generate it", file=sys.stderr)
        sys.exit(1)
    if args.cmd == "send":
        msg = {"id": f"msg-{int(time.time())}", "from": args.frm, "to": args.to,
               "type": args.type, "task_id": args.task,
               "payload": json.loads(args.payload)}
        if args.state:
            msg["state"] = args.state
        if args.deadline:
            msg["deadline"] = args.deadline
        print(json.dumps(rpc(args.url, "message/send", {"message": msg}, key), indent=2))
    elif args.cmd == "get":
        print(json.dumps(rpc(args.url, "tasks/get", {"id": args.task}, key), indent=2))
    elif args.cmd == "list":
        print(json.dumps(rpc(args.url, "tasks/list", {}, key), indent=2))
    elif args.cmd == "cancel":
        print(json.dumps(rpc(args.url, "tasks/cancel", {"id": args.task}, key), indent=2))


if __name__ == "__main__":
    main()
