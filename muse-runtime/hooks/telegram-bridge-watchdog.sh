#!/usr/bin/env bash
# Watchdog for the Telegram bridge queue (telegram-bridge-watchdog).
# Polls: any claimed item whose heartbeat went stale (its turn died
# mid-work)? Recycle it back to pending so the intake's next poll
# re-claims and re-wakes it. Silent on health and on successful
# repair (recovery is logged to watchdog_recoveries.log for the
# supervisor to report). Wakes an agent ONLY when the sweep itself
# fails.
# Never sends a Telegram message, never closes work, never touches
# "sending" items (their send outcome is unknown — only an operator
# reconciles those), never touches a live-heartbeat claim.
# Honors telegram-bridge/WATCHDOG_PAUSE: if that file exists, never
# sweep (an operator is working the queue by hand).
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"
# muse-runtime/ holds bridge_core/ and .venv. Derive it from this
# script's own location when run from the repo; after install to
# ~/hooks/scripts/, set BRIDGE_HOME to your checkout's muse-runtime/.
if [ -z "${BRIDGE_HOME:-}" ]; then
  _candidate="$(cd "$(dirname "$0")/.." && pwd || true)"
  if [ -d "$_candidate/bridge_core" ]; then BRIDGE_HOME="$_candidate"; fi
  unset _candidate
fi
: "${BRIDGE_HOME:?set BRIDGE_HOME to the muse-runtime directory of the bridge checkout}"
cd "$BRIDGE_HOME"
export TELEGRAM_BRIDGE_STATE_PATH=~/workspace/telegram-bridge/state.json
PAUSE_FILE=~/workspace/telegram-bridge/WATCHDOG_PAUSE
REC_LOG=~/workspace/telegram-bridge/watchdog_recoveries.log
STATE_DIR="$HOME/hooks/state/telegram-bridge-watchdog"
mkdir -p "$STATE_DIR"

if [ -f "$PAUSE_FILE" ]; then
  silent "watchdog paused by marker file" '{}'
fi

# One sweep at a time; overlapping polls bow out.
exec 9>"$STATE_DIR/sweep.lock"
if ! flock -n 9; then
  silent "sweep already in progress" '{}'
fi

sweep_out=$(mktemp /tmp/telegram-watchdog-out.XXXXXX)
sweep_rc=0
.venv/bin/python -m bridge_core.telegram --watchdog >"$sweep_out" 2>&1 || sweep_rc=$?
ts_now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [ "$sweep_rc" -ne 0 ]; then
  err_msg=$(tr '\n' ' ' < "$sweep_out" | head -c 300)
  printf '%s FAILED sweep_rc=%s %s\n' "$ts_now" "$sweep_rc" "$err_msg" >> "$REC_LOG"
  rm -f "$sweep_out"
  wake "telegram bridge watchdog sweep FAILED - queue may be stuck" "{\"at\":\"$ts_now\",\"detail\":\"$err_msg\"}"
fi

recycled=$(python3 -c "import json,sys; print(len(json.load(open('$sweep_out')).get('recycled', [])))" 2>/dev/null || echo 0)
pending=$(python3 -c "import json,sys; print(json.load(open('$sweep_out')).get('pending', 0))" 2>/dev/null || echo 0)
rm -f "$sweep_out"

if [ "$recycled" -gt 0 ]; then
  printf '%s recycled=%s pending=%s\n' "$ts_now" "$recycled" "$pending" >> "$REC_LOG"
  log "recycled-dead-claims" "{\"recycled\":$recycled,\"pending\":$pending}"
  silent "recycled" "{\"recycled\":$recycled,\"pending\":$pending}"
fi

silent "healthy" "{\"pending\":$pending}"
