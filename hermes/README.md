# Hermes — the multi-bot setup

This directory is the rest of the system behind the bridge: the code that
lets several Muse bots talk to each other (bot-to-bot communication), the
bridge that makes Hermes answer through Quinn, and the watchers that keep
it all alive. It is the live code running on the Oracle VM and on the
Hatch host, sanitized for public release: no keys, tokens, IDs, or
personal details. Anything deployment-specific is an environment
variable or a `<PLACEHOLDER>`.

## What runs where

- **Oracle VM (Ubuntu)** — the A2A bus, the Hermes gateway, and Quinn's
  bridge. One shared bus on HTTPS 443; each bot also runs its own
  `a2a_server.py` copy on its own port (8765-8768) as its registered
  endpoint. The bus is the inbox: a task sent to a bot's own port does
  not wake it.
- **Hatch host** — the Telegram bridge (`muse-runtime/`, the other half
  of this repo) and the token-free intake watchdog.

## Bot-to-bot notification (the push layer)

This is the script Westin asked for: the thing that notifies a bot when
another bot talks to it.

- `a2a_server.py` — the bus. JSON-RPC 2.0 over HTTPS, stdlib only.
  Every `message/send` emits a push event. `GET /events?bot=<id>&after=<seq>`
  long-polls up to 25s and returns immediately when a new task arrives;
  it carries no payload, only task id, from, to, and type. Events are
  capped at 1000 and the sequence is persisted in `push_state.json`.
  `GET /push/status` reports push health. `pushNotifications: true` in
  the Agent Card.
- `a2a-push-intake.sh` — the intake each bot runs. Long-polls
  `/events`, falls back to `tasks/list` if push is stale, and wakes the
  bot's hook with the new task. This replaced plain polling: a bridge
  task sent at 05:41:41 UTC was answered at 05:41:51 UTC, 10s instead
  of ~50s.
- `a2a_client.py`, `a2a_client_v2.py` — the send/list/get clients.
  v2 is current. Both read the shared key from a key file at call time
  and never print it.
- `audit_verify.py` — verifies `audit.jsonl` (the bus logs every
  message verbatim there).

## Hermes brain (Quinn)

- `quinn-bridge.py` — the bridge Hermes talks to on port 8769. It
  forwards chat completions to Quinn over A2A (task `qh-<slug>-001`,
  600s timeout, polls the task store every 5s) and falls back to the
  local Ollama model when Quinn does not answer. Stdlib only.
- `docs/DESIGN.md` — the bridge design. `docs/A2A-INSTANT.md` — the
  push design notes. `docs/A2A-SETUP.md` — the setup guide the bots
  were onboarded with. `docs/ROLES.md` — the bot roster.

## Watchers

- `quinn-health-watch.py` — the passive health watcher, run by
  `systemd/quinn-health-watch.timer` every 5 min. Layer 1 checks
  `systemctl is-active` for hermes-gateway, quinn-bridge, a2a-quinn,
  and a2a-server plus their HTTP health endpoints, alerting only after
  2 consecutive failures. Layer 2 reads the task store directly and
  alerts only when a task to Quinn sits working and unanswered for 30+
  min. No agent turn is burned and silence alone never alerts. Alerts
  go to the front-door bot over A2A; if that fails, a direct Telegram
  send to the alert group is the fallback. Run with `--dry-run` to
  check without sending anything. Stdlib only.
- `intake-watchdog.sh` — the token-free intake watchdog, run by
  `systemd/intake-watchdog.timer` every 5 min on the Hatch host. It
  reads the Telegram and A2A last-poll ages from state files; healthy,
  it logs one line and exits (zero tokens). If Telegram intake is stale
  over 180s it runs the bridge poll directly, which claims any pending
  batch. This bounds a hook-scheduler freeze to about 15 min per
  message instead of hours. A2A staleness is only logged: no token-free
  wake exists, and the intake's list sweep catches up on resume.

## Systemd units

`systemd/` holds the live units: `a2a-server.service`,
`quinn-bridge.service`, `quinn-health-watch.service` +
`quinn-health-watch.timer`, `intake-watchdog.service` +
`intake-watchdog.timer`. They use the deployment paths
(`/home/ubuntu/a2a/`, `/home/hatch/workspace/`); adjust paths, user,
and host for your own install before enabling them.

## Configuration

| Variable | Where | Default |
| --- | --- | --- |
| `A2A_URL` | push intake, bridge, health watch | `https://YOUR-A2A-HOST/` |
| `A2A_KEY_FILE` | bridge | `/home/ubuntu/a2a/key` |
| `ALERT_GROUP_CHAT_ID` | health watch fallback | unset (fallback off) |
| `OWNER_TELEGRAM_USERNAME` | health watch fallback | unset (no @mention) |
| `INTAKE_WATCHDOG_HOME` | intake watchdog | `/home/hatch` |
| `TELEGRAM_BRIDGE_STATE_PATH` | intake watchdog | bridge state file |

Secrets live outside this repo, always: the A2A shared key
(`key`, 0600), the Telegram bot token (in the Hermes `.env`, read at
call time, never logged), TLS certs (`tls/`), and the task store /
logs (`tasks.json`, `audit.jsonl`, `server.log`). Never commit those.

## What is deliberately NOT in this repo

- `GROUP_MEMORY.md` and any file with personal details (name, age,
  home address, personal email, Telegram user ids).
- Runtime state: `tasks.json`, `push_state.json`, `audit.jsonl`,
  `state.json`, logs, `seen.json`, `counters.json`.
- Keys, tokens, TLS material, `.env` files, `secrets/`.
- Retired code: the old active-probe watchdog (`quinn-watchdog.py`,
  replaced by `quinn-health-watch.py`), `.bak` files, per-bot
  one-off bridge variants.
- Skills live in `../skills/` at the repo root
  (`telegram-collab-protocol`, `telegram-chat-rules`,
  `proactive-finisher`).

## Known limitations (honest list, Nov 2026)

- The Ollama fallback returns no content on the current VM (model on
  CPU, 3 GB RAM free). A 502 is the honest failure mode; there is no
  working backup model behind Quinn right now.
- The bus truncates task reads at 32KB while bridge turns carry ~46KB
  payloads, and stale-push re-delivery can re-wake completed tasks.
  The confirm-policy fix (confirm once, only for work actually done)
  keeps histories from growing further.
- The hook scheduler itself can freeze for hours (worker lane and
  hook scheduler share one lane). The intake watchdog bounds the
  damage; the freeze itself needs a runtime-side fix.
- `systemctl is-active` will not catch a hung bus (service up, port
  listening, closing connections). A bus-liveness probe is still open.
