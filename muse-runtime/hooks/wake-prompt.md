# Wake prompt — the unified prompt (v2)

The single wake prompt that used to live here was replaced first by
the tiered prompts, then by ONE static prompt for every wake.

The hook definition prompt is now `prompts/unified.md` (see
`hooks/hook-definition-unified-prompt.md` for the exact text to paste
plus install steps). It is the woken agent's full instruction for the
turn: identity, the wake payload (with pre-hydrated `context_md`),
group reply rules (short form; `CHAT_RULES.md` wins on conflict),
group modes, safety, voice, media, commands, and sending via
`--reply`.

The old files are kept for history only and must not be installed:
- `prompts/router.md` — the old router prompt (superseded)
- `prompts/tier-quick.md`, `prompts/tier-standard.md`,
  `prompts/tier-deep.md` — the old per-batch prompts (superseded)

See `SETUP.md` (Step 10) for how to install the hook with the
unified prompt, and `TEST-BOT-INSTALL.md` for the test-bot install.
