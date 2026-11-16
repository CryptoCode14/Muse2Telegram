#!/usr/bin/env python3
"""quinn-watchdog.py — VM-side watchdog for Quinn-as-Hermes-brain.

Probes the quinn-bridge (http://127.0.0.1:8769/v1/chat/completions) with a
short "reply with exactly: QUINN OK" request. A probe passes only when the
bridge returns HTTP 200 with QUINN OK in the reply — i.e. Quinn actually
answered over A2A. Ollama fallback replies do NOT contain QUINN OK, so a
fallback answer counts as a failure: if Quinn is down, Hermes is on the
backup and Westin needs to know.

On 2 consecutive failed probes, posts an alert to the Master Muse Telegram
group (<your-telegram-group-id>) MENTIONING Westin (@<owner-telegram-username>) via a direct
Telegram Bot API send from the Helper Quinn bot (token read at runtime
from /home/ubuntu/.hermes/.env, never hardcoded or logged). The mention
is sent as an explicit "mention" entity so Telegram parses and notifies
it. This path depends on neither Quinn nor the Hermes gateway — it works
precisely when Quinn is down. If the direct send fails, falls back to an
A2A inform to westin-jr asking him to post the mention.

Alerts re-fire at most once per 30 minutes while Quinn stays down. When
a probe passes after an alert, a plain recovery notice (no @ mention)
is posted to the group.

Run by systemd timer quinn-watchdog.timer every 5 min (oneshot service).
State in quinn-brain/watchdog-state.json. Create quinn-brain/WATCHDOG_PAUSE
to pause alerting during deliberate maintenance (probe skipped).

Flags (manual use only):
  --probe-only   run one probe, print PASS/FAIL, do not alert or write state
  --test-alert   send one labelled TEST alert to the group, verify the
                 mention entity in Telegram's response, exit

Stdlib only. No money, orders, or ServiceTitan.
"""
import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.request

BRIDGE_URL = "http://127.0.0.1:8769/v1/chat/completions"
ENV_FILE = "/home/ubuntu/.hermes/.env"
STATE_FILE = "/home/ubuntu/a2a/quinn-brain/watchdog-state.json"
LOCK_FILE = "/home/ubuntu/a2a/quinn-brain/watchdog.lock"
PAUSE_FILE = "/home/ubuntu/a2a/quinn-brain/WATCHDOG_PAUSE"
A2A_CLIENT = "/home/ubuntu/a2a/a2a_client.py"
A2A_URL = "https://<your-vm-host>/"
A2A_KEY = "/home/ubuntu/a2a/key"

GROUP_CHAT_ID = "<your-telegram-group-id>"
WESTIN_USERNAME = "<owner-telegram-username>"  # @<owner-telegram-username>
PROBE_TIMEOUT = 300  # seconds; bridge waits 600s for Quinn, probe gives up first
FAILURES_TO_ALERT = 2
ALERT_COOLDOWN_S = 1800  # re-alert at most every 30 min while down

PROBE_PROMPT = "WATCHDOG PING — reply with exactly: QUINN OK"


def log(msg):
    print("%s quinn-watchdog: %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg),
          flush=True)


def read_env_token():
    """Read TELEGRAM_BOT_TOKEN from the Hermes .env without logging it."""
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return val or None
    except OSError as e:
        log("cannot read %s: %s" % (ENV_FILE, e))
    return None


def probe():
    """One end-to-end probe. Returns (ok, detail)."""
    body = json.dumps({
        "model": "quinn",
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "stream": False,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(BRIDGE_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
        content = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
        if resp.status == 200 and "QUINN OK" in content.upper():
            return True, "reply=%r" % content.strip()[:40]
        return False, "http=%s reply=%r" % (resp.status, content.strip()[:80])
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:120]
        except Exception:
            detail = ""
        return False, "http=%s %s" % (e.code, detail)
    except Exception as e:
        return False, "error: %s" % (type(e).__name__,)


def telegram_send(text, entities=None):
    """Direct Bot API send to Master Muse. Returns (ok, info)."""
    token = read_env_token()
    if not token:
        return False, "no TELEGRAM_BOT_TOKEN in %s" % ENV_FILE
    body = {"chat_id": GROUP_CHAT_ID, "text": text}
    if entities:
        body["entities"] = entities
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % token,
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
        if result.get("ok"):
            msg = result.get("result", {})
            ents = [(e.get("type"), e.get("offset"), e.get("length"))
                    for e in msg.get("entities", [])]
            return True, "message_id=%s entities=%s" % (msg.get("message_id"), ents)
        return False, "telegram ok=false: %s" % (result,)
    except urllib.error.HTTPError as e:
        return False, "telegram http=%s" % e.code
    except Exception as e:
        return False, "telegram error: %s" % type(e).__name__


def a2a_fallback_alert(detail):
    """Best-effort fallback: ask Westin Jr. over A2A to post the mention."""
    import subprocess
    payload = json.dumps({"message":
        "WATCHDOG ALERT (fallback — direct Telegram send failed): Quinn "
        "(Hermes brain) is not replying via quinn-bridge (%s). Please post "
        "to Master Muse mentioning Westin (@<owner-telegram-username>) that Quinn is "
        "down and Hermes is on Ollama backup." % detail})
    try:
        r = subprocess.run([sys.executable, A2A_CLIENT, "--url", A2A_URL,
                            "--key-file", A2A_KEY, "send", "--from",
                            "quinn-watchdog", "--to", "westin-jr", "--type",
                            "inform", "--task", "quinn-watchdog-alert-001",
                            "--payload", payload],
                           capture_output=True, timeout=60)
        return r.returncode == 0
    except Exception as e:
        log("a2a fallback failed: %s" % e)
        return False


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        s.setdefault("consecutive_failures", 0)
        s.setdefault("last_alert_at", 0)
        s.setdefault("alerted", False)
        return s
    except Exception:
        return {"consecutive_failures": 0, "last_alert_at": 0, "alerted": False,
                "last_ok_at": 0, "last_probe_at": 0, "last_result": "never"}


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_FILE)


def send_alert(detail, test=False):
    if test:
        text = ("@%s [WATCHDOG TEST — please ignore] This is a test of the "
                "Quinn watchdog alert. If you can see this @ mention, the "
                "mention path works." % WESTIN_USERNAME)
    else:
        text = ("@%s \u26a0\ufe0f Quinn (Hermes brain) is DOWN \u2014 no reply "
                "from quinn-bridge on %d consecutive watchdog probes "
                "(last probe: %s). Hermes is on Ollama backup. Quinn's A2A "
                "brain is not answering." % (WESTIN_USERNAME,
                                             FAILURES_TO_ALERT, detail))
    entities = [{"type": "mention", "offset": 0,
                 "length": len("@" + WESTIN_USERNAME)}]
    ok, info = telegram_send(text, entities)
    log("alert send: ok=%s %s" % (ok, info))
    return ok, info


def main():
    args = set(sys.argv[1:])
    if "--probe-only" in args:
        ok, detail = probe()
        print("PROBE %s: %s" % ("PASS" if ok else "FAIL", detail))
        return 0 if ok else 1
    if "--test-alert" in args:
        ok, info = send_alert("test", test=True)
        print("TEST ALERT %s: %s" % ("SENT" if ok else "FAILED", info))
        return 0 if ok else 1

    if os.path.exists(PAUSE_FILE):
        log("paused by %s — skipping probe" % PAUSE_FILE)
        return 0

    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another watchdog run is active — skipping")
        return 0

    state = load_state()
    now = int(time.time())
    ok, detail = probe()
    state["last_probe_at"] = now
    state["last_result"] = ("PASS: " + detail) if ok else ("FAIL: " + detail)
    if ok:
        if state.get("alerted"):
            ok2, info = telegram_send(
                "Quinn (Hermes brain) is BACK \u2014 watchdog probe passing "
                "again (quinn-bridge answered QUINN OK). Hermes brain "
                "restored; Ollama backup no longer in use.")
            log("recovery notice: ok=%s %s" % (ok2, info))
            state["alerted"] = False
        state["consecutive_failures"] = 0
        state["last_ok_at"] = now
        log("probe PASS (%s)" % detail)
    else:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        log("probe FAIL #%d (%s)" % (state["consecutive_failures"], detail))
        if (state["consecutive_failures"] >= FAILURES_TO_ALERT
                and now - state.get("last_alert_at", 0) >= ALERT_COOLDOWN_S):
            sent, info = send_alert(detail)
            if sent:
                state["last_alert_at"] = now
                state["alerted"] = True
            else:
                log("direct Telegram alert failed — trying A2A fallback to westin-jr")
                if a2a_fallback_alert(detail):
                    state["last_alert_at"] = now
                    state["alerted"] = True
                    log("A2A fallback alert sent to westin-jr")
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
