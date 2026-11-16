#!/bin/bash
# intake-watchdog.sh — token-free intake watchdog.
#
# Runs from a systemd timer every 5 minutes (intake-watchdog.timer).
# It does the same liveness check the cron watchdog did, but as a plain
# script: no agent turn, no tokens.
#
# What it does each run:
#   1. Reads Telegram last_successful_poll and A2A last_poll from state
#      files and computes their ages.
#   2. Healthy (telegram <= 180s, a2a <= 120s): logs one line, exits.
#   3. Telegram stale: runs the bridge poll directly
#      (bridge_core.telegram, same env as the hook intake). The poll claims
#      any pending batch and refreshes last_successful_poll. Claimed items
#      are processed by the hook intake when the scheduler resumes — the
#      claim lease is 600s, so worst case ~10 min after resume. A message
#      arriving mid-freeze is therefore answered within ~15 min, with zero
#      agent turns spent watching.
#   4. A2A stale: logs it. A2A tasks need an agent turn to process, and no
#      token-free wake exists (hook `wake` only works inside a hook run;
#      there is no hatch wake CLI). The intake's 60s list sweep catches up
#      on resume.
#
# Exit 0 on healthy or recovered; exit 1 if the direct poll itself fails
# (systemd records the failure; the next run retries).
set -u

# NOTE: do NOT use $HOME here — under the systemd timer HOME is /root,
# not the owner's home. Set INTAKE_WATCHDOG_HOME to the install base
# (the directory containing workspace/); default is this deployment's path.
BASE="${INTAKE_WATCHDOG_HOME:-/home/hatch}"
BRIDGE_HOME="$BASE/workspace/Muse2Telegram/muse-runtime"
STATE="$BASE/workspace/telegram-bridge/state.json"
A2A_STATE="$BASE/hooks/state/a2a-intake.json"
LOG="$BASE/workspace/telegram-bridge/intake-watchdog-script.log"
TELEGRAM_STALE=180
A2A_STALE=120

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '%s %s\n' "$(ts)" "$*" >> "$LOG"; }

now=$(date +%s)

tg_age=""
a2a_age=""
if [[ -r "$STATE" ]]; then
    tg_age=$(python3 -c "import json,sys,time; d=json.load(open('$STATE')); print(round(time.time()-d.get('last_successful_poll',0),1))" 2>/dev/null || echo "")
fi
if [[ -r "$A2A_STATE" ]]; then
    a2a_age=$(python3 -c "import json,sys,time; d=json.load(open('$A2A_STATE')); print(round(time.time()-d.get('last_poll',0),1))" 2>/dev/null || echo "")
fi

if [[ -z "$tg_age" || -z "$a2a_age" ]]; then
    log "ERROR cannot read state (telegram_age=${tg_age:-?} a2a_age=${a2a_age:-?})"
    exit 1
fi

tg_stale=0; a2a_stale=0
python3 -c "exit(0 if $tg_age > $TELEGRAM_STALE else 1)" 2>/dev/null && tg_stale=1
python3 -c "exit(0 if $a2a_age > $A2A_STALE else 1)" 2>/dev/null && a2a_stale=1

if [[ "$tg_stale" -eq 0 && "$a2a_stale" -eq 0 ]]; then
    log "healthy telegram_age=$tg_age a2a_age=$a2a_age"
    exit 0
fi

# Telegram stale: run the poll directly.
note=""
if [[ "$tg_stale" -eq 1 ]]; then
    out=$(cd "$BRIDGE_HOME" && TELEGRAM_BRIDGE_STATE_PATH="$STATE" \
        TELEGRAM_BRIDGE_SEND_CMD="$BASE/workspace/custom-connectors/custom.telegram-muse-bot/bin/telegram-sender" \
        TELEGRAM_BRIDGE_BOT_USERNAME=${TELEGRAM_BRIDGE_BOT_USERNAME:-your-bot-username} \
        TELEGRAM_BRIDGE_LONG_POLL_SECONDS=0 \
        timeout 60 .venv/bin/python -m bridge_core.telegram 2>&1)
    rc=$?
    if [[ $rc -ne 0 ]]; then
        log "ERROR direct poll failed rc=$rc telegram_age=$tg_age a2a_age=$a2a_age out=$(printf '%s' "$out" | head -c 200)"
        exit 1
    fi
    # Count claimed items in the payload, if any.
    items=$(printf '%s' "$out" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    n=0
    for k in ('items','updates','claimed','messages'):
        v=d.get(k)
        if isinstance(v,list): n=max(n,len(v))
    print(n)
except Exception:
    print(0)
" 2>/dev/null || echo 0)
    new_age=$(python3 -c "import json,time; print(round(time.time()-json.load(open('$STATE')).get('last_successful_poll',0),1))" 2>/dev/null || echo "?")
    note="telegram poll ran, claimed_items=$items last_poll_age_now=$new_age"
fi

if [[ "$a2a_stale" -eq 1 ]]; then
    note="$note a2a_stale (needs agent turn on resume; sweep catches up)"
fi

log "recovered telegram_age=$tg_age a2a_age=$a2a_age $note"
exit 0
