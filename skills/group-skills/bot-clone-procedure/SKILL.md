---
name: bot-clone-procedure
description: Clone the MUSE_MCP + Telegram daemon setup for another Muse bot, repeatably — per-bot env, fresh profile, own shim port, VNC login, DM test — parked ready until Westin says go.
---

# Bot Clone Procedure

## Purpose

Every Muse bot on the Oracle VM runs the same shape: its own MUSE_MCP shim + driver (own port, own Chrome profile) and its own generalized Telegram daemon. This skill is the checklist for cloning that setup for a new bot, so the next clone is a procedure, not a research project.

## The shape (one instance per bot, not shared)

- One MUSE_MCP install serves all bots (shared fork at /home/ubuntu/muse-mcp), but each bot gets its OWN shim + driver instance: own port, own Chrome profile, own systemd unit. The driver serializes all work through one queue with a single global pending reply, so a shared instance would let bots read each other's replies. A Chrome profile also cannot be opened twice.
- Ports: Dorian 8787, Dex 8788, Gus 8789. The next bot gets the next free port.
- Per-bot dirs: /home/ubuntu/bots/<bot>/ with muse-mcp.env (profile, shim port, 127.0.0.1, headless), telegram-daemon.env (shim URL, bot username, owner id, state file, idle timeout 600s), token.env (0600, placeholder until Westin provides the real token), and profile/ (fresh, empty — NEVER copied from Dorian or another bot).
- Template units: muse-mcp@.service and telegram-daemon@.service in /etc/systemd/system/.

## The clone steps (in order)

1. Run deploy-bot.sh <bot> (generalizes the daemon to config-driven telegram-daemon.py; refuses to start without a token, shim URL, and username).
2. Verify the shim: muse-mcp@<bot> active + enabled, /health answers ok:true, loggedIn:false (expected — no login yet), browserRunning:false.
3. Verify the daemon: telegram-daemon@<bot> inactive + disabled BY DESIGN (the guard refuses cleanly without a token). Do not force it on.
4. Verify no port conflict with the bots already running, and Dorian + the other clones untouched and healthy.
5. Record the clone state (port, profile path, service state) in a report file.

## The hold rule (hard)

- A clone is PARKED READY until Westin says go. Nothing starts without his token and his VNC login. Do not start any bot clone until he says so, and do not report a clone as "ready for login" in a way that contradicts his hold.
- Dex is not first (Westin's direct word). The order is Westin's call, not the clone order.

## The two inputs only Westin can provide

1. The bot's Telegram token, into /home/ubuntu/bots/<bot>/token.env (0600).
2. The bot's Meta account login via VNC (headed Chrome, the bot's OWN fresh profile, the bot's OWN Meta account — never Westin's account, never another bot's profile). Record the bot's viewerId, re-run deploy-bot.sh <bot>, then DM-test the bot in Telegram.

## Constraints

- RAM: ~1.3 GB per headless Chrome when up. The 12 GB VM fits 4–5 bots only because browsers close after 10 min idle (MUSE_IDLE_TIMEOUT=600). If a bot's browser stays up, the budget breaks.
- The daemon's Muse replies come from the shim's single serialized browser session — long replies block other messages.
- Never restart a daemon while a Muse turn is in flight (the reply-drop rule). Never copy a Chrome profile between bots.

## What this skill does not do

- It does not cover the initial MUSE_MCP fork setup or the Telegram daemon build — that is the repo (CryptoCode14/muse-telegram-mcp). This skill clones an already-built setup.
- It does not cover A2A onboarding (keys, signing, server ports 8765–8768). That is a separate procedure.
