# Quinn as the brain for Hermes — full replacement, Ollama as backup

Owner: Westin. Lead / brain: Quinn (Muse, A2A `quinn`, port 8768).
Helpers: Dorian (coding, A2A `dorian`, port 8765), Dex (web dev, A2A `dex`, port 8767).
Date: 2026-11-13. Status: building.

## Goal, in Westin's words
Quinn is the brain for Hermes. Get rid of the slow local Ollama model as the
primary brain, let Quinn run the Hermes harness directly, keep local Ollama
Qwen 2.5 only as backup.

## What we verified before building
- Helper Quinn = local Hermes agent on Oracle VM, Telegram @<helper-telegram-username>.
  Brain was Ollama Qwen 2.5 (qwen2.5:7b / qwen2.5:7b-64k), hermes-gateway
  port 8644, ollama port 11434 localhost only.
- Fixes done 2026-11-13 ~22:20 MST by Quinn directly: config.yaml
  `agent.reasoning_effort: none`, .env
  `TELEGRAM_GROUP_ALLOWED_CHATS=<YOUR-GROUP-CHAT-ID>`,
  `TELEGRAM_GROUP_ALLOWED_USERS=<OWNER-ID>,<BOT-IDS>`,
  ollama restarted, RAM 3.4 GB → 9.6 GB available.
- Telegram send via Helper Quinn VERIFIED: `hermes send --to
  telegram:<YOUR-GROUP-CHAT-ID>` arrived in Master Muse as message_id 27.
- LLM via Hermes still unusably slow: `hermes --reasoning none -z "Reply
  with exactly: HELLO QUINN"` produced no output in ~4 min, llama-server
  ~157% CPU. Web browse via Hermes NOT VERIFIED: web skills are only
  blocked-page-recovery, browser CDP check false, tool_call errors looped.
- Hermes model providers are OpenAI-compatible HTTP endpoints. The
  "custom" / ollama-launch provider in
  `/home/ubuntu/.hermes/hermes-agent/plugins/model-providers/custom/`
  just points at a `base_url` (currently http://127.0.0.1:11434/v1).
  No Hermes plugin rewrite is needed to swap the brain — we only need a
  local HTTP service that speaks OpenAI Chat Completions and forwards to Quinn.

## Architecture (full replacement)
```
Hermes gateway / agent loop
  -> model.base_url = http://127.0.0.1:8769/v1   (NEW: quinn-bridge)
  -> provider = custom, model = quinn
  -> quinn-bridge (Python stdlib only, localhost:8769, systemd quinn-bridge)
       -> A2A send: quinn-helper -> quinn, task qh-<slug>-NNN
       -> poll /home/ubuntu/a2a/tasks.json every 5s, up to 10 min
       -> Quinn (Muse) replies over A2A with plain text answer
       -> bridge returns OpenAI chat-completion JSON to Hermes
       -> on timeout / no reply / Quinn down: fallback to Ollama
            http://127.0.0.1:11434/v1 (proxy the same request)
  -> Ollama Qwen 2.5: backup only, never primary
```

Endpoints the bridge must implement (v1):
- `GET /health` — `{"status":"ok","brain":"quinn","fallback":"ollama"}`
- `GET /v1/models` — list `[{"id":"quinn","object":"model"}]`
- `POST /v1/chat/completions` — OpenAI format in, OpenAI format out.
  First version: text only, `stream: false`. If Hermes sends `stream: true`,
  return a single non-streaming-compatible chunk or a streamed fake
  (one chunk + [DONE]) — document which, do not silently hang.
  If Hermes sends `tools`, log them and, in v1, either (a) return text
  asking Quinn, with tool names listed in the prompt, or (b) return a
  tool_call only if Quinn's reply is clearly structured as one.
  Do NOT invent tool calls.

A2A contract (already exists, skills/team/a2a-bridge/SKILL.md):
- Send: `python3 /home/ubuntu/a2a/a2a_client.py --url
  https://YOUR-A2A-HOST/ --key-file /home/ubuntu/a2a/key send
  --from quinn-helper --to quinn --type request --task qh-<slug>-001
  --payload {message: <self-contained prompt>}`
- `--from` is always `quinn-helper`. Never send as `quinn`.
- Task ids unique, `qh-` + short slug + `-001`, never reuse.
- Reply: poll `/home/ubuntu/a2a/tasks.json`, find task id, `history`
  entries where `from` is `quinn`, reply is in `payload`.
- Poll every 5s, wait up to 10 min, then fallback to Ollama.
- Quinn's A2A intake polls every 5s; Quinn must answer with plain text,
  no tool loops, and say plainly if it cannot do the turn.

## Shared workspace
On the Oracle VM, shared by all three bots via the A2A bus and files:
`/home/ubuntu/a2a/quinn-brain/`
- `DESIGN.md` (this file), `TASKS.md`, `STATUS.md`
- `dorian/` — Dorian's deliverables
- `dex/` — Dex's deliverables
- `quinn/` — Quinn's integration, config, tests
- `quinn-bridge/` — the bridge server code, once built

Dorian and Dex do NOT have direct SSH as Quinn does; they work in their
own environments and deliver code / files over A2A to Quinn, who places
them on the VM, reviews, and integrates. Code delivered over A2A must be
complete files, with filename + path, no truncated snippets. If a file is
too long for one A2A message, split it into numbered parts and Quinn
reassembles it.

## Parallel work split
- **Quinn (lead, architect, integration, testing):**
  This design, shared workspace, A2A responder side, config switch,
  systemd service `quinn-bridge`, end-to-end test, fallback test,
  final report to Westin. Owns `/home/ubuntu/.hermes/config.yaml`
  changes (backup first) and the switch-over decision.
- **Dorian (coding, Hermes-side + A2A client library):**
  `dorian/` — `a2a_client_lib.py`: send task to Quinn, poll tasks.json,
  timeout, error handling, task-id generation. Plus Hermes config
  change plan: exact config.yaml edits (model.base_url, model.default,
  providers.ollama-launch as fallback), and a test script that calls
  the bridge with a real Hermes-style chat-completions request.
  No money, orders, or ServiceTitan. No keys in code or messages.
- **Dex (web dev, bridge HTTP server):**
  `dex/` — `quinn_bridge.py`: stdlib-only HTTP server on 127.0.0.1:8769,
  `GET /health`, `GET /v1/models`, `POST /v1/chat/completions`
  (non-streaming first), request logging, Ollama fallback proxy
  (forward the same OpenAI request to 127.0.0.1:11434/v1 on timeout),
  systemd unit file `quinn-bridge.service`. No research, websites
  only is NOT a blocker here — this is a local web service, his lane.
  No keys in code or messages.

## Rules
- Never paste a private key, the shared A2A key, a token, or a password
  in Telegram, Muse, A2A, a group, or a repo. The A2A key is read from
  `/home/ubuntu/a2a/key` by the client, never printed.
- Never touch money, orders, or another bot's lane without Westin's
  direct go. This project touches Hermes + Ollama only.
- Backup every config before editing it. Hermes config.yaml,
  .env, and gateway state are backed up with a dated `.bak` suffix.
- Do not remove / uninstall Ollama. It is the backup. Do not stop
  ollama, hermes-gateway, or any a2a-* service unless it is down.
- One test Telegram message only, clearly marked, @-mentioning
  @OWNER, when a re-test is needed. Do not spam the group.
- Report status over A2A to Quinn with: what file, what it does,
  what is still missing, and how Quinn should test it.

## Milestones
1. Shared workspace + design (Quinn) — done first.
2. Dex bridge server + Dorian A2A library, in parallel.
3. Quinn integrates, runs bridge on 8769, tests /v1/models + a simple
   chat completion via Quinn.
4. Config switch (backup first): Hermes model -> quinn-bridge,
   Ollama kept as fallback in bridge, not as primary.
5. End-to-end: Hermes `-z` turn via Quinn, plus a fallback test
   (simulate Quinn down / timeout -> Ollama answers).
6. Report to Westin, with what verified and what did not.
