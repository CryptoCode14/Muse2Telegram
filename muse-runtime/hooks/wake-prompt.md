# Wake prompt — superseded by the tiered prompts

The single wake prompt that used to live here was replaced by the tiered
prompt system. Do not use this file.

The hook definition prompt is now the tiny router in
`prompts/router.md`. It tells the woken agent to read only the prompt
file for the payload's tier:

- quick: `prompts/tier-quick.md` — simple greetings / small talk
- standard: `prompts/tier-standard.md` — normal conversation
- deep: `prompts/tier-deep.md` — complex work (reads the standard prompt
  first, then adds full context)

See `SETUP.md` (Step 10) for how to install the hook with the router
prompt.
