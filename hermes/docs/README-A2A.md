# A2A Endpoints for Westin's Bots

Spin up A2A (Agent2Agent) endpoints so Westin Jr., Gus, Dorian, and any
new bot can hand each other work directly, without going through Westin
and without polling a shared file. Built 2026-11-11 from the research
report at `~/workspace/research_notes/bot-to-bot-direct-communication-20261111-2115/report.md`.

## What is running right now (verified live)

- **Server:** `a2a_server.py` (Python 3 stdlib only, no extra deps),
  listening on `http://127.0.0.1:8765/` on this VM.
  - `GET /.well-known/agent-card.json` — Westin Jr.'s Agent Card (public).
  - `GET /agents` — registry of known bots (public).
  - `GET /health` — `{"ok": true}`.
  - `POST /` — JSON-RPC 2.0: `message/send`, `tasks/get`, `tasks/list`,
    `tasks/cancel`. State-changing calls need `Authorization: Bearer <key>`.
- **Client:** `a2a_client.py` — `send`, `get`, `list`, `cancel`, `card`, `agents`.
- **Task store:** `tasks.json` (file-backed). **Audit log:** `audit.jsonl`
  (append-only, every message verbatim — a human can replay any handoff).
- **Envelope:** `id, from, to, type, task_id` (+ optional `context_id`,
  `state, deadline, payload, evidence, provenance`). `type` is a closed
  vocabulary: `request | inform | propose | confirm | refuse | cancel | decide`.
  Only `request` from an authenticated sender causes action.
- **Auth:** a shared API key in `key` (0600, generated on first run).
  Never commit it, never paste it in chat. Give it to a new bot over a
  secure channel.

## Verified live on this VM (2026-11-11)

- No key → `unauthorized` (-32001). Bad `type` → rejected (-32602).
- Round trip: westin-jr `request` task-001 to dorian → task `working`;
  dorian `confirm` + `state: completed` → task `completed`, full history
  on `tasks/get`. Audit log has both messages.
- Client wrapper: `list`, `card`, `send` all work.

## Run it

```
cd ~/workspace/a2a
python3 a2a_server.py --port 8765        # foreground
# or detached:
(setsid nohup python3 a2a_server.py --port 8765 > server.log 2>&1 < /dev/null &)
```

The server binds `127.0.0.1` only. It is a dev instance, not a service:
if the VM restarts, start it again with the command above. (A systemd
service is the durable path, same pattern as the trading bot.)

## How a new bot joins (room left for Dorian, Gus, anyone)

1. Give the bot the endpoint URL and the shared key (secure channel, never chat).
2. Add it to `agents.json` with its `id`, `name`, `role`, and `url`
   (its own Agent Card URL once it serves one); set `status` to `online`.
   Westin Jr. is `online`; Gus and Dorian are already in the registry as
   `pending` with `url: null` — fill the URL in when they come online.
3. The bot reads Westin Jr.'s Agent Card at
   `/.well-known/agent-card.json`, then uses `a2a_client.py` (or any
   JSON-RPC 2.0 client) to `message/send`. It reads task state with
   `tasks/get` instead of re-asking for the whole history.

## What is NOT done / blocked (plain)

- **Gus and Dorian cannot reach this endpoint yet.** It binds localhost
  on this VM only. There is no public URL and no shared network path
  right now: Tailscale on this VM is disconnected (approval link expired),
  and no public HTTPS endpoint / domain / VPS exists for it.
  Next step (Westin's call): reconnect Tailscale on this VM and have
  Gus/Dorian join the same tailnet, then bind the server to the tailnet
  IP; or put it behind Tailscale Funnel / a small VPS for a public URL.
  Until then this is verified locally only — do not claim Gus/Dorian are
  connected.
- **No shared Redis yet.** The report's first choice is a shared Redis
  (Streams + Pub/Sub + KV) under the A2A facade. No Redis is installed
  here, no Docker, and a localhost Redis would not be reachable by
  Gus/Dorian anyway. Next step: a reachable Redis (Upstash free tier, or
  a VPS) that all three bots can reach, then wire the server's task store
  to it. The A2A HTTP layer above works without it.
- **Per-message signing is not in v1.** v1 uses the shared Bearer key for
  auth. Per-message HMAC/Ed25519 signatures (the report's top defence
  against inter-agent prompt injection) are the next hardening step,
  once keys are exchanged. The audit log already records every message
  verbatim so a human can review what the bots agreed.
- **No SSE streaming / push notifications yet.** `message/send` is
  request/response; a bot polls `tasks/get` for status. SSE is the next
  step if a bot needs live progress.
