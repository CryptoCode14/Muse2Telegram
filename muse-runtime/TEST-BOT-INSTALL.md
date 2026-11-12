# Test-bot install (v2, unified prompt)

Install the v2 bridge against a **test bot first, never the main bot**.
This is the exact install path for verifying the unified prompt,
`context_md` pre-hydration, and `--reply` before anything touches the
live bot. It follows `SETUP.md`; this file only pins down the
test-specific values and the v2 differences. Read `SETUP.md` Steps
0–2 for the parts that are identical (prerequisites, secure
credential registration); the steps below replace the live values
with test values.

## 0. Test values (fill in, never guess)

- Test bot username (from BotFather, no `@`): `TEST_BOT_USERNAME`
- Test bot display name: pick one that is clearly a test bot, e.g.
  `Muse Test Bot` — never the main bot's display name.
- Owner's numeric Telegram user id: `TEST_OWNER_ID` (your own id;
  learn it during adoption, Step 7 of `SETUP.md`).
- Test chat: a private DM with the test bot first; then a test group
  you create containing only you and the test bot.
- Test state dir: `~/workspace/telegram-bridge-test/` — separate from
  the live `~/workspace/telegram-bridge/`. Its files:
  - `~/workspace/telegram-bridge-test/state.json` (created on first run)
  - `~/workspace/telegram-bridge-test/context.md` (write this by hand
    before the first wake; see below)
  - `~/workspace/telegram-bridge-test/PAUSED`
  - `~/workspace/telegram-bridge-test/held.jsonl`
  - `~/workspace/telegram-bridge-test/intake-errors.log`
- Test bridge checkout: this repo's `muse-runtime/` directory, e.g.
  `~/workspace/muse2telegram-v2/muse-runtime` (run every bridge
  command from inside it).
- Test hooks folder: `~/hooks-test/scripts/` for the scripts,
  `~/hooks-test/definitions/` for the hook definitions (or the
  equivalent test location in your runtime). Do NOT install the test
  hook into the live hooks folder.

## 1. Create the test bot in BotFather

Message @BotFather, send `/newbot`, pick a display name and a
username. Copy the token it gives you — do not paste it in chat, in
a file, or in a command.

## 2. Register the test credential (secure flow)

Register the token exactly as in `SETUP.md` Step 2 (provider
`telegram-muse-bot`, auth scheme `api_key`, placement
`url_path_segment:/bot{}`, api hosts `api.telegram.org`), but as a
**separate** connection for the test bot — never reuse the main bot's
connection. The token is stored in the Secure Vault and must never
appear in a file, a command argument, an environment variable, or a
log.

## 3. Set up the venv and run the offline tests

```text
cd ~/workspace/muse2telegram-v2/muse-runtime && python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```

**Done looks like:** the suite runs with no NEW failures versus the
v2 baseline (331 tests; the known pre-existing failures are
environmental — missing live `~/hooks/prompts/` files and
media-download mocks — not test-bot failures). Do not proceed on a
failing suite you cannot explain.

## 4. Install the files (test paths)

- Sender: copy `muse-runtime/bin/telegram-sender` to the test
  connector bin and make it executable (`chmod +x`).
- Hook script and decider: copy
  `muse-runtime/hooks/telegram-bridge-intake.sh` and
  `muse-runtime/hooks/telegram-bridge-decide.py` to
  `~/hooks-test/scripts/` and make them executable.
- Watchdog: copy `muse-runtime/hooks/telegram-bridge-watchdog.sh`
  to `~/hooks-test/scripts/` and
  `muse-runtime/hooks/telegram-bridge-watchdog.json` to
  `~/hooks-test/definitions/` (poll every 60 seconds, as in
  `SETUP.md` Step 10).
- Prompts: do NOT copy `router.md` or any `tier-*.md` file. The
  unified prompt is installed as the hook definition prompt itself
  (Step 6 below).
- Chat rules: keep a copy of `muse-runtime/CHAT_RULES.md` where the
  test Muse can read it.

## 5. Write the test context.md

Before the first wake, write
`~/workspace/telegram-bridge-test/context.md` by hand: a short
compact context for the test Muse (who the user is, how they talk,
what matters). Keep it short for the first test — a few lines is
enough to prove hydration works. The intake hook pre-loads this file
into every wake payload as `context_md` (at most 6000 chars; longer
content is truncated with a trailing `[context truncated]` marker).
If the file is missing, `context_md` is null and the wake still
proceeds — the agent then reads context.md itself.

## 6. Env vars (test values, every bridge command + hook script)

```text
TELEGRAM_BRIDGE_STATE_PATH=~/workspace/telegram-bridge-test/state.json
TELEGRAM_BRIDGE_SEND_CMD=<path to the test telegram-sender>
TELEGRAM_BRIDGE_BOT_USERNAME=<test bot username, no @>
TELEGRAM_BRIDGE_PAUSED_PATH=~/workspace/telegram-bridge-test/PAUSED
TELEGRAM_BRIDGE_HELD_PATH=~/workspace/telegram-bridge-test/held.jsonl
TELEGRAM_BRIDGE_SETTLE_SECONDS=2
```

In the hook script (`telegram-bridge-intake.sh`), set
`TELEGRAM_BRIDGE_BOT_USERNAME` to the test bot's username, and set
`TELEGRAM_BRIDGE_STATE_PATH` / `TELEGRAM_BRIDGE_SEND_CMD` /
`TELEGRAM_BRIDGE_PAUSED_PATH` / `TELEGRAM_BRIDGE_HELD_PATH` to the
test paths above. Do NOT point any of them at the live
`~/workspace/telegram-bridge/` paths.

## 7. Install the hook with the unified prompt

Create a hook that runs `telegram-bridge-intake.sh` on a **5 second**
poll interval, using the test scripts folder.

The hook definition prompt is the unified prompt in
`muse-runtime/prompts/unified.md` — paste it verbatim (replacing the
placeholders `[YOUR MUSE NAME]`, `[USER]`, `@YOUR_BOT_USERNAME`),
following the exact install steps in
`muse-runtime/hooks/hook-definition-unified-prompt.md`. Do NOT use
the old router text and do NOT install any `tier-*.md` file.

Also install the queue watchdog hook from
`muse-runtime/hooks/telegram-bridge-watchdog.json` (poll every
**60 seconds**).

**Done looks like:** both hooks are enabled and a dry run (no
messages waiting) exits silently.

## 8. Adopt the owner (test bot)

Send a DM to the test bot first ("How are you?"), then run the
intake once (see `SETUP.md` Step 6). The first DM only registers
you as a candidate; a second DM is the one that gets a reply. Then
adopt, passing the mode from `SETUP.md` Step 5 (default **guarded**
if undecided):

```text
cd ~/workspace/muse2telegram-v2/muse-runtime && TELEGRAM_BRIDGE_STATE_PATH=~/workspace/telegram-bridge-test/state.json .venv/bin/python -m bridge_core.telegram --adopt <TEST_OWNER_ID> <trusted|guarded>
```

**Done looks like:** the command prints `{"owner_id": <id>,
"default_group_mode": "<mode>"}` and the test state file has
`owner_id` and `default_group_mode` set.

## 9. Test checklist (run all of these on the test bot)

- [ ] Test DM answered on its own; the reply threads under the
  question and the bot's display name is right.
- [ ] Wake payload carries `context_md` with the contents of the
  test context.md (check by running the intake once with a message
  waiting and inspecting the payload; do not post the payload
  anywhere — it contains your context).
- [ ] `context_md` is null (not a crash) when context.md is renamed
  away; the wake still proceeds.
- [ ] Typing shows pre-wake for a DM, a command (`/who`), and a
  group @mention — and does NOT show pre-wake for an open group
  message the agent may stay silent on.
- [ ] Group @mention answered; open group message with nothing to
  add stays silent (closed with `--done`, no reply).
- [ ] `/who`, `/help`, `/menu` (with keyboard), `/status`, `/pause`
  then `/resume` all behave as the unified prompt describes.
- [ ] A long task (10+ minutes of work) sends the acknowledgement
  first ("Got it, looking into this, will reply when ready."), then
  the full reply.
- [ ] Voice note: if the Whisper tools are absent, the turn does
  NOT fail — it says so briefly and asks for text instead.
- [ ] Group mode: run
  `.venv/bin/python -m bridge_core.telegram --group-mode`
  (read-only) and check the test group's mode matches the answer
  from `SETUP.md` Step 5.

## 10. Promote to the main bot (only after the checklist is green)

Repeat Steps 4–9 with the live values from `SETUP.md` (live bot
username, live credential connection, live state dir
`~/workspace/telegram-bridge/`, live hooks folder). Keep the test
install in place until the live install is verified — do not delete
the test bot, its credential, or its state dir as part of promotion.
