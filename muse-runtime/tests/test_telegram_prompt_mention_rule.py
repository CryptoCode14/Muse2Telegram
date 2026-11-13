"""Offline tests for the @mention rule, stated as wake routing.

The rule lives in the wake prompt (live unified prompt, and the repo
template muse-runtime/prompts/unified.md): how a message wakes
another bot. The bridge wakes a bot only when the incoming message
@mentions it, so a message that asks another bot a question carries
that bot's @mention (its username exactly as it appears in the group
buffer), and a message that is not a question carries no @mention.
The decider is unchanged — it only wakes a bot message on @mention,
and that behaviour is already covered by test_telegram_decide.py /
test_telegram_bot_sender.py, so this file only checks the prompt
text. No network or credentials are used.
"""
import json
import unittest
from pathlib import Path

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

# The repo templates. When this file runs from the fork's
# muse-runtime/tests/, they are ../prompts/<tier>.md; when it runs
# from the eval suite, the fork lives under ~/workspace.
_TEMPLATE_CANDIDATES = [
    Path(__file__).resolve().parents[1] / "prompts",
    Path.home() / "workspace" / "muse-agent-bridge-fork"
    / "muse-runtime" / "prompts",
]


def _template_path(name):
    for candidate in _TEMPLATE_CANDIDATES:
        path = candidate / name
        if path.is_file():
            return path
    return None


def _assert_mention_rule(testcase, text, where):
    # A message that asks another bot anything carries that bot's
    # @mention, its username exactly as it appears in the group buffer.
    testcase.assertIn("carries that bot's @mention", text, where)
    testcase.assertIn(
        "exactly as it appears in the group buffer", text, where
    )
    # A message that is not a question carries no @mention.
    testcase.assertIn("carries no @mention", text, where)
    # The rule must not hardcode any bot's username: the agent takes
    # the username from the group buffer (sender_name).
    testcase.assertNotIn("GusGrandValleyBot", text, where)
    # No imperative safety-adjacent wording about @mentions.
    testcase.assertNotIn("Do not @mention a bot on every message", text, where)


def _assert_mention_rule_unified(testcase, text, where):
    # Same rule, stated in the unified prompt's wording: the bridge
    # wakes a bot only when the incoming message @mentions it (the
    # tier files said "carries that bot's @mention"). The substance
    # is the same; the tier wording is not in the unified prompt.
    testcase.assertIn(
        "wakes a bot only when the incoming message @mentions it",
        text, where,
    )
    testcase.assertIn(
        "exactly as it appears in the group buffer", text, where
    )
    testcase.assertIn("carries no @mention", text, where)
    testcase.assertNotIn("GusGrandValleyBot", text, where)
    testcase.assertNotIn("Do not @mention a bot on every message", text, where)


def _assert_mention_stop(testcase, text, where):
    # The standard tier states the stop condition: once a question is
    # answered, further messages carry no @mention unless a new
    # question comes up.
    testcase.assertIn("Once a question is answered", text, where)
    testcase.assertIn("unless a new question comes up", text, where)


class MentionRulePromptTests(unittest.TestCase):
    def test_live_unified_has_mention_rule(self):
        texts = _live_unified_texts()
        if not texts:
            self.skipTest("live unified prompt not present on this machine")
        for where, text in texts:
            _assert_mention_rule_unified(self, text, where)
            _assert_mention_stop(self, text, where)

    def test_template_standard_has_mention_rule(self):
        template = _template_path("tier-standard.md")
        if template is None:
            self.skipTest("repo template prompt not present on this machine")
        text = template.read_text()
        _assert_mention_rule(self, text, f"template {template}")
        _assert_mention_stop(self, text, f"template {template}")

    def test_live_unified_has_quick_mention_rule(self):
        texts = _live_unified_texts()
        if not texts:
            self.skipTest("live unified prompt not present on this machine")
        for where, text in texts:
            _assert_mention_rule_unified(self, text, where)

    def test_template_quick_has_mention_rule(self):
        template = _template_path("tier-quick.md")
        if template is None:
            self.skipTest("repo template prompt not present on this machine")
        text = template.read_text()
        _assert_mention_rule(self, text, f"template {template}")


if __name__ == "__main__":
    unittest.main()
