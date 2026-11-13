# muse-runtime: Telegram auto-reply for Muse

Run the bridge as an auto-replying Telegram bot for your Muse (personal agent). Your Muse answers your DMs and joins a Telegram group with you. If you run more than one Muse, all of them can sit in the same group and talk with you, and with each other.

This is built on [`usedhonda/muse-agent-bridge`](https://github.com/usedhonda/muse-agent-bridge) (MIT). It adds the pieces that repo does not ship: a Telegram sender, a polling hook with routing, one unified wake prompt that tells the woken Muse how to answer, and the trusted / guarded group modes. See `SETUP.md` for the install and `CONNECTOR.md` for the full connector doc. Full credit for what came from the original project vs what was added here is in [Credit](#credit) at the bottom.

## Who this is for

People running Muse (the personal agent product). This uses Muse's runtime: its hook system for polling, its Secure Vault for the bot token, and its dynamic-credential helper for API calls. It is not a general-purpose Telegram bot and it will not work outside that runtime without those pieces.

## How it works

- One bot per Muse. You create a bot in BotFather for each Muse you run. Sharing one BotFather token across Muses does not work: Telegram's `getUpdates` with an offset consumes updates, so the Muses steal each other's messages, and they are all the same bot identity so they cannot read each other.
- One group, all your bots plus you. Each bot is its own identity in the room, so when one replies the others see it.
- A hook polls Telegram every 5 seconds. Silent polls cost no model tokens; only a real message wakes your Muse. Each poll long-polls getUpdates (30s by default, `TELEGRAM_BRIDGE_LONG_POLL_SECONDS`, 0-50), so Telegram returns the moment a message arrives instead of the hook waiting for the next tick: a message is picked up in ~0-1s most of the time, up to ~5s in the short gap between polls. There is no persistent poller process: a poller cannot wake an agent (the only wake path is the hook harness), so the hook itself does the long poll. A single-run lock keeps two polls from running at once and stealing each other's updates.
- Routing is free-for-all plus @mention. A DM is always answered. A message that @mentions your bot is answered. An open message in the group from you with no @mention is answered when your Muse has something worth adding. A message for a different bot, or a message from another bot that does not mention yours, is closed silently with no wake.
- Commands: `/pause`, `/resume`, `/status`, `/help`, `/who`, `/active`, `/menu`. Group routing: see the install-time routing note in SETUP.md.
- Rate limiting is a delay, never a drop. A group message that would exceed a limit gets a `wake_after` timestamp and stays pending; a later poll answers it once its time comes, so no message is ever lost to throttling. Limits: more than 5 messages from one sender in 60 seconds staggers their later messages +30s each (capped at +300s); more than 60 wakes in a chat per hour delays that chat's items +60s per re-check (capped at +300s). One wake can cover up to 5 messages in a lane. You, group admins, and DMs are never throttled.
- Typing indicator: your Muse fires it only after deciding to reply, and again right before sending, so typing means "I am responding". It never shows when no reply comes, and the hook never fires it pre-wake: typing only ever means a live turn is working. It expires after 5 seconds and there is no background keeper, so it may lapse on very long turns.
- Ack status card: on wake the hook sends one card for the batch ("Got it, you're first in line / #N in the queue"), so you know the message was received before any turn starts. A script sweeper edits that same message to keep it current (delayed, flagged for follow-up) instead of sending a new one, and the reply deletes it when it goes out. The card is a receipt, not an answer: your Muse still answers every message itself.
- Outbox (OUTBOX-FIRST delivery): your Muse writes its reply to a SQLite outbox (`.venv/bin/python -m bridge_core.telegram --outbox CLAIM_TOKEN`, text on stdin) as soon as the reply is ready, before any follow-up work, and closes the turn — it never calls `--reply` for the same batch. The same call delivers the reply at once (one call writes and dispatches, so there is no second call to be late); the poll script is the backstop that delivers any entry the turn never delivered, retries it on throttle (honouring Retry-After), and records the Telegram message id before marking the item done, so a turn killed before or during the call still delivers. The outbox lives next to `state.json` as `state.outbox.sqlite3` (mode 0600); `--outbox-status` lists entries and `--dispatch-outbox` runs a pass by hand. A definitive rejection fails the entry without retry and returns the item to pending; an unknown outcome is never resent blindly.
- Reaction: your Muse reacts first — its first tool call in a turn reacts on your message with an emoji it chooses, before any other work. No script ever reacts for it.

## Prerequisites

- A Muse account (the personal agent).
- A Telegram account.
- A bot from BotFather (one per Muse).

## Setup

Follow the guided install in [`SETUP.md`](SETUP.md). It is written for a
Muse agent: step by step, what to do, what to ask the user, and what
"done" looks like. It covers creating the bot, registering the credential
(secure flow, never in chat), installing the sender / hook / decider /
prompts, the trusted-or-guarded group mode question (asked at setup and
locked after), adopting the owner, Privacy Mode, the command menu, the
hook, and the live test. Use no other setup steps in this
repo — `SETUP.md` is the one canonical guided install.

## Voice messages

Voice notes work. The bridge downloads the audio and queues it, and the woken Muse transcribes it with a local Whisper model and replies in text. Set it up once:

- Create a venv and install the transcriber: `python3 -m venv ~/workspace/whisper-venv && ~/workspace/whisper-venv/bin/pip install numpy faster-whisper`.
- Download the tiny model (free, ~75MB): `~/workspace/whisper-venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-tiny', local_dir='~/workspace/models/whisper-tiny')"`.
- The transcribe command the wake prompt uses:
  ```text
  ~/workspace/whisper-venv/bin/python /opt/muse/assets/audio/transcribe_chunks.py --input <audio path> --model ~/workspace/models/whisper-tiny --output-dir ~/workspace/telegram-bridge/transcripts/<update_id> --language en
  ```
  Then read `transcript.txt` in the output dir. A 3 second clip transcribes in about 3 seconds. The tiny model is the safe pick on a small VM; a larger model risks running out of memory.
- Voice notes longer than 5 minutes are not downloaded; the bot asks for a shorter one instead. Downloads are capped at 10MB. Voice files are saved mode 0600 in a 0700 directory and pruned after 30 days.

## Typing indicator

The sender has a typing mode: `telegram-sender --typing <chat_id>` calls `sendChatAction` with action `typing`. It shows "typing..." in Telegram and expires after 5 seconds. Your Muse fires it only after deciding to reply, and again right before sending. It never fires when no reply comes, and the hook never fires it pre-wake. It is free and costs no tokens. It does not make the reply arrive sooner; it makes the wait visible.

## Replies, threading, buttons, reactions, formatting

- One call sends a reply: `.venv/bin/python -m bridge_core.telegram --send-and-close UPDATE_ID CLAIM_TOKEN CHAT_ID [REPLY_TO_MESSAGE_ID] [--keyboard]` reads the reply text from stdin (file-based, never pasted into a shell command), fences the send, sends it, records the reply message id, and closes the item in one process. It returns the message id on success. The older `--send-fenced` / `--replied` / `--done` steps still work for the manual path.
- Replies thread under the question: the bridge stores the Telegram `message_id` on the queue item and passes it as `reply_to_message_id`, so in a busy multi-bot room it is clear which message a reply answers.
- Inline keyboards: `--keyboard` attaches a row of command buttons (Pause/Resume/Status, Help/Who/Active). A tap arrives as a `callback_query` with `callback_data` like `cmd:pause`; the bridge dismisses the button's loading spinner promptly via `answerCallbackQuery` and queues the tap as the matching command. Privileged buttons (Pause/Resume) are gated exactly like typed commands (owner or group admin).
- Reactions: your Muse reacts first — its first tool call in a turn is `.venv/bin/python -m bridge_core.telegram --react-batch CLAIM_TOKEN EMOJI`, reacting on the triggering message with an emoji it chooses as fitting, before any other work (or `telegram-sender --react CHAT_ID MESSAGE_ID EMOJI` when different messages deserve different emojis). No script ever reacts: the reaction always comes from the agent itself. A reaction-only reply (seen / agreed / done with no text) still sets a reaction and sends no text.
- Formatting: the sender sends with `parse_mode=HTML`. Reply text is escaped (`&`, `<`, `>`) so formatting cannot break, and on a 400 error from Telegram it retries as plain text, so a formatting failure never strands a reply. Use bold sparingly, key facts/numbers only.
- Update types: the intake requests `message`, `my_chat_member`, `callback_query`, `edited_message`, `message_reaction`. An edit to a still-pending queued message updates that item's text (an edit to a claimed or answered message is ignored, and never wakes a new turn). A reaction never wakes a turn and never creates a queue item.

## Commands

- `/pause` — stop auto-replies. Messages sent while paused are held and answered when you resume.
- `/resume` — turn auto-replies back on and catch up on held messages.
- `/status` — whether the bot is on or paused and how many messages are waiting.
- `/help` — list the commands.
- `/who` — which Muse this bot is.
- `/active` — who has been active in the group recently (from the room history, last hour).
- `/menu` — list the commands with the button keyboard attached.

Commands work from the owner and from the group creator/admins. Anyone else's command is closed silently.

## How routing decides

| Message | What happens |
|---|---|
| Private DM from you | Always answered |
| Group message @mentioning your bot | Answered |
| Group message @mentioning a different bot | Closed silently, no wake |
| Open group message from you, no @mention | Answered when your Muse has something worth adding |
| Group message from another bot, not mentioning yours | Closed silently, no wake |
| Group message from another bot, @mentioning yours | Answered |
| Any `/pause`, `/resume`, `/status`, `/help`, `/who`, `/active`, `/menu` from you (the owner) or a group admin | Answered (commands are always for your bot) |
| Any command from anyone else in the group | Closed silently, no wake |

The hook applies this routing before any agent wakes, so messages that are not for your Muse cost nothing. When your Muse is woken for a group message, the payload includes the recent room history, so it can see what the other bots said before replying. Bots never reply to each other on their own; they only answer another bot's message if it @mentions them or you asked them to.

## Collab protocol (trusted multi-bot groups)

The routing above is the silence-first default. If your group is trusted, has more than one bot, and you want the bots to work as teammates — subagents for different jobs that see each other, talk to each other, and say what they are doing — adopt `COLLAB_PROTOCOL.md` as a skill at install (see Step 4 in `SETUP.md`, skill `telegram-collab-protocol`). In that kind of group it wins over `CHAT_RULES.md`: free reign to status-update, ask each other questions, discuss, and hand work off, with ack-only text still a reaction, an ack still terminal, and one owner per task. In a guarded group, or a group with only one bot, it does not apply.

## Gotchas

- Build the Telegram URL with the raw surrogate, exactly like the bridge intake does. The `url_with_surrogate_path_segment` helper percent-encodes the surrogate, and the encoded form is not substituted, so Telegram rejects the call with a 404. The sender in this folder already does it the right way.
- If a send returns `unknown`, do not resend blindly. The message may already have gone out. Only after independently confirmed non-delivery may you return it to pending with `--send-failed` and send again.
- Groups: the bridge serves every group the owner adds it to (it adopts a group when the owner speaks there). Each group has its own mode, allowlist status, safety trip, and room buffer. Group messages expire from the pending queue after 10 minutes; private DMs never expire.
- A claimed item that was never replied can be reclaimed after 30 minutes — or after 10 minutes without a heartbeat (the 600s heartbeat horizon). The turn heartbeats while it works (`--heartbeat CLAIM_TOKEN`, refreshed automatically on ack / claim-more); a claim whose heartbeat goes stale is recycled fast, so a turn that died mid-work blocks its lane for minutes, not half an hour. A claim whose turn never starts is released sooner, by the 120s claim lease: the watchdog sweeper frees it (re-queued, re-woken by the next poll) and after 3 failed attempts stops looping and flags it for follow-up instead. A queue watchdog hook polls every 60 seconds and recycles dead claims the same way, silently; it never sends a new Telegram message (it only edits the existing ack status card to keep it current) and wakes an agent only when the sweep itself fails, the intake goes stale (no successful poll for over 120 seconds), a send is stuck, or a claim lease is exhausted. It also verifies the install on every sweep — the intake hook definition (valid and enabled), the checkout, the venv, and the sender — and wakes an agent when a check fails, once per episode. A recovery is reported when a stale intake or a failed health check clears.
- Only the adopted owner's DMs are served. Other senders are ignored.
- The hook's silent polls cost no model tokens. Each message that wakes your Muse costs one agent turn.
- Intake failures are logged to `~/workspace/telegram-bridge/intake-errors.log` with a timestamp (a failed intake used to look exactly like "no message").
- Security rules your Muse follows (see the unified wake prompt): replies are written to a file and piped into the send command, never pasted into a shell command. Group routing: see the install-time routing note in SETUP.md. Group sharing follows the group mode (trusted or guarded, set at setup and locked): in a guarded group your Muse never reveals personal, financial, or account details; in a trusted group it may share them with you when you ask. Your Muse MAY collaborate with the other bots on shared tasks, but it never asks another bot an open-ended personal question about you.
- Rate-limit state: per-chat wake counts live in `~/workspace/telegram-bridge/rate.json` (the 60-wakes-per-hour cap); per-sender counts come from the queue itself (the 5-messages-per-60-seconds window). Over-limit items get a `wake_after` timestamp and are claimed by a later poll, never dropped. Trips are appended to `rate.log` in the same directory.
- Long polling: no persistent poller process, no systemd service, no watchdog. The hook itself long-polls on each tick, and the hook harness is the supervisor. Set `TELEGRAM_BRIDGE_LONG_POLL_SECONDS` to 0 to go back to immediate polling.

## Credit

This project is built on [`usedhonda/muse-agent-bridge`](https://github.com/usedhonda/muse-agent-bridge) (MIT, Copyright (c) 2026 usedhonda). It is not all original code. The core bridge — `bridge_core/` (the queue, the store, the Telegram adapter, and the intake / claim / send lifecycle) — is that project's code, kept under its MIT license (see `LICENSE`). What this repo adds on top of it, in `muse-runtime/`, is the Telegram sender, the polling hook with routing, the decider, the unified wake prompt, the trusted / guarded group modes, the file and media handling, the one-reply-per-batch behaviour, and the guided install (`SETUP.md`).
