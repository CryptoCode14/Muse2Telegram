#!/usr/bin/env python3
"""
test_bridge.py — test the quinn-bridge with a real Hermes-style
OpenAI chat-completions request.

Run on the Oracle VM (the bridge listens on localhost:8769):
    python3 test_bridge.py
    python3 test_bridge.py --host 127.0.0.1 --port 8769 --timeout 180

What it checks, in order:
  1. GET  /health       -> {"status":"ok","brain":"quinn","fallback":"ollama"}
  2. GET  /v1/models    -> list contains a model with id "quinn"
  3. POST /v1/chat/completions (Hermes-style, model "quinn",
     stream false, messages with a system + user turn) ->
     OpenAI chat-completion JSON with choices[0].message.content.
     The user turn asks for exactly "HELLO QUINN" so the reply is
     unambiguous: if Quinn is the brain, the content is that text;
     if the bridge fell back to Ollama, the content will differ
     (and the bridge log will show "fallback ollama").

Exit code 0 = all checks passed (bridge up, quinn model listed,
chat completion returned a reply). Exit code 1 = a check failed;
the output says which one and why. A chat completion that returns
a reply is a PASS even if the reply is not exactly "HELLO QUINN"
— that just means the fallback answered, which is still a working
bridge; the script says so plainly instead of hiding it.

Stdlib only. No keys, tokens, or passwords. No money, orders,
or ServiceTitan. This touches the bridge on localhost only.
"""
import argparse
import json
import sys
import urllib.request
import urllib.error

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8769
DEFAULT_TIMEOUT = 660  # Option 2 (2026-11-14): test timeout must exceed the bridge 600s wait, or the test gives up before the bridge does. Ladder now: test 660s > bridge 600s > Quinn reply.


def get(base, path, timeout=30):
    req = urllib.request.Request(base + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode()


def post(base, path, obj, timeout=DEFAULT_TIMEOUT):
    data = json.dumps(obj).encode()
    req = urllib.request.Request(
        base + path, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help="HTTP timeout for the chat completion (seconds)")
    ap.add_argument("--prompt", default="Reply with exactly: HELLO QUINN",
                    help="user message to send to the bridge")
    args = ap.parse_args()
    base = f"http://{args.host}:{args.port}"
    failures = []

    # 1. /health
    print(f"[1/3] GET {base}/health")
    try:
        status, body = get(base, "/health")
        data = json.loads(body)
        fb = data.get("fallback")
        # v1 bridge: fallback == "ollama" (string). Dex v2: fallback is a
        # dict {"backend": "ollama", "model": ..., "base_url": ...}.
        fb_ok = fb == "ollama" or (isinstance(fb, dict) and fb.get("backend") == "ollama")
        ok = status == 200 and data.get("status") == "ok" \
            and data.get("brain") == "quinn" \
            and fb_ok
        print(f"      status={status} body={body.strip()}")
        print("      PASS" if ok else "      FAIL: unexpected /health body")
        if not ok:
            failures.append("health")
    except Exception as e:
        print(f"      FAIL: {e}")
        failures.append("health")

    # 2. /v1/models
    print(f"[2/3] GET {base}/v1/models")
    try:
        status, body = get(base, "/v1/models")
        data = json.loads(body)
        ids = [m.get("id") for m in data.get("data", [])]
        ok = status == 200 and "quinn" in ids
        print(f"      status={status} models={ids}")
        print("      PASS" if ok else "      FAIL: model 'quinn' not listed")
        if not ok:
            failures.append("models")
    except Exception as e:
        print(f"      FAIL: {e}")
        failures.append("models")

    # 3. POST /v1/chat/completions (Hermes-style)
    print(f"[3/3] POST {base}/v1/chat/completions (model=quinn, stream=false)")
    req_body = {
        "model": "quinn",
        "stream": False,
        "messages": [
            {"role": "system",
             "content": "You are Quinn, the Muse brain for Hermes. "
                        "Answer in plain text only."},
            {"role": "user", "content": args.prompt},
        ],
    }
    try:
        status, body = post(base, "/v1/chat/completions", req_body,
                            timeout=args.timeout)
        data = json.loads(body)
        choices = data.get("choices", [])
        content = ""
        if choices:
            content = (choices[0].get("message") or {}).get("content", "")
        print(f"      status={status} model={data.get('model')}")
        print(f"      reply: {content!r}")
        if status == 200 and content:
            if content.strip() == "HELLO QUINN":
                print("      PASS: Quinn answered with exactly HELLO QUINN")
            else:
                print("      PASS (with note): bridge returned a reply, but "
                      "not exactly HELLO QUINN — the Ollama fallback may "
                      "have answered. Check quinn-bridge.log for "
                      "'fallback ollama' vs 'quinn replied'.")
        else:
            print("      FAIL: no usable reply in the chat completion")
            failures.append("chat")
    except urllib.error.HTTPError as e:
        print(f"      FAIL: HTTP {e.code}: {e.read().decode()[:300]}")
        failures.append("chat")
    except Exception as e:
        print(f"      FAIL: {e}")
        failures.append("chat")

    print()
    if failures:
        print(f"RESULT: FAIL ({', '.join(failures)})")
        return 1
    print("RESULT: PASS — bridge is up, quinn model listed, "
          "chat completion returned a reply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
