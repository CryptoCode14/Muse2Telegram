"""Tests for the "unknown" send outcome: timeout budget and diagnostics.

A send that reports "unknown" may or may not have gone out, so the
item stays in "sending" for reconciliation and is never resent
blindly. Two things must hold for that to be safe and diagnosable:

- The subprocess budget (SEND_TIMEOUT_SECONDS) must exceed the
  sender's worst case (credential fetch plus up to three Bot API
  calls, 30s each for text, 60s each for a media multipart call),
  or a slow-but-successful send is misreported as "unknown".
- An "unknown" outcome must log WHY (timeout, exit code, or no
  message_id) plus the sender's stderr, so the operator can tell a
  likely-delivered timeout from a definitive rejection.

No Muse credentials, Telegram calls, or external sender are used:
subprocess.run is mocked.
"""
import io
import subprocess
import unittest
from contextlib import redirect_stderr
from unittest import mock

from bridge_core import telegram


class TelegramSendUnknownTests(unittest.TestCase):
    def run_sender(self, run_result):
        """Run _run_send_command with subprocess.run mocked.

        run_result is either a subprocess.CompletedProcess or an
        exception to raise. Returns ((outcome, message_id), stderr).
        """
        err = io.StringIO()
        with mock.patch.object(
            telegram.subprocess, "run", return_value=run_result
        ) as run_mock:
            with redirect_stderr(err):
                if isinstance(run_result, BaseException):
                    run_mock.side_effect = run_result
                outcome = telegram._run_send_command(
                    "/fake/sender", 123456789, "hello"
                )
        return outcome, err.getvalue()

    def test_timeout_budget_covers_sender_worst_case(self):
        # Credential fetch plus three text calls at 30s (or three
        # media multipart calls at 60s) is roughly 95s / 190s. The
        # budget must leave headroom over the media worst case, or
        # a slow-but-successful send is misreported as "unknown".
        self.assertGreaterEqual(telegram.SEND_TIMEOUT_SECONDS, 180)

    def test_timeout_reports_unknown_and_logs_it(self):
        outcome, err = self.run_sender(
            subprocess.TimeoutExpired(cmd=["sender"], timeout=200.0)
        )
        self.assertEqual(outcome, ("unknown", None))
        self.assertIn("timed out", err)

    def test_nonzero_exit_reports_unknown_and_logs_stderr(self):
        outcome, err = self.run_sender(
            subprocess.CompletedProcess(
                args=["sender"], returncode=1,
                stdout="", stderr="telegram HTTP 403\n",
            )
        )
        self.assertEqual(outcome, ("unknown", None))
        self.assertIn("exit 1", err)
        self.assertIn("telegram HTTP 403", err)

    def test_exit_zero_without_message_id_reports_unknown(self):
        outcome, err = self.run_sender(
            subprocess.CompletedProcess(
                args=["sender"], returncode=0, stdout="\n", stderr="",
            )
        )
        self.assertEqual(outcome, ("unknown", None))
        self.assertIn("without message_id", err)

    def test_success_returns_message_id_and_logs_nothing(self):
        outcome, err = self.run_sender(
            subprocess.CompletedProcess(
                args=["sender"], returncode=0, stdout="61\n", stderr="",
            )
        )
        self.assertEqual(outcome, ("sent", "61"))
        self.assertNotIn("send-unknown", err)

    def test_sender_stderr_is_truncated_in_log(self):
        outcome, err = self.run_sender(
            subprocess.CompletedProcess(
                args=["sender"], returncode=1,
                stdout="", stderr="x" * 2000,
            )
        )
        self.assertEqual(outcome, ("unknown", None))
        # The log line carries at most ~500 chars of sender stderr.
        self.assertLess(len(err), 800)
        self.assertIn("send-unknown", err)


if __name__ == "__main__":
    unittest.main()
