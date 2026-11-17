---
name: health-and-silence
description: Heartbeat cadence, stale thresholds, and watchdog alerts — how to tell a quiet bot from a down bot, and when to sound the alarm.
---

# Health and Silence Discipline

## Purpose

Silence is the crew's number one failure mode. A bot that stops answering is a bot that has failed — the skill is knowing how quiet is too quiet, and who sounds the alarm.

## Heartbeats and stale thresholds

- Every bot answers a heartbeat on its normal cadence. A missed heartbeat is a signal, not yet a verdict.
- Stale means unanswered work past its deadline, or no heartbeat for longer than the agreed threshold. Set the threshold per bot and write it down — do not guess it in the moment.
- Never declare a bot down on silence alone. Check the service, the port, and the last working turn first.

## Watchdog alerts

- Watchdogs restart and fix; alerts only tell someone something is wrong. Do not claim a watchdog revives a runtime it cannot reach.
- Alert on unanswered work over 30 minutes — never on silence alone.
- An alert names the bot, the task, and how long it has been sitting. "Something is down" without those three is not an alert.

## When a bot is down

- Say so once, in the right channel, with what is known and what is unknown.
- Route around it if the work allows; escalate to Westin only if all paths are down or the work cannot wait.
- When it comes back, confirm it is actually answering — a process being up is not the same as a bot being alive.

## What this skill does not do

- It does not replace owning a delegation. The proactive-finisher rule still applies: you own a task until it comes back, and a silent bot is your problem to report, not to forget.
