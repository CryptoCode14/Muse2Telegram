# Muse2Telegram v2 — simulation harness

In-memory simulation of the Telegram bridge. No network, no real
token, no live state, no live bot. Every run uses a fresh temp state
dir (except `kill-9-mid-send`, which deliberately shares one dir
between a worker subprocess and the restarting parent).

## Files

| File | What it is |
|---|---|
| `fake_tg.py` | `FakeTelegram`: in-memory Bot API (`getUpdates`, `sendMessage`, `editMessageText` with the real 400s, `sendChatAction`, `deleteMessage`), plus `inject_message` / `inject_update` and duplicate-send counting. Also an `api_call(method, params)` adapter so the real `bridge_core.telegram.ingest` can run against it. |
| `fake_harness.py` | `FakeClock` (manually advanced; starts at wall time so it aligns with the real code's `time.time()` intake stamps), `FakeHarness` (wake receipts), `FakeTurn` (drives the real `bridge_core.telegram.main` intake, real `bridge_core.queue`, and real `send_and_close` / `send_batch_and_close` / `send_fenced` through a generated fake sender subprocess), `detect_features()` (probes the imported v2 code for warm lease / delivery ledger / auto-cover / edit probe / escalation hint so scenarios degrade instead of failing hard). |
| `run_scenario.py` | Scenario runner + CLI (`--list`, `--scenario`, `--mode`, `--report`, `--json`). |
| `report.py` | Thin wrapper: `report.py` == `run_scenario.py --report`. |
| `_kill_worker.py` | Subprocess worker for `kill-9-mid-send` (real intake + real `send_fenced`, then sleeps to be SIGKILLed). |

## How to run

From the repo root, with the repo's own venv (stdlib only, no
pytest — the suite is `unittest`):

```bash
cd <repo root>
# The bridge fails loud without a bot username (Job 1 template
# fix), so the runner needs one in the environment — any value
# works for the fake harness:
export TELEGRAM_BRIDGE_BOT_USERNAME=ExampleBot
muse-runtime/.venv/bin/python simulation/run_scenario.py --list
muse-runtime/.venv/bin/python simulation/run_scenario.py --scenario single-dm-reply --mode v2
muse-runtime/.venv/bin/python simulation/run_scenario.py --scenario all --mode baseline
muse-runtime/.venv/bin/python simulation/report.py
muse-runtime/.venv/bin/python simulation/run_scenario.py --report --json
# unit tests for fake_tg only:
muse-runtime/.venv/bin/python -m unittest discover -s muse-runtime/tests -p 'test_v2_simulation.py' -v
# (or the whole existing suite: muse-runtime/.venv/bin/python -m unittest discover -s muse-runtime/tests)
```

`--mode` only changes the label and the v2 pass bar for now; the
code under test is whatever is in the repo at run time.

## Scenarios

| Scenario | What it measures |
|---|---|
| `single-dm-reply` | 1 DM, 1 wake, 1 send; first-signal / reply latency (simulated). |
| `rapid-back-and-forth-5` | 5-message exchange; warm-lease hit rate (0/4 in the harness: FakeTurn drives `send_and_close`, not `--reply`, so the warm hold in `telegram._warm_hold` is exercised by the unit tests in `tests/test_telegram_reply.py`, not here). |
| `dm-plus-group-concurrent` | DM + group arriving together; independent lanes, no cross-lane blocking. |
| `dropped-wake` | Wake fires, turn never starts; recovery time to recycle + re-wake. Shipped recycle ~600 s. |
| `hang-no-heartbeat` | Claim with no heartbeat; recycle time. Shipped recycle ~600 s (the heartbeat horizon). Simulated clock, no real sleep. |
| `die-mid-send` | Turn dies after `send_fenced`, before the reply record; probe/ledger recovery, no duplicate. |
| `send-unknown-delivered` | Sender reports unknown but the send landed; edit-probe says delivered, closed with evidence, no re-send. |
| `send-unknown-not-delivered` | Sender reports unknown, nothing sent; item returns to pending, re-wake sends exactly once. |
| `sibling-single-send` | Batch of 3, turn answers only the first; expects siblings closed as DONE not replied when auto-cover is on, else reports orphans. |
| `quick-to-deep-escalation` | Quick turn, then a deep follow-up; expects a tier-escalation / re-hydration hint in the wake payload. |
| `warm-claim-no-heartbeat` | Warm-claimed batch, no heartbeat; v2 recycle target ~120 s. |
| `kill-9-mid-send` | Worker subprocess SIGKILLed mid-send; restart on the same state dir; recovery without a duplicate. |

Every result has: `duplicate_sends`, `silent_drops`,
`time_to_first_signal`, `time_to_reply`, `recovery_time`,
`warm_lease_hits`, `input_tokens_modelled`, `status`
(`pass` / `not-implemented` / `fail`), `notes`.

## Models and assumptions (read before quoting numbers)

- **Time is simulated.** The fake clock is advanced explicitly;
  recovery times (120/600 s) cost milliseconds of wall time.
  Modelled latencies: wake 0.5 s, quick work 2.0 s, deep work
  30.0 s, re-wake 0.5 s. Only `kill-9-mid-send` recovery_time is
  wall seconds (kill to reconcile).
- **Tokens are modelled** as `len(text)//4` on inbound text only.
  Provider caching is unverified; system prompt and history are
  not modelled, so the number is a floor.
- **The model turn is canned.** No LLM runs; reply texts are fixed
  strings. What is real: intake (`telegram.main`), queue
  (`claim_batch`, heartbeat, watchdog recycle, `mark_*`), and the
  send fence + sender subprocess boundary (fake sender script).
- **The sender subprocess is fake** but the boundary is real:
  `send_fenced` spawns it, parses its exit code / stdout
  message_id, and reports sent/unknown exactly as with the live
  `telegram-sender`.
- **Not-implemented is a verdict, not a crash.** If a v2 feature
  (warm lease, delivery ledger, sibling auto-cover, fast re-wake,
  escalation hint) is absent from the imported code, the scenario
  reports `not-implemented` with the measured fallback and keeps
  going.

## What this does NOT verify

Live hook harness wake latency, real tool-call timeouts, real
HTTP 409/429 from Telegram, the real `editMessageText` probe
against api.telegram.org, real Whisper transcription, or a real
VM restart. Those need the live test plan (inventory §6: test bot
first, never the main bot).
