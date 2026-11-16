#!/usr/bin/env python3
"""quinn-health-watch.py — passive health watch for Quinn-as-Hermes-brain.

Replaces the old active-probe watchdog (quinn-watchdog.py, which burned a
bridge chat turn every 5 min and fired false DOWN alerts whenever Quinn's
turn start ran slower than the probe interval). Two passive layers, no
agent turn burned:

LAYER 1 — service health (cheap, every run). systemctl is-active for
  hermes-gateway, quinn-bridge, a2a-quinn, a2a-server, plus HTTP health
  endpoints (gateway 8644/health, bridge 8769/health, A2A /health).
  A service counts as down only after 2 consecutive failed checks
  (10 min). systemd Restart=always already restarts it; we alert only
  if it stays down.

LAYER 2 — unanswered work (dead-man's switch on the task queue, no ping).
  Reads /home/ubuntu/a2a/tasks.json directly and finds tasks addressed
  to quinn that are still status working, whose latest message is a
  request from someone else, unanswered for 30+ minutes. Alert only if
  such work exists — silence with no pending work (e.g. overnight) is
  normal and never alerts. Each task alerts once (tracked in state);
  alerts re-fire at most every 60 min while work stays unanswered;
  a recovery notice goes out when the queue clears.

No Layer 3 active ping, per Westin: dropped entirely.

ALERT PATH: A2A inform to westin-jr (he is the front door; he decides
whether the owner needs to know). If the A2A send fails — e.g. the bus
itself is down, exactly when out-of-band matters — fall back to a
direct Telegram Bot API send to the alert group mentioning the owner,
reusing the helper bot token from the Hermes .env file
(never hardcoded or logged).

FIRST RUN / BASELINE: the task store already holds stale "working"
tasks (dozens, hours old, from before the confirm-policy fix). On the
first run with no state file, all currently-unanswered task ids are
recorded as baseline and NOT alerted on — only work that becomes
unanswered after deploy can trip an alert.

Run by systemd timer quinn-health-watch.timer every 5 min (oneshot).
State in quinn-brain/health-watch-state.json. Create
quinn-brain/WATCHDOG_PAUSE to pause alerting during maintenance.

Flags (manual use only):
  --dry-run    print what would be checked/alerted, write no state, send nothing
  --test-alert send one labelled TEST alert (A2A to westin-jr), exit

Stdlib only. No money, orders, or ServiceTitan.
"""
import fcntl
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

TASKS_FILE = "/home/ubuntu/a2a/tasks.json"
STATE_FILE = "/home/ubuntu/a2a/quinn-brain/health-watch-state.json"
LOCK_FILE = "/home/ubuntu/a2a/quinn-brain/health-watch.lock"
PAUSE_FILE = "/home/ubuntu/a2a/quinn-brain/WATCHDOG_PAUSE"
ENV_FILE = "/home/ubuntu/.hermes/.env"
A2A_CLIENT = "/home/ubuntu/a2a/a2a_client.py"
A2A_URL = "https://YOUR-A2A-HOST/"
A2A_KEY = "/home/ubuntu/a2a/key"

# Fallback-alert destination. Set these in the environment on the host
# that runs the watcher; empty means the Telegram fallback is off
# (A2A alerts still work).
GROUP_CHAT_ID = os.environ.get("ALERT_GROUP_CHAT_ID", "")
WESTIN_USERNAME = os.environ.get("OWNER_TELEGRAM_USERNAME", "")  # without @

SERVICES = ["hermes-gateway", "quinn-bridge", "a2a-quinn", "a2a-server"]
HEALTH_URLS = {
    "hermes-gateway": "http://127.0.0.1:8644/health",
    "quinn-bridge": "http://127.0.0.1:8769/health",
    "a2a-server": "https://YOUR-A2A-HOST/health",
}
SERVICE_FAILS_TO_ALERT = 2      # consecutive failed checks before alerting
UNANSWERED_MIN = 30             # minutes of unanswered work before alerting
ALERT_COOLDOWN_S = 3600         # re-alert at most every 60 min per condition
HTTP_TIMEOUT = 10


def log(msg):
    print("%s quinn-health-watch: %s" % (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg), flush=True)


def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


# ---------------- Layer 1: service health ----------------

def check_service(name):
    """Returns (ok, detail). systemctl is-active + HTTP health if known."""
    try:
        r = subprocess.run(["systemctl", "is-active", name],
                           capture_output=True, timeout=10, text=True)
        active = r.stdout.strip() == "active"
    except Exception as e:
        return False, "systemctl error: %s" % type(e).__name__
    if not active:
        return False, "systemctl is-active=%s" % (r.stdout.strip() or "?")
    url = HEALTH_URLS.get(name)
    if url:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                body = resp.read().decode()[:120]
            if resp.status != 200:
                return False, "health http=%s" % resp.status
            return True, "active, health ok (%s)" % body[:60]
        except urllib.error.HTTPError as e:
            return False, "health http=%s" % e.code
        except Exception as e:
            return False, "health error: %s" % type(e).__name__
    return True, "active"


# ---------------- Layer 2: unanswered work ----------------

def unanswered_tasks(now):
    """Tasks to quinn, still working, latest msg a request from someone
    else, unanswered > UNANSWERED_MIN. Returns list of
    (task_id, age_min, sender, summary)."""
    try:
        with open(TASKS_FILE) as f:
            tasks = json.load(f)
    except Exception as e:
        log("cannot read %s: %s" % (TASKS_FILE, e))
        return None
    out = []
    for tid, t in tasks.items():
        if t.get("status") != "working":
            continue
        hist = t.get("history") or []
        if not hist:
            continue
        last = hist[-1]
        if last.get("to") != "quinn" or last.get("from") == "quinn":
            continue
        if last.get("type") not in ("request", "inform"):
            continue
        ref = t.get("updated_at") or t.get("created_at") or ""
        age_min = (now - parse_ts(ref)) / 60.0
        if age_min < UNANSWERED_MIN:
            continue
        msg = ""
        payload = last.get("payload") or {}
        if isinstance(payload, dict):
            msg = (payload.get("message") or payload.get("result") or "")[:80]
        out.append((tid, round(age_min), last.get("from"), msg))
    out.sort(key=lambda x: -x[1])
    return out


# ---------------- Alerting ----------------

def read_env_token():
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError as e:
        log("cannot read %s: %s" % (ENV_FILE, e))
    return None


def telegram_send(text, entities=None):
    if not GROUP_CHAT_ID:
        return False, "ALERT_GROUP_CHAT_ID not set, Telegram fallback off"
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
            return True, "message_id=%s" % result.get("result", {}).get("message_id")
        return False, "telegram ok=false: %s" % (result,)
    except urllib.error.HTTPError as e:
        return False, "telegram http=%s" % e.code
    except Exception as e:
        return False, "telegram error: %s" % type(e).__name__


def a2a_inform(message, task_id="quinn-health-watch-alert-001"):
    payload = json.dumps({"message": message})
    try:
        r = subprocess.run([sys.executable, A2A_CLIENT, "--url", A2A_URL,
                            "--key-file", A2A_KEY, "send", "--from",
                            "quinn-health-watch", "--to", "westin-jr",
                            "--type", "inform", "--task", task_id,
                            "--payload", payload],
                           capture_output=True, timeout=60, text=True)
        if r.returncode == 0:
            return True, "a2a inform sent (%s)" % task_id
        return False, "a2a send rc=%s: %s" % (r.returncode, (r.stderr or "")[:120])
    except Exception as e:
        return False, "a2a send error: %s" % type(e).__name__


def send_alert(text, mention_westin=False):
    """Primary: A2A inform to westin-jr. Fallback: direct Telegram send
    (with @Westin mention only when the A2A path failed)."""
    ok, info = a2a_inform(text)
    log("alert via A2A: ok=%s %s" % (ok, info))
    if ok:
        return True
    log("A2A alert failed — falling back to direct Telegram send")
    entities = None
    if mention_westin and WESTIN_USERNAME:
        text = "@%s %s" % (WESTIN_USERNAME, text)
        entities = [{"type": "mention", "offset": 0,
                     "length": len("@" + WESTIN_USERNAME)}]
    ok2, info2 = telegram_send(text, entities)
    log("alert via Telegram fallback: ok=%s %s" % (ok2, info2))
    return ok2


# ---------------- State ----------------

def default_state():
    return {"service_fail_counts": {}, "service_alerted": [],
            "last_alert_at": 0, "alerted_task_ids": [],
            "baseline_done": False, "work_alerted": False,
            "last_run_at": 0}


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        d = default_state()
        d.update(s)
        return d
    except Exception:
        return default_state()


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_FILE)


# ---------------- Main ----------------

def main():
    args = set(sys.argv[1:])
    dry = "--dry-run" in args
    if "--test-alert" in args:
        ok = send_alert("[WATCHDOG TEST — please ignore] Quinn health-watch "
                        "test alert. If Westin Jr. sees this over A2A, the "
                        "alert path works.")
        print("TEST ALERT %s" % ("SENT" if ok else "FAILED"))
        return 0 if ok else 1

    if os.path.exists(PAUSE_FILE):
        log("paused by %s — skipping" % PAUSE_FILE)
        return 0

    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another health-watch run is active — skipping")
        return 0

    state = load_state()
    now = int(time.time())
    state["last_run_at"] = now

    # Layer 1
    down = []
    for svc in SERVICES:
        ok, detail = check_service(svc)
        log("layer1 %s: %s (%s)" % (svc, "OK" if ok else "DOWN", detail))
        if dry:
            continue
        counts = state.setdefault("service_fail_counts", {})
        if ok:
            counts[svc] = 0
            if svc in state.get("service_alerted", []):
                state["service_alerted"].remove(svc)
                send_alert("RECOVERY: %s is back up (health checks passing)." % svc)
        else:
            counts[svc] = counts.get(svc, 0) + 1
            if counts[svc] >= SERVICE_FAILS_TO_ALERT:
                down.append(svc)
    if down and not dry:
        if now - state.get("last_alert_at", 0) >= ALERT_COOLDOWN_S:
            new_down = [s for s in down if s not in state.get("service_alerted", [])]
            if new_down:
                send_alert("HEALTH ALERT: service(s) down on 2 consecutive "
                           "checks (10 min): %s. systemd Restart=always "
                           "should have restarted them; still down, needs a "
                           "look." % ", ".join(new_down), mention_westin=True)
                state["service_alerted"] = list(
                    set(state.get("service_alerted", [])) | set(new_down))
                state["last_alert_at"] = now
        else:
            log("service(s) down but inside 60-min alert cooldown: %s" % down)

    # Layer 2
    unans = unanswered_tasks(now)
    if unans is None:
        log("layer2 skipped: tasks.json unreadable")
    else:
        log("layer2: %d task(s) to quinn unanswered 30+ min" % len(unans))
        for tid, age, sender, msg in unans[:5]:
            log("  unanswered: %s %d min from %s: %s" % (tid, age, sender, msg))
        if dry:
            print("DRY RUN: no state written, no alerts sent")
            return 0
        if not state.get("baseline_done"):
            # First run: seed baseline so pre-existing stale work never alerts.
            state["alerted_task_ids"] = [t[0] for t in unans]
            state["baseline_done"] = True
            log("baseline seeded with %d pre-existing unanswered task(s); "
                "these will not alert" % len(unans))
        else:
            still_open = {t[0] for t in unans}
            state["alerted_task_ids"] = [t for t in state.get("alerted_task_ids", [])
                                         if t in still_open]
            new_unans = [t for t in unans if t[0] not in state["alerted_task_ids"]]
            if new_unans:
                if now - state.get("last_alert_at", 0) >= ALERT_COOLDOWN_S:
                    oldest = new_unans[0]
                    send_alert("WATCHDOG: Quinn has unanswered work — %d "
                               "task(s) to Quinn unanswered 30+ min. Oldest: "
                               "%s (%d min, from %s). Quinn may be down or "
                               "stuck; Hermes is answering on Ollama backup "
                               "meanwhile." % (len(unans), oldest[0], oldest[1],
                                               oldest[2]), mention_westin=True)
                    state["alerted_task_ids"].extend(t[0] for t in new_unans)
                    state["last_alert_at"] = now
                    state["work_alerted"] = True
                else:
                    log("unanswered work but inside 60-min alert cooldown")
            elif not unans and state.get("work_alerted"):
                send_alert("RECOVERY: Quinn's queue is clear — no tasks "
                           "unanswered 30+ min. Quinn is answering again.")
                state["work_alerted"] = False
    if not dry:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
