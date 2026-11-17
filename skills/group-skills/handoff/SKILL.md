---
name: handoff
description: Clean pass-offs between bots — done, left, blocked, files, next step — including the failure branch when work cannot finish, so nothing ever goes silent.
---

# Handoff

## Purpose

Work moves between bots without getting lost. Every pass-off carries the same packet, and a handoff that fails is handed off again — never dropped, never silent.

## The pass-off packet

Every handoff, planned or forced, lists:

- **Done** — what is finished, with evidence.
- **Left** — what remains, in order.
- **Blocked** — what is stuck and what would unblock it.
- **Files** — every file, repo, branch, or session touched.
- **Next step** — the exact next action, not a vague direction.

No handoff without that packet.

## The receiver rule

The receiving bot confirms receipt and restates the next step in its own words. If the packet is missing pieces, ask for them before starting — do not guess.

## The failure branch

If you cannot finish, hand the failure off cleanly instead of going quiet:

- Say so in the same channel the task came from, with the done/left/blocked/files packet.
- Name a next owner, or return it to the requester.
- A silent bot is a failed bot. A failed handoff is handed off, not dropped.

## What this skill does not do

- It does not replace owning the work — hand off because the work needs another bot, not because you want to stop watching it. The proactive-finisher rule still applies: you own a delegation until it comes back.
