## What this changes

<!-- One or two sentences. -->

## Checklist

- [ ] The offline suite passes: `cd muse-runtime && python3 -m unittest discover -s tests` (578 tests, zero failures)
- [ ] No failing test was weakened, deleted, or edited to pass
- [ ] No personal identities in shipped files (generic `ExampleBot` / `MyBot` / `[USER]` only)
- [ ] No `state.json`, token, credential, or personal data committed
- [ ] Doc numbers match the code (suite count, prompt size, watchdog poll, heartbeat horizon) wherever they are stated
- [ ] If the unified prompt changed, its `Size:` header was regenerated and the hook-definition embed kept byte-identical

## How it was verified

<!-- Suite output, and any live test-bot check. -->
