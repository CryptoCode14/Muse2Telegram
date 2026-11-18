# Telegram Web App — secure password entry (all bots)

One-time password form that opens **inside Telegram** (a Telegram Web App /
Mini App). No browser link, no Secure Vault card, nothing typed in chat.
The owner taps a button in a DM from the bot, types the password, it is saved
on the server only.

Built for Quinn first (2026-11-18, tested live by the owner), then generalized
to the whole fleet. Hosted on the MCP server (port 8443).

## Bots wired up

| Bot id | Telegram bot | Token source |
|---|---|---|
| `quinn` / `quinn-muse` | @QuinnMuseBot | `/home/ubuntu/bots/quinn-muse/token.env` |
| `westin-jr` | @Westinmusebot | `/home/ubuntu/bots/westin-jr/token.env` |
| `gus` | @GusGrandValleyBot | `/home/ubuntu/bots/gus/token.env` |
| `dorian` | @DorianCodingBot | `/home/ubuntu/bots/dorian/token.env` |
| `dex` | @DextheDeveloperBot | `/home/ubuntu/bots/dex/token.env` |
| `copilot` | @GitHubCopilot22Bot | `/home/ubuntu/.copilot/extensions/copilot-cli-telegram-bridge/bots.json` |

## How to use it (from the VM, over SSH)

Send the button to the owner (DM only — never the group):

```bash
# Quinn (default)
/home/ubuntu/tg-secure/send-tg-secure.sh

# Another bot, default purpose label
/home/ubuntu/tg-secure/send-tg-secure.sh gus

# Another bot, with a specific purpose label (shown on the form)
/home/ubuntu/tg-secure/send-tg-secure.sh gus "Gus's ServiceTitan password"
/home/ubuntu/tg-secure/send-tg-secure.sh dorian "Dorian's GitHub password"
```

The script prints `ok: True | message_id: <n>` when the DM is sent.
Dry-run (no message sent, token still created — remove it from
`tokens/` if you don't use it):

```bash
DRY_RUN=1 /home/ubuntu/tg-secure/send-tg-secure.sh gus "Gus's ServiceTitan password"
```

## What happens

1. The sender creates a **one-time token** scoped to one bot + one purpose:
   `/home/ubuntu/tg-secure/tokens/<token>.json` (0600).
2. The owner taps **Enter password** in the DM. The form opens inside
   Telegram at `https://YOUR-VM-HOST:8443/tg-secure?setup=<token>&bot=<bot>&purpose=<label>`.
3. On submit, the server validates the Telegram **initData** signature
   against **that bot's own token** and checks the user is the owner
   (Telegram id `TG_SECURE_OWNER_ID`). Anyone else is rejected.
4. The password is saved to `/home/ubuntu/tg-secure/<bot>/password.txt`
   (0600, dir 0700). For Quinn it is also written to the legacy
   `/home/ubuntu/tg-secure/password.txt` so Quinn's existing consumers
   keep working.
5. The one-time token is deleted — the link cannot be reused.
6. Status lands in `/home/ubuntu/tg-secure/<bot>/status.json`
   (`{"status": "done", "at": ..., "user_id": YOUR_OWNER_TELEGRAM_ID, "bot": ..., "purpose": ...}`).

Check whether the owner has submitted:

```bash
cat /home/ubuntu/tg-secure/<bot>/status.json
ls /home/ubuntu/tg-secure/<bot>/password.txt   # exists (0600) only after a submit
```

## Rules

- **Only the owner can submit.** The initData check enforces user id
  `TG_SECURE_OWNER_ID` for every bot. Do not weaken this.
- **One token = one submission.** Generate a fresh token per request
  (the sender does this). Never reuse a link, never share a link with
  anyone but the owner.
- **Never print, log, or paste a token or password value** — in chat, in
  A2A messages, in files, or in logs. The sender and the handler never
  print them.
- **Never send the button to the group.** DM to the owner only
  (chat_id `TG_SECURE_OWNER_CHAT_ID`).
- The password is held in memory only during the submit request and is
  written once to the per-bot file. It never leaves the VM.

## Code

- Handler: `/home/ubuntu/a2a-v2/mcp/tg_secure_form.py` (mounted on the MCP
  server in `mcp_server.py`, routes `/tg-secure`, `/tg-secure-submit`,
  `/tg-secure-status`). The hook is try/except-wrapped so a form error can
  never break MCP traffic.
- Sender: `/home/ubuntu/tg-secure/send-tg-secure.sh` (generic, bot as
  first arg).

## initData validation — do not regress

Telegram WebApp initData is validated with:

```
secret = HMAC-SHA256(key="WebAppData", msg=bot_token)
hash   = HMAC-SHA256(key=secret, msg=data_check_string)
```

where `data_check_string` is the `\n`-joined sorted `key=value` pairs
excluding `hash`. **Using `SHA256(bot_token)` as the secret is WRONG** —
that was the 2026-11-18 "bad initData signature" bug (every submit failed
until it was fixed to the `WebAppData` HMAC). If you touch
`_validate_init_data`, re-test a live submit before calling it done.
