"""Worker subprocess for the kill-9-mid-send scenario.

Runs the REAL intake + REAL send_fenced against a FakeTelegram,
then sleeps so the parent can SIGKILL it mid-send — i.e. after the
send went out but before any reply record was written. The parent
restarts a harness on the same state dir and reconciles from the
sender log.

Usage: python3 _kill_worker.py --state-dir DIR --owner-id 111
Writes <state-dir>/sent.marker once send_fenced reports sent.
Never touches the network or a real token.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_harness import FakeClock, make_turn  # noqa: E402

REPLY_TEXT = "Your total is $42.50."
INBOUND_TEXT = "please confirm my order total"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--owner-id", type=int, default=111)
    args = parser.parse_args()

    state_dir = Path(args.state_dir)
    clock = FakeClock()
    turn, _tmp = make_turn(state_dir=state_dir, clock=clock,
                           owner_id=args.owner_id)
    fake = turn.fake
    fake.inject_message(args.owner_id, INBOUND_TEXT,
                        sender_id=args.owner_id)
    clock.advance(0.5)
    payload = turn.wake_once()
    if payload is None:
        print("kill-worker: wake claimed nothing", file=sys.stderr)
        return 1
    clock.advance(2.0)
    result = turn.send_fenced_only(payload["items"][0],
                                   payload["claim_token"], REPLY_TEXT,
                                   now=clock.now())
    if not result.get("ok"):
        print(f"kill-worker: send_fenced failed: {result}",
              file=sys.stderr)
        return 1
    # The send is out (sender log proves it); the reply record is
    # NOT written yet. Signal readiness, then sleep to be killed.
    (state_dir / "sent.marker").write_text("sent\n", encoding="utf-8")
    print("kill-worker: sent, sleeping to be killed", flush=True)
    import time
    time.sleep(60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
