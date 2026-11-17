---
name: checkpoint-commit-discipline
description: Never lose work and never leave a shared tree unrecoverable — commit before risky work, one feature per step, verify then commit, fresh context per iteration.
---

# Checkpoint / Commit Discipline

## Purpose

Work that is not committed does not exist. A session can die, a runtime can stall, a VM can be replaced — if the work was never committed, it is gone. This skill is the safety net under every other skill: commit early, commit often, and never start the next thing until the last thing is safe.

## The checkpoint rule (hard)

- Commit before risky work, so a bad change can be rolled back.
- Never work in a zero-commit worktree on shared code. If there is nothing to roll back to, you have no safety net. Establish a checkpoint (a commit, a tag, a backup) before the first real change.
- One feature per session or step. Implement it, verify it end to end, commit, and update the progress file before starting the next. Do not stack uncommitted work.

## One iteration, one commit

- Each iteration gets a fresh context. Pick the single most important unchecked item, implement it fully with no placeholders, verify, update the plan file, and commit.
- Memory lives in files and git, not conversation. If the plan is only in your head, it dies with the session.
- Separate structural changes from behavioural changes. Never mix them in one commit. Tidy first when both are needed, and commit red, green, and refactor separately.

## Checklists for multi-step work

- Use a checklist or scratchpad for multi-step work. Write every error to it, fix and verify one item at a time, and check it off before moving on.
- A correction at step two is cheap; after a full implementation it is a rewrite. Course-correct early and often — interrupt, rewind, and re-prompt rather than letting a wrong direction run to completion.

## What this skill does not do

- It does not replace verification-before-done. Verification proves the work is real; checkpoint discipline proves it is recoverable. Both run before anything ships.
- It does not replace the reviewer skill. A committed change still needs review before it merges.
