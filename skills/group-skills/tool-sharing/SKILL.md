---
name: tool-sharing
description: Publish reusable skills and scripts so other bots reuse them instead of rebuilding — purpose, inputs, outputs, owner, version.
---

# Tool Sharing

## Purpose

A skill or script one bot builds should be usable by every bot. Publish it properly once, instead of each bot rebuilding it badly.

## How to publish

Every shared skill or script carries:

- **Purpose** — what it does, in one line.
- **Inputs** — what it needs, and in what form.
- **Outputs** — what it returns, and where it writes.
- **Owner** — which bot maintains it.
- **Version notes** — what changed, and when.

Put it where the other bots can find it, and tell them it exists — once, in the right channel.

## The reuse rule

- Before building something new, check whether a shared skill or script already does it.
- If you improve a shared tool, update its version notes and tell the other bots what changed.
- Do not fork a shared tool silently. If it needs to diverge, say so and name the new owner.

## What this skill does not do

- It does not install anything on another bot by itself. Install is a separate step, owned by whoever runs the rollout, and it needs Westin's go when it touches another bot's setup.
