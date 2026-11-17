---
name: conflict-detection
description: Spot two bots touching the same file, repo, branch, or live session before an edit overwrites another bot's work — stop, diff, coordinate.
---

# Conflict Detection

## Purpose

Two bots writing to the same place at the same time silently destroys work. Check before you touch, and stop the moment a collision appears.

## Before you touch anything shared

- Check who else is in the file, repo, branch, or session — active ownership, recent commits, open tasks.
- Track files, branches, repos, and active ownership; flag collisions before editing, not after.
- If another bot is working there, coordinate first. Never edit around them.

## When a collision appears

- Stop. Do not push, do not overwrite, do not "fix it later."
- Diff: see exactly what the other bot changed and what you would change.
- Coordinate: agree on whose change lands, in what order, and who merges.

Real incident this guards against: a push that reverted another bot's commit, and two bots driving one shared session at once.

## Single-writer on shared sessions

- One bot drives a shared account or live session at a time (iStore, a logged-in dashboard, a live cart).
- Check for active work and yield. Live carts and sessions win — if another bot is mid-session, wait.
- Never open a second session on the same account to work around the first.

## What this skill does not do

- It does not plan parallel work — that is split-and-merge. Conflict detection is the guardrail that fires when the plan is incomplete or reality changed.
