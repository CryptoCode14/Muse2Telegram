# Hermes config change plan — point Hermes at the Quinn bridge

Owner of this change: Quinn (lead). This is a PLAN for Quinn to
execute — Dorian does not edit /home/ubuntu/.hermes/config.yaml
directly. Quinn owns the config.yaml changes (backup first) and the
switch-over decision (DESIGN.md).

Goal: Hermes stops using local Ollama Qwen 2.5 as its primary brain
and talks to the quinn-bridge instead (http://127.0.0.1:8769/v1,
model `quinn`, provider `custom`). Local Ollama Qwen 2.5 stays as
backup — the fallback lives in the bridge server itself
(quinn-bridge.py `ollama_fallback`, which proxies the same OpenAI
request to http://127.0.0.1:11434/v1 when Quinn times out or is
down). Ollama is NOT removed or stopped.

## Current state (verified 2026-11-13, /home/ubuntu/.hermes/config.yaml)

```yaml
# lines 73-79
model:
  api_key: ollama
  base_url: http://127.0.0.1:11434/v1
  default: qwen2.5:7b-64k
  context_length: 64000
  ollama_num_ctx: 65536
  provider: ollama-launch
```

```yaml
# lines 99-105
providers:
  ollama-launch:
    api: http://127.0.0.1:11434/v1
    default_model: qwen2.5:7b
    models:
      - qwen2.5:7b
    name: Ollama
```

## Step 0 — Backup first (never edit without a backup)

```bash
cp /home/ubuntu/.hermes/config.yaml \
   /home/ubuntu/.hermes/config.yaml.bak-20261113
cp /home/ubuntu/.hermes/.env \
   /home/ubuntu/.hermes/.env.bak-20261113 2>/dev/null || true
ls -la /home/ubuntu/.hermes/config.yaml.bak-20261113
```

Verify the backup exists and is non-empty before proceeding.

## Step 1 — Exact config.yaml edits (model section only)

Change ONLY these three lines in the `model:` section (lines 75, 76, 79).
Leave `api_key`, `context_length`, and `ollama_num_ctx` untouched —
the bridge ignores the api key (localhost, no auth), and the context
settings are still valid for the fallback path.

Before:

```yaml
model:
  api_key: ollama
  base_url: http://127.0.0.1:11434/v1
  default: qwen2.5:7b-64k
  context_length: 64000
  ollama_num_ctx: 65536
  provider: ollama-launch
```

After:

```yaml
model:
  api_key: ollama
  base_url: http://127.0.0.1:8769/v1
  default: quinn
  context_length: 64000
  ollama_num_ctx: 65536
  provider: custom
```

That is:
- `base_url`: `http://127.0.0.1:11434/v1` -> `http://127.0.0.1:8769/v1`
- `default`: `qwen2.5:7b-64k` -> `quinn`
- `provider`: `ollama-launch` -> `custom`

## Step 2 — Leave the providers section alone

Do NOT delete or change the `providers.ollama-launch` entry
(lines 99-105). It is the Ollama provider definition and stays
valid. The fallback to Ollama is done by the bridge server, not by
Hermes switching providers — Hermes always talks to the bridge,
and the bridge decides (Quinn first, Ollama on timeout). Keeping
the entry means Ollama is still addressable if Hermes ever needs
it directly, and it documents what the backup is.

No new provider entry is needed: the `custom` provider in Hermes
just points at a `base_url` (the quinn-bridge), per DESIGN.md.
No Hermes plugin rewrite is needed.

## Step 3 — Verify the bridge is up before switching Hermes

Do NOT switch Hermes until the bridge answers. Run from the VM:

```bash
curl -s http://127.0.0.1:8769/health
# expect: {"status":"ok","brain":"quinn","fallback":"ollama"}

curl -s http://127.0.0.1:8769/v1/models
# expect: {"object":"list","data":[{"id":"quinn","object":"model",...}]}
```

If either fails, do NOT proceed — fix the bridge first
(systemd `quinn-bridge`, port 8769). Hermes must never point
at a dead bridge.

## Step 4 — Restart Hermes gateway (Quinn decides when)

Hermes reads config.yaml at startup, so the gateway must restart
to pick up the new base_url. Quinn owns this decision and the
timing. Do NOT stop Ollama — it is the backup and must keep
running. Do NOT stop any a2a-* service.

```bash
# Quinn's call — restart only the Hermes gateway, not Ollama:
sudo systemctl restart hermes-gateway
# (or the equivalent command Quinn uses for the gateway)
sudo systemctl status hermes-gateway --no-pager | head -15
```

## Step 5 — End-to-end verification (after restart)

```bash
# 1. A Hermes turn that must be answered by Quinn, not Ollama:
hermes --reasoning none -z "Reply with exactly: HELLO QUINN"
# Expect the exact text back. Check the bridge log for the task:
# /home/ubuntu/a2a/quinn-brain/quinn-bridge.log should show
# "quinn replied task=qh-... chars=..." (not "fallback ollama").

# 2. Confirm the bridge is the path (not direct Ollama):
# - /v1/models on 8769 returns id "quinn".
# - A slow or missing Quinn reply falls back to Ollama (bridge log
#   shows "fallback ollama") — see the fallback test in Step 6.
```

## Step 6 — Fallback test (proves Ollama is still the backup)

Simulate Quinn down / timeout and confirm Ollama answers:

```bash
# Option A (preferred, no service stop): set a short bridge timeout
# for one request so Quinn cannot reply in time, then check the
# response came from Ollama (bridge log shows "fallback ollama").
# Option B: stop ONLY the Quinn A2A responder path briefly (Quinn's
# call), send a Hermes turn, confirm Ollama answers, then restore.
# Never stop Ollama itself for this test.
```

Record which option was used and the result in STATUS.md.

## Rollback (if anything is wrong)

```bash
cp /home/ubuntu/.hermes/config.yaml.bak-20261113 \
   /home/ubuntu/.hermes/config.yaml
sudo systemctl restart hermes-gateway
# Verify Hermes is back on Ollama:
grep -A 6 '^model:' /home/ubuntu/.hermes/config.yaml
# base_url should be http://127.0.0.1:11434/v1 again.
```

Rollback restores the exact pre-change config. No bridge
changes are needed for rollback — the bridge keeps running and
Hermes simply stops calling it.

## What this plan does NOT change

- No Ollama removal, uninstall, or stop. Ollama stays as backup.
- No .env changes (the 2026-11-13 Telegram allowlist fixes stay).
- No a2a-* service changes.
- No keys, tokens, or passwords in this plan or in config.yaml
  (api_key stays the literal string `ollama`, which is not a
  real secret — the bridge ignores it on localhost).
- No money, orders, or ServiceTitan. This touches Hermes + Ollama only.
