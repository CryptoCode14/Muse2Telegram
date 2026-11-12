# Telegram Muse Bot

- `custom.telegram-muse-bot` connects Muse to the Telegram Bot API for
  the user's own Muse bot. It authenticates with the registered `api_key`
  credential and calls only `api.telegram.org`.
- Helpers live in `muse-runtime/bin/`. `telegram-sender` posts one message
  to one chat for the muse-agent-bridge.
- The bridge checkout lives wherever the installer put it (see
  `SETUP.md`); its state lives at `~/workspace/telegram-bridge/state.json`.
  The bridge supplies intake, queueing and the reply lifecycle. This
  connector supplies only the Telegram API calls.

## Connect

Register the connection with the secure registration flow
(`credentials.request_api_access`) and these details. Reuse them to
reconnect.

```json
{
  "provider": "telegram-muse-bot",
  "auth_scheme": "api_key",
  "placement": "url_path_segment:/bot{}",
  "api_hosts": ["api.telegram.org"]
}
```

- The bot is created by the user in BotFather. The token is entered by
  the user on the secure registration page and is stored in the Secure
  Vault. It is never in a file, a command argument or this doc.
- Bot display name: set it with the Bot API `setMyName` call, or in
  BotFather. The username is the one from creating the bot in the
  @BotFather mini app (My Bots > create a new bot; the old `/newbot`
  command still works).

- Before the first change in a conversation, prove access with a harmless
  read. Run the bridge intake from the checkout. A clean run prints
  nothing when no messages wait and exits 0.

```text
cd <bridge checkout> && TELEGRAM_BRIDGE_STATE_PATH=~/workspace/telegram-bridge/state.json .venv/bin/python -m bridge_core.telegram
```

## Credentials

- The registered connection holds the credential. Helpers import the
  runtime's `dynamic_credentials` helper, which returns an `hsurr:`
  surrogate and places it where the provider reads the key. Muse swaps
  in the real credential only on an approved request to a registered host.
- Never put a key, token, password or cookie in this doc, a helper, a
  command argument, an environment variable or a log. Never ask the user
  to paste one in chat.
- Keep redirects disabled on authenticated requests.
- On a 401 or 403, check the request first. Confirm that the helper
  attached the credential, that the host is registered and that the
  placement and scopes match.
- Only when a request that carried the credential is still rejected,
  call `credentials.request_api_access` with the details above and
  `"reconnect": true`.

## Fetch updates

Fetching updates reads new messages sent to the bot and queues the ones
from the adopted owner. It changes the stored update offset and claims
at most one batch.

```text
cd <bridge checkout> && TELEGRAM_BRIDGE_STATE_PATH=~/workspace/telegram-bridge/state.json .venv/bin/python -m bridge_core.telegram
```

The command prints one JSON payload when a batch is available and prints
nothing when no batch is available. Pass the payload items and claim
token to one runtime turn.

## Send one message

Sending a message posts reply text to one chat through the bot. The
bridge invokes the sender as `[executable, chat_id]` with the reply
text on stdin. Run it directly only to test the credential path.

```text
echo "test" | TELEGRAM_BRIDGE_SEND_CMD=<path to telegram-sender> <path to telegram-sender> <chat_id>
```

The sender exits 0 and prints the Telegram message id as its last line
only after confirmed acceptance. Any failure exits nonzero and prints no
message id.

The sender builds the Telegram URL with the raw surrogate, exactly like
the bridge intake does. The helper `url_with_surrogate_path_segment`
percent-encodes the surrogate, and the encoded form is not substituted,
so Telegram rejects the call with a 404.

## Auto-reply hook

- Commands (/pause, /resume, /status, /help, /who, /active, /menu).
  Group routing: see the install-time routing note in SETUP.md.
- Rate limiting delays, never drops: more than 5 messages in 60 seconds
  from one non-owner sender staggers the extras (+30s each, capped
  +300s); more than 60 wakes in 1 hour in a chat delays further items
  +60s per re-check. Delayed items stay pending and are answered later,
  so nothing is ever lost. The owner, group admins, and DMs are never
  throttled. State in `~/workspace/telegram-bridge/rate.json`, trips in
  `rate.log`.
- Long polling: the hook holds getUpdates open 30s (env
  `TELEGRAM_BRIDGE_LONG_POLL_SECONDS`, 0-50), so a message is picked up
  in ~0-1s instead of ~2.5s average. A flock on `<state>.poll.lock`
  prevents two polls from running at once. Polling costs no model tokens.
- Typing indicator: the agent fires `telegram-sender --typing <chat_id>`
  (sendChatAction) ONLY after deciding to reply, and again right before
  sending, so typing means "I am responding" and never shows when no
  reply is coming. It stops when the reply lands.
- Replies are sent with one `--send-and-close` call (fences, sends,
  records, closes) and thread under the question via
  `reply_to_message_id`. The fence refuses any `reply_to` that is not
  the target item's own `message_id`, so a reply can never thread under
  the bot's own message.
- Heartbeat: a turn refreshes its claim's heartbeat while it works
  (`--heartbeat CLAIM_TOKEN`, first thing after waking and before
  any step that may take over a minute; the ack and claim-more
  paths refresh it automatically). A claim whose heartbeat goes
  stale for 120s (`TELEGRAM_BRIDGE_HEARTBEAT_STALE_SECONDS`) is
  recycled on the next claim, so a turn that died mid-work blocks
  its lane for minutes, not the full 30-minute stale TTL. A queue
  watchdog hook polls every 60s and recycles dead claims the same
  way, silently; it never sends a Telegram message and wakes an
  agent only when the sweep itself fails or the intake goes stale
  (no successful poll for over 120 seconds). It also verifies the
  install on every sweep — the intake hook definition (valid and
  enabled), the checkout, the venv, and the sender — and wakes an
  agent when a check fails, once per episode. A recovery is
  reported when a stale intake or a failed health check clears.
- Batching: when a batch holds several messages the agent sends ONE
  reply for all of them, not one per message. Before sending it
  re-checks with `--claim-more CHAT_ID CLAIM_TOKEN` (messages that
  arrived during the turn join the same reply), then sends once with
  `--send-batch-and-close PRIMARY_ID CLAIM_TOKEN CHAT_ID
  [REPLY_TO_MESSAGE_ID] [--keyboard] [--cover OTHER_UPDATE_ID ...]`,
  which records the reply and closes every covered item (`done_reason`
  `covered-by-batch-reply`). The intake also waits out a 2s settle
  window (`TELEGRAM_BRIDGE_SETTLE_SECONDS`, set in the hook script) and
  re-fetches once so rapid follow-ups join the same batch instead of
  becoming a second turn.
- Inline keyboards (Pause/Resume/Status/Help/Who/Active) are attached
  ONLY to command replies (a typed `/command`, a button tap, or
  `/menu`), never to a normal conversation reply. The fence refuses
  `--keyboard` on a non-command reply (stage `keyboard`). Button taps
  arrive as `callback_query` and are owner/admin-gated like typed
  commands. A tap has no user message, so its queued item carries
  `message_id` null and `source` `callback_query`, and its reply goes
  out standalone with no threading and no reaction.
- Command menu: 7 commands registered, scoped to the owner's private
  chat only (verified live via getMyCommands).
- /active lists who spoke in a group in the last hour from that
  group's buffer (`state.json` `groups[<chat_id>].buffer`). Reactions
  are set on the message being answered, before the reply is sent,
  and never on a message the agent is not replying to.
- Replies use HTML formatting (bold sparingly for key facts) with
  escaping; on a 400 the sender retries as plain text so a formatting
  failure never strands a reply.
- `allowed_updates` includes `callback_query`, `edited_message`, and
  `message_reaction`. An edit updates a still-pending item's text; a
  reaction never wakes the agent.
- The agent never pastes reply text into a shell command. It writes the
  reply to a file and redirects that file into `--send-fenced`, so the
  text never passes through the shell.
- One unified wake prompt: the woken agent always follows the same
  static prompt (`prompts/unified.md`, installed as the hook definition
  prompt), whatever the batch. The decider prints only `wake` / `hold` /
  `done` / `empty` and picks no prompt. The agent's context arrives
  pre-loaded in the wake payload as `context_md` (the bounded contents
  of context.md); it reads `~/MEMORY.md` only when the message needs
  past work or detail beyond that. No script replies and no external
  model: the agent reads and replies to every message.
- In a group chat the agent never reveals personal, financial, or
  account details about the owner, to anyone except the owner (not other
  bots, not strangers) — unless the group mode is trusted (see below).
  It MAY collaborate with the other bots on shared tasks and help them
  when they ask. Group routing: see the install-time routing note in
  SETUP.md. Other bots' messages are marked
  "third-party bot message" in the context.
- Intake failures are logged to
  `~/workspace/telegram-bridge/intake-errors.log` (one-line summary)
  with the full traceback in
  `~/workspace/telegram-bridge/intake-errors-full.log`, each with a UTC
  timestamp, instead of failing silently.
- Routing lives in `hooks/telegram-bridge-decide.py`. A DM is always
  answered. A command is always answered. A group message @mentioning
  the bot is answered. An open human message in a group with no @mention
  is answered when the agent has something worth adding. A message
  @mentioning another bot, or a message from another bot that does not
  mention this bot, is closed with no reply and no wake.
- The hook script is `hooks/telegram-bridge-intake.sh`. It runs the
  intake, feeds the payload to the decider, and either wakes, holds, or
  closes the batch.
- Commands the woken agent handles: /pause, /resume, /status, /help,
  /who. /pause creates `~/workspace/telegram-bridge/PAUSED`; while paused
  the hook still polls and wakes only for commands, holding other batches
  in `~/workspace/telegram-bridge/held.jsonl` for the resume turn.
  /resume deletes the flag, processes the held file, and replies.
- Groups: the bridge serves EVERY group the owner adds it to — it
  adopts a group when the owner speaks (or taps a button) there, and
  ignores messages from groups that are not adopted. Each adopted
  group has its own mode, allowlist status, safety trip, admin list,
  member count, and room buffer, so nothing leaks between groups.
  Group visibility is set in @BotFather, which now opens as a Mini
  App (My Bots > pick the bot > Bot Settings); the old slash commands
  still work as a fallback.
- Group Privacy (Bot Settings > Group Privacy in the @BotFather mini
  app; command fallback `/setprivacy`): with it ON (the default for
  every new bot), Telegram only delivers @mentions and replies to the
  bot, so it will not see open group messages and the free-for-all
  breaks. With it OFF, the bot sees all group messages from humans.
  `getMe` reports it as `can_read_all_group_messages` (true only when
  Group Privacy is off). A bot that is an administrator of a group
  receives ALL group messages from humans regardless of Group Privacy.
  The setting applies when the bot JOINS a group: turning it off only
  takes effect for groups joined after the change — for a group it is
  already in, remove the bot and add it again (or make it an admin).
  For new groups where the bot will NOT be an admin, turn Group
  Privacy off BEFORE adding the bot.
- Bot-to-bot visibility: Telegram does NOT deliver messages sent by
  one bot to another bot, even if the receiving bot is a group admin
  and even with Group Privacy off. The fix is Bot-to-Bot
  Communication Mode, enabled in @BotFather (Bot Settings) for each
  bot that should see the others; a bot with the mode enabled
  receives ALL messages from other bots in a group where it has admin
  rights (or Group Privacy off). Do NOT chase Group Privacy for a
  missing bot message — privacy never delivers bot-to-bot messages.
  When a bot message IS delivered, it lands in that group's buffer
  and is enqueued; the decider closes it silently unless it @mentions
  this bot (then it wakes). Loop prevention: no auto-reply to an
  unprompted bot message, queue dedup, and per-sender/per-chat rate
  limiting. Asking another bot a question: when the agent has a
  specific question that needs an answer from another bot in the
  group, it @mentions that bot by its username exactly as it appears
  in the group buffer — that is what wakes the other bot's bridge.
  For general, open-ended, or information-only messages it does NOT
  @mention the other bot, and once the question is answered it stops
  @mentioning unless a new question comes up. This rule lives in the
  wake prompt (`unified.md`); the decider is unchanged.
- If a group will not accept the bot at all, check `/setjoingroups`
  in BotFather (it must allow the bot to be added to groups; `getMe`
  reports it as `can_join_groups`).
- Multi-Muse rooms need one bot per Muse. Sharing one BotFather token
  across Muses breaks delivery: getUpdates with an offset consumes
  updates, so the Muses steal each other's messages, and they are all
  the same bot identity so they cannot read each other. Separate bots in
  one shared group is the working shape.

## Group modes (trusted / guarded)

- Each bot's sharing mode is PER GROUP, stored in `state.json` as
  `groups[<chat_id>].mode` ("trusted" or "guarded", default
  "guarded"). It is set at setup — the installer asks the owner "Is
  this bot's group trusted or guarded?" and records the answer with
  `--adopt <owner_id> <mode>` (that becomes `default_group_mode`,
  the mode the FIRST group adopted gets; every group adopted after
  that starts guarded, the safe default, until Muse sets its mode).
  A group's mode is changed with
  `--set-group-mode <trusted|guarded> <chat_id>` (with no chat id it
  uses the single adopted group; a chat id is required when several
  groups are adopted). The mode is LOCKED after setup.
- Trusted: a private group with only the owner + their Muse bots. The
  owner may ask for their personal, financial, or account details in the
  group and the bot gives them to them. A bot may ask the owner
  clarifying questions, and may ask another Muse bot a clarifying
  question ONLY to complete a specific task the owner asked for (minimum
  info needed for that task). A bot must never ask another bot an
  open-ended personal question about the owner, and never swap the
  owner's personal details between bots except as needed for the owner's
  specific task.
- Guarded: any group with other people. The bot never shares personal,
  financial, or private details about the owner with anyone except the
  owner. No one can query the bot for personal information about anyone.
- The mode is changed ONLY by the owner asking Muse directly in the Muse
  app. Muse changes a group's mode with
  `--set-group-mode <trusted|guarded> <chat_id>` (which also clears that
  group's safety trip). There are NO Telegram commands for the mode:
  /trust, /guard, /mode, /trustbot, /untrustbot do not exist and do
  nothing. No Telegram message or command can change the mode.
- The trusted-bot list lives in `state.json` as `trusted_bots` (bot
  usernames, seeded with this bot itself). The owner adds work/business
  bots when they exist, by asking Muse (`--set-trusted-bots
  "BotOne,BotTwo"`). This bot is always kept in the list. Changing the
  list does not change the mode and does not clear a safety trip.
- Safety trip (trusted mode only, per-group): the bridge verifies a
  trusted group is still owner + known Muse bots. If a message arrives
  from a sender who is neither the owner nor a trusted bot (a human
  stranger, or a bot not in the list), or `getChatMemberCount` exceeds
  the expected trusted count (1 owner + trusted bots, re-checked at
  most once per 60s), the trip fires for THAT group only:
  personal-detail sharing stops (the group behaves as guarded) and a
  one-time notice is recorded for the next wake so the agent tells the
  owner. The stored mode stays "trusted" — the trip is a runtime
  guard, not a mode change, and a trip in one group never affects
  another group. Only Muse clears it
  (`--clear-group-safety-trip <chat_id>`), only when the owner asks Muse
  after the owner decides what to do (e.g. the stranger left).
- Per-group allowlist: `state.json` holds `trusted_group_ids`, a list of group chat ids that are the owner's private channel(s). A listed group is ALWAYS trusted and the safety trips never apply there, no matter who joins — the member-count check and the stranger-speaking check are both skipped for it, and its effective mode ignores any trip record. Every other group keeps the normal trip rules. The allowlist is per-group and is set ONLY by Muse, only when the owner asks Muse (`--allowlist-group <chat_id>`, `--unallowlist-group <chat_id>`, `--list-allowlisted-groups`). There is no Telegram path to it. Listing a group does not change the mode and does not clear a trip; use `--clear-group-safety-trip` for that. The allowlist only bypasses the trips — a group whose configured mode is guarded stays guarded even when listed.
- The wake payload carries `group_mode` (THIS group's locked mode),
  `group_safety_tripped` (whether THIS group's trip is active),
  `group_allowlisted` (whether this group is on the allowlist),
  `group_mode_effective` (the mode the agent must apply — "guarded" when
  the mode is guarded or the trip is active, "trusted" otherwise; an allowlisted group in trusted mode is always "trusted"),
  `group_mode_notice` (the one-time trip notice), and
  `adopted_group_ids` (every group the bot serves). A private DM is
  always trusted. The unified prompt applies the same way in every group — the mode only changes
  what the agent may share.
- Inspect the modes with `--group-mode` (read-only: one record per
  adopted group — mode, allowlisted, trip status, effective mode,
  cached member count — plus the trusted bots, the allowlist, and the
  default mode the first group adopted gets). With a chat id it prints
  only that group. The per-group CLIs `--set-group-mode`,
  `--clear-group-safety-trip`, `--refresh-admins`, and `--active` take
  an optional chat id; with no chat id they use the single adopted
  group, and a chat id is required when several groups are adopted.

## Voice messages

- Voice notes are downloaded by the bridge and queued with a marker
  giving the saved audio path. Before this, a voice message had no text,
  so the intake consumed it and silently dropped it.
- The woken agent transcribes the audio with the local Whisper helper
  (faster-whisper, tiny model, free) and replies in text to what was said:
  ```text
  ~/workspace/whisper-venv/bin/python /opt/muse/assets/audio/transcribe_chunks.py --input '<audio path>' --model ~/workspace/models/whisper-tiny --output-dir ~/workspace/telegram-bridge/transcripts/<update_id> --language en
  ```
  Then it reads `transcript.txt` in the output dir. If the marker says
  the audio could not be downloaded, the agent asks the sender to resend
  it. If the marker says the note is longer than 5 minutes, the agent asks
  for a shorter one.
- Voice notes are capped: over 5 minutes is not downloaded, and downloads
  are capped at 10MB. Voice files are saved 0600 and voice/transcript
  directories 0700; files older than 30 days are pruned.
- Reply latency: an agent turn has a floor (roughly 25–35s for a quick
  greeting, longer for complex work or media), so replies are never
  instant. The hook polls every 5 seconds and the woken agent reads a
  compact context file instead of the full memory. The typing indicator
  (sendChatAction) makes the wait visible instead of silent.

## Media (both directions)

- Inbound: a photo or document the owner sends is downloaded by the
  bridge (largest photo size) and queued with a marker carrying the saved
  file path under `~/workspace/telegram-bridge/media/<update_id>/`, plus
  the caption if any. A photo with no caption is still queued, never
  silently dropped. The agent views the image or reads the file and
  replies to what it shows or says. Media items are always requests
  (never swept as chatter). Same routing as text (DM always answered;
  group rules unchanged). Stickers, video, and animations are not handled.
- Inbound caps: files over 20MB (Telegram's Bot API download limit) are
  not downloaded; the item is queued with a "too big" note instead.
  Failed downloads are queued with a "could not download" note. Media
  files are saved 0600, directories 0700, pruned after 30 days.
- Outbound: the sender has `--photo` (sendPhoto, 10MB cap) and
  `--document` (sendDocument, 50MB cap) modes, multipart upload, HTML
  caption with plain-text fallback on 400 (same retry chain as text).
  Captions are at most 1024 characters. The agent sends media only when
  the owner asked for a file/image or it generated one for the task,
  never unasked.
- Outbound fencing: the bridge has `--send-photo-and-close` and
  `--send-document-and-close` (same shape as `--send-batch-and-close`,
  with optional `--cover`), which fence, send, record the reply, and
  close the item in one call. The file must live under the home directory
  (paths outside it are refused) and must fit the per-kind cap. A
  confirmed media send is never resent; an unknown outcome stays in
  `sending` for reconciliation, same as text.

## Adopt the owner

Adoption binds the bridge to one Telegram user id. Before adoption,
intake records candidate sender ids but serves nobody.

```text
cd <bridge checkout> && TELEGRAM_BRIDGE_STATE_PATH=~/workspace/telegram-bridge/state.json .venv/bin/python -m bridge_core.telegram --adopt <owner_user_id> [trusted|guarded]
```

The optional second argument sets the group sharing mode at setup (see
"Group modes" above). When omitted the mode stays as configured
(guarded by default). Inspect the candidates in the state file and
confirm the id belongs to the user before adopting. Adoption is one time.
Changing it later needs a stopped runtime and an audited state migration.

After adoption, only that owner's private DMs are queued. Other private
senders are ignored.

## Reply lifecycle

Claim one batch from the intake payload, then for each item run the
fenced send, record the reply and close the item. Use the same state path
and sender environment for every step.

```text
.venv/bin/python -m bridge_core.telegram --send-fenced <update_id> <claim_token> <chat_id>
.venv/bin/python -m bridge_core.telegram --replied <update_id> <message_id> <claim_token>
.venv/bin/python -m bridge_core.telegram --done <update_id> <claim_token>
```

Reply text must be 1 to 4096 characters. The bridge refuses empty or
oversized replies before sending.

A send that returns `unknown` stays in `sending` for operator
reconciliation. Never resend it blindly. Only independently confirmed
non-delivery permits `--send-failed` to return it to pending.

## Recover from errors

- A 401 means the token is wrong or revoked. Recheck the registered
  credential and reconnect with the details above.
- A 403 from authd means the credential is not available to the calling
  context. The intake and sender run from the sandbox and the hook
  context. Unregistered connectors returned this error.
- After a failed or timed-out change, check the current state with a read
  before trying again, because the first request may have completed.

## Limits

- The bot answers only the adopted owner in private DMs. Groups are
  served through the auto-reply hook above: the Telegram adapter has no
  per-sender allowlist or mention gate of its own, so the hook's decider
  applies the routing (free-for-all + @mention) before any agent wakes.
- The bridge does not supply a model. The wake hook above supplies the
  scheduler and the wake; intake alone produces no replies.
- Telegram caps one message at 4096 characters.

## Related skills

- [Custom Connector Setup](/opt/muse/skills/custom-connector-setup/SKILL.md)
- Use `muse.skill_search` to find other skills and `muse.connector_search`
  to find other connectors.
