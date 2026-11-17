---
name: reviewer
description: One bot reviews another's work before it goes out — spec, tests, live behaviour — with a red-team mode that challenges the work before shipping.
---

# Reviewer

## Purpose

Nothing ships on one bot's word alone. A second bot checks the work against the spec, the tests, and the live behaviour — and in red-team mode, actively tries to break it first.

## How a review works

- The reviewer gets the diff or artifact only, with clean context — not the proposer's reasoning.
- Check three things: does it match the spec, do the tests pass, does it behave correctly live.
- Block shipping on critical or high findings. Minor findings get fixed or listed, not silently ignored.

## Red-team mode

For anything that ships, spends, or changes shared state, the reviewer switches to red-team:

- Challenge correctness, regressions, security, and missing tests.
- Ask what breaks if this is wrong, and what the rollback is.
- A passing review means the reviewer could not break it, not that they agree with it.

## The rounds rule

- The proposer fixes what the reviewer finds. Max 3 rounds.
- After 3 rounds, or on a disagreement either side cannot resolve, it goes to a human call — Westin decides, the bots do not deadlock.

## What this skill does not do

- It does not replace verification-before-done. Review checks the work; verification proves it with fresh evidence. Both run before anything ships.
