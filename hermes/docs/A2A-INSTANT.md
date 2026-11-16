# A2A instant notification — diagnosis and fail-safe design

Date: 2026-11-13 ~22:35 MST. Owner: Westin. Lead: Quinn.

## The problem, in Westin's words
Bot-to-bot messages have mixed results. A message sent out of the blue,
after hours of silence, does not immediately wake the other bot, even
though intake is supposed to poll every 5 seconds. We need a system
where a new message immediately wakes the recipient, they see it in
seconds, and reply back — fail-safe, always knowing each other are
talking.

## Evidence — measured, not guessed
- Quinn bridge test, 2026-11-13 22:33 MST: bridge sent A2A task
  qh-11436dbc-001 to Quinn at 05:33:13 UTC. Quinn replied
  "HELLO QUINN BRIDGE" at 05:34:03 UTC — 50 seconds later.
  Task completed, bridge returned the exact reply to Hermes.
  So Quinn's intake DOES work, but wake + read + reply took 50s,
  not 5s.
- Dorian / Dex brain tasks, sent 22:31 MST: quinn-brain-dorian-001
  and quinn-brain-dex-001, HTTP 200, status working. At 22:35+,
  history still 1 (only Quinn's send), no replies. 4+ minutes, no wake.
- Earlier: Dex replied to hello in ~17s once, and to intro in ~17s.
  Westin Jr. replied to hellos. Gus and Dorian never replied to
  hellos, intros, onboarding, or GROUP.md update. Dorian has never
  replied to any A2A task Quinn sent.
- Supervisor state.json: all bots UP, health_ok true, heartbeats
  current. That is the A2A *server* (port 8765-8768, systemd) being
  up — not the Muse *agent* being awake. Server up ≠ agent listening.

## Root cause — why 5s polling is not instant
1. **No push at all.** `a2a_server.py` (Oracle VM, /home/ubuntu/a2a/)
   is a file-backed JSON task store. Agent Card says
   `capabilities: {streaming: False, pushNotifications: False}`.
   A task lands instantly, but the other bot only sees it when it
   looks. Polling is the only mechanism.
2. **Polling is expensive and fragile.** The intake hook
   (`~/hooks/scripts/a2a-intake.sh`, poll_interval_secs 5) does a
   full `tasks/list` every tick — tasks.json is now 557 KB and growing,
   audit.jsonl 510 KB — via urllib `a2a_client.py`, timeout 15s.
   If the list call fails or times out, the hook stays silent.
   It never wakes on a guess.
3. **Wake delivery is not 5s.** The hook runtime has to deliver the
   wake to the Muse agent, the agent has to start, read the task
   (`a2a_client.py get --task`), think, and send a reply
   (`send --from <bot> --to <sender> --type confirm --state completed`).
   For Quinn that whole loop was 50s. For a bot whose Muse is idle
   for hours, or whose hook is not installed/enabled, it may never
   happen in that session at all.
4. **Stale re-wake is 600s.** If a wake is missed, the intake script
   only re-reports that task after 10 minutes (`STALE_REWAKE_SECONDS=600`).
   So a missed message waits 10 minutes, not 5 seconds.
5. **No presence, no ack.** There is no presence file, no heartbeat
   from the Muse agent itself, and no delivery receipt beyond
   task status `working`. A sender cannot tell if the recipient is
   actually listening, offline, or just slow. No escalation exists.
6. **Per-bot intake is unverified.** Each bot needs its own
   a2a-intake hook installed, enabled, with the right BOT_NAME,
   key, and client path, and its Muse runtime running continuously.
   Dorian's has never produced a reply — likely missing, disabled,
   wrong BOT_NAME, or his Muse only wakes when Westin talks to him
   directly. We have never verified Dorian's or Gus's intake hooks.

## What IS instant today (proven)
- **Telegram @mentions in Master Muse.** Westin told Quinn on
  2026-11-13: bots are wired to reply only to something that
  @-mentions them. A plain name does not reach them; an @-mention
  does. Telegram itself is push. That is why Dorian greeted
  @<helper-telegram-username> in the group, and why Dex replied fast
  when he was already active. This is the only proven instant
  wake channel we have.
- **A2A server health / ports.** All A2A servers (8765-8768)
  are up, Agent Cards load externally, tasks land instantly.
  The store is reliable; the wake is not.

## Fail-safe design — layered, no single point of failure
Layer 1 — **Sender does A2A + Telegram @mention ping, every time.**
  On every A2A send, the sender also posts ONE short message to
  Master Muse (your trusted group) via its own Telegram bot, @-mentioning
  the recipient by exact username: e.g. "@<dorian-telegram-username> new A2A
  task quinn-brain-dorian-001 from Quinn, please check A2A."
  No task details in Telegram, just the wake. This uses the proven
  push channel, and it works even if the recipient's intake hook
  is broken. Cost: one group message per task, clearly marked.

Layer 2 — **a2a-notify daemon on the VM (new, Quinn builds).**
  A small Python systemd service `a2a-notify`, watching
  /home/ubuntu/a2a/tasks.json with inotify / 1s poll (cheap, no LLM).
  On a new task addressed to X, it:
  - sends the Telegram @mention ping (via <quinn-telegram-username> sender,
    or via the sender bot if configured),
  - logs delivery to /home/ubuntu/a2a/a2a-notify.log,
  - re-pings at 60s if still `working` with no reply,
  - escalates at 10 min: ping again + notify Quinn / Westin.
  This removes the need for every sender to remember the ping,
  and it catches tasks sent by any bot, not just Quinn.

Layer 3 — **Fix and verify every bot's intake hook.**
  - Check each bot: intake hook installed, enabled, poll 5s,
    BOT_NAME correct, key + client work, list call succeeds.
  - Reduce STALE_REWAKE_SECONDS from 600s to 60s.
  - Make the list call cheap: server-side `tasks/list?since=<watermark>`
    or a `tasks/get` for new ids only, instead of full 557 KB list
    every 5s. Dorian can build this server change.
  - Record per-bot intake status in /home/ubuntu/a2a/presence.json.

Layer 4 — **Presence / heartbeat from the agent, not just the server.**
  Each bot's intake hook writes `last_seen` (agent wake time) to
  /home/ubuntu/a2a/presence.json. Sender checks presence before
  sending, and the notifier escalates if presence is stale.
  Supervisor heartbeats already exist for servers; this adds
  agent-level presence.

Layer 5 — **Push in the A2A server (long-term, Dorian builds).**
  Add `pushNotifications: True` to a2a_server.py: on message/send,
  POST a webhook to the recipient's A2A endpoint / a subscriber
  URL, with task_id / from / type only (no payload). Each bot
  runs a tiny local notifier that receives the webhook and
  triggers its Muse hook immediately. This is the proper fix —
  polling becomes the fallback, push becomes the primary.
  Until that lands, Layers 1-2 are the fail-safe.

## What Quinn will do next (with Westin's go)
1. Layer 1 immediately: for the Dorian/Dex brain tasks already sent,
   post one @mention ping each in Master Muse, and do A2A+ping for
   every future send.
2. Build a2a-notify v1 on the VM (Quinn, stdlib Python, systemd),
   with 60s re-ping and 10 min escalation, and test it with a
   task to Dex (who replies fastest) and to Dorian (who never has).
3. Ask Dorian to build Layer 5 (server push) and the cheap list
   endpoint, and Dex to help test the notifier, in parallel.
4. Verify each bot's intake hook and publish presence.json.
5. Report measured wake latency before / after: send time →
   first reply time, per bot.

## Risks / limits
- Telegram ping adds group noise (one message per A2A task).
  Mitigate: short, clearly marked, only for tasks that need a reply.
- If a bot's Muse is completely offline (not running), no wake
  system can reach it — presence + escalation to Westin is the
  fail-safe, not a guarantee.
- A2A key must never be in the notifier's Telegram message or logs.
