# Muse2Telegram

A Telegram auto-reply bridge for Muse (the personal agent). Your Muse answers your Telegram DMs and joins a Telegram group with you. If you run more than one Muse, all of them can sit in the same group and talk with you, and with each other.

**Telegram only.** This project is built specifically for Telegram.

## What it does

- One bot per Muse. You create a bot in BotFather for each Muse you run.
- Your Muse answers your private DMs, always.
- Your Muse joins one Telegram group with you (and your other Muse bots). A message that @mentions your bot is answered. An open message from you is answered when your Muse has something worth adding.
- Text, voice notes (transcribed locally with Whisper), photos, and documents — both directions. Send your Muse a photo or a file and it sees it; it can send photos and files back.
- Multiple messages sent at once get one reply that covers all of them.
- Tiered replies: simple chat gets a fast, minimal turn; complex work gets the full context. The system picks the level itself.
- Trusted / guarded group modes, set at setup and locked. In a trusted group (just you and your Muse bots) you can ask for your personal details. In a guarded group (anyone else in it) your Muse never shares personal details about you. The mode is picked when you install it and can only be changed by asking Muse directly — never from Telegram.
- Commands: `/pause`, `/resume`, `/status`, `/help`, `/who`, `/active`, `/menu`. Owner and group admins only, verified by sender id.

## Install

Follow the guided install in [`muse-runtime/SETUP.md`](muse-runtime/SETUP.md). It is written for a Muse agent: step by step, what to do, what to ask the user, and what "done" looks like. It covers creating the bot in BotFather, registering the credential (secure flow, never in chat), installing the sender / hook / decider / prompts, the trusted-or-guarded group mode question (asked at setup and locked after), adopting the owner, Privacy Mode, and the live test. That is the one canonical install — use no other setup steps in this repo.

Details on how it works live in [`muse-runtime/README.md`](muse-runtime/README.md), and the full connector doc in [`muse-runtime/CONNECTOR.md`](muse-runtime/CONNECTOR.md).

## Credit

This project is built on [`usedhonda/muse-agent-bridge`](https://github.com/usedhonda/muse-agent-bridge) (MIT, Copyright (c) 2026 usedhonda). It is not all original code. The core bridge (`bridge_core/` — the queue, the store, the Telegram adapter, and the intake / claim / send lifecycle) is that project's code, kept under its MIT license (see `LICENSE`). What this repo adds on top of it, in `muse-runtime/`, is the Telegram sender, the polling hook with routing, the decider, the tiered wake prompts, the trusted / guarded group modes, the file and media handling, the one-reply-per-batch behaviour, and the guided install.

## License

MIT. See `LICENSE` (Copyright (c) 2026 usedhonda).
