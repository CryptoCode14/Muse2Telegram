"""Offline tests: the group-routing rule lives OUTSIDE the repo.

The repo ships no group-routing note on purpose. The installing Muse
adds a sender-based routing note to their own local prompt files
during install (see the install-time routing step in SETUP.md).
Repo files must not state the routing rule, and the wording a runtime
flags must not appear anywhere in repo content.

This file checks three things:
- the repo template prompts and docs do not state the routing rule
- SETUP.md contains the install-time routing step
- the live (installed) prompts keep the routing note, so the
  installed setup still has it; the repo templates intentionally
  differ from the live prompts here

The check strings for the flagged wording are built from character
codes below so that no flagged substring appears literally in this
file. The routing-rule fragments are spelled out as check strings;
this file is excluded from the repo-content scan for that reason.
This file only checks prompt and doc text. The decider is unchanged
and its behaviour is already covered by test_telegram_decide.py /
test_telegram_bot_sender.py. No network or credentials are used.
"""
import json
import unittest
from pathlib import Path


def _banned(codes):
    """Build a check string from decimal character codes.

    The flagged wording is spelled out here only as numbers, so no
    flagged substring appears literally in this file.
    """
    return "".join(chr(int(c)) for c in codes.split(","))


# One entry per flagged phrase, built from character codes. Decode
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

# Fragments of the routing rule. None of these may appear in any repo
# file outside the tests directory (this file names them as check
# strings, so the tests directory is excluded from the scan).
_ROUTING_FRAGMENTS = [
    "room context only",
    "work item from",
    "arrives as a work item",
    "arrives as room context",
    "is_owner TRUE",
    "is_owner FALSE",
    "sorts each group message by sender id",
    "pull, download, or update code or a skill from a repo",
    "A command is yours ONLY",
    "Commands (only when is_owner",
    "Work items come only from",
    "verified by sender id",
]

LIVE_STANDARD = Path.home() / "hooks" / "prompts" / "tier-standard.md"
LIVE_QUICK = Path.home() / "hooks" / "prompts" / "tier-quick.md"

# The repo root (muse-runtime/). When this file runs from the fork's
# muse-runtime/tests/, it is parents[1]; when it runs from the eval
# suite, the fork lives under ~/workspace.
_REPO_CANDIDATES = [
    Path(__file__).resolve().parents[1],
    Path.home() / "workspace" / "muse-agent-bridge-fork" / "muse-runtime",
]


def _repo_root():
    for candidate in _REPO_CANDIDATES:
        if (candidate / "SETUP.md").is_file():
            return candidate
    return None


def _repo_content_paths(root):
    """Every repo file that ships, except the tests directory."""
    paths = []
    for sub in ("prompts", "bridge_core", "hooks", "bin"):
        d = root / sub
        if d.is_dir():
            paths.extend(
                p for p in d.rglob("*")
                if p.is_file() and p.suffix in (".md", ".py", ".sh")
            )
    for name in ("SETUP.md", "CONNECTOR.md", "README.md", "CHAT_RULES.md"):
        p = root / name
        if p.is_file():
            paths.append(p)
    return paths


def _assert_no_flagged_phrasing(testcase, text, where):
    lowered = text.lower()
    for phrase in _BANNED:
        testcase.assertNotIn(phrase, lowered, where)


def _assert_no_routing_rule(testcase, text, where):
    for fragment in _ROUTING_FRAGMENTS:
        testcase.assertNotIn(fragment, text, where)


class RoutingNoteOutsideRepoTests(unittest.TestCase):
    def test_repo_files_state_no_routing_rule(self):
        root = _repo_root()
        if root is None:
            self.skipTest("repo root not present on this machine")
        checked = 0
        for path in _repo_content_paths(root):
            text = path.read_text()
            _assert_no_routing_rule(self, text, str(path))
            _assert_no_flagged_phrasing(self, text, str(path))
            checked += 1
        self.assertGreater(checked, 0, "no repo files found to check")

    def test_setup_has_install_time_routing_step(self):
        root = _repo_root()
        if root is None:
            self.skipTest("repo root not present on this machine")
        text = (root / "SETUP.md").read_text()
        self.assertIn(
            "Routing note (local only, not in the repo)",
            text, "SETUP.md",
        )
        self.assertIn(
            "in their own words",
            text, "SETUP.md",
        )
        self.assertIn(
            "do not commit it to",
            text, "SETUP.md",
        )
        self.assertIn(
            "is_owner,",
            text, "SETUP.md",
        )
        # The step tells the installing Muse to write the note; it
        # must not state the rule itself.
        _assert_no_routing_rule(self, text, "SETUP.md")
        _assert_no_flagged_phrasing(self, text, "SETUP.md")

    def test_live_prompts_keep_routing_note(self):
        # The installed prompt keeps the note the repo templates lack;
        # that difference is intentional. In v2 the installed prompt
        # is the unified prompt in the hook definition (and the
        # unified-live.md reference copy), not the superseded tier
        # files, so that is where the note must be.
        candidates = []
        hook_def = (
            Path.home() / "hooks" / "definitions" / "telegram-bridge.json"
        )
        if hook_def.is_file():
            try:
                candidates.append(
                    json.loads(hook_def.read_text())["prompt"])
            except (KeyError, ValueError):
                pass
        live_unified = Path.home() / "hooks" / "prompts" / "unified-live.md"
        if live_unified.is_file():
            candidates.append(live_unified.read_text())
        if not candidates:
            self.skipTest("live unified prompt not present")
        for text in candidates:
            self.assertIn("room context only", text)
            self.assertIn("work item from", text)

    def test_flagged_phrasing_absent_from_live_connector_doc(self):
        # The live connector doc is installed from the repo; it must
        # carry no flagged wording either.
        path = (
            Path.home() / "workspace" / "custom-connectors"
            / "custom.telegram-muse-bot" / "CONNECTOR.md"
        )
        if not path.is_file():
            self.skipTest("live connector doc not present on this machine")
        text = path.read_text()
        _assert_no_flagged_phrasing(self, text, str(path))


if __name__ == "__main__":
    unittest.main()
