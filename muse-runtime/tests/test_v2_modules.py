"""Tests for the v2 new modules: message cache, delivery ledger,
prompt pin, deterministic memory recall, and prompt assembly.

Covers only the new files (bridge_core/message_cache.py,
delivery_ledger.py, prompt_pin.py, memory_recall.py,
prompt_assembly.py). No Telegram, no network, no existing modules.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bridge_core import delivery_ledger as ledger_mod
from bridge_core import memory_recall as recall
from bridge_core import prompt_assembly as assembly
from bridge_core import prompt_pin as pin_mod
from bridge_core.delivery_ledger import DeliveryLedger
from bridge_core.message_cache import MessageCache
from bridge_core.prompt_pin import PromptPin


def _dead_pid() -> int:
    """A pid guaranteed dead: spawn `true` and reap it."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    pid = proc.pid
    proc.wait()
    return pid  # Reaped already; the pid cannot be alive.


class MessageCacheTests(unittest.TestCase):
    def _cache(self, tmp, **kwargs):
        return MessageCache(Path(tmp) / "cache.json", **kwargs)

    def test_record_and_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp)
            cache.record(11, 1, "hello", sender_id=7, timestamp=1000.0)
            got = cache.get(11, 1)
            self.assertEqual(got["text"], "hello")
            self.assertEqual(got["sender_id"], 7)
            self.assertEqual(got["timestamp"], 1000.0)
            self.assertIsNone(cache.get(11, 99))
            self.assertIsNone(cache.get(12, 1))
            # Reload from disk: the record survives.
            cache2 = self._cache(tmp)
            self.assertEqual(cache2.get(11, 1)["text"], "hello")
            # Cache file is private.
            mode = oct((Path(tmp) / "cache.json").stat().st_mode & 0o777)
            self.assertEqual(mode, "0o600")

    def test_reply_chain_depth_4_oldest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp)
            # Chain A <- B <- C <- D <- E <- F (each replies to previous).
            prev = None
            for i, letter in enumerate("ABCDEF", start=1):
                cache.record(5, i, letter, reply_to_message_id=prev,
                             timestamp=1000.0 + i)
                prev = i
            chain = cache.reply_chain(5, 6)  # ancestors of F
            self.assertEqual([r["text"] for r in chain], ["B", "C", "D", "E"])
            self.assertEqual(len(chain), 4)  # depth cap, A excluded
            # Short chain: all ancestors returned.
            self.assertEqual(
                [r["text"] for r in cache.reply_chain(5, 3)], ["A", "B"])
            # No reply link: empty.
            self.assertEqual(cache.reply_chain(5, 1), [])
            # Unknown message: empty.
            self.assertEqual(cache.reply_chain(5, 99), [])

    def test_group_window_watermark(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp)
            for i in range(1, 6):
                cache.record(-100, i, f"m{i}", timestamp=1000.0 + i,
                             chat_type="group")
            self.assertEqual(
                [r["text"] for r in cache.group_window(-100)],
                ["m1", "m2", "m3", "m4", "m5"])
            # Watermark is a timestamp: strictly after it.
            self.assertEqual(
                [r["text"] for r in cache.group_window(-100, watermark=1002.0)],
                ["m3", "m4", "m5"])
            self.assertEqual(
                cache.group_window(-100, watermark=1005.0), [])
            # Limit keeps the newest, still in received order.
            self.assertEqual(
                [r["text"] for r in cache.group_window(-100, limit=2)],
                ["m4", "m5"])

    def test_dm_history_last_10(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp)
            for i in range(1, 16):
                cache.record(9, i, f"d{i}", timestamp=1000.0 + i)
            history = cache.dm_history(9)
            self.assertEqual(len(history), 10)
            self.assertEqual([r["text"] for r in history],
                             [f"d{i}" for i in range(6, 16)])
            self.assertEqual(len(cache.dm_history(9, limit=3)), 3)

    def test_caps_prune_oldest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp, max_memory=5, max_persistent=3)
            for i in range(1, 8):
                cache.record(1, i, f"x{i}", timestamp=1000.0 + i)
            # In-memory cap: oldest evicted.
            self.assertIsNone(cache.get(1, 1))
            self.assertIsNone(cache.get(1, 2))
            self.assertEqual(cache.get(1, 3)["text"], "x3")
            # Persistent cap: reload sees only the newest 3.
            cache2 = self._cache(tmp, max_memory=5, max_persistent=3)
            self.assertEqual(
                [r["text"] for r in cache2.dm_history(1, limit=10)],
                ["x5", "x6", "x7"])
            # prune() enforces caps and reports drops.
            cache3 = MessageCache(Path(tmp) / "c3.json",
                                  max_memory=2, max_persistent=2)
            for i in range(1, 4):
                cache3.record(1, i, f"y{i}", timestamp=1000.0 + i)
            self.assertEqual(cache3.prune(), 0)  # already trimmed on record

    def test_build_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp)
            cache.record(3, 1, "first", sender_id=7, timestamp=1000.0)
            cache.record(3, 2, "reply to first", sender_id=8,
                         reply_to_message_id=1, timestamp=1001.0)
            cache.record(3, 3, "latest", sender_id=7, timestamp=1002.0)
            ctx = cache.build_context(3, reply_to_message_id=2)
            self.assertIn("[Reply chain]", ctx)
            self.assertIn("[1] 7: first", ctx)
            self.assertIn("[2] 8: reply to first", ctx)
            # The reply target is not repeated in the recent window.
            self.assertEqual(ctx.count("reply to first"), 1)
            self.assertIn("[Recent messages]", ctx)
            self.assertIn("[3] 7: latest", ctx)
            self.assertEqual(cache.build_context(99), "")


class DeliveryLedgerTests(unittest.TestCase):
    def _ledger(self, tmp):
        return DeliveryLedger(Path(tmp) / "ledger.db")

    def test_lifecycle_pending_attempting_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            oid = ledger.record_obligation(
                session_key="agent:main:telegram:private:9",
                platform="telegram", chat_id=9, content="hi")
            row = ledger.get(oid)
            self.assertEqual(row["state"], "pending")
            self.assertEqual(row["attempts"], 0)
            self.assertTrue(ledger.mark_attempting(oid))
            row = ledger.get(oid)
            self.assertEqual(row["state"], "attempting")
            self.assertEqual(row["owner_pid"], os.getpid())
            self.assertTrue(ledger.mark_delivered(oid))
            self.assertEqual(ledger.get(oid)["state"], "delivered")
            # Unknown ids report False, never raise.
            self.assertFalse(ledger.mark_delivered("nope"))
            ledger.close()

    def test_failed_then_manual_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            oid = ledger.record_obligation(
                session_key="s", platform="telegram", chat_id=1,
                content="x")
            ledger.mark_attempting(oid)
            self.assertTrue(ledger.mark_failed(oid, "403 forbidden"))
            row = ledger.get(oid)
            self.assertEqual(row["state"], "failed")
            self.assertEqual(row["last_error"], "403 forbidden")
            # A failed row is NOT swept, but a manual retry works.
            self.assertEqual(ledger.sweep_recoverable(), [])
            self.assertTrue(ledger.mark_attempting(oid))
            self.assertTrue(ledger.mark_delivered(oid))
            ledger.close()

    def test_sweep_only_dead_owner_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            dead = _dead_pid()
            self.assertFalse(ledger_mod.pid_alive(dead))
            # Dead owner, attempting: swept.
            oid_dead = ledger.record_obligation(
                session_key="s", platform="telegram", chat_id=1,
                content="dead")
            ledger.mark_attempting(oid_dead, owner_pid=dead,
                                  owner_started_at=1000.0)
            # Live owner (this process), attempting: never swept.
            oid_live = ledger.record_obligation(
                session_key="s", platform="telegram", chat_id=1,
                content="live")
            ledger.mark_attempting(oid_live, owner_pid=os.getpid())
            # Pending, no owner: swept (never started).
            oid_pending = ledger.record_obligation(
                session_key="s", platform="telegram", chat_id=1,
                content="pending")
            swept = ledger.sweep_recoverable()
            swept_ids = {row["obligation_id"] for row in swept}
            self.assertIn(oid_dead, swept_ids)
            self.assertIn(oid_pending, swept_ids)
            self.assertNotIn(oid_live, swept_ids)
            for row in swept:
                self.assertTrue(row["recovered"])
                self.assertEqual(row["attempts"], 1)
            # Live owner's row is untouched.
            self.assertEqual(ledger.get(oid_live)["state"], "attempting")
            self.assertEqual(ledger.get(oid_live)["owner_pid"], os.getpid())
            ledger.close()

    def test_sweep_recycled_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            oid = ledger.record_obligation(
                session_key="s", platform="telegram", chat_id=1,
                content="recycled")
            # Our own pid but a bogus start time: the recorded owner is
            # gone (pid recycled), so the row is recoverable.
            ledger.mark_attempting(oid, owner_pid=os.getpid(),
                                  owner_started_at=1.0)
            swept = ledger.sweep_recoverable()
            self.assertEqual(
                [r["obligation_id"] for r in swept], [oid])
            ledger.close()

    def test_sweep_attempts_cap_and_stale_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            dead = _dead_pid()
            oid = ledger.record_obligation(
                session_key="s", platform="telegram", chat_id=1,
                content="exhausted")
            for _ in range(3):
                ledger.mark_attempting(oid, owner_pid=dead,
                                      owner_started_at=1000.0)
                swept = ledger.sweep_recoverable()
                self.assertEqual(len(swept), 1)
            # At max attempts: no longer swept.
            ledger.mark_attempting(oid, owner_pid=dead,
                                  owner_started_at=1000.0)
            self.assertEqual(ledger.sweep_recoverable(), [])
            # Attempting with no owner and a fresh update is not stale.
            oid2 = ledger.record_obligation(
                session_key="s", platform="telegram", chat_id=1,
                content="fresh")
            ledger.mark_attempting(oid2, owner_pid=dead,
                                  owner_started_at=1000.0)
            ledger._conn.execute(
                "UPDATE delivery_obligations SET owner_pid=NULL, "
                "owner_started_at=NULL, updated_at=? WHERE obligation_id=?",
                (1700000000.0, oid2))
            ledger._conn.commit()
            # Fresh (updated now-ish in test terms): pass an explicit now
            # just after the update so it is not stale.
            self.assertEqual(
                ledger.sweep_recoverable(now=1700000001.0), [])
            # Same row is stale a day later.
            swept = ledger.sweep_recoverable(now=1700000000.0 + 90000)
            self.assertEqual(
                [r["obligation_id"] for r in swept], [oid2])
            ledger.close()

    def test_recovered_marker_constant(self):
        self.assertEqual(
            ledger_mod.RECOVERED_MARKER,
            "Recovered reply \u2014 the bridge restarted while sending; "
            "this may be a repeat.")

    def test_prune_delivered_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            old = ledger.record_obligation(
                session_key="s", platform="telegram", chat_id=1,
                content="old", created_at=1000.0)
            ledger.mark_delivered(old)
            ledger._conn.execute(
                "UPDATE delivery_obligations SET updated_at=1000.0 "
                "WHERE obligation_id=?", (old,))
            ledger._conn.commit()
            pending = ledger.record_obligation(
                session_key="s", platform="telegram", chat_id=1,
                content="keep")
            self.assertEqual(ledger.prune(now=2000000.0), 1)
            self.assertIsNone(ledger.get(old))
            self.assertIsNotNone(ledger.get(pending))
            ledger.close()


class PromptPinTests(unittest.TestCase):
    def test_pin_and_byte_stable_render(self):
        record = {"session_id": "sess-1"}
        pin = PromptPin(context_key="k1", context_prompt="CTX\nline2",
                        redact_pii=False, channel_prompt="chan")
        self.assertTrue(pin_mod.pin_prompt(record, pin,
                                           expected_session_id="sess-1"))
        first = pin_mod.render_prompt_pin(record)
        self.assertEqual(first, "CTX\nline2")
        self.assertEqual(pin_mod.render_prompt_pin(record), first)
        self.assertEqual(record["prompt_pin"]["version"], 1)
        self.assertEqual(record["prompt_pin"]["context_key"], "k1")

    def test_cas_failure_leaves_record_alone(self):
        record = {"session_id": "sess-1"}
        pin = PromptPin(context_key="k1", context_prompt="CTX")
        self.assertFalse(pin_mod.pin_prompt(record, pin,
                                            expected_session_id="sess-2"))
        self.assertNotIn("prompt_pin", record)
        self.assertEqual(pin_mod.render_prompt_pin(record), "")
        # No expected id: pins fine.
        self.assertTrue(pin_mod.pin_prompt(record, pin))
        self.assertEqual(pin_mod.render_prompt_pin(record), "CTX")
        # Invalid pin (empty context key, wrong version) is refused.
        bad = PromptPin(context_key="", context_prompt="CTX")
        self.assertFalse(pin_mod.pin_prompt(record, bad))
        self.assertEqual(pin_mod.render_prompt_pin(record), "CTX")

    def test_build_session_context_prompt(self):
        prompt = pin_mod.build_session_context_prompt(
            platform="telegram", chat_id=123, chat_type="private",
            user_id=456)
        self.assertIn("Current Session Context", prompt)
        self.assertIn("Source: telegram", prompt)
        self.assertIn("Chat: private 123", prompt)
        self.assertIn("User: 456", prompt)
        redacted = pin_mod.build_session_context_prompt(
            chat_id=123, user_id=456, redact_pii=True)
        self.assertIn("User: redacted:", redacted)
        self.assertNotIn("456", redacted)
        self.assertIn("User: unknown",
                      pin_mod.build_session_context_prompt())


class MemoryRecallTests(unittest.TestCase):
    MEMORY = (
        "Westin likes his hockey game on Friday night at the rink.\n"
        "The team plays hard and the goalie stands tall.\n\n"
        "The Delta feed store opens at 9am and closes at 5pm on weekdays.\n"
        "Customers come in for chicken feed and horse feed.\n\n"
        "The hockey game Friday night needs new skates and tape.\n"
        "Friday night hockey is the best night of the week.\n\n"
        "Unrelated note about the weather being cold in November.\n"
        "Snow is coming and the roads will be icy."
    )

    def test_top_3_and_threshold(self):
        snippets = recall.extract_snippets(
            self.MEMORY, "hockey game friday night")
        self.assertLessEqual(len(snippets), 3)
        self.assertTrue(all(s["score"] >= 0.65 for s in snippets))
        # The two hockey sections outrank the feed-store section.
        texts = [s["text"] for s in snippets]
        self.assertTrue(any("Friday night" in t for t in texts))
        self.assertFalse(any("feed store" in t for t in texts))
        scores = [s["score"] for s in snippets]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_single_word_scores_085_base(self):
        snippets = recall.extract_snippets(self.MEMORY, "hockey")
        self.assertTrue(snippets)
        self.assertTrue(all(s["score"] >= 0.65 for s in snippets))

    def test_no_match_returns_empty(self):
        self.assertEqual(
            recall.extract_snippets(self.MEMORY, "quantum banana"), [])
        self.assertEqual(recall.extract_snippets("", "hockey"), [])
        self.assertEqual(recall.extract_snippets(self.MEMORY, ""), [])
        self.assertEqual(recall.format_snippets([]), "")

    def test_char_cap_1800(self):
        filler = " ".join(["lorem"] * 120)
        memory = "\n\n".join(
            f"Section {i} about hockey game friday night. {filler}"
            for i in range(5)
        )
        snippets = recall.extract_snippets(memory, "hockey game friday night")
        total = sum(len(s["text"]) for s in snippets)
        self.assertLessEqual(total, 1800)
        self.assertLessEqual(len(snippets), 3)
        self.assertTrue(snippets)

    def test_format_snippets(self):
        snippets = recall.extract_snippets(self.MEMORY, "hockey")
        formatted = recall.format_snippets(snippets)
        self.assertTrue(formatted.startswith("<active_memory>\n"))
        self.assertTrue(formatted.endswith("</active_memory>"))
        self.assertIn("hockey", formatted.lower())

    def test_has_recall_intent(self):
        self.assertTrue(recall.has_recall_intent("do you remember what I said?"))
        self.assertTrue(recall.has_recall_intent("what did I say about the game"))
        self.assertTrue(recall.has_recall_intent("my preference is early games"))
        self.assertTrue(recall.has_recall_intent("remind me what we decided"))
        self.assertFalse(recall.has_recall_intent("what time is the game"))
        self.assertFalse(recall.has_recall_intent("hello there"))
        self.assertFalse(recall.has_recall_intent(""))


class PromptAssemblyTests(unittest.TestCase):
    def test_no_truncation_when_it_fits(self):
        prompt, truncated = assembly.assemble_prompt(
            static_hints="HINTS", envelope="ENV",
            reply_chain_text="CHAIN", window_text="WINDOW",
            memory_snippets_text="MEM", transcript_text="TRANSCRIPT",
            max_chars=12000)
        self.assertFalse(truncated)
        for part in ("HINTS", "ENV", "MEM", "CHAIN", "WINDOW", "TRANSCRIPT"):
            self.assertIn(part, prompt)
        self.assertNotIn(assembly.BOUNDED_CONTEXT_NOTICE, prompt)
        # Static hints stay first.
        self.assertTrue(prompt.startswith("HINTS"))

    def test_newest_first_cut_and_notice(self):
        transcript = "\n".join(f"msg {i}" for i in range(1, 21))
        prompt, truncated = assembly.assemble_prompt(
            static_hints="HINTS", envelope="ENV",
            transcript_text=transcript, max_chars=40)
        self.assertTrue(truncated)
        self.assertTrue(prompt.startswith(assembly.BOUNDED_CONTEXT_NOTICE))
        self.assertIn("HINTS", prompt)
        self.assertIn("ENV", prompt)
        # Newest messages kept, oldest cut, cut at a line boundary.
        self.assertIn("msg 20", prompt)
        self.assertNotIn("msg 1\n", prompt)
        self.assertNotIn("msg 2\n", prompt)

    def test_static_and_envelope_always_kept(self):
        prompt, truncated = assembly.assemble_prompt(
            static_hints="HINTS", envelope="ENV",
            transcript_text="x" * 5000, max_chars=20)
        self.assertTrue(truncated)
        self.assertIn("HINTS", prompt)
        self.assertIn("ENV", prompt)

    def test_empty_sections_skipped(self):
        prompt, truncated = assembly.assemble_prompt(
            static_hints="HINTS", max_chars=100)
        self.assertFalse(truncated)
        self.assertEqual(prompt, "HINTS")


if __name__ == "__main__":
    unittest.main()
