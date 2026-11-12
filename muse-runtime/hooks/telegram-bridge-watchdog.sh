#!/usr/bin/env bash
# Watchdog for the Telegram bridge queue (telegram-bridge-watchdog).
# Polls: any claimed item whose heartbeat went stale (its turn died
# mid-work)? Recycle it back to pending so the intake's next poll
# re-claims and re-wakes it. Also releases never-started claims past
# the 120s claim lease (escalating at 3 attempts) and refreshes the
# ack status cards by EDITING the existing card messages — it never
# sends a new Telegram message, never closes work, never touches
# "sending" items (their send outcome is unknown — only an operator
# reconciles those), never touches a live-heartbeat claim. Also checks
# intake liveness: if the intake has not polled successfully in over
# INTAKE_STALE_SECONDS (reported by the sweep as intake_wake, once
# per stale episode), wake an agent so the stuck intake gets looked
# at. Also verifies the install on every sweep — the intake hook
# definition (valid and enabled), the checkout, the venv, and the
# sender — and wakes an agent when a check fails, once per episode.
# Also surfaces items stuck in "sending" past the 1h horizon
# (reported by the sweep as stuck_sending) and wakes an agent to
# reconcile them against the send evidence, and items whose claim
# lease is exhausted (lease_escalated, once per escalation) and
# wakes an agent to re-queue or answer them. Silent on health and
# on successful repair (recovery is logged to
# watchdog_recoveries.log for the supervisor to report). Wakes an
# agent ONLY when the sweep itself fails (at most once per 600s — a
# file-based cooldown, so a failing sweep does not wake on every
# 60s poll), the intake is stale, a health check fails, a send is
# stuck, or a claim lease is exhausted.
# Honors telegram-bridge/WATCHDOG_PAUSE: if that file exists, never
# sweep (an operator is working the queue by hand).
set -euo pipefail
# The hook runtime provides wake/silent/log. Outside the Muse hook
# runtime HATCH_HOOK_RUNTIME is unset; say so plainly instead of
# failing with an "unbound variable" error under set -u.
if [ -z "${HATCH_HOOK_RUNTIME:-}" ]; then
  echo "telegram-bridge-watchdog.sh requires the Muse hook runtime (HATCH_HOOK_RUNTIME is not set)" >&2
  exit 1
fi
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
export TELEGRAM_BRIDGE_SEND_CMD=~/workspace/custom-connectors/custom.telegram-muse-bot/bin/telegram-sender
# No TELEGRAM_BRIDGE_BOT_USERNAME export here on purpose: the sweep
# resolves the bot username from state.json, where the intake cached
# it (see bridge_core.telegram._bot_username). A plain re-copy of
# this script over an install must not break the sweep.
export TELEGRAM_BRIDGE_HOOK_DEFINITIONS_DIR=~/hooks/definitions
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
  # Sweep-failure wake cooldown (600s, file-based): a failing
  # sweep would otherwise wake an agent on every 60s poll. Only
  # wake if more than 600s have passed since the last
  # sweep-failure wake; otherwise log and stay silent.
  last_wake_file="$STATE_DIR/last-sweep-failure-wake"
  last_wake=0
  if [ -f "$last_wake_file" ]; then
    last_wake=$(cat "$last_wake_file" 2>/dev/null || echo 0)
    case "$last_wake" in ''|*[!0-9]*) last_wake=0 ;; esac
  fi
  now_epoch=$(date +%s)
  if [ $((now_epoch - last_wake)) -gt 600 ]; then
    printf '%s' "$now_epoch" > "$last_wake_file"
    wake "telegram bridge watchdog sweep FAILED - queue may be stuck" "{\"at\":\"$ts_now\",\"detail\":\"$err_msg\"}"
  else
    silent "sweep FAILED (wake cooldown)" "{\"detail\":\"$err_msg\"}"
  fi
fi

recycled=$(python3 -c "import json,sys; print(len(json.load(open('$sweep_out')).get('recycled', [])))" 2>/dev/null || echo 0)
pending=$(python3 -c "import json,sys; print(json.load(open('$sweep_out')).get('pending', 0))" 2>/dev/null || echo 0)
intake_wake=$(python3 -c "import json,sys; print(json.load(open('$sweep_out')).get('intake_wake', False))" 2>/dev/null || echo False)
intake_age=$(python3 -c "import json,sys; print(json.load(open('$sweep_out')).get('intake_age_seconds'))" 2>/dev/null || echo null)
health_wake=$(python3 -c "import json,sys; print(json.load(open('$sweep_out')).get('health_wake', False))" 2>/dev/null || echo False)
health_failed=$(python3 -c "import json,sys; d=json.load(open('$sweep_out')).get('health') or {}; print(','.join(n for n,c in d.get('checks', {}).items() if not c.get('ok')))" 2>/dev/null || echo "")
health_recovered=$(python3 -c "import json,sys; print(json.load(open('$sweep_out')).get('health_recovered', False))" 2>/dev/null || echo False)
intake_recovered=$(python3 -c "import json,sys; print(json.load(open('$sweep_out')).get('intake_recovered', False))" 2>/dev/null || echo False)
stuck_sending=$(python3 -c "import json,sys; print(','.join(str(i) for i in json.load(open('$sweep_out')).get('stuck_sending', [])))" 2>/dev/null || echo "")
lease_escalated=$(python3 -c "import json,sys; print(','.join(str(i) for i in json.load(open('$sweep_out')).get('lease_escalated', [])))" 2>/dev/null || echo "")
rm -f "$sweep_out"

# Stale intake: wake once per episode (the sweep enforces the
# cooldown and clears it when the intake recovers). This exits
# via wake, so a stale intake is reported instead of "healthy".
if [ "$intake_wake" = "True" ]; then
  printf '%s intake-stale age=%s recycled=%s pending=%s\n' "$ts_now" "$intake_age" "$recycled" "$pending" >> "$REC_LOG"
  wake "telegram bridge intake stale - no successful poll for ${intake_age}s" "{\"at\":\"$ts_now\",\"intake_age_seconds\":$intake_age,\"recycled\":$recycled,\"pending\":$pending}"
fi

# Failed health check: wake once per episode (the sweep enforces
# the cooldown and clears it when the install recovers). This exits
# via wake, so a broken install is reported instead of "healthy".
# The watchdog never repairs a definition, checkout, venv, or
# sender itself — the wake is the repair path.
if [ "$health_wake" = "True" ]; then
  printf '%s health failed checks=%s recycled=%s pending=%s\n' "$ts_now" "$health_failed" "$recycled" "$pending" >> "$REC_LOG"
  wake "telegram bridge health check failed - $health_failed" "{\"at\":\"$ts_now\",\"failed_checks\":\"$health_failed\",\"recycled\":$recycled,\"pending\":$pending}"
fi

# Stuck send: an item sat in "sending" past the 1h horizon, so its
# send outcome is unknown and no later step can close it. Wake an
# agent to reconcile it against the send evidence (the watchdog
# prompt's (D) branch): --replied + --done if it went out,
# --send-failed if it provably never did, nothing if still
# unknown. The script never marks it itself.
if [ -n "$stuck_sending" ]; then
  printf '%s stuck-sending ids=%s\n' "$ts_now" "$stuck_sending" >> "$REC_LOG"
  wake "telegram bridge send stuck in sending - $stuck_sending" "{\"at\":\"$ts_now\",\"stuck_sending\":\"$stuck_sending\"}"
fi

# Lease exhausted: an item's turn never started 3 times running
# (120s lease each), so the sweeper stopped re-waking it and its
# ack card says flagged for follow-up. The sweep reports each
# escalation once, so this wakes an agent once per escalation,
# never on every poll. The prompt's (E) branch tells the agent
# what to do: answer it directly, or --clear-lease to re-queue
# it for the intake's next poll.
if [ -n "$lease_escalated" ]; then
  printf '%s lease-escalated ids=%s\n' "$ts_now" "$lease_escalated" >> "$REC_LOG"
  wake "telegram bridge claim lease exhausted - $lease_escalated" "{\"at\":\"$ts_now\",\"lease_escalated\":\"$lease_escalated\"}"
fi

# A recovery is only counted once fresh output is seen (the sweep
# reports it on the clearing sweep): log it so the supervisor can
# report that a stale intake or a failed health check cleared.
if [ "$health_recovered" = "True" ] || [ "$intake_recovered" = "True" ]; then
  printf '%s recovered health=%s intake=%s pending=%s\n' "$ts_now" "$health_recovered" "$intake_recovered" "$pending" >> "$REC_LOG"
fi

if [ "$recycled" -gt 0 ]; then
  printf '%s recycled=%s pending=%s\n' "$ts_now" "$recycled" "$pending" >> "$REC_LOG"
  log "recycled-dead-claims" "{\"recycled\":$recycled,\"pending\":$pending}"
  silent "recycled" "{\"recycled\":$recycled,\"pending\":$pending}"
fi

silent "healthy" "{\"pending\":$pending}"
