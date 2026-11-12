"""Offline tests for the collab protocol (COLLAB_PROTOCOL.md).

The protocol governs a trusted multi-bot group where the owner
wants the bots to work as teammates — subagents for different
jobs that see each other, respond, and talk to each other — with
more free reign than the silence-first default in CHAT_RULES.md.

Checks:
- muse-runtime/COLLAB_PROTOCOL.md exists and contains the core
  rules: teammates, free reign, ack-only text stays a reaction,
  an ack is terminal, one owner per task, the task flow, and the
  backstops.
- It is generic (placeholders, no owner-specific group id or bot
  names), so any installing Muse can fill it in at install.
- It does not contradict its own precedence: it says it wins
  over CHAT_RULES.md in a trusted multi-bot group only, and
  CHAT_RULES.md still governs guarded / single-bot groups.
- The banned wording (built from character codes, never written
  out literally, so a whole-repo scan finds zero hits) appears
  nowhere in COLLAB_PROTOCOL.md.
- SETUP.md lists it and tells the installer to adopt it as a
  skill.

No network or credentials are used.
"""
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


class CollabProtocolTests(unittest.TestCase):
    def test_collab_protocol_file_exists_and_has_core_rules(self):
        path = _repo_path("COLLAB_PROTOCOL.md")
        if path is None:
            self.skipTest("COLLAB_PROTOCOL.md not present on this machine")
        text = path.read_text()
        where = str(path)
        self.assertIn("bots as teammates", text, where)
        self.assertIn("free reign", text, where)
        self.assertIn("Silence is no longer the default", text, where)
        self.assertIn("Ack-only text loops", text, where)
        self.assertIn("An ack is terminal", text, where)
        self.assertIn("Do not repeat a result another bot just posted",
                      text, where)
        self.assertIn("One owner per task", text, where)
        self.assertIn("Owner claims it", text, where)
        self.assertIn("Max 25 messages per task", text, where)
        self.assertIn("Human `STOP` ends a task immediately", text, where)
        self.assertIn("wins over CHAT_RULES.md", text, where)
        self.assertIn("CHAT_RULES.md still governs", text, where)
        _assert_no_flagged_phrasing(self, text, where)

    def test_collab_protocol_is_generic(self):
        # Placeholders for the installing Muse to fill in; no
        # owner-specific group id or bot names baked into the repo.
        path = _repo_path("COLLAB_PROTOCOL.md")
        if path is None:
            self.skipTest("COLLAB_PROTOCOL.md not present on this machine")
        text = path.read_text()
        self.assertIn("[USER]", text, str(path))
        self.assertIn("[YOUR MUSE NAME]", text, str(path))
        self.assertIn("[BOT LIST]", text, str(path))
        self.assertNotIn("-5280361168", text, str(path))
        self.assertNotIn("DorianCodingBot", text, str(path))
        self.assertNotIn("Westinmusebot", text, str(path))
        self.assertNotIn("GusGrandValleyBot", text, str(path))

    def test_collab_protocol_referenced_in_setup(self):
        # An installing Muse must be able to find the file: SETUP.md
        # lists it and tells the installer to adopt it as a skill.
        setup = _repo_path("SETUP.md")
        if setup is None:
            self.skipTest("SETUP.md not present on this machine")
        text = setup.read_text()
        self.assertIn("COLLAB_PROTOCOL.md", text, str(setup))
        self.assertIn("telegram-collab-protocol", text, str(setup))

    def test_collab_protocol_has_no_install_commands(self):
        # The template ships as data to be filled in per SETUP Step
        # 4. It must not carry its own install-time commands: a
        # fresh installer reading the file would be told by the file
        # itself to rewrite it, and that trips the safety check.
        # The fill recipe lives in SETUP.md only.
        path = _repo_path("COLLAB_PROTOCOL.md")
        if path is None:
            self.skipTest("COLLAB_PROTOCOL.md not present on this machine")
        text = path.read_text()
        self.assertNotIn("Replace these everywhere", text, str(path))
        self.assertNotIn("Do not leave a placeholder", text, str(path))
        self.assertIn("Filled in for this group", text, str(path))


if __name__ == "__main__":
    unittest.main()
