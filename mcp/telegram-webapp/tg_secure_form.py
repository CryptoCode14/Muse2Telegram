"""Multi-bot one-time Telegram Web App secure password form, mounted on the
MCP server (port 8443).

Routes (public, guarded by a one-time token + Telegram initData validation):
  GET  /tg-secure?setup=<token>&bot=<bot>&purpose=<label>
       -> Telegram Web App HTML (password form) for that bot/purpose
  POST /tg-secure-submit
       -> validate setup token + Telegram initData against THAT bot's token
          (HMAC-SHA256, user must be the owner), save ONLY the password to that
          bot's file (0600), invalidate the token
  GET  /tg-secure-status?setup=<token> -> status JSON for that submission

Bots (bot id -> Telegram bot token source):
  quinn / quinn-muse -> /home/ubuntu/bots/quinn-muse/token.env
  westin-jr          -> /home/ubuntu/bots/westin-jr/token.env
  gus                -> /home/ubuntu/bots/gus/token.env
  dorian             -> /home/ubuntu/bots/dorian/token.env
  dex                -> /home/ubuntu/bots/dex/token.env
  copilot            -> /home/ubuntu/.copilot/extensions/copilot-cli-telegram-bridge/bots.json

One-time tokens are per-submission, scoped to one bot + one purpose:
  /home/ubuntu/tg-secure/tokens/<token>.json  (0600)
  {"bot": "<bot>", "purpose": "<label>", "created": "<iso>"}
Passwords land per-bot:
  /home/ubuntu/tg-secure/<bot>/password.txt   (0600, dir 0700)
  /home/ubuntu/tg-secure/<bot>/status.json    (0600)

Backward compat (Quinn): the original single global form-token
(/home/ubuntu/tg-secure/form-token) is still honoured for Quinn when no
per-submission token matches; it saves to BOTH the legacy
/home/ubuntu/tg-secure/password.txt and the per-bot quinn-muse file.

The password is held in memory only during the request and written once.
It is never logged and never returned in any response. Only the owner
(Telegram user id TG_SECURE_OWNER_ID) may submit, for every bot, enforced by
initData signature validation against that bot's own token.

initData validation (Telegram WebApp spec — do NOT regress):
  secret = HMAC-SHA256(key=b"WebAppData", msg=bot_token)
  calc   = HMAC-SHA256(key=secret, msg=data_check_string)
  data_check_string = "\\n".join(sorted "key=value" pairs, excluding hash)
  NOTE: SHA256(bot_token) as the secret is WRONG (that was the 2026-11-18
  "bad initData signature" bug); the secret is HMAC with key "WebAppData".
"""
import hashlib
import hmac
import html as html_lib
import json
import os
import secrets
import time
import urllib.parse

BASE_DIR = "/home/ubuntu/tg-secure"
TOKEN_DIR = os.path.join(BASE_DIR, "tokens")
LEGACY_TOKEN_FILE = os.path.join(BASE_DIR, "form-token")
LEGACY_PASSWORD_FILE = os.path.join(BASE_DIR, "password.txt")
LEGACY_STATUS_FILE = os.path.join(BASE_DIR, "status.json")
COPILOT_BOTS_JSON = "/home/ubuntu/.copilot/extensions/copilot-cli-telegram-bridge/bots.json"
# Owner's Telegram user id: set TG_SECURE_OWNER_ID in the environment.
ALLOWED_USER_ID = int(os.environ.get("TG_SECURE_OWNER_ID", "0"))
MAX_AUTH_AGE_S = 86400

# Bot id -> (token source kind, path). "env" = TELEGRAM_BOT_TOKEN= in a
# .env file; "botsjson" = {"copilot": {"token": ...}} JSON.
BOT_TOKEN_SOURCES = {
    "quinn-muse": ("env", "/home/ubuntu/bots/quinn-muse/token.env"),
    "westin-jr": ("env", "/home/ubuntu/bots/westin-jr/token.env"),
    "gus": ("env", "/home/ubuntu/bots/gus/token.env"),
    "dorian": ("env", "/home/ubuntu/bots/dorian/token.env"),
    "dex": ("env", "/home/ubuntu/bots/dex/token.env"),
    "copilot": ("botsjson", COPILOT_BOTS_JSON),
}
# Aliases accepted in the URL; canonicalised to the key above.
BOT_ALIASES = {
    "quinn": "quinn-muse",
    "quinn-muse": "quinn-muse",
    "westin-jr": "westin-jr",
    "gus": "gus",
    "dorian": "dorian",
    "dex": "dex",
    "copilot": "copilot",
}
# Default purpose label per bot when the sender does not give one.
DEFAULT_PURPOSES = {
    "quinn-muse": "Quinn's Gmail password",
    "westin-jr": "Westin Jr.'s account password",
    "gus": "Gus's account password",
    "dorian": "Dorian's account password",
    "dex": "Dex's account password",
    "copilot": "Copilot's account password",
}
LEGACY_QUINN_PURPOSE = "Quinn's Gmail password"


def canonical_bot(bot):
    if not bot:
        return None
    return BOT_ALIASES.get(str(bot).strip().lower())


def _bot_dir(bot):
    return os.path.join(BASE_DIR, bot)


def _password_file(bot):
    return os.path.join(_bot_dir(bot), "password.txt")


def _status_file(bot):
    return os.path.join(_bot_dir(bot), "status.json")


def new_token(bot, purpose):
    """Create a one-time token scoped to (bot, purpose). Returns the token
    string, or '' on failure. Used by the sender script (imported)."""
    bot = canonical_bot(bot)
    if not bot:
        return ""
    purpose = (purpose or DEFAULT_PURPOSES.get(bot, "Account password")).strip()
    token = secrets.token_hex(16)
    try:
        os.makedirs(TOKEN_DIR, exist_ok=True)
        os.chmod(TOKEN_DIR, 0o700)
        rec = {"bot": bot, "purpose": purpose,
               "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        path = os.path.join(TOKEN_DIR, token + ".json")
        with open(path, "w") as f:
            f.write(json.dumps(rec) + "\n")
        os.chmod(path, 0o600)
        return token
    except Exception:
        return ""


def _token_record(setup):
    """Look up a one-time token. Returns (bot, purpose, legacy) where
    legacy=True means the old global Quinn form-token matched."""
    if not setup:
        return None, None, False
    setup = setup.strip()
    if "/" in setup or "\\" in setup or setup in (".", ".."):
        return None, None, False
    try:
        with open(os.path.join(TOKEN_DIR, setup + ".json"), "r") as f:
            rec = json.loads(f.read())
        bot = canonical_bot(rec.get("bot", ""))
        purpose = (rec.get("purpose") or "").strip()
        if bot and purpose:
            return bot, purpose, False
    except Exception:
        pass
    # Legacy fallback: the old global Quinn form-token.
    try:
        with open(LEGACY_TOKEN_FILE, "r") as f:
            legacy_token = f.read().strip()
        if legacy_token and legacy_token == setup:
            return "quinn-muse", LEGACY_QUINN_PURPOSE, True
    except Exception:
        pass
    return None, None, False


def _read_bot_token(bot):
    """Read the Telegram bot token for a bot id. Returns '' if unknown."""
    bot = canonical_bot(bot)
    if not bot:
        return ""
    kind, path = BOT_TOKEN_SOURCES[bot]
    try:
        if kind == "env":
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("TELEGRAM_BOT_TOKEN="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        else:  # botsjson (copilot)
            with open(path, "r") as f:
                data = json.loads(f.read())
            return (data.get("copilot", {}) or {}).get("token", "") or ""
    except Exception:
        pass
    return ""


def _write_status(bot, obj):
    try:
        os.makedirs(_bot_dir(bot), exist_ok=True)
        os.chmod(_bot_dir(bot), 0o700)
        with open(_status_file(bot), "w") as f:
            f.write(json.dumps(obj, indent=2) + "\n")
        os.chmod(_status_file(bot), 0o600)
    except Exception:
        pass


def _read_status(bot):
    try:
        with open(_status_file(bot), "r") as f:
            return json.loads(f.read())
    except Exception:
        return {"status": "waiting"}


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _validate_init_data(init_data, bot_token):
    """Validate Telegram Web App initData. Returns (ok, user_id, error)."""
    if not init_data or not bot_token:
        return False, None, "missing initData"
    try:
        params = urllib.parse.parse_qs(init_data, keep_blank_values=True)
    except Exception:
        return False, None, "bad initData"
    recv_hash = params.get("hash", [""])[0]
    if not recv_hash:
        return False, None, "missing hash"
    pairs = []
    for key in sorted(params.keys()):
        if key == "hash":
            continue
        pairs.append("%s=%s" % (key, params[key][0]))
    data_check_string = "\n".join(pairs)
    # Telegram WebApp spec: secret = HMAC-SHA256(key="WebAppData", msg=token).
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv_hash):
        return False, None, "bad initData signature"
    try:
        auth_date = int(params.get("auth_date", ["0"])[0])
    except Exception:
        return False, None, "bad auth_date"
    if abs(int(time.time()) - auth_date) > MAX_AUTH_AGE_S:
        return False, None, "initData expired"
    try:
        user = json.loads(params.get("user", ["{}"])[0])
        user_id = int(user.get("id", 0))
    except Exception:
        return False, None, "bad user"
    if user_id != ALLOWED_USER_ID:
        return False, None, "wrong user"
    return True, user_id, ""


def _form_html(bot, purpose, token):
    bot_esc = html_lib.escape(bot)
    purpose_esc = html_lib.escape(purpose)
    return """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Secure password &mdash; __BOT__</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
body{font-family:sans-serif;max-width:520px;margin:32px auto;padding:0 16px;
  background:var(--tg-theme-bg-color,#fff);color:var(--tg-theme-text-color,#222)}
input{width:100%;padding:12px;font-size:16px;margin:8px 0;box-sizing:border-box;
  background:var(--tg-theme-secondary-bg-color,#f4f4f4);color:var(--tg-theme-text-color,#222);
  border:1px solid var(--tg-theme-hint-color,#999);border-radius:6px}
button{padding:12px 20px;font-size:16px;background:var(--tg-theme-button-color,#0b6efa);
  color:var(--tg-theme-button-text-color,#fff);border:0;border-radius:6px;width:100%}
.note{color:var(--tg-theme-hint-color,#555);font-size:14px}
#msg{margin-top:12px;font-size:15px}
</style></head>
<body>
<h2>Secure password &mdash; __BOT__</h2>
<p class="note">Enter the password for __PURPOSE__.
It is sent once over HTTPS to the server on the Oracle VM and saved
there only. It is never shown to anyone and never leaves the VM.</p>
<label for="pw">__PURPOSE__</label>
<input id="pw" type="password" autocomplete="current-password" required>
<button id="go" type="button">Submit securely</button>
<p id="msg" class="note"></p>
<script>
(function(){
  var tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { try { tg.ready(); tg.expand(); } catch (e) {} }
  var msg = document.getElementById('msg');
  document.getElementById('go').addEventListener('click', function(){
    var pw = document.getElementById('pw').value;
    if (!pw) { msg.textContent = 'Please enter the password.'; return; }
    msg.textContent = 'Sending...';
    var initData = (tg && tg.initData) ? tg.initData : '';
    fetch('/tg-secure-submit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({setup_token: '__TOKEN__', bot: '__BOT__', password: pw, initData: initData})
    }).then(function(r){ return r.json().then(function(j){ return {ok: r.ok, j: j}; }); })
    .then(function(res){
      if (res.ok && res.j && res.j.status === 'ok') {
        msg.textContent = 'Saved securely. You can close this.';
        document.getElementById('pw').value = '';
        if (tg) { setTimeout(function(){ try { tg.close(); } catch (e) {} }, 800); }
      } else {
        msg.textContent = 'Failed: ' + ((res.j && res.j.error) || ('HTTP ' + res.status));
      }
    }).catch(function(e){ msg.textContent = 'Network error: ' + e; });
  });
})();
</script>
</body></html>""".replace("__BOT__", bot_esc).replace("__PURPOSE__", purpose_esc).replace("__TOKEN__", token)


async def _send(send, status, body, ctype="text/html"):
    if isinstance(body, str):
        body = body.encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", ctype.encode()),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def _read_body(receive):
    raw = b""
    while True:
        msg = await receive()
        if msg["type"] != "http.request":
            break
        raw += msg.get("body", b"")
        if not msg.get("more_body", False):
            break
    return raw


def _any_token_exists():
    try:
        if os.path.exists(LEGACY_TOKEN_FILE):
            with open(LEGACY_TOKEN_FILE, "r") as f:
                if f.read().strip():
                    return True
    except Exception:
        pass
    try:
        return bool(os.listdir(TOKEN_DIR))
    except Exception:
        return False


def _invalidate_token(setup, legacy):
    try:
        if legacy:
            os.remove(LEGACY_TOKEN_FILE)
        else:
            os.remove(os.path.join(TOKEN_DIR, setup.strip() + ".json"))
    except Exception:
        pass


async def handle_tg_secure(scope, receive, send):
    """Return True if this request was handled (caller should return)."""
    path = scope.get("path", "")
    method = scope.get("method", "")
    qs = urllib.parse.parse_qs(scope.get("query_string", b"").decode(errors="replace"))
    if not _any_token_exists():
        return False

    if method == "GET" and path == "/tg-secure":
        setup = qs.get("setup", [""])[0]
        bot, purpose, legacy = _token_record(setup)
        if not bot:
            await _send(send, 403, "<p>Invalid or expired link.</p>")
            return True
        html = _form_html(bot, purpose, setup)
        await _send(send, 200, html)
        return True

    if method == "GET" and path == "/tg-secure-status":
        setup = qs.get("setup", [""])[0]
        bot, purpose, legacy = _token_record(setup)
        if not bot:
            await _send(send, 403, json.dumps({"status": "forbidden"}), "application/json")
            return True
        if legacy:
            try:
                with open(LEGACY_STATUS_FILE, "r") as f:
                    status = json.loads(f.read())
            except Exception:
                status = {"status": "waiting"}
        else:
            status = _read_status(bot)
        await _send(send, 200, json.dumps(status), "application/json")
        return True

    if method == "POST" and path == "/tg-secure-submit":
        raw = await _read_body(receive)
        data = {}
        ctype = ""
        for k, v in scope.get("headers", []):
            if k.decode(errors="replace").lower() == "content-type":
                ctype = v.decode(errors="replace")
        try:
            if "application/json" in ctype:
                data = json.loads(raw.decode(errors="replace") or "{}")
            else:
                form = urllib.parse.parse_qs(raw.decode(errors="replace"))
                data = {k: v[0] for k, v in form.items()}
        except Exception:
            await _send(send, 400, json.dumps({"status": "error", "error": "bad request"}), "application/json")
            return True
        setup = data.get("setup_token", "")
        # Authoritative bot/purpose come from the token record, never from
        # the client-sent bot field.
        bot, purpose, legacy = _token_record(setup)
        if not bot:
            await _send(send, 403, json.dumps({"status": "error", "error": "invalid or expired link"}), "application/json")
            return True
        password = data.get("password", "")
        if not password:
            await _send(send, 400, json.dumps({"status": "error", "error": "no password received"}), "application/json")
            return True
        ok, user_id, err = _validate_init_data(data.get("initData", ""), _read_bot_token(bot))
        if not ok:
            await _send(send, 403, json.dumps({"status": "error", "error": err}), "application/json")
            return True
        try:
            os.makedirs(_bot_dir(bot), exist_ok=True)
            os.chmod(_bot_dir(bot), 0o700)
            with open(_password_file(bot), "w") as f:
                f.write(password + "\n")
            os.chmod(_password_file(bot), 0o600)
            # Quinn backward compat: also keep the legacy password path.
            if bot == "quinn-muse":
                with open(LEGACY_PASSWORD_FILE, "w") as f:
                    f.write(password + "\n")
                os.chmod(LEGACY_PASSWORD_FILE, 0o600)
        except Exception:
            await _send(send, 500, json.dumps({"status": "error", "error": "save failed"}), "application/json")
            return True
        # Drop the password reference immediately.
        password = None
        data = None
        # Invalidate the one-time token.
        _invalidate_token(setup, legacy)
        status_obj = {"status": "done", "at": _now(), "user_id": user_id,
                      "bot": bot, "purpose": purpose}
        _write_status(bot, status_obj)
        if bot == "quinn-muse":
            try:
                with open(LEGACY_STATUS_FILE, "w") as f:
                    f.write(json.dumps(status_obj, indent=2) + "\n")
                os.chmod(LEGACY_STATUS_FILE, 0o600)
            except Exception:
                pass
        await _send(send, 200, json.dumps({"status": "ok"}), "application/json")
        return True

    return False
