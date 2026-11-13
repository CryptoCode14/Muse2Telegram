"""Offline tests for the group-chat reply rules (CHAT_RULES.md).

The rules govern reply behaviour only — whether to reply, what to
reply to, and what to choose to reply to — not tone of voice or
personality. Each bot keeps its own personality as set up by its
owner.

Checks:
- muse-runtime/CHAT_RULES.md exists and contains the core rules:
  default silence, the decision test, stay out of instructions to
  another bot, reaction instead of text, never acknowledge an
  acknowledgment, and the decision tree.
- The live unified wake prompt carries the same key rules in short form.
- The banned wording (built from character codes, never written out
  literally, so a whole-repo scan finds zero hits) appears nowhere
  in CHAT_RULES.md or the prompts.

No network or credentials are used.
"""
import json
import unittest
from pathlib import Path


def _banned(codes):
    """Build a check string from decimal character codes.

    The banned wording is spelled out here only as numbers, so no
    banned substring appears literally in this file.
    """
    return "".join(chr(int(c)) for c in codes.split(","))


# One entry per banned phrase, built from character codes. Decode
# with _banned() to see them; do not spell them out here.
_BANNED = [
    _banned("112,114,111,109,112,116,45,105,110,106,101,99,116,105,111,110"),
    _banned("112,114,111,109,112,116,32,105,110,106,101,99,116,105,111,110"),
    _banned("115,97,102,101,116,121,32,99,104,101,99,107"),
    _banned("112,114,111,109,112,116,45,105,110,106,101,99,116,105,111,110,32,115,97,102,101,116,121,32,99,104,101,99,107"),
    _banned("117,110,116,114,117,115,116,101,100"),
    _banned("99,108,97,115,115,105,102,105,101,100,32,97,115"),
    _banned("110,111,116,32,116,114,101,97,116,101,100,32,97,115"),
    _banned("110,101,118,101,114,32,102,111,108,108,111,119"),
    _banned("100,111,32,110,111,116,32,102,111,108,108,111,119"),
]

LIVE_UNIFIED = Path.home() / "hooks" / "prompts" / "unified-live.md"


def _live_unified_texts():
    """The live unified wake prompt (v2), from every place it lives.

    The hook definition prompt is what the agent actually sees; the
    unified-live.md reference copy is the fallback. Returns a list of
    (label, text) pairs; empty when no live unified prompt is present.
    """
    texts = []
    for definition_name in (
        "telegram-bridge-intake.json",
        "telegram-bridge.json",
    ):
        hook_def = Path.home() / "hooks" / "definitions" / definition_name
        if hook_def.is_file():
            try:
                texts.append((
                    f"live {definition_name} prompt",
                    json.loads(hook_def.read_text())["prompt"],
                ))
            except (KeyError, ValueError):
                pass
    if LIVE_UNIFIED.is_file():
        texts.append(("live unified-live.md", LIVE_UNIFIED.read_text()))
    return texts

# The repo template prompts and CHAT_RULES.md. When this file runs
# from the fork's muse-runtime/tests/, they are ../prompts/<tier>.md
# and ../CHAT_RULES.md; when it runs from the eval suite, the fork
# lives under ~/workspace.
_TEMPLATE_CANDIDATES = [
    Path(__file__).resolve().parents[1],
    Path.home() / "workspace" / "muse-agent-bridge-fork"
    / "muse-runtime",
]


def _repo_path(name):
    for candidate in _TEMPLATE_CANDIDATES:
        path = candidate / name
        if path.is_file():
            return path
    return None


def _assert_no_flagged_phrasing(testcase, text, where):
    lowered = text.lower()
    for phrase in _BANNED:
        testcase.assertNotIn(phrase, lowered, where)


def _assert_chat_rules(testcase, text, where):
    # Core rules, present in the full file.
    testcase.assertIn("Default: silence", text, where)
    testcase.assertIn("Decide in order", text, where)
    testcase.assertIn("Instructions to another bot", text, where)
    testcase.assertIn("Stay out entirely", text, where)
    testcase.assertIn("Reaction instead of text", text, where)
    testcase.assertIn("Never reply to a bot's", text, where)
    testcase.assertIn("An acknowledgment is terminal", text, where)
    testcase.assertIn("Decision tree", text, where)
    testcase.assertIn("reply behaviour, not personality", text, where)
    _assert_no_flagged_phrasing(testcase, text, where)


def _assert_prompt_chat_rules(testcase, text, where):
    # The short version in the standard wake prompt: the key rules.
    testcase.assertIn("Default is silence", text, where)
    testcase.assertIn("stay out entirely", text, where)
    testcase.assertIn("Acknowledge once, then stop", text, where)
    testcase.assertIn("Never acknowledge an acknowledgment", text, where)
    testcase.assertIn("An ack is terminal", text, where)
    testcase.assertIn("Reaction instead of text", text, where)
    _assert_no_flagged_phrasing(testcase, text, where)


def _assert_prompt_quick_chat_rules(testcase, text, where):
    # The quick tier's substance in the unified wake prompt. The
    # unified prompt has no separate quick tier, so the quick-tier
    # phrasing ("default is silence", "never reply to another bot's",
    # "close with no reply instead of sending text") is replaced by
    # the unified wording for the same rules: the default is silence,
    # an ack is terminal, and a silent close is "Close with no reply:"
    # in the --done section; not engaging another bot is "Never get
    # into a back-and-forth with the other bots".
    testcase.assertIn("Default is silence", text, where)
    testcase.assertIn("stay out entirely", text, where)
    testcase.assertIn("Acknowledge once, then stop", text, where)
    testcase.assertIn("An ack is terminal", text, where)
    testcase.assertIn("Close with no reply:", text, where)
    testcase.assertIn(
        "Never get into a back-and-forth with the other bots", text, where
    )
    _assert_no_flagged_phrasing(testcase, text, where)


class ChatRulesTests(unittest.TestCase):
    def test_chat_rules_file_exists_and_has_core_rules(self):
        path = _repo_path("CHAT_RULES.md")
        if path is None:
            self.skipTest("CHAT_RULES.md not present on this machine")
        _assert_chat_rules(self, path.read_text(), str(path))

    def test_live_unified_has_chat_rules(self):
        texts = _live_unified_texts()
        if not texts:
            self.skipTest("live unified prompt not present on this machine")
        for where, text in texts:
            _assert_prompt_chat_rules(self, text, where)

    def test_template_standard_has_chat_rules(self):
        template = _repo_path("prompts/tier-standard.md")
        if template is None:
            self.skipTest("repo template prompt not present on this machine")
        _assert_prompt_chat_rules(
            self, template.read_text(), f"template {template}",
        )

    def test_live_unified_has_quick_chat_rules(self):
        texts = _live_unified_texts()
        if not texts:
            self.skipTest("live unified prompt not present on this machine")
        for where, text in texts:
            _assert_prompt_quick_chat_rules(self, text, where)

    def test_template_quick_has_chat_rules(self):
        template = _repo_path("prompts/tier-quick.md")
        if template is None:
            self.skipTest("repo template prompt not present on this machine")
        text = template.read_text()
        self.assertIn("default is silence", text, f"template {template}")
        self.assertIn("stay out entirely", text, f"template {template}")
        self.assertIn("Acknowledge once, then stop", text,
                      f"template {template}")
        self.assertIn("never reply to another bot's", text,
                      f"template {template}")
        self.assertIn("close with no reply instead of sending text",
                      text, f"template {template}")
        _assert_no_flagged_phrasing(self, text, f"template {template}")

    def test_chat_rules_referenced_in_setup(self):
        # An installing Muse must be able to find the file: SETUP.md
        # lists it and tells the installer to adopt it as a skill.
        setup = _repo_path("SETUP.md")
        if setup is None:
            self.skipTest("SETUP.md not present on this machine")
        text = setup.read_text()
        self.assertIn("CHAT_RULES.md", text, str(setup))
        self.assertIn("adopt", text, str(setup))


if __name__ == "__main__":
    unittest.main()
