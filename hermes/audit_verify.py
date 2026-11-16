#!/usr/bin/env python3
"""Verify the A2A v2 audit chain (audit.jsonl).

Usage:
  python3 audit_verify.py [path/to/audit.jsonl]

Recomputes every chained entry: chain_hash must equal
SHA-256(prev_hash + canonical JSON of the entry without
seq/prev_hash/chain_hash). Lines without chain fields are old v0/v1
entries: reported as "legacy, unchained" and do NOT fail verification.

Exit 0 + "OK" when every chained entry verifies; exit 1 and the first
bad seq when the chain breaks.
"""
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def verify(path):
    """Return a dict: ok, checked, legacy, chained, first_bad_seq, error."""
    result = {"ok": True, "checked": 0, "legacy": 0, "chained": 0,
              "first_bad_seq": None, "error": None}
    prev_hash = "0" * 64
    try:
        fh = open(path)
    except FileNotFoundError:
        result["error"] = f"file not found: {path}"
        result["ok"] = False
        return result
    with fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            result["checked"] += 1
            try:
                entry = json.loads(line)
            except Exception:
                result["ok"] = False
                result["first_bad_seq"] = f"line {lineno} (unparsable)"
                result["error"] = f"line {lineno} is not valid JSON"
                return result
            if not entry.get("chain_hash"):
                result["legacy"] += 1
                continue
            core = {k: v for k, v in entry.items()
                    if k not in ("seq", "prev_hash", "chain_hash")}
            if entry.get("prev_hash") != prev_hash:
                result["ok"] = False
                result["first_bad_seq"] = entry.get("seq")
                result["error"] = (f"seq {entry.get('seq')}: prev_hash mismatch "
                                   f"(expected chain to continue from {prev_hash[:16]}...)")
                return result
            expected = hashlib.sha256(
                (prev_hash + canonical_json(core)).encode()).hexdigest()
            if expected != entry["chain_hash"]:
                result["ok"] = False
                result["first_bad_seq"] = entry.get("seq")
                result["error"] = f"seq {entry.get('seq')}: chain_hash mismatch"
                return result
            result["chained"] += 1
            prev_hash = entry["chain_hash"]
    return result


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "audit.jsonl"
    result = verify(path)
    if result["error"]:
        print(f"FAIL: {result['error']}")
        print(f"checked={result['checked']} chained={result['chained']} "
              f"legacy={result['legacy']}")
        sys.exit(1)
    print(f"OK: audit chain verifies "
          f"(checked={result['checked']} chained={result['chained']} "
          f"legacy={result['legacy']} unchained)")
    sys.exit(0)


if __name__ == "__main__":
    main()
