#!/usr/bin/env bash
# Poll the Telegram bridge intake every run. Wake an agent only when a
# message needs this Muse; otherwise close the batch silently or hold it.
#
# Routing lives in telegram-bridge-decide.py (free-for-all + @mention):
#   wake - a DM, a command, an @mention of this bot, or an open human
#          message in the group with no @mention at all. On wake the
#          hook sends the ack status card for the batch
#          (--send-claim-ack, one message with the queue position)
#          and then wakes the agent: the card is the receipt that
#          covers the wait before any turn starts. (Messages that
#          arrived while the lane was busy already got their card
#          at intake, from run_once itself; --send-claim-ack
#          adopts an existing card and never sends a second.)
#          The hook never fires the typing indicator: typing means
#          "I am responding", so it fires only while a turn is actually
#          running (the agent fires it after deciding to reply).
#   hold - paused and no command; each item is held in the queue
#          itself (wake_after +24h) for the resume turn to
#          re-claim with --unhold and answer.
#   done - nothing in the batch needs this Muse; close each item with
#          no reply so it never wakes anyone.
set -euo pipefail
# The hook runtime provides wake/silent/log. Outside the Muse hook
# runtime HATCH_HOOK_RUNTIME is unset; say so plainly instead of
# failing with an "unbound variable" error under set -u.
if [ -z "${HATCH_HOOK_RUNTIME:-}" ]; then
  echo "telegram-bridge-intake.sh requires the Muse hook runtime (HATCH_HOOK_RUNTIME is not set)" >&2
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
export TELEGRAM_BRIDGE_BOT_USERNAME="YOUR_BOT_USERNAME"  # <-- set to your bot username
# Refuse the literal placeholder: it looks like a username but is
# not one, and polling as it would answer @mentions meant for nobody.
if [ "$TELEGRAM_BRIDGE_BOT_USERNAME" = "YOUR_BOT_USERNAME" ]; then
  echo "TELEGRAM_BRIDGE_BOT_USERNAME is still the placeholder YOUR_BOT_USERNAME: set it to your bot's Telegram username (no '@') in telegram-bridge-intake.sh" >&2
  exit 1
fi
# export TELEGRAM_BRIDGE_BOT_ALIASES=""  # <-- optional: short names that also @mention this bot, comma-separated, no @ (e.g. "Example")
export TELEGRAM_BRIDGE_PAUSED_PATH=~/workspace/telegram-bridge/PAUSED
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
# Hook-layer helper from the decider module (hydrate_payload),
# run without executing its main(). Reads the payload on stdin;
# prints the hydrated payload JSON on line 1.
hydrate_wake_payload() {
  python3 -c 'import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("telegram_bridge_decide", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
payload = json.load(sys.stdin)
print(json.dumps(mod.hydrate_payload(payload), separators=(",", ":")))' "$DECIDER"
}
case "$action" in
  wake)
    # Pre-hydrate context_md into the payload (bounded to 6000
    # chars, null when context.md is missing — never fails the
    # wake). If hydration itself fails, wake with the original
    # payload instead of going silent.
    if hydrated=$(printf '%s' "$payload" | hydrate_wake_payload 2>/dev/null); then
      payload=$(printf '%s' "$hydrated" | sed -n '1p')
    fi
    # Record when this wake is issued, so --latency-report can
    # measure wake latency (wake issued -> turn started). The claim
    # token comes from the payload; a stamp failure never blocks
    # the wake.
    claim_token=$(printf '%s' "$payload" | python3 -c "import json,sys; print(json.load(sys.stdin).get('claim_token',''))")
    if [ -n "$claim_token" ]; then
      .venv/bin/python -m bridge_core.telegram --wake-issued "$claim_token" >/dev/null 2>&1 || true
      # Send the ack status card for the batch (one message, with
      # the queue position) before waking: the card is the receipt
      # that covers the wait before any turn starts, and the
      # sweeper edits that same message to keep it current. When
      # a batch item already carries an intake card, this adopts
      # it and sends nothing — never a second card. A
      # failed card never blocks the wake — the turn's own
      # --send-ack still works, and the wake is the priority.
      .venv/bin/python -m bridge_core.telegram --send-claim-ack "$claim_token" >/dev/null 2>&1 || true
    fi
    wake "telegram-message" "$payload"
    ;;
  hold)
    # Hold the batch in the queue itself (wake_after +24h), never
    # in a side file: the queue is the store of record, and the
    # resume turn re-claims the items with --unhold. The claim
    # token comes from the payload; a hold that fails for one item
    # (stale token) leaves the rest held.
    claim_token=$(printf '%s' "$payload" | python3 -c "import json,sys; print(json.load(sys.stdin).get('claim_token',''))")
    ids=$(printf '%s' "$payload" | python3 -c "import json,sys; print(' '.join(str(it.get('update_id')) for it in json.load(sys.stdin).get('items',[]) if it.get('update_id') is not None))")
    for update_id in $ids; do
      .venv/bin/python -m bridge_core.telegram --hold "$update_id" "$claim_token" >/dev/null 2>&1 || true
    done
    silent "telegram-held-while-paused"
    ;;
  done)
    claim_token=$(printf '%s' "$payload" | python3 -c "import json,sys; print(json.load(sys.stdin).get('claim_token',''))")
    ids=${decision#done }
    # Guard: a bare "done" (no ids — nothing woke and no item
    # carried an update id) expands to the literal word "done";
    # never pass it to --done as an update id.
    if [ -n "$ids" ] && [ "$ids" != "done" ]; then
      for update_id in $ids; do
        .venv/bin/python -m bridge_core.telegram --done "$update_id" "$claim_token" >/dev/null 2>&1 || true
      done
    fi
    silent "telegram-not-for-me"
    ;;
  *)
    silent "telegram-no-decision"
    ;;
esac
