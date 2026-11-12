"""Offline tests for the v2 unified prompt + pre-hydration + tier removal.

Covers (v2, see prompts/unified.md, hooks/telegram-bridge-decide.py,
hooks/telegram-bridge-intake.sh):
- The unified prompt is one static text, smaller (modelled tokens,
  chars/4) than the old standard prompt, and carries every rule that
  must not be lost: group reply rules (short form), the @mention rule,
  group modes, the allowlist rule, --reply as the single send path,
  silence via --done, ack-first for 10+ minute work, unknown-send
  handling, commands, gated voice transcription, and media.
- The hook-definition install file embeds the unified prompt verbatim.
- The wake payload gains "context_md" (bounded to 6000 chars, with a
  truncation marker; null when context.md is missing/unreadable) and
  carries NO "tier" field any more.
- The decider prints only "wake" / "hold" / "done <ids>" / "empty"
  with no tier word, whatever the batch (a greeting that was "quick"
  and a long task that was "deep" both print bare "wake").
- Typing fires pre-wake only for DM / command / @mention, never for
  an open group message (decision function + the intake.sh wiring).

No network or credentials are used. The Telegram sender is mocked
where a send is involved; the intake.sh shell helper is exercised by
extracting it verbatim from the script.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve()
RUNTIME = HERE.parent.parent  # muse-runtime/

PROMPT = RUNTIME / "prompts" / "unified.md"
OLD_STANDARD = RUNTIME / "prompts" / "tier-standard.md"
HOOK_DEFINITION = RUNTIME / "hooks" / "hook-definition-unified-prompt.md"
DECIDER = RUNTIME / "hooks" / "telegram-bridge-decide.py"
INTAKE = RUNTIME / "hooks" / "telegram-bridge-intake.sh"

BOT_USERNAME = "ExampleBot"


def _modelled_tokens(text):
    return round(len(text) / 4)


def load_decider():
    # The decider refuses to import without its bot username; set
    # the fixture name first so the module loads.
    os.environ["TELEGRAM_BRIDGE_BOT_USERNAME"] = BOT_USERNAME
    spec = importlib.util.spec_from_file_location(
        "telegram_bridge_decide_v2test", DECIDER
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_decider(payload, paused=False, aliases=None):
    """Run the decider as a script (its real entry point) and return
    its stdout stripped."""
    env = dict(os.environ)
    env["TELEGRAM_BRIDGE_BOT_USERNAME"] = BOT_USERNAME
    if aliases is None:
        env.pop("TELEGRAM_BRIDGE_BOT_ALIASES", None)
    else:
        env["TELEGRAM_BRIDGE_BOT_ALIASES"] = aliases
    # Paused is a file's EXISTENCE, so the path must not exist at
    # all when the test is not paused.
    tmpdir = tempfile.mkdtemp()
    paused_path = os.path.join(tmpdir, "PAUSED")
    if paused:
        Path(paused_path).write_text("paused")
    env["TELEGRAM_BRIDGE_PAUSED_PATH"] = paused_path
    try:
        proc = subprocess.run(
            [sys.executable, str(DECIDER)],
            input=json.dumps(payload),
            capture_output=True, text=True, env=env, timeout=30,
        )
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


class UnifiedPromptTests(unittest.TestCase):
    def test_unified_prompt_is_smaller_than_old_standard(self):
        text = PROMPT.read_text()
        old = OLD_STANDARD.read_text()
        tokens = _modelled_tokens(text)
        old_tokens = _modelled_tokens(old)
        # Real measured number, in the failure message too: the
        # unified prompt must be smaller than the old standard
        # prompt it replaces (13306 chars / ~3300 modelled tokens).
        self.assertLess(
            tokens, old_tokens,
            f"unified prompt is {len(text)} chars / ~{tokens} modelled "
            f"tokens; old standard is {len(old)} chars / ~{old_tokens}",
        )
        # The header comment states the same measured size.
        m = re.search(r"Size: (\d+) chars, ~(\d+) modelled tokens",
                      text)
        self.assertIsNotNone(m, "header comment must state the size")
        self.assertEqual(int(m.group(1)), len(text))
        self.assertEqual(int(m.group(2)), tokens)

    def test_unified_prompt_has_no_tier_word(self):
        # There is no per-batch prompt selection any more, so the
        # word must not appear anywhere in the prompt itself.
        self.assertNotIn("tier", PROMPT.read_text().lower())

    def test_unified_prompt_carries_collab_gate(self):
        # Collab protocol, gated, never the default: in a trusted
        # multi-bot group where the telegram-collab-protocol skill
        # is installed, the prompt points at COLLAB_PROTOCOL.md
        # instead of the silence rules; everywhere else the
        # silence rules below apply.
        text = PROMPT.read_text()
        for phrase in (
            "Collab protocol (trusted multi-bot groups only)",
            "group_mode_effective is trusted",
            "telegram-collab-protocol skill",
            "COLLAB_PROTOCOL.md",
            "instead of the silence rules below",
            "one owner per task",
            "An ack is still terminal",
        ):
            self.assertIn(phrase, text)

    def test_unified_prompt_carries_chat_rules_short_version(self):
        text = PROMPT.read_text()
        for phrase in (
            "Default is silence",
            "stay out entirely",
            "Acknowledge once, then stop",
            "Never acknowledge an acknowledgment",
            "An ack is terminal",
            "Reaction instead of text",
            "CHAT_RULES.md wins",
        ):
            self.assertIn(phrase, text)

    def test_unified_prompt_carries_mention_rule(self):
        text = PROMPT.read_text()
        for phrase in (
            "carries that bot's @mention",
            "exactly as it appears in the group buffer",
            "carries no @mention",
            "Once a question is answered",
            "unless a new question comes up",
            "or a configured alias of it",
        ):
            self.assertIn(phrase, text)

    def test_unified_prompt_carries_mode_and_allowlist_rules(self):
        text = PROMPT.read_text()
        for phrase in (
            "group_mode_effective",
            "TRUSTED",
            "GUARDED",
            "No fishing",
            "/trust",
            "group_mode_notice",
            "group_allowlisted",
            "ALLOWLISTED",
            "private channel",
        ):
            self.assertIn(phrase, text)

    def test_unified_prompt_carries_send_rules(self):
        text = PROMPT.read_text()
        for phrase in (
            "--reply CLAIM_TOKEN",
            "NEVER paste reply text into a shell command",
            "--heartbeat CLAIM_TOKEN",
            "A claim alone does not start the turn",
            "Do NOT call --claim-more or --send-and-close",
            "do NOT call --reply at all; close with --done and a reason instead",
            "--done UPDATE_ID CLAIM_TOKEN REASON",
            "stage stale-token",
            "10 minutes or more",
            "Got it, looking into this, will reply when ready.",
            "stage unknown",
            "do NOT resend blindly",
            "--send-failed",
            "--keyboard ONLY when the reply answers a command",
            "1 to 4096 characters",
            "No em dashes",
        ):
            self.assertIn(phrase, text)

    def test_unified_prompt_carries_commands_voice_media(self):
        text = PROMPT.read_text()
        for phrase in (
            "/pause", "/resume", "/status", "/help", "/who", "/active",
            "/menu", "context_md", "6000",
            "[voice message", "whisper-venv", "whisper-tiny",
            "do NOT fail the turn",
            "[photo]", "[document",
        ):
            self.assertIn(phrase, text)

    def test_hook_definition_embeds_unified_prompt_verbatim(self):
        doc = HOOK_DEFINITION.read_text()
        prompt = PROMPT.read_text()
        self.assertIn(prompt, doc)
        self.assertIn("hook-definition-unified-prompt.md", doc)

    def test_work_from_line_names_all_three_env_vars(self):
        # Gus's 7e8121d gap: the prompt made TELEGRAM_BRIDGE_BOT_USERNAME
        # required (fail loud when unset) but the Work-from line named
        # only STATE_PATH and SEND_CMD, so a fresh install following the
        # template broke on --reply. The line must name all three vars,
        # in both prompt files.
        for path in (PROMPT, HOOK_DEFINITION):
            lines = [ln for ln in path.read_text().splitlines()
                     if ln.startswith("Work from")]
            self.assertEqual(len(lines), 1, f"{path}: one Work-from line")
            for var in ("TELEGRAM_BRIDGE_STATE_PATH",
                        "TELEGRAM_BRIDGE_SEND_CMD",
                        "TELEGRAM_BRIDGE_BOT_USERNAME"):
                self.assertIn(var, lines[0], f"{path}: {var} missing")


class WatchdogDefinitionTests(unittest.TestCase):
    def test_watchdog_json_is_a_reference_not_a_drop_in(self):
        # The shipped watchdog JSON holds the hook's settings, but
        # no runtime-stamped fields: a verbatim copy does not parse
        # (created_at_ms is missing), so SETUP must tell the
        # installer to create the hook through the hook tool.
        path = RUNTIME / "hooks" / "telegram-bridge-watchdog.json"
        data = json.loads(path.read_text())
        self.assertEqual(data["id"], "telegram-bridge-watchdog")
        # 60 s poll since the intake-liveness fix: the watchdog is
        # the detector now, so it has to check often enough to
        # catch a stuck intake within ~2 minutes of its last poll.
        self.assertEqual(data["poll_interval_secs"], 60)
        self.assertEqual(
            data["script_path"],
            "~/hooks/scripts/telegram-bridge-watchdog.sh",
        )
        self.assertIn("prompt", data)
        self.assertNotIn("created_at_ms", data)
        self.assertNotIn("updated_at_ms", data)
        setup = (RUNTIME / "SETUP.md").read_text()
        self.assertIn("do not copy the JSON file verbatim", setup)


class ContextHydrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tmp.name, "state.json")
        Path(self.state_path).write_text("{}")
        self.decider = load_decider()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_context(self, text):
        Path(self.tmp.name, "context.md").write_text(text)

    def test_short_context_is_passed_through(self):
        self._write_context("hello context")
        out = self.decider.hydrate_payload(
            {"claim_token": "t", "items": []}, state_path=self.state_path)
        self.assertEqual(out["context_md"], "hello context")
        self.assertEqual(out["claim_token"], "t")

    def test_long_context_is_bounded_to_6000_with_marker(self):
        self._write_context("x" * 7000)
        out = self.decider.hydrate_payload(
            {"items": []}, state_path=self.state_path)
        self.assertEqual(len(out["context_md"]), 6000)
        self.assertTrue(out["context_md"].endswith("[context truncated]"))
        self.assertTrue(out["context_md"].startswith("x" * 100))

    def test_exactly_6000_chars_is_not_truncated(self):
        self._write_context("y" * 6000)
        out = self.decider.hydrate_payload(
            {"items": []}, state_path=self.state_path)
        self.assertEqual(out["context_md"], "y" * 6000)

    def test_missing_context_file_is_null_not_a_failure(self):
        out = self.decider.hydrate_payload(
            {"items": []}, state_path=self.state_path)
        self.assertIsNone(out["context_md"])

    def test_unreadable_context_file_is_null_not_a_failure(self):
        Path(self.tmp.name, "context.md").mkdir()
        out = self.decider.hydrate_payload(
            {"items": []}, state_path=self.state_path)
        self.assertIsNone(out["context_md"])

    def test_hydrated_payload_has_no_tier(self):
        self._write_context("ctx")
        out = self.decider.hydrate_payload(
            {"tier": "standard", "items": []},
            state_path=self.state_path)
        self.assertNotIn("tier", out)
        self.assertEqual(out["context_md"], "ctx")

    def test_hydrate_does_not_mutate_the_input(self):
        self._write_context("ctx")
        payload = {"tier": "quick", "items": [{"update_id": 1}]}
        self.decider.hydrate_payload(payload, state_path=self.state_path)
        self.assertEqual(payload["tier"], "quick")
        self.assertNotIn("context_md", payload)

    def test_decider_module_has_no_tier_api(self):
        self.assertFalse(hasattr(self.decider, "pick_tier"))
        self.assertFalse(hasattr(self.decider, "_tier_for_item"))
        self.assertFalse(hasattr(self.decider, "TIER_QUICK"))
        self.assertFalse(hasattr(self.decider, "TIER_STANDARD"))
        self.assertFalse(hasattr(self.decider, "TIER_DEEP"))


class DeciderOutputTests(unittest.TestCase):
    def _item(self, update_id, text, chat_type="private", **kw):
        item = {
            "update_id": update_id,
            "chat_id": 111 if chat_type == "private" else -777,
            "chat_type": chat_type,
            "sender_id": 111,
            "text": text,
        }
        item.update(kw)
        return item

    def test_greeting_dm_prints_bare_wake(self):
        # Was "wake quick": a standalone greeting is the simplest
        # batch, and the output must carry no tier word.
        out = run_decider({"items": [self._item(1, "hey")]})
        self.assertEqual(out, "wake")

    def test_long_task_prints_bare_wake(self):
        # Was "wake deep": a long lookup message.
        out = run_decider(
            {"items": [self._item(1, "please look up " + "x" * 600)]})
        self.assertEqual(out, "wake")

    def test_command_prints_bare_wake(self):
        out = run_decider(
            {"items": [self._item(1, "/who", is_owner=True)]})
        self.assertEqual(out, "wake")

    def test_open_group_message_prints_bare_wake(self):
        out = run_decider({"items": [
            self._item(1, "hello everyone", chat_type="group",
                       sender_id=222)]})
        self.assertEqual(out, "wake")

    def test_paused_dm_prints_bare_hold(self):
        out = run_decider({"items": [self._item(1, "hey")]}, paused=True)
        self.assertEqual(out, "hold")

    def test_paused_command_prints_bare_wake(self):
        out = run_decider(
            {"items": [self._item(1, "/resume", is_owner=True)]},
            paused=True)
        self.assertEqual(out, "wake")

    def test_at_alias_wakes(self):
        # "@Example" is a configured alias of the test bot
        # (ExampleBot): it wakes just like @ExampleBot.
        out = run_decider({"items": [
            self._item(1, "@Example check this", chat_type="group",
                       sender_id=222)]}, aliases="Example")
        self.assertEqual(out, "wake")

    def test_at_unaliased_short_name_still_closes(self):
        # No aliases configured: "@Example" mentions someone else,
        # so the decider closes it with no wake.
        out = run_decider({"items": [
            self._item(1, "@Example check this", chat_type="group",
                       sender_id=222)]})
        self.assertEqual(out, "done 1")

    def test_bot_message_without_mention_prints_done(self):
        out = run_decider({"items": [
            self._item(1, "beep boop", chat_type="group", sender_id=333,
                       is_bot=True, sender_name="@SomeOtherBot")]})
        self.assertEqual(out, "done 1")

    def test_empty_and_bad_payloads_print_empty(self):
        self.assertEqual(run_decider({"items": []}), "empty")
        self.assertEqual(run_decider({}), "empty")

    def test_no_output_contains_tier(self):
        for payload in (
            {"items": [self._item(1, "hey")]},
            {"items": [self._item(1, "please look up " + "x" * 600)]},
            {"items": [self._item(1, "/who", is_owner=True)]},
            {"items": [self._item(1, "hey")]},
        ):
            out = run_decider(payload)
            self.assertNotIn("tier", out.lower(), out)

    def test_decider_refuses_to_run_without_bot_username(self):
        # No default any more: without TELEGRAM_BRIDGE_BOT_USERNAME
        # the decider must fail loudly instead of silently answering
        # as some other bot.
        env = dict(os.environ)
        env.pop("TELEGRAM_BRIDGE_BOT_USERNAME", None)
        tmpdir = tempfile.mkdtemp()
        env["TELEGRAM_BRIDGE_PAUSED_PATH"] = os.path.join(tmpdir, "PAUSED")
        try:
            proc = subprocess.run(
                [sys.executable, str(DECIDER)],
                input=json.dumps({"items": [self._item(1, "hey")]}),
                capture_output=True, text=True, env=env, timeout=30,
            )
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("TELEGRAM_BRIDGE_BOT_USERNAME", proc.stderr)


class TypingPreWakeTests(unittest.TestCase):
    def setUp(self):
        self.decider = load_decider()

    def _payload(self, *items):
        return {"items": list(items)}

    def _item(self, chat_id, chat_type, text, **kw):
        item = {"update_id": 1, "chat_id": chat_id,
                "chat_type": chat_type, "sender_id": 111, "text": text}
        item.update(kw)
        return item

    def test_dm_fires_typing(self):
        out = self.decider.typing_chat_id(self._payload(
            self._item(111, "private", "hey")))
        self.assertEqual(out, 111)

    def test_command_fires_typing(self):
        out = self.decider.typing_chat_id(self._payload(
            self._item(-777, "group", "/who", is_owner=True)))
        self.assertEqual(out, -777)

    def test_mention_fires_typing(self):
        out = self.decider.typing_chat_id(self._payload(
            self._item(-777, "group", "@ExampleBot what do you think?")),
            bot_username=BOT_USERNAME)
        self.assertEqual(out, -777)

    def test_open_group_message_does_not_fire_typing(self):
        out = self.decider.typing_chat_id(self._payload(
            self._item(-777, "group", "hello everyone", sender_id=222)),
            bot_username=BOT_USERNAME)
        self.assertIsNone(out)

    def test_other_bot_message_does_not_fire_typing(self):
        out = self.decider.typing_chat_id(self._payload(
            self._item(-777, "group", "beep boop", sender_id=333,
                       is_bot=True, sender_name="@SomeOtherBot")),
            bot_username=BOT_USERNAME)
        self.assertIsNone(out)

    def test_alias_fires_typing(self):
        # "@Example" is a configured alias here: typing fires for it
        # just like for @ExampleBot.
        with mock.patch.dict(os.environ,
                             {"TELEGRAM_BRIDGE_BOT_ALIASES": "Example"}):
            decider = load_decider()
            out = decider.typing_chat_id(self._payload(
                self._item(-777, "group", "@Example what do you think?",
                           sender_id=222)),
                bot_username=BOT_USERNAME)
            self.assertEqual(out, -777)

    def test_mention_of_another_bot_does_not_fire_typing(self):
        out = self.decider.typing_chat_id(self._payload(
            self._item(-777, "group", "@SomeOtherBot check this out",
                       sender_id=222)),
            bot_username=BOT_USERNAME)
        self.assertIsNone(out)

    def test_first_definite_item_wins(self):
        out = self.decider.typing_chat_id(self._payload(
            self._item(-777, "group", "hello everyone", sender_id=222),
            self._item(111, "private", "hey")),
            bot_username=BOT_USERNAME)
        self.assertEqual(out, 111)

    def test_intake_shell_wiring(self):
        # The shell side: no tier injection any more, hydration in
        # the wake branch, and the --typing call guarded so it only
        # runs when typing_chat_id returned a chat.
        text = INTAKE.read_text()
        self.assertNotIn("inject_tier", text)
        self.assertNotIn('tier="standard"', text)
        self.assertIn("hydrate_and_typing", text)
        self.assertIn("hydrate_payload", text)
        self.assertIn("typing_chat_id", text)
        self.assertIn(
            '"$TELEGRAM_BRIDGE_SEND_CMD" --typing "$typing_chat"', text)
        self.assertIn('if [ -n "$typing_chat" ]; then', text)
        # The hold branch stores the payload unhydrated (the resume
        # turn reads context.md itself).
        hold = text.split("hold)")[1].split("done)")[0]
        self.assertNotIn("hydrate", hold)

    def test_intake_shell_helper_runs_verbatim(self):
        # Extract hydrate_and_typing() verbatim from intake.sh and
        # run it: a DM payload hydrates and yields its chat id; an
        # open group payload hydrates and yields an empty typing line.
        text = INTAKE.read_text()
        start = text.index("hydrate_and_typing() {")
        end = text.index("\n}\n", start) + len("\n}\n")
        func = text[start:end]
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            Path(state_path).write_text("{}")
            Path(tmp, "context.md").write_text("test context")
            func_path = os.path.join(tmp, "func.sh")
            Path(func_path).write_text(func)
            script = (
                f'DECIDER={DECIDER}\n'
                f'export TELEGRAM_BRIDGE_STATE_PATH={state_path}\n'
                f'export TELEGRAM_BRIDGE_BOT_USERNAME={BOT_USERNAME}\n'
                f'source {func_path}\n'
            )
            for payload, expected_typing in (
                ('{"claim_token":"t","items":[{"update_id":1,'
                 '"chat_id":111,"chat_type":"private","sender_id":111,'
                 '"text":"hey"}]}', "111"),
                ('{"claim_token":"t","items":[{"update_id":2,'
                 '"chat_id":-777,"chat_type":"group","sender_id":222,'
                 '"text":"hello all"}]}', ""),
            ):
                full = script + f"printf '%s' '{payload}' | hydrate_and_typing\n"
                proc = subprocess.run(
                    ["bash", "-c", full],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                lines = proc.stdout.splitlines()
                self.assertEqual(len(lines), 2, proc.stdout)
                hydrated = json.loads(lines[0])
                self.assertEqual(hydrated["context_md"], "test context")
                self.assertNotIn("tier", hydrated)
                self.assertEqual(lines[1], expected_typing)


if __name__ == "__main__":
    unittest.main()
