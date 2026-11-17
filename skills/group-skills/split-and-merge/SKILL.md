---
name: split-and-merge
description: Break big jobs across bots and merge the results without doubling work — ownership, boundaries, interfaces, and one merger.
---

# Split and Merge

## Purpose

Big jobs get faster when bots work in parallel, and slower when they step on each other. Split cleanly, merge once, under one owner.

## Before any parallel work

- **Ownership** — every piece has one named owner bot.
- **Boundaries** — say exactly what each piece covers and what it must not touch.
- **Interfaces** — agree on the shape of what each piece returns (file, format, fields) before anyone starts.
- **Integration order** — say which pieces land first and what depends on what.

Do not start parallel work until all four are written down.

## The file-ownership check

Before touching a shared file, repo, or branch, check who else is working in it. If another bot owns that piece, coordinate first — do not edit around them.

## The merge rule

- One bot owns the merge. That bot resolves overlaps and decides the final version.
- Merge results, not effort: if two bots produced the same piece, keep one and say which one was dropped.
- The merger verifies the merged result live before calling it done (see verification-before-done).

## What this skill does not do

- It does not cover live collisions while editing — that is conflict-detection. Split-and-merge is the plan; conflict-detection is the guardrail when the plan breaks.
