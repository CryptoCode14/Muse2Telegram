# Guided install: Telegram auto-reply for Muse

This guide is written **for a Muse agent** (not just a human). If you are a
Muse and a user pointed you at this repo and said "install this", follow
this file top to bottom. Do the work yourself: run the commands, write the
files, register the things. Ask the user only for the things this guide
marks **ASK THE USER** — never guess those, never skip them. Everything
else, you do.

## Credit

This project is built on [`usedhonda/muse-agent-bridge`](https://github.com/usedhonda/muse-agent-bridge) (MIT, Copyright (c) 2026 usedhonda). It is not all original code. The core bridge (`bridge_core/` — the queue, the store, the Telegram adapter, and the intake / claim / send lifecycle) is that project's code, kept under its MIT license (see `LICENSE`). What this repo adds on top of it is the Telegram sender, the polling hook with routing, the decider, the unified wake prompt, the trusted / guarded group modes, the file and media handling, the one-reply-per-batch behaviour, and this guide.

## What this installs

A Telegram bot that is your Muse. It polls Telegram, wakes your Muse when
a message needs it, and replies as you. It works in a private DM with the
owner and in every group the owner adds it to. It handles text, voice notes (transcribed locally),
photos, and documents. It does **not** supply a model — your Muse is the
model. The hook is the only wake path; there is no persistent poller.

One bot per Muse. Each Muse needs its own BotFather bot, its own
credential, its own bridge state, and its own hook. Sharing one BotFather
token across Muses does not work: Telegram's `getUpdates` with an offset
consumes updates, so the Muses steal each other's messages, and they are
all the same bot identity so they cannot read each other.

## Placeholders

Replace these everywhere (files, commands, prompts) with the real values
for this install. Do not leave a placeholder in place.

- `[YOUR MUSE NAME]` — this Muse's name (e.g. the name the user calls it).
- `[USER]` — the user's name.
- `@YOUR_BOT_USERNAME` — the bot's Telegram username (from BotFather, no
  `@` in env vars and commands that take a bare username).
- `[YOUR TELEGRAM USER ID]` — the owner's numeric Telegram user id. You
  learn it during adoption (Step 7); do not guess it.

## Step 0 — Prerequisites

Confirm all of these before you start. If one is missing, stop and tell
the user what is missing.

- The user has a Telegram account and can open @BotFather (it opens as a Mini App now).
- This Muse runs in the Muse app/runtime: it can run local commands,
  schedule a hook, and dispatch a wake payload to a model turn.
- Python 3.9+ with `venv` on the host.
- The runtime's `dynamic_credentials` helper (imported from
  `/opt/muse/skills/custom-connector-setup/bin`), exposing
  `dynamic_credential_entry`. The bridge and sender use it to fetch the
  bot token surrogate. Do not substitute a raw token anywhere.
- Surrogate-aware egress for `api.telegram.org` in the hook execution
  context. An ordinary shell with HTTPS access alone is insufficient.

## Step 1 — Create the bot in BotFather

@BotFather now opens as a Telegram Mini App — a settings UI with
buttons (Edit Info, Commands, Mini Apps, Bot Settings, Login Widget,
Serverless, Games). The old slash commands still work, but the mini
app is the primary UI: use it first, and fall back to the command
when a setting is not in the mini app.

**ASK THE USER.** Tell the user: "Open Telegram and open @BotFather
(the mini app), tap My Bots, then create a new bot, pick a display
name and a username. Copy the token it gives you — do not paste it
in chat." (The old way still works: message @BotFather and send
`/newbot`.)

The user must NOT paste the token in chat, in a file, or in a command.
When they have it, move to Step 2, where they enter it on the secure
registration page.

## Step 2 — Register the credential (secure flow)

**ASK THE USER (secure page).** Register the token as an API credential
using the runtime's secure registration flow (the secure entry page it
shows — in this runtime, `credentials.request_api_access`). The user
pastes the token **there**, never in chat. Register it with exactly:

- provider: `telegram-muse-bot`
- auth scheme: `api_key`
- placement: `url_path_segment:/bot{}`
- api hosts: `api.telegram.org`

The registered connection is named `custom.telegram-muse-bot`. The token
is stored in the Secure Vault. It must never appear in a file, a command
argument, an environment variable, or a log.

**Done looks like:** the registration succeeds and the connector
`custom.telegram-muse-bot` exists. Do not proceed until it does.

## Step 3 — Get the bridge code

`muse-runtime/` is the self-contained install package — everything
below comes from it. Clone this repo and use its `muse-runtime/`
directory as the bridge checkout (or copy `muse-runtime/` alone to
the host). Every bridge command in this guide runs from inside
`muse-runtime/`:

```text
muse-runtime/
  SETUP.md            this file
  README.md           overview (points here)
  CONNECTOR.md        the connector doc
  CHAT_RULES.md       group-chat reply rules — adopt as a skill when
                      installing (reply behaviour, not personality;
                      the wake prompts carry the short version)
  COLLAB_PROTOCOL.md  collab protocol for a trusted multi-bot group
                      — adopt as a skill when installing, ONLY for
                      that kind of group (teammates, not silence;
                      see Step 4)
  requirements.txt    standard library only (no third-party packages)
  bridge_core/        the Telegram adapter + queue + state store
    telegram.py       intake, queueing, send fence, group modes
    queue.py          the queue core
    store.py          the JSON state store
    __init__.py
  tests/              offline tests (local fixtures, fake responses)
  bin/telegram-sender the send executable (sendMessage / sendPhoto /
                      sendDocument / typing / reactions / setMyCommands)
  hooks/
    telegram-bridge-intake.sh   the hook script (polls, decides, wakes)
    telegram-bridge-decide.py   the router (wake / hold / done)
    telegram-bridge-watchdog.sh the queue watchdog (recycles dead claims)
    telegram-bridge-watchdog.json
                                the watchdog hook definition (120s poll)
  prompts/
    unified.md        the hook-definition prompt (one static prompt
                      for every wake; 12220 chars, ~3055 modelled
                      tokens)
    router.md, tier-quick.md, tier-standard.md, tier-deep.md
                      superseded in v2; kept for history only,
                      do not install
```

Set up a venv and install the requirements, then run the offline tests.
They use local fixtures and fake platform responses — no credentials and
no live calls:

```text
cd <bridge checkout> && python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```

**Done looks like:** all offline tests pass. Do not proceed on a failing
suite.

## Step 4 — Install the files

Copy the pieces to where they live on this host. Use these exact paths
(the bridge, hook, and prompts all assume them):

- Sender: copy `muse-runtime/bin/telegram-sender` to the connector bin
  (e.g. `~/workspace/custom-connectors/custom.telegram-muse-bot/bin/telegram-sender`).
  Make it executable (`chmod +x`).
- Bridge: the `muse-runtime/` directory is the bridge checkout. Note
  its path (e.g. `~/workspace/muse-agent-bridge-fork/muse-runtime`);
  every bridge command runs from there (`cd` into it first).
- Hook script and decider: copy `muse-runtime/hooks/telegram-bridge-intake.sh`
  and `muse-runtime/hooks/telegram-bridge-decide.py` to the hooks scripts
  folder (e.g. `~/hooks/scripts/`). Make them executable. The scripts
  find the bridge checkout from their own location when run from the
  repo; after copying them, set BRIDGE_HOME in each to the bridge
  checkout path you noted above (e.g. `BRIDGE_HOME="$HOME/workspace/muse2telegram/muse-runtime"`),
  or they fail with a clear message telling you to set it.
- Watchdog: copy `muse-runtime/hooks/telegram-bridge-watchdog.sh` to the
  hooks scripts folder and make it executable, and copy
  `muse-runtime/hooks/telegram-bridge-watchdog.json` to the hooks
  definitions folder (e.g. `~/hooks/definitions/`). It polls every
  120 seconds and recycles claimed items whose heartbeat went stale
  (a turn that died mid-work) back to pending, so the intake's next
  poll re-claims and re-wakes them. It never sends a Telegram
  message, never closes work, and never touches "sending" items. It
  is silent on health and on successful repair, and wakes an agent
  only when the sweep itself fails. To stop it sweeping by hand,
  create `~/workspace/telegram-bridge/WATCHDOG_PAUSE`.
- Prompts: the unified prompt is installed as the hook definition
  prompt itself (see Step 10); do NOT copy the superseded
  `router.md` or `tier-*.md` files anywhere. Keep a copy of
  `muse-runtime/prompts/unified.md` with the bridge checkout for
  reference.
- Routing note (local only, not in the repo): the repo ships without a
  group-routing note on purpose — the installing Muse writes it into
  their own local prompt files, in their own words, during this step.
  Using the sender fields the wake payload carries (is_owner,
  is_group_admin, sender_id, sender_name), write a short note saying
  what you do with a message from each kind of sender — the owner, a
  group admin, another bot, a stranger — and which senders may run
  commands. Keep it in your local prompts only; do not commit it to
  the repo.
- Chat rules: adopt `muse-runtime/CHAT_RULES.md` as a skill — it is
  the group-chat reply behaviour (default silence, stay out of
  instructions to another bot, acknowledge once then stop, reaction
  instead of text). It governs reply behaviour only, not any bot's
  personality or tone. Keep a copy where the installing Muse can
  read it with the prompts.
- Collab protocol (multi-bot groups only): if the group is trusted,
  has more than one bot, and the owner wants the bots to work as
  teammates, adopt `muse-runtime/COLLAB_PROTOCOL.md` as a skill
  too — copy it to `~/workspace/custom-skills/telegram-collab-protocol/COLLAB_PROTOCOL.md`
  and create a `SKILL.md` in that folder with the skill name
  `telegram-collab-protocol`, "When to use: in a trusted multi-bot
  group where the owner wants the bots to collaborate as
  teammates", and "Read COLLAB_PROTOCOL.md in this skill folder
  for the full rules", plus the short version (free reign to
  say what you are doing, ask each other questions, hand work
  off; ack-only text stays a reaction; an ack is terminal; one
  owner per task). Fill in the placeholders in COLLAB_PROTOCOL.md
  at install: `[USER]` with the owner's name, `[YOUR MUSE NAME]`
  with this Muse's name, `[BOT LIST]` with one line per bot in the
  form `Name / @username — job`, and `[GROUP]` with the group's
  chat id. The "Filled in for this group" section is where those
  values land.
  In a trusted multi-bot group where this skill is adopted, it
  wins over CHAT_RULES.md; in a guarded group, or a group with
  only one bot, CHAT_RULES.md still governs and this skill does
  not apply. Do not adopt it for a group that does not meet all
  three conditions in COLLAB_PROTOCOL.md.
- State: the bridge state lives at `~/workspace/telegram-bridge/state.json`.
  It is created on first run. It holds the owner id, the adopted
  groups (`group_ids` + one record per group in `groups`: mode, admin
  list, member count, safety trip, notice, room buffer), the default
  mode, and the queue. It is **never** committed to the repo and never shared.

Set these env vars for every bridge command and in the hook script:

```text
TELEGRAM_BRIDGE_STATE_PATH=~/workspace/telegram-bridge/state.json
TELEGRAM_BRIDGE_SEND_CMD=<path to telegram-sender>
TELEGRAM_BRIDGE_BOT_USERNAME=<your bot username, no @>
TELEGRAM_BRIDGE_BOT_ALIASES=<optional comma-separated short names, no @>
TELEGRAM_BRIDGE_PAUSED_PATH=~/workspace/telegram-bridge/PAUSED
TELEGRAM_BRIDGE_HELD_PATH=~/workspace/telegram-bridge/held.jsonl
TELEGRAM_BRIDGE_SETTLE_SECONDS=2
```

In the hook script, set `TELEGRAM_BRIDGE_BOT_USERNAME` to your bot's
username. The decider falls back to a default when the env var is unset
— do not rely on the fallback; set it.

`TELEGRAM_BRIDGE_BOT_ALIASES` is optional: comma-separated short
names (no "@", e.g. `Dorian` for a bot whose username is
`DorianCodingBot`). An `@<alias>` @mention wakes this bot just like
`@<username>`, in the decider, the adapter's preclaim filter, and
the typing indicator, and the unified prompt says so. This is
per-bot: every bot that installs the repo sets its own aliases, and
each bot only wakes for its own username and aliases — `@Dorian`
wakes Dorian, `@Gus` wakes Gus, and so on. Unset, only the username
counts.

## Step 5 — Ask the mode question (trusted or guarded)

**ASK THE USER.** This is a setup-time choice and it is locked after
setup. Ask in chat, in plain words:

> "Is this bot's group trusted or guarded? Trusted means a private group
> with only you and your Muse bots in it — the bot may share your
> personal details with you there. Guarded means any group with other
> people in it — the bot never shares your personal details in the
> group. Which one?"

- **trusted** — a private group with only the owner + their Muse bots.
  The owner may ask for their personal details in the group and the bot
  gives them to them. A bot may ask another Muse bot a clarifying
  question only to complete a specific task the owner asked for.
- **guarded** — any group with other people. The bot never shares
  personal, financial, or private details about the owner with anyone
  except the owner. No one can query the bot for personal information
  about anyone.

Record the answer. You will store it in Step 7 (adoption) with
`--adopt <owner_id> <mode>`. Do not guess it. If the user has not
decided, default to **guarded** (the safe mode) and tell them so.

That answer is the mode the FIRST group adopted gets. The bridge
serves every group the owner adds it to: a group the bot joins later
starts guarded (the safe default) until the owner tells Muse to set
that group's mode. Each group has its own mode, allowlist status,
and safety trip, so nothing leaks between groups.

The mode is **locked** after setup. There are no Telegram commands that
change it (`/trust`, `/guard`, `/mode`, `/trustbot`, `/untrustbot` do not
exist and do nothing). The only way to change a group's mode later is
the owner asking Muse directly in the Muse app, and Muse changes it with
`--set-group-mode <trusted|guarded> <chat_id>`. Tell the user that when
you record their answer.

## Step 6 — Run the intake once (no reply yet)

Run the intake with no arguments. It fetches updates once, queues
servable messages, and prints a wake payload (or nothing when there is
no work):

```text
cd <bridge checkout> && TELEGRAM_BRIDGE_STATE_PATH=~/workspace/telegram-bridge/state.json .venv/bin/python -m bridge_core.telegram
```

Send a DM to the bot first ("How are you?"), then run the intake. The
first DM only registers the user as a candidate; a second DM is the one
that gets a reply. This run proves the credential works from the
sandbox: a clean run exits 0 with no output when nothing waits, and a
`403` means the credential is not available to the calling context
(check the registration in Step 2).

## Step 7 — Adopt the owner

Inspect the state file, find the candidate id, and confirm it belongs
to the user (ask them to confirm the number if you are unsure). Then
adopt it, passing the mode from Step 5:

```text
cd <bridge checkout> && TELEGRAM_BRIDGE_STATE_PATH=~/workspace/telegram-bridge/state.json .venv/bin/python -m bridge_core.telegram --adopt [YOUR TELEGRAM USER ID] <trusted|guarded>
```

Adoption is one time. After it, only the owner's private DMs are
queued; other private senders are ignored. Changing an established
owner is refused and needs a deliberate, offline state migration.

**Done looks like:** the command prints `{"owner_id": <id>,
"default_group_mode": "<mode>"}` and the state file has `owner_id` and
`default_group_mode` set.

## Step 8 — Group visibility: Group Privacy OFF + Bot-to-Bot Communication Mode ON

Two BotFather settings decide what the bot sees in a group. Both live
in the @BotFather mini app under My Bots > pick the bot > Bot
Settings. (The old slash commands still work as a fallback.)

**Group Privacy (the free-for-all).** If they want the group
free-for-all to work (the bot answering open group messages, not just
@mentions), tell them: "In @BotFather, open the bot's Bot Settings
and set Group Privacy to off." The command fallback is: message
@BotFather, send `/setprivacy`, pick the bot, choose Disable. Why:
with Group Privacy on (the default for every new bot), Telegram only
delivers @mentions and replies to the bot, so it will not see open
group messages and the free-for-all breaks. With it off, the bot sees
all group messages from humans.

If they do not want the free-for-all (mentions only), they may leave
Group Privacy on — but tell them the group will then only answer
@mentions.

**Admin override.** A bot that is an administrator of a group
receives ALL group messages from humans regardless of Group Privacy,
so for a group where the bot is an admin the privacy setting does
not matter. Check it with `getMe`: `can_read_all_group_messages`
is true only when Group Privacy is off.

**Remove-and-re-add rule.** Group Privacy applies when the bot JOINS
a group. If the owner turns it off, it only takes effect for groups
the bot joins AFTER the change. For a group it is already in, they
must REMOVE the bot and ADD IT AGAIN (or make it an admin). This
matters when the bot is added to new groups where it will NOT be an
admin: turn Group Privacy off BEFORE adding the bot, or make it an
admin.

**Bot-to-Bot Communication Mode (seeing other bots).** By default
Telegram does NOT deliver messages sent by one bot to another bot —
even if the receiving bot is a group admin, even with Group Privacy
off (Bots FAQ: "bots will not be able to see messages from other
bots regardless of mode"). If the owner's group has more than one
Muse bot and they need to see each other, tell them: "In @BotFather,
open each bot's Bot Settings and enable Bot-to-Bot Communication
Mode." A bot with the mode enabled receives ALL messages from other
bots in a group where it has admin rights (or Group Privacy off).
Enable it on every bot that should see the others. Do NOT chase
Group Privacy for a missing bot message — privacy never delivers
bot-to-bot messages; only this setting does.

**Asking another bot a question: @mention it.** The bridge only
wakes a bot when another bot @mentions it — that is the loop
prevention, so a bot message with no @mention is closed silently and
never answered. When the agent has a specific question that needs an
answer from another bot in the group, it @mentions that bot by its
username exactly as it appears in the room history. When its message
is general, open-ended, or just sharing information, it does NOT
@mention the other bot. Once the question is answered it stops
@mentioning unless a new question comes up. This rule is in the wake
prompt (`unified.md`); do not change the decider for it.

**Can the bot be added to groups at all?** If a group will not
accept the bot, check `/setjoingroups` in BotFather (it must allow
the bot to be added to groups). `getMe` reports it as
`can_join_groups`.

## Step 9 — Register the command menu

Run once, scoped to the owner's private chat so the commands are not
advertised in the group. Do this after the group is adopted, so admin
gating is in place:

```text
<path to telegram-sender> --register-commands [YOUR TELEGRAM USER ID]
```

This calls `setMyCommands` with /pause, /resume, /status, /help, /who,
/active, /menu. Group routing: see the install-time routing note in Step 4.

## Step 10 — Install and enable the hook

Create a hook that runs `telegram-bridge-intake.sh` on a **5 second**
poll interval. The hook script polls Telegram (long-poll 30s), feeds the
payload to the decider, and wakes your Muse only when a message needs
it. Silent polls cost no model tokens.

The hook definition prompt is the unified prompt in
`muse-runtime/prompts/unified.md` — use that text verbatim as the hook
definition prompt (replacing the placeholders), following the exact
install steps in `muse-runtime/hooks/hook-definition-unified-prompt.md`.
It is the woken agent's full instruction for the turn: one static text
for every wake (13054 chars, ~3264 modelled tokens). There is no
per-batch prompt selection any more: the decider prints only `wake` /
`hold` / `done` / `empty`, and the intake hook pre-loads the agent's
context into the wake payload as `context_md` (the bounded contents of
context.md, at most 6000 chars, null when the file is missing). Do not
install the superseded `router.md` or `tier-*.md` files.

**Done looks like:** the hook is enabled, polling every 5s, and a dry
run (no messages waiting) exits silently.

Also create the queue watchdog hook using the settings in
`muse-runtime/hooks/telegram-bridge-watchdog.json` (poll every **120
seconds**) — create it through the hook tool, which stamps its own
created and updated times; do not copy the JSON file verbatim, it
has no timestamps and will not parse. It recycles claimed items
whose heartbeat went stale — a
turn that died mid-work — back to pending, so the intake's next poll
re-claims and re-wakes them. It never sends a Telegram message and
wakes an agent only when the sweep itself fails. **Done looks like:**
the hook is enabled, polling every 120s, and a dry run exits silently
with a healthy sweep report.

## Step 11 — Test it live

1. Send a test DM ("How are you?"). The hook should answer it on its
   own within about 5 seconds (expect roughly 25–35 seconds for the
   full agent turn, not instant).
2. Confirm the reply threads under the question and the bot's display
   name is right. If the display name is not what the user wants, the
   bot can rename itself with the Bot API `setMyName` call (or the user
   can do it in BotFather).
3. If a group is set up, confirm the group mode: run
   `.venv/bin/python -m bridge_core.telegram --group-mode` (read-only)
   and check the group's `mode` in the `groups` list matches the answer
   from Step 5.
4. If the user set up a group, create it, add the bot, and say something
   in it. The bridge adopts every group where the owner speaks. When a
   group is adopted the bridge fetches its admin list
   (`getChatAdministrators`) and caches it in that group's record; the
   list refreshes when a command arrives from someone not in it, at most
   once per 60 seconds.

## Step 12 — For more Muses (one bot per Muse)

Repeat Steps 1–11 for each Muse (own bot, own credential, own bridge
state, own hook), then add all the bots to the same group. In a trusted
group, add each new bot's username to the trusted-bot list so the
safety trip does not fire on it: ask Muse
(`--set-trusted-bots "BotOne,BotTwo"` — the list always keeps this bot
itself). The group mode itself does not change when a bot is added.

## What "done" looks like (checklist)

Verify all of these before telling the user it is ready:

- [ ] Bot created in BotFather; token registered via the secure flow
  (never in chat, a file, or a command).
- [ ] Connector `custom.telegram-muse-bot` registered with the exact
  details from Step 2.
- [ ] Offline tests pass.
- [ ] Sender, hook, decider, and unified prompt installed at the paths in
  Step 4 and Step 10; env vars set, including the bot username.
- [ ] Mode asked and recorded (`state.json` `default_group_mode` is the
  answer from Step 5; the first group adopted gets it, later groups
  start guarded).
- [ ] Owner adopted (`--adopt` printed the owner id and the mode).
- [ ] Group Privacy set as the user chose (OFF for the free-for-all); Bot-to-Bot Communication Mode ON if the group has more than one bot.
- [ ] Command menu registered (scoped to the owner's DM).
- [ ] Hook enabled, 5s poll, unified prompt in the hook definition.
- [ ] Test DM answered on its own. Group (if any) adopted and the mode
  confirmed with `--group-mode`.

## Changing the mode later (owner only, via Muse)

The mode is locked after setup. The owner changes a group's mode by
asking Muse directly in the Muse app (e.g. "set the Telegram bot to
guarded in the family group"). Muse changes it with:

```text
.venv/bin/python -m bridge_core.telegram --set-group-mode <trusted|guarded> <chat_id>
```

With no chat id it uses the single adopted group; a chat id is
required when several groups are adopted (so a per-group change can
never land on the wrong group). The same applies to
`--clear-group-safety-trip`, `--refresh-admins`, and `--active`.
`--group-mode` lists every adopted group with its mode.

Setting the mode also clears any safety trip. There is no way to change
it from Telegram. If the group was trusted and someone who should not be
there was detected, the safety trip locks sharing down (the group
behaves as guarded) and the agent tells the owner; the stored mode stays
"trusted" until the owner tells Muse what to do. Muse clears the trip
with `--clear-group-safety-trip` (after the stranger left) or changes
the mode with `--set-group-mode`.

## Allowlisting a private group (optional, owner only, via Muse)

If the owner has a private channel that is only them and their Muse
bots (for example a master channel where all their Muses sit), they can
ask Muse to allowlist that group. A listed group is always trusted and
the safety trips never apply there, no matter who joins — the owner
controls who is in it. Every other group keeps the normal trip rules.
Muse allowlists it with:

```text
.venv/bin/python -m bridge_core.telegram --allowlist-group <chat_id>
```

Use `--unallowlist-group <chat_id>` to remove a group from the list,
and `--list-allowlisted-groups` to see which groups are listed. There
is no way to change the allowlist from Telegram. Listing a group does
not change its mode and does not clear a safety trip.

## Gotchas

- Build the Telegram URL with the raw surrogate, exactly like the
  bridge intake does. The `url_with_surrogate_path_segment` helper
  percent-encodes the surrogate, and the encoded form is not
  substituted, so Telegram rejects the call with a 404. The sender in
  this folder already does it the right way.
- If a send returns `unknown`, do not resend blindly. The message may
  already have gone out. Only after independently confirmed
  non-delivery may you return it to pending with `--send-failed` and
  send again.
- Long work (10+ minutes): the turn sends the fixed acknowledgement
  first (`--send-ack UPDATE_ID CLAIM_TOKEN CHAT_ID
  [REPLY_TO_MESSAGE_ID]`), so the owner knows the turn is working.
  The ack does not close the item; the full reply still closes it
  with `--send-and-close`. One ack per item.
- Groups: the bridge serves every group the owner adds it to (it adopts
  a group when the owner speaks there). Each group has its own mode,
  allowlist status, safety trip, and room buffer. Group messages expire
  from the pending queue after 10 minutes; private DMs never expire. A
  claimed item that was never replied can be reclaimed after 30 minutes.
- Only the adopted owner's DMs are served. Other senders are ignored.
- The hook's silent polls cost no model tokens. Each message that wakes
  your Muse costs one agent turn. An agent turn has a floor (roughly
  25–35s for a quick greeting, longer for complex work or media) — it
  is never instant.
- Intake failures are logged to
  `~/workspace/telegram-bridge/intake-errors.log` with a timestamp (a
  failed intake used to look exactly like "no message").
- How the bridge routes messages (see the unified prompt): the agent
  sends with `--reply` (reply text on stdin via heredoc), never pasted
  into a shell command. Group routing: see the install-time routing note
  in Step 4. In a guarded group the bridge never reveals personal,
  financial, or account details; in a trusted group it may share them
  with the owner when the owner asks, and may ask another Muse bot a
  clarifying question only to complete a specific task the owner asked
  for — never an open-ended personal question, no fishing.
- Rate-limit state: per-chat wake counts live in
  `~/workspace/telegram-bridge/rate.json` (the 60-wakes-per-hour cap);
  per-sender counts come from the queue itself (the 5-messages-per-60-
  seconds window). Over-limit items get a `wake_after` timestamp and
  are claimed by a later poll, never dropped. Trips are appended to
  `rate.log` in the same directory.
- Long polling: no persistent poller process, no systemd service, no
  watchdog. The hook itself long-polls on each tick, and the hook
  harness is the supervisor. Set `TELEGRAM_BRIDGE_LONG_POLL_SECONDS`
  to 0 to go back to immediate polling.
- Never commit `state.json`, a token, or any personal data to the repo.
  The repo holds code + docs only.
