#!/bin/bash
# Send a one-time Telegram Web App secure-password button to the owner (DM only)
# for any bot in the fleet.
#
# Usage:
#   send-tg-secure.sh                       -> Quinn (default), Gmail password
#   send-tg-secure.sh gus                   -> Gus, default purpose
#   send-tg-secure.sh gus "Gus's ServiceTitan password"
#   send-tg-secure.sh copilot "Copilot's GitHub token"
#   DRY_RUN=1 send-tg-secure.sh gus "..."  -> print redacted payload, do not send
#
# Bots: quinn (quinn-muse), westin-jr, gus, dorian, dex, copilot.
# Each bot's Telegram token is read from its own token store; the Web App
# validates the submit against THAT bot's token, and only the owner
# (the owner's chat id) may submit. The password lands in
# /home/ubuntu/tg-secure/<bot>/password.txt (0600).
# Never prints a token or password value.
set -euo pipefail

BOT_RAW="${1:-quinn-muse}"
PURPOSE_ARG="${2:-}"
BASE_DIR="/home/ubuntu/tg-secure"
MCP_DIR="/home/ubuntu/a2a-v2/mcp"
CHAT_ID="${TG_SECURE_OWNER_CHAT_ID:-}"  # owner's Telegram chat id
BASE="${TG_SECURE_BASE_URL:-https://YOUR-VM-HOST:8443/tg-secure}"

# Canonical bot id + display name + default purpose.
case "$BOT_RAW" in
  quinn|quinn-muse) BOT="quinn-muse"; NAME="Quinn"; DEF_PURPOSE="Quinn's Gmail password"; TOKEN_FILE="/home/ubuntu/bots/quinn-muse/token.env"; TOKEN_KIND="env" ;;
  westin-jr)        BOT="westin-jr";  NAME="Westin Jr."; DEF_PURPOSE="Westin Jr.'s account password"; TOKEN_FILE="/home/ubuntu/bots/westin-jr/token.env"; TOKEN_KIND="env" ;;
  gus)              BOT="gus";        NAME="Gus";     DEF_PURPOSE="Gus's account password"; TOKEN_FILE="/home/ubuntu/bots/gus/token.env"; TOKEN_KIND="env" ;;
  dorian)           BOT="dorian";     NAME="Dorian";  DEF_PURPOSE="Dorian's account password"; TOKEN_FILE="/home/ubuntu/bots/dorian/token.env"; TOKEN_KIND="env" ;;
  dex)              BOT="dex";        NAME="Dex";     DEF_PURPOSE="Dex's account password"; TOKEN_FILE="/home/ubuntu/bots/dex/token.env"; TOKEN_KIND="env" ;;
  copilot)          BOT="copilot";    NAME="Copilot"; DEF_PURPOSE="Copilot's account password"; TOKEN_FILE="/home/ubuntu/.copilot/extensions/copilot-cli-telegram-bridge/bots.json"; TOKEN_KIND="botsjson" ;;
  *) echo "unknown bot: $BOT_RAW (use quinn, westin-jr, gus, dorian, dex, copilot)" >&2; exit 2 ;;
esac
PURPOSE="${PURPOSE_ARG:-$DEF_PURPOSE}"
if [ -z "$CHAT_ID" ]; then echo "set TG_SECURE_OWNER_CHAT_ID to the owner's Telegram chat id" >&2; exit 1; fi

# Create the one-time token (scoped to this bot + purpose) via the handler.
SETUP=$(python3 - "$BOT" "$PURPOSE" <<'PYEOF'
import sys
sys.path.insert(0, "/home/ubuntu/a2a-v2/mcp")
from tg_secure_form import new_token
tok = new_token(sys.argv[1], sys.argv[2])
if not tok:
    sys.exit(1)
print(tok)
PYEOF
)
if [ -z "$SETUP" ]; then echo "failed to create one-time token" >&2; exit 1; fi

# Read THIS bot's Telegram token (never printed).
if [ "$TOKEN_KIND" = "env" ]; then
  BOT_TOKEN=$(sed -n "s/^TELEGRAM_BOT_TOKEN=//p" "$TOKEN_FILE" | tr -d '"' | tr -d "'" | tr -d '[:space:]')
else
  BOT_TOKEN=$(python3 -c "import json; print(json.load(open('$TOKEN_FILE'))['copilot']['token'])")
fi
if [ -z "$BOT_TOKEN" ]; then echo "failed to read bot token for $BOT" >&2; exit 1; fi

URL_QUERY=$(python3 -c "import urllib.parse,sys; print(urllib.parse.urlencode({'setup':sys.argv[1],'bot':sys.argv[2],'purpose':sys.argv[3]}))" "$SETUP" "$BOT" "$PURPOSE")
URL="${BASE}?${URL_QUERY}"

TEXT="$NAME needs a password (one time, for: $PURPOSE). Tap the button below — it opens a secure form right inside Telegram. Nothing is typed in chat."

PAYLOAD=$(python3 - "$URL" "$TEXT" "$CHAT_ID" <<'PYEOF'
import json, sys
url, text, chat_id = sys.argv[1], sys.argv[2], sys.argv[3]
print(json.dumps({
    "chat_id": int(chat_id),
    "text": text,
    "reply_markup": {"inline_keyboard": [[{"text": "Enter password", "web_app": {"url": url}}]]}
}))
PYEOF
)

if [ "${DRY_RUN:-0}" = "1" ]; then
    echo "DRY RUN - would send to chat_id $CHAT_ID (bot: $BOT, purpose: $PURPOSE)"
    echo "web_app url host: ${BASE}?setup=<one-time-token>&bot=$BOT&purpose=<url-encoded>"
    echo "payload keys: $(echo "$PAYLOAD" | python3 -c "import json,sys; d=json.load(sys.stdin); print(sorted(d.keys())); print('keyboard:', d['reply_markup']['inline_keyboard'][0][0]['text'], '| web_app url present:', 'web_app' in d['reply_markup']['inline_keyboard'][0][0])")"
    echo "note: a one-time token was created for this dry run; it remains valid until used or removed from $BASE_DIR/tokens/"
    exit 0
fi

RESP=$(curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" -d "$PAYLOAD")
echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print('ok:', d.get('ok'), '| message_id:', d.get('result',{}).get('message_id'))"
