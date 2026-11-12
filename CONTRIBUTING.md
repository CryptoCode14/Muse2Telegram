# Contributing

## Before you send a pull request

- Run the offline suite and keep it green:

  ```text
  cd muse-runtime && python3 -m unittest discover -s tests
  ```

  The baseline is 478 tests with zero failures. Do not proceed on a
  failing suite you cannot explain, and do not weaken, delete, or
  edit a failing test to make it pass — if a test fails after your
  change, the code is wrong, not the test.

- **No personal identities in shipped files.** Use generic examples
  only: `ExampleBot` / `MyBot` for bots, `owner` / `[USER]` for the
  user. Never commit a real bot username, user id, group id, name,
  token, or personal detail to the repo.

- **Doc numbers must match the code.** If you change the suite, the
  unified prompt, the watchdog poll, or the heartbeat horizon, update
  every number that states it (`README.md`, `muse-runtime/README.md`,
  `muse-runtime/SETUP.md`, `muse-runtime/CONNECTOR.md`,
  `muse-runtime/TEST-BOT-INSTALL.md`, `simulation/README.md`). The
  unified prompt carries a `Size:` header the suite enforces — after
  editing `muse-runtime/prompts/unified.md`, regenerate the header to
  the exact new character count and keep the embed in
  `muse-runtime/hooks/hook-definition-unified-prompt.md`
  byte-identical to it.

- **Never commit** `state.json`, a token, a credential surrogate, or
  any personal data. The repo holds code + docs only.

- Keep changes small and scoped: one behaviour per pull request,
  with the tests that prove it.

## Security reports

Do not open a public issue for a security problem. See
`SECURITY.md` for how to report it privately.
