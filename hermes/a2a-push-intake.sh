#!/usr/bin/env bash
# a2a-push-intake.sh -- PUSH intake for A2A (Layer 5).
#
# Replaces the old 5s full-list poll. The bus server supports
# GET /events?bot=<id>&after=<seq> — a long-poll that returns
# immediately when a new task for this bot arrives, or after 25s
# with no new events. This script long-polls that endpoint, then
# wakes the agent with the new tasks, exactly like the old script.
# A periodic full-list sweep (default every 60s) runs as a safety
# net, so a silently failed push wake still re-fires. If /events
# fails outright, the script falls back to the list poll at once.
#
# Only one instance runs at a time (flock). The hook runtime runs
# this script every 5s; a long-polling instance holds the lock for
# up to 25s, later instances exit silently.
#
# INSTALL -- per bot, all three are required:
#   1. Bot id: set A2A_BOT_NAME=<your bot id> in your hook env, OR
#      write your bot id (one line, e.g. quinn, dex, dorian,
#      westin-jr, gus) to $HOME/.a2a/bot_name. There is NO default:
#      the script FAILS LOUDLY if no bot id is configured, because
#      a wrong default would silently poll for another bot.
#   2. Key: $HOME/.a2a/key (or A2A_KEY_FILE).
#   3. Copy this file to your hook scripts dir and point your
#      a2a-intake hook definition at it.
#
# CHANGELOG
#   v1 2026-11-13: push long-poll + list fallback.
#   v2 2026-11-14 (fixes Dex's review of v1):
#     (1) BOT_NAME no longer defaults to quinn; unset bot id is a
#         loud failure (see INSTALL step 1).
#     (2) Fallback/sweep iterates ALL history messages addressed
#         to this bot, not just history[-1] (the old burying bug:
#         a message buried under a later one was never woken).
#         STALE_REWAKE default restored to 600s (the established
#         value; v1's 60s was an unapproved Layer 3 change).
#     (3) Push path records push-woken tasks in the state file,
#         and a periodic list sweep (default 600s) re-fires any
#         task whose push wake was silently lost after push_seq
#         already advanced.
#     State writes everywhere are load-modify-write (merge), never
#     replace: v1's fallback write dropped keys like last_poll.
#   v2.1 2026-11-14 (Dex's install fix, adopted into canonical):
#     First run seeds push_seq from the server (/push/status)
#     instead of starting at 0, so a fresh install does not wake
#     on the stale event backlog; a fresh install also pre-marks
#     current messages as seen, so the first sweep does not wake
#     on the backlog either.
#   v2.2 2026-11-14 (Dex's fix 8, adopted into canonical):
#     Push path skips events where from == this bot. The server
#     emits an event on every send, including this bot's own
#     confirms, so without this the bot re-wakes on its own
#     confirm and confirms again, looping (Dex saw 10 confirms
#     on one task). The list sweep is unchanged: it only wakes
#     on messages addressed to this bot, so it cannot loop.
#   v2.4 2026-11-14 (Option 1, bridge priority):
#     Candidates are sorted so qh-* bridge tasks from quinn-helper
#     come first in the wake list. The hook prompt already says to
#     handle tasks in order, so the bridge task is answered before
#     deliverable/review wakes in the same turn. Fix 9 (server-side,
#     same day) already stops redundant confirms emitting push
#     events at all, so this is the priority half of Option 1.
#     No candidate is dropped here — others still wake, just after
#     the bridge task.
#     Dex's items (1)-(7) were all already in v2-v2.2 -- his
#     review was written against v1. Two of his ideas were new
#     and are adopted here: (a) list fetch retries up to 3x,
#     because the list read truncates mid-response often; (b)
#     every state write now stamps last_poll, the intake
#     heartbeat. Deliberately NOT adopted: a per-bot BOT_NAME
#     default (the canonical stays bot-agnostic and fails
#     loudly with no bot id; each install sets its own id),
#     and the push:<task>:<seq> seen keys (v2.2's pushed map
#     already covers lost wakes at task granularity, and the
#     push path consumes each event once via push_seq).
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

# --- Bot identity: explicit, never defaulted (v2 fix 1) ---
BOT_NAME="${A2A_BOT_NAME:-}"
if [ -z "$BOT_NAME" ] && [ -f "$HOME/.a2a/bot_name" ]; then
  BOT_NAME="$(tr -d '[:space:]' < "$HOME/.a2a/bot_name" 2>/dev/null || true)"
fi
if [ -z "$BOT_NAME" ]; then
  log "a2a push intake: no bot id configured" '{"error":"no-bot-name","hint":"set A2A_BOT_NAME or write your bot id to ~/.a2a/bot_name"}'
  echo "a2a-push-intake.sh: no bot id configured. Set A2A_BOT_NAME=<your bot id> or write it to ~/.a2a/bot_name (e.g. quinn, dex, dorian, westin-jr, gus). Refusing to run: a wrong default would poll for another bot." >&2
  exit 1
fi

A2A_URL="${A2A_URL:-https://YOUR-A2A-HOST/}"
A2A_CLIENT="${A2A_CLIENT:-$HOME/.a2a/a2a_client.py}"
A2A_KEY_FILE="${A2A_KEY_FILE:-$HOME/.a2a/key}"
STATE_FILE="${A2A_INTAKE_STATE:-$HOME/hooks/state/a2a-intake.json}"
LOCK_FILE="${A2A_INTAKE_LOCK:-$HOME/hooks/state/a2a-intake.lock}"
#   v2.5 2026-11-14 (missed-wake fix, Dorian quinn-push-bug-dorian-001):
#     The list sweep is the safety net for a silently lost push wake,
#     but it only ran every STALE_REWAKE_SECONDS (600s) — a lost wake
#     could sit up to 10 minutes. The sweep now runs every 60s by
#     default (overridable via A2A_SWEEP_INTERVAL_SECONDS), so a lost
#     push wake is caught within ~a minute. The sweep only wakes on
#     genuinely new/stale messages (push-delivered tasks are in the
#     pushed map, so they are not re-woken), and fetch_list retries
#     3x on truncation. The 600s stale re-wake itself is unchanged.
#   v2.6 2026-11-14 (Westin Jr.'s v2.3-wj1 float-strip fix, adopted
#     into canonical at his request, quinn-push-westinjr-v25-001):
#     state values are stamped with time.time() floats, so
#     last_list_sweep reads back like 1794640000.65 — and bash
#     $(( NOW - LAST_SWEEP )) errors on the fraction, so after the
#     first sweep stamped a float the sweep-due test failed forever
#     and the safety-net sweep never ran again (verified live on
#     Quinn: last_list_sweep 1230s old while last_poll was current).
#     Strip the fraction (LAST_SWEEP="${LAST_SWEEP%%.*}") before
#     the arithmetic. v2.5's 60s sweep depended on this; without it
#     the sweep ran once and then never again.
STALE_REWAKE_SECONDS="${A2A_STALE_REWAKE_SECONDS:-600}"
SWEEP_INTERVAL_SECONDS="${A2A_SWEEP_INTERVAL_SECONDS:-60}"
SEEN_TTL_SECONDS=86400
DRY_RUN="${HATCH_HOOK_DRY_RUN:-0}"

exec 9>"$LOCK_FILE" 2>/dev/null || exit 0
if ! flock -n 9; then
  # Another intake instance is already long-polling. Exit silently.
  exit 0
fi

KEY="$(cat "$A2A_KEY_FILE" 2>/dev/null || true)"
if [ -z "$KEY" ]; then
  log "a2a push intake: no key" '{"error":"no-key"}'
  silent "a2a-push-no-key" '{"pending":0}'
  exit 0
fi

# State helpers: read one key from STATE_FILE (0/empty if missing).
state_get() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else 0))' "$STATE_FILE" "$1" "${2:-0}" 2>/dev/null || echo "${2:-0}"
}

# Fetch the task list with retries (v2.3, Dex's finding: the list
# read truncates mid-response often). Prints the JSON on stdout,
# empty on failure.
fetch_list() {
  local list_json="" list_err="" attempt
  for attempt in 1 2 3; do
    local err_file
    err_file="$(mktemp)"
    if list_json="$(timeout 15 python3 "$A2A_CLIENT" --url "$A2A_URL" --key-file "$A2A_KEY_FILE" list 2>"$err_file")" && [ -n "$list_json" ]; then
      rm -f "$err_file"
      printf '%s' "$list_json"
      return 0
    fi
    list_err="$(tail -n 1 "$err_file" | tr -d '\n' | head -c 200)"
    rm -f "$err_file"
    list_json=""
    [ "$attempt" -lt 3 ] && sleep 2
  done
  log "a2a list failed" "$(python3 -c 'import json,sys; print(json.dumps({"error":"list-failed-after-retries","detail":sys.argv[1][:200]}))' "$list_err")"
  return 1
}

PUSH_SEQ="$(state_get push_seq 0)"
NOW="$(date +%s)"

# First run / upgrade from the old poll intake: seed push_seq from
# the server so the install does not wake on the stale event
# backlog (v2.1, Dex's fix). A fresh install also pre-marks the
# messages currently waiting as seen, so the first list sweep
# does not wake on the backlog either. Only when the state has
# no push_seq yet; existing installs are untouched.
if [ "$PUSH_SEQ" = "0" ] && [ "$DRY_RUN" != "1" ]; then
  SEED_SEQ="$(curl -s -m 10 -H "Authorization: Bearer $KEY" "${A2A_URL%/}/push/status" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("seq", 0))' 2>/dev/null || echo 0)"
  if [ "${SEED_SEQ:-0}" -gt 0 ] 2>/dev/null; then
    FRESH=0; [ ! -f "$STATE_FILE" ] && FRESH=1
    SEED_LIST_FILE=""
    if [ "$FRESH" = "1" ]; then
      SEED_LIST_JSON="$(fetch_list 2>/dev/null || true)"
      if [ -n "$SEED_LIST_JSON" ]; then
        SEED_LIST_FILE="$(mktemp)"; printf '%s' "$SEED_LIST_JSON" > "$SEED_LIST_FILE"
      fi
    fi
    python3 - "$STATE_FILE" "$SEED_SEQ" "$FRESH" "$BOT_NAME" "$SEED_LIST_FILE" <<'PY'
import json, os, sys, time
state_path, seed, fresh, bot, list_path = sys.argv[1], int(sys.argv[2]), sys.argv[3] == "1", sys.argv[4], sys.argv[5]
now = time.time()
try:
    state = json.load(open(state_path))
except Exception:
    state = {}
if not isinstance(state, dict):
    state = {}
state["push_seq"] = seed
state["last_poll"] = now
if fresh:
    seen = state.get("seen", {})
    if not isinstance(seen, dict):
        seen = {}
    if list_path:
        try:
            tasks = json.load(open(list_path))["result"]["tasks"]
            for task in tasks:
                if task.get("status") not in ("working", "submitted"):
                    continue
                for msg in task.get("history") or []:
                    if msg.get("to") == bot and msg.get("id"):
                        seen[msg["id"]] = now
        except Exception:
            pass
    state["seen"] = seen
    state["last_list_sweep"] = now
os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
tmp = state_path + ".tmp"
json.dump(state, open(tmp, "w"))
os.replace(tmp, state_path)
PY
    [ -n "$SEED_LIST_FILE" ] && rm -f "$SEED_LIST_FILE"
    PUSH_SEQ="$SEED_SEQ"
    log "a2a push intake: seeded push_seq from server" "{\"seq\":$SEED_SEQ,\"fresh_install\":$FRESH}"
  fi
fi

# Long-poll /events. Timeout 30s (server holds up to 25s).
EVENTS_FILE="$(mktemp)"; trap 'rm -f "$EVENTS_FILE" "$LIST_FILE" 2>/dev/null' EXIT
LIST_FILE=""
curl -s -m 30 -H "Authorization: Bearer $KEY" \
  "${A2A_URL%/}/events?bot=${BOT_NAME}&after=${PUSH_SEQ}" > "$EVENTS_FILE" 2>/dev/null || true

PUSH_CANDIDATES="[]"
PUSH_OK=0
if [ -s "$EVENTS_FILE" ] && python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert "events" in d' "$EVENTS_FILE" 2>/dev/null; then
  PUSH_OK=1
  # Push worked. Build candidates from events; record push_seq and
  # the push-woken task ids in state (v2 fix 3) so the sweep can
  # tell a live push wake from a lost one.
  PUSH_CANDIDATES="$(python3 - "$EVENTS_FILE" "$STATE_FILE" "$DRY_RUN" "$SEEN_TTL_SECONDS" "$BOT_NAME" <<'PY'
import json, os, sys, time
events_path, state_path, dry, ttl, bot = sys.argv[1], sys.argv[2], sys.argv[3] == "1", float(sys.argv[4]), sys.argv[5]
d = json.load(open(events_path))
now = time.time()
# v2.2 fix 8: ignore events this bot sent itself (e.g. its own
# confirms) -- otherwise each confirm re-wakes a worker that
# confirms again, looping. push_seq still advances from d["seq"].
events = [e for e in d.get("events", []) if e.get("from") != bot]
cands = [{"task_id": e["task_id"], "from": e["from"], "type": e["type"], "message_id": e["task_id"], "reason": "push"} for e in events]
if not dry:
    try:
        state = json.load(open(state_path))
    except Exception:
        state = {}
    if not isinstance(state, dict):
        state = {}
    pushed = state.get("pushed", {})
    if not isinstance(pushed, dict):
        pushed = {}
    for e in events:
        pushed[e["task_id"]] = now
    state["pushed"] = pushed
    state["push_seq"] = d.get("seq", state.get("push_seq", 0))
    state["last_poll"] = now
    for k in ("seen", "pushed"):
        m = state.get(k)
        if isinstance(m, dict):
            state[k] = {kk: vv for kk, vv in m.items() if now - vv < ttl}
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    tmp = state_path + ".tmp"
    json.dump(state, open(tmp, "w"))
    os.replace(tmp, state_path)
print(json.dumps(cands))
PY
)"
fi

# Decide whether the list sweep is due: it is the safety net for a
# silently lost push wake (v2 fix 3). Runs when push failed, or when
# the last sweep is older than SWEEP_INTERVAL_SECONDS.
SWEEP_DUE=0
if [ "$PUSH_OK" != "1" ]; then
  log "a2a push events failed, falling back to list" '{"error":"events-failed"}'
  SWEEP_DUE=1
else
  LAST_SWEEP="$(state_get last_list_sweep 0)"
  # v2.6: last_list_sweep is a time.time() float; bash $(( )) cannot
  # do arithmetic on floats, so strip the fraction first.
  LAST_SWEEP="${LAST_SWEEP%%.*}"
  if [ "$((NOW - LAST_SWEEP))" -ge "$SWEEP_INTERVAL_SECONDS" ]; then
    SWEEP_DUE=1
  fi
fi

SWEEP_CANDIDATES="[]"
if [ "$SWEEP_DUE" = "1" ]; then
  LIST_JSON="$(fetch_list 2>/dev/null || true)"
  if [ -z "$LIST_JSON" ]; then
    if [ "$PUSH_OK" = "1" ]; then
      log "a2a list sweep failed, push candidates still stand" '{"error":"list-failed"}'
    else
      silent "a2a-list-failed" '{"pending":0}'
      exit 0
    fi
  else
    LIST_FILE="$(mktemp)"
    printf '%s' "$LIST_JSON" > "$LIST_FILE"
    SWEEP_CANDIDATES="$(python3 - "$STATE_FILE" "$DRY_RUN" "$BOT_NAME" "$STALE_REWAKE_SECONDS" "$SEEN_TTL_SECONDS" "$LIST_FILE" <<'PY' || echo "[]"
import json, os, sys, time
state_path, dry, bot, stale, ttl, list_path = (sys.argv[1], sys.argv[2] == "1", sys.argv[3], float(sys.argv[4]), float(sys.argv[5]), sys.argv[6])
now = time.time()
try:
    tasks = json.load(open(list_path))["result"]["tasks"]
except Exception:
    sys.exit(2)
try:
    state = json.load(open(state_path))
except Exception:
    state = {}
if not isinstance(state, dict):
    state = {}
seen = state.get("seen", {})
pushed = state.get("pushed", {})
if not isinstance(seen, dict):
    seen = {}
if not isinstance(pushed, dict):
    pushed = {}
candidates = []
for task in tasks:
    if task.get("status") not in ("working", "submitted"):
        continue
    tid = task.get("id")
    # v2 fix 2: every history message addressed to this bot, not
    # just history[-1] -- a message buried under a later one must
    # still wake.
    for msg in task.get("history") or []:
        if msg.get("to") != bot:
            continue
        msg_id = msg.get("id")
        if not msg_id:
            continue
        last_wake = seen.get(msg_id)
        pushed_at = pushed.get(tid)
        if last_wake is None and pushed_at is None:
            reason = "new"
        elif last_wake is not None and now - last_wake > stale:
            reason = "stale"
        elif pushed_at is not None and now - pushed_at > stale:
            reason = "stale-push"
        else:
            continue
        candidates.append({"task_id": tid, "from": msg.get("from"), "type": msg.get("type"), "message_id": msg_id, "reason": reason})
        seen[msg_id] = now
seen = {k: v for k, v in seen.items() if now - v < ttl}
pushed = {k: v for k, v in pushed.items() if now - v < ttl}
if not dry:
    state["seen"] = seen
    state["pushed"] = pushed
    state["last_list_sweep"] = now
    state["last_poll"] = now
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    tmp = state_path + ".tmp"
    json.dump(state, open(tmp, "w"))
    os.replace(tmp, state_path)
print(json.dumps(candidates))
PY
)"
  fi
fi

# Merge push + sweep candidates, deduped by task_id (first wins).
# Option 1 (v2.4): qh-* bridge tasks from quinn-helper sort first,
# so the wake turn answers the bridge before other tasks.
CANDIDATES="$(printf '%s\n%s' "$PUSH_CANDIDATES" "$SWEEP_CANDIDATES" | python3 -c '
import json, sys
merged, seen_ids = [], set()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    for c in json.loads(line):
        if c["task_id"] in seen_ids:
            continue
        seen_ids.add(c["task_id"])
        merged.append(c)
merged.sort(key=lambda c: 0 if (c["task_id"].startswith("qh-") and c.get("from") == "quinn-helper") else 1)
print(json.dumps(merged))
')"

COUNT="$(printf '%s' "$CANDIDATES" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
if [ "$COUNT" -gt 0 ]; then
  wake "a2a task for $BOT_NAME" "{\"tasks\":$CANDIDATES}"
else
  silent "no-new-a2a-tasks" '{"pending":0}'
fi
