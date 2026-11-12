# Muse2Telegram

A Telegram auto-reply bridge for Muse, the personal agent. Your Muse answers your Telegram DMs and joins a Telegram group with you. If you run more than one Muse, all of them can sit in the same group and talk with you, and with each other.

**Telegram only.** This project is built specifically for Telegram.

## Risks and scope — read first

- **Unofficial.** This bridge is not affiliated with Telegram or with
  Muse. It is a community project, not an official integration.
- **A bot token is full control of the bot.** Anyone holding it can
  read the bot's messages and send as the bot. Never paste it in
  chat, a file, or a command — register it only through the secure
  flow in `muse-runtime/SETUP.md` Step 2.
- **Trusted mode shares personal details.** In a trusted group the bot
  gives the owner their personal, financial, and account details when
  asked. Use trusted only for a private group of the owner and their
  own Muse bots — never for a group with other people in it.
- **A listed group is ALWAYS trusted.** A group on the allowlist stays
  trusted and the safety trips never apply there, no matter who joins
  it later. The owner controls who is in it.
- **Group Privacy OFF means the agent reads every group message.**
  With Group Privacy off (or the bot an admin), Telegram delivers all
  human group messages to the bot. With it on, only @mentions and
  replies to the bot reach it. See `muse-runtime/SETUP.md` Step 8.
- **Telegram Bot API rate limits apply.** The bridge delays
  over-limit wakes instead of dropping messages, but Telegram-side
  limits (including 429s) can still delay sends.

## What it does

- One bot per Muse. You create a bot in BotFather for each Muse you run.
- Your Muse answers your private DMs, always.
- Your Muse joins a Telegram group with you (and your other Muse bots). A message that @mentions your bot is answered. An open message from you is answered when your Muse has something worth adding.
- Text, voice notes (transcribed locally with Whisper), photos, and documents, both directions. Send your Muse a photo or a file and it sees it; it can send photos and files back.
- Multiple messages sent at once get one reply that covers all of them.
- Trusted / guarded group modes, set at setup and locked. In a trusted group (just you and your Muse bots) you can ask for your personal details. In a guarded group (anyone else in it) your Muse never shares personal details about you. The mode is picked when you install it and can only be changed by asking Muse directly, never from Telegram.
- Commands: `/pause`, `/resume`, `/status`, `/help`, `/who`, `/active`, `/menu`. Owner and group admins only, verified by sender id.

## v2 rewrite: built to stop ghosting

The v1 bridge had a failure mode where agents would silently never reply. The queue, fence, token, and lane design was sound for never duplicating a reply, but almost every ambiguity resolved to silence instead. v2 is a full rewrite of the reply path around that finding. The whole tree in this repo is v2, in one commit on top of the v1 history.

What changed:

- **One static wake prompt, pre-hydrated.** The old router plus three tier prompts (quick, standard, deep) are gone. Every wake gets the same static prompt with your compact context attached by the hook, bounded to 6,000 chars, so the first thing the agent does can be the reply instead of several setup tool calls.
- **One send command: `--reply`.** A single call that heartbeats, fires typing, optionally reacts, deletes the acknowledgement message, sends once through the existing fencing, and closes the whole batch. Siblings are closed as covered, never marked as replied when they were not.
- **Lane-scoped warm hold.** After sending, `--reply` waits up to 25 seconds for a follow-up in the same chat, without holding the poll lock, so a back-and-forth stays warm with no new wake per message. Other chats are never blocked by it.
- **Offset advances only after durable commit, plus receipt dedupe.** Updates are no longer consumed before they are safely queued, and a 24 hour receipt record stops redelivery from ever double-answering.
- **Pre-claim noise filter.** Messages that are not for this bot (open group chatter, commands to other bots, non-mentions) are closed before claiming, so they never occupy a lane.
- **Delivery ledger, message cache, prompt pin, deterministic recall.** New modules under `bridge_core/`: a SQLite delivery ledger replacing the old sending black hole, a disk-backed message cache with reply chains, a byte-stable prompt pin, and keyword recall with bounded summaries.
- **Simulation harness.** `simulation/` replays scripted Telegram scenarios against the real queue and claim code with a fake Telegram and a fake agent turn, and reports duplicate sends and silent drops. 492 tests in `muse-runtime/tests/`.

What v2 does not do: no webhooks, no Redis or Postgres, no parallel turns in one lane, no hardcoded conversational answers, and no script that answers `/status` or `/who` for the agent. Speed comes from plumbing, not from taking the agent out of the loop.

## Measured latency (live test, test bot)

Measured against a test bot with the author DMing it: cold first reply about 20 seconds, warm follow-ups about 15 to 25 seconds in rapid back-and-forth, 0 duplicate sends, 0 silent drops across the test. The 5 to 8 seconds you feel is the typing indicator to reply tail, not send to reply. The remaining cost is the intake poll plus settle, the hook wake, and a full inference pass per reply, which plumbing cannot shorten. A persistent-session design could go faster, but it needs a paired device per agent, so it is not this bridge. To measure your own bridge, run `.venv/bin/python -m bridge_core.telegram --latency-report`: it reports per-stage medians (ack, pickup, wake, turn, total) against the P0 targets, plus recovery metrics, from the live state file.

## Install

Follow the guided install in [`muse-runtime/SETUP.md`](muse-runtime/SETUP.md). It is written for a Muse agent: step by step, what to do, what to ask the user, and what "done" looks like. It covers creating the bot in BotFather, registering the credential (secure flow, never in chat), installing the sender, hook, decider, and prompts, the trusted-or-guarded group mode question (asked at setup and locked after), adopting the owner, Privacy Mode, and the live test. That is the one canonical install, use no other setup steps in this repo.

Details on how it works live in [`muse-runtime/README.md`](muse-runtime/README.md), and the full connector doc in [`muse-runtime/CONNECTOR.md`](muse-runtime/CONNECTOR.md).

## Layout

- `muse-runtime/` — the bridge itself. `bridge_core/` is the queue, store, Telegram adapter, and send lifecycle; `hooks/` is the intake, decider, and watchdog hook scripts; `prompts/unified.md` is the one wake prompt; `bin/telegram-sender` is the Telegram send command; `tests/` is the suite.
- `simulation/` — scripted end-to-end scenarios with a fake Telegram and a fake agent turn.
- `.github/workflows/` — CI and security workflows.

## Requirements

- A Muse agent with the hook harness (intake hook on a 5 second poll, watchdog hook on a 60 second poll).
- A Telegram bot token from BotFather.
- Python 3.10 or newer, standard library only for the bridge itself. Whisper transcription needs the `faster-whisper` model installed separately; without it, voice notes fail gracefully and the agent asks for text.

This bridge runs inside the Muse runtime only — it needs the hook
harness, the Secure Vault, and the runtime's dynamic-credential
helper (`dynamic_credentials` + `authd`). It does not run as a
standalone project: without those pieces there is no polling, no
wake, and no credential.

## Credit

This project is built on [`usedhonda/muse-agent-bridge`](https://github.com/usedhonda/muse-agent-bridge) (MIT, Copyright (c) 2026 usedhonda). It is not all original code. The core bridge (`bridge_core/`, the queue, the store, the Telegram adapter, and the intake, claim, and send lifecycle) is that project's code, kept under its MIT license (see `LICENSE`). What this repo adds on top of it is the Telegram sender, the polling hook with routing, the decider, the unified wake prompt with pre-hydration, the single `--reply` command with lane-scoped warm hold, the trusted / guarded group modes, the file and media handling, the one-reply-per-batch behaviour, the delivery ledger, message cache, prompt pin, and recall modules, the simulation harness, and the guided install.

## License

MIT. See `LICENSE` (Copyright (c) 2026 usedhonda).
