# Security Policy

## Reporting a vulnerability

**Do not open a public issue for a security report.** Use GitHub's
private vulnerability reporting for this repository (the "Report a
vulnerability" button on the repo's Security tab). If that is not
available to you, contact the repository owner privately through
GitHub instead of posting anything public.

A good report includes:

- What the issue is and what an attacker could do with it.
- Steps to reproduce, and the commit you reproduced it on.
- What is affected (bridge, sender, hook scripts, docs).

## What never belongs in a report

Never include a real bot token, a credential surrogate, or anyone's
personal data in a report — public or private. Reproduce with a test
bot and redacted values.

## Scope

This project is a bridge between a personal Muse agent and the
Telegram Bot API. It is unofficial and unaffiliated with Telegram
and with Muse. A bot token is full control of the bot: treat it like
a password and register it only through the secure flow in
`muse-runtime/SETUP.md` Step 2, never in chat, a file, or a command.
