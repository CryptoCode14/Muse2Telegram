#!/usr/bin/env bash
# Poll the Telegram bridge intake every run. Wake an agent only when a
# message needs this Muse; otherwise close the batch silently or hold it.
#
# Routing lives in telegram-bridge-decide.py (free-for-all + @mention):
#   wake - a DM, a command, an @mention of this bot, or an open human
#          message in the group with no @mention at all.
#   hold - paused and no command; the payload is appended to held.jsonl
#          for the resume turn to answer or close.
#   done - nothing in the batch needs this Muse; close each item with
#          no reply so it never wakes anyone.
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"
cd ~/workspace/muse-agent-bridge-eval
export TELEGRAM_BRIDGE_STATE_PATH=~/workspace/telegram-bridge/state.json
export TELEGRAM_BRIDGE_SEND_CMD=~/workspace/custom-connectors/custom.telegram-muse-bot/bin/telegram-sender
export TELEGRAM_BRIDGE_BOT_USERNAME="YOUR_BOT_USERNAME"  # <-- set to your bot username
export TELEGRAM_BRIDGE_PAUSED_PATH=~/workspace/telegram-bridge/PAUSED
export TELEGRAM_BRIDGE_HELD_PATH=~/workspace/telegram-bridge/held.jsonl
# Settle window (seconds): when a message lands, intake waits out
# the rest of this window and re-fetches once, so a rapid follow-up
# joins the same batch instead of becoming a second turn.
export TELEGRAM_BRIDGE_SETTLE_SECONDS=2
DECIDER=~/hooks/scripts/telegram-bridge-decide.py

payload=""
intake_rc=0
err_file=$(mktemp /tmp/telegram-intake-err.XXXXXX)
payload=$(.venv/bin/python -m bridge_core.telegram 2>"$err_file") || intake_rc=$?
# Log intake failures with a timestamp instead of swallowing them
# silently. A failed intake (bad credential, corrupt state) used to
# look exactly like "no message", so the bot could go dark unnoticed.
if [ "$intake_rc" -ne 0 ]; then
  err_msg=$(tr '\n' ' ' < "$err_file" | head -c 300)
  printf '%s intake failed (exit %s): %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$intake_rc" "$err_msg" >> ~/workspace/telegram-bridge/intake-errors.log
  # Keep the FULL traceback too: the one-line summary above truncates
  # it to 300 chars, which once hid the real exception behind the
  # module entry point. A future failure is diagnosable from this file.
  {
    printf '=== %s intake failed (exit %s) ===\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$intake_rc"
    cat "$err_file"
    printf '\n'
  } >> ~/workspace/telegram-bridge/intake-errors-full.log
  rm -f "$err_file"
  silent "telegram-intake-failed"
  exit 0
fi
rm -f "$err_file"
if [ -z "$payload" ]; then
  silent "no-telegram-message"
  exit 0
fi

decision=$(printf '%s' "$payload" | python3 "$DECIDER")
# The decider prints "wake <tier>" / "hold <tier>" / "done <ids>" /
# "empty". The tier is rule-based (see telegram-bridge-decide.py)
# and only changes how much context the woken agent loads.
action=${decision%% *}
tier=${decision#* }
if [ "$tier" = "$decision" ] || [ -z "$tier" ]; then tier="standard"; fi
case "$tier" in quick|standard|deep) ;; *) tier="standard" ;; esac
# Inject the tier into the payload JSON so the woken agent (via the
# router prompt in the hook definition) knows which prompt file to
# follow. Validates to compact JSON; the runtime compacts it again.
inject_tier() {
  python3 -c "import json,sys; p=json.load(sys.stdin); p['tier']=sys.argv[1]; print(json.dumps(p, separators=(',',':')))" "$1" <<<"$payload"
}
case "$action" in
  wake)
    # The typing indicator is NOT fired here. Typing must mean "I am
    # responding": the agent fires it only after it decides to reply
    # (see the wake prompt). Firing it on wake would show typing even
    # when the agent decides there is nothing to add.
    payload=$(inject_tier "$tier")
    wake "telegram-message" "$payload"
    ;;
  hold)
    payload=$(inject_tier "$tier")
    printf '%s\n' "$payload" >> "$TELEGRAM_BRIDGE_HELD_PATH"
    silent "telegram-held-while-paused"
    ;;
  done)
    claim_token=$(printf '%s' "$payload" | python3 -c "import json,sys; print(json.load(sys.stdin).get('claim_token',''))")
    for update_id in ${decision#done }; do
      .venv/bin/python -m bridge_core.telegram --done "$update_id" "$claim_token" >/dev/null 2>&1 || true
    done
    silent "telegram-not-for-me"
    ;;
  *)
    silent "telegram-no-decision"
    ;;
esac
