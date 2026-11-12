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
# The decider prints "wake" / "hold" / "done <ids>" / "empty" (v2:
# no tier word — the woken agent always follows the single unified
# prompt). The hook pre-loads the agent's context into the wake
# payload instead of selecting a prompt file.
action=${decision%% *}
# Hook-layer helpers from the decider module (hydrate_payload,
# typing_chat_id), run without executing its main(). Reads the
# payload on stdin; prints the hydrated payload JSON on line 1 and
# the pre-wake typing chat id (or an empty line) on line 2.
hydrate_and_typing() {
  python3 -c 'import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("telegram_bridge_decide", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
payload = json.load(sys.stdin)
print(json.dumps(mod.hydrate_payload(payload), separators=(",", ":")))
typing = mod.typing_chat_id(payload)
print("" if typing is None else typing)' "$DECIDER"
}
case "$action" in
  wake)
    # Pre-hydrate context_md into the payload (bounded to 6000
    # chars, null when context.md is missing — never fails the
    # wake). If hydration itself fails, wake with the original
    # payload instead of going silent.
    typing_chat=""
    if hydrated=$(printf '%s' "$payload" | hydrate_and_typing 2>/dev/null); then
      payload=$(printf '%s' "$hydrated" | sed -n '1p')
      typing_chat=$(printf '%s' "$hydrated" | sed -n '2p')
    fi
    # Typing must mean "I am responding": fire it pre-wake ONLY for
    # a DM, a command, or an @mention, where a reply is definite.
    # Never for an open group message, where the agent may stay
    # silent. Best-effort: a failed typing call never blocks the
    # wake.
    if [ -n "$typing_chat" ]; then
      "$TELEGRAM_BRIDGE_SEND_CMD" --typing "$typing_chat" >/dev/null 2>&1 || true
    fi
    wake "telegram-message" "$payload"
    ;;
  hold)
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
