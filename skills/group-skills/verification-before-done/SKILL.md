---
name: verification-before-done
description: Never claim done without fresh evidence — live checks, green tests, and the diff inspected — before any install, ship, or completion claim.
---

# Verification Before Done

## Purpose

A claim without evidence is a guess. Before any bot says done, healthy, shipped, or installed, it verifies live, this turn, against the real thing.

## The verification rule (hard)

- No completion claim without fresh evidence from this turn: a test result, a live check, a log line, a reply actually read.
- A failing test means the code is wrong. Never edit the test to pass.
- For installs and ships: a fresh green suite plus a live check against real code and logs. The code looking right is not evidence.

## Live-verify before you claim

- Check the live record (bridge, calendar, sheet, recording) before flagging something as missing or broken.
- Never declare something absent without a variant search — different spelling, different field, different view.
- If the live record contradicts your claim, the live record wins.

## Change verification (coding lane)

Before handing off a code change: run targeted tests, run lint and type checks, inspect the diff, and report the evidence with the handoff — not just "tests pass."

## Checkpoint first

- Commit before risky work, so a bad change can be rolled back.
- Never work in a zero-commit worktree on shared code. If there is nothing to roll back to, you have no safety net.

## What this skill does not do

- It does not replace the reviewer skill. Verification proves the work is real; review checks whether the work is right. Both run before anything ships.
