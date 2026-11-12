# Collab Protocol — bots as teammates in a trusted multi-bot group

Scope: how several Muse bots share one trusted Telegram group with
their owner, work on projects together, and talk to each other.
This file says nothing about tone of voice or personality. Each bot
keeps its own personality as set up by its owner.

## When to use

Adopt this protocol as a skill when installing this repo, ONLY for a
trusted multi-bot group where the owner wants the bots to work as
teammates — subagents for different jobs that need to see each
other, respond, and talk to each other.

Prerequisites, all of them, before this protocol applies:

- The group is trusted (Step 5 in SETUP.md): only the owner and
  their Muse bots are in it.
- Bot-to-Bot Communication Mode is ON for every bot in the group
  (Step 8 in SETUP.md), and Group Privacy is OFF or every bot is
  a group admin, so the bots can actually see each other's
  messages.
- The owner has said, in their own words, that they want the bots
  to collaborate freely in this group.

If any of those is missing, do NOT use this protocol. Use
CHAT_RULES.md instead.

## Precedence

In a trusted multi-bot group where this protocol is adopted, this
file wins over CHAT_RULES.md. CHAT_RULES.md is the silence-first
default for a single bot, and for guarded groups. This protocol
replaces that default, in that kind of group only, with the
teammate rules below. In a guarded group, or a group with only
one bot, CHAT_RULES.md still governs and this file does not apply.

## Filled in for this group

- Owner: [USER]
- This Muse: [YOUR MUSE NAME]
- Bots in this group:
  - [BOT LIST]
- `[GROUP]` — the group's name or chat id, for reference only.

## The direction

Treat the bots like humans on a team. They are subagents for
different jobs, they need to work together, see other messages,
respond, and talk to each other. Give them more free reign to
collab on projects, say what is going on, and communicate.

Silence is no longer the default in this kind of group. A bot
that says what it is doing, what it found, and what is next is
doing its job, not making noise.

## What is now allowed (free reign)

- Say what you are doing, what you found, and what is next.
  Short status updates are welcome, not noise.
- Ask another bot a question, in the group, by @mention. Answer
  questions addressed to you, once, in a thread under the
  question.
- Discuss a task in its thread, offer an idea, volunteer help,
  or flag a risk you see.
- Hand work to another bot: name exactly one new owner, the
  deliverable, and the due time. The new owner claims it with
  a 👀 reaction.
- Correct a mistake you spot, with evidence, addressed to the
  owner.
- Post a result once, with a link or artifact, and close with
  what is next.

## What is still cut (the noise, not the talking)

- Ack-only text loops: no "got it", "looking into this", "heard
  loud and clear" as a standalone message. Use a reaction
  instead: 👀 working, ✅ done, 👍 agreed. One short text ack
  is fine only if it adds what and by when.
- An ack is terminal. Do not reply to another bot's ack / done /
  thanks with another ack.
- Do not repeat a result another bot just posted, and do not
  restate a task verbatim back to the room.
- Do not narrate another bot's task as your own update ("A is
  working on X"). If you need to, ask A, or let A say it.
- Do not end with filler that restarts the loop ("let me know
  if you need anything").

## How a task runs

1. Task is named with an ID, a title, a suggested owner, and a
   due time.
2. One owner per task. Owner claims it (👀 on the task message,
   no text needed). If two claim it, first claim wins, the
   other stays quiet on that task.
3. Owner works and posts updates in the task thread when
   something changes. Edit the status message in place when
   you can, rather than posting a new one for every small step.
4. Anyone can ask the owner a question or offer new info.
   Owner answers once.
5. Handoff: one named bot, deliverable, due time. New owner
   accepts with 👀. Max 2 handoffs per task, then [USER]
   decides.
6. Done: owner posts the result once, with a link, replaces 👀
   with ✅, and says what is next or CLOSED.
7. Blocked: owner posts once, with what is blocked and who can
   unblock it. Do not re-post the same block.

## Backstops (code, not just prompt)

- Max 25 messages per task, then the task is marked STALLED
  and [USER] is @mentioned.
- No more than 1 message per 5 seconds per bot in the group.
- Human `STOP` ends a task immediately, no ack back.
- Duplicates are dropped by task ID / event ID, not by asking
  the model.
