"""Policy tests for the unbounded project-scoped claude-opinion entry point."""

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, SCRIPTS_DIR)

import ask_claude  # noqa: E402


class CanonicalProjectTests(unittest.TestCase):
    def tearDown(self):
        ask_claude._project_root.cache_clear()

    def test_git_root_is_canonicalized(self):
        proc = MagicMock(returncode=0, stdout="/tmp/link/project\n")
        with patch("ask_claude.subprocess.run", return_value=proc), \
             patch("ask_claude.os.path.realpath", return_value="/real/project"):
            ask_claude._project_root.cache_clear()
            self.assertEqual(ask_claude._project_root(), "/real/project")

    def test_non_git_directory_uses_canonical_cwd(self):
        proc = MagicMock(returncode=1, stdout="")
        with patch("ask_claude.subprocess.run", return_value=proc), \
             patch("ask_claude.os.getcwd", return_value="/tmp/current"), \
             patch("ask_claude.os.path.realpath", return_value="/real/current"):
            ask_claude._project_root.cache_clear()
            self.assertEqual(ask_claude._project_root(), "/real/current")

    def test_same_project_and_session_key_share_run_lock(self):
        root = "/fixed/project"
        root_hash = hashlib.sha256(root.encode()).hexdigest()[:16]
        with patch.object(ask_claude, "_project_root", return_value=root), \
             patch.dict(os.environ, {"CLAUDE_OPINION_SESSION_KEY": "review"}, clear=False):
            suffix = hashlib.sha256(b"review").hexdigest()[:16]
            self.assertTrue(
                ask_claude._run_lock_path().endswith(
                    f"/{root_hash}-{suffix}.json.run.lock"
                )
            )


class UnboundedExecutionTests(unittest.TestCase):
    def test_help_probe_has_no_timeout_argument(self):
        captured = {}
        fake = MagicMock(returncode=0, stdout="help", stderr="")

        def fake_run(*args, **kwargs):
            captured.update(kwargs)
            return fake

        ask_claude._claude_help_text.cache_clear()
        try:
            with patch("ask_claude.subprocess.run", side_effect=fake_run):
                self.assertEqual(ask_claude._claude_help_text(), "help")
        finally:
            ask_claude._claude_help_text.cache_clear()
        self.assertNotIn("timeout", captured)

    def test_claude_process_has_no_timeout_and_uses_project_cwd(self):
        captured = {}
        prompt = "x" * 200_000
        output = "y" * 200_000

        class FakeProc:
            pid = 12345
            returncode = 0

            def communicate(self, *args, **kwargs):
                captured["communicate_args"] = args
                captured["communicate_kwargs"] = kwargs
                return output, ""

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["popen_kwargs"] = kwargs
            return FakeProc()

        with patch("ask_claude.subprocess.Popen", side_effect=fake_popen), \
             patch.dict(os.environ, {"CLAUDE_OPINION_TIMEOUT": "1"}, clear=False):
            result = ask_claude._run_claude_proc(
                ["claude", "-p", "--add-dir", "/repo"], prompt
            )

        self.assertEqual(result.stdout, output)
        self.assertEqual(captured["popen_kwargs"]["cwd"], "/repo")
        self.assertTrue(captured["popen_kwargs"]["start_new_session"])
        self.assertEqual(captured["communicate_kwargs"], {"input": prompt})
        self.assertNotIn("timeout", captured["communicate_kwargs"])

    def test_command_has_no_turn_budget_or_agent_fanout_flags(self):
        with patch.object(ask_claude, "_best_effort_level", return_value="max"):
            cmd = ask_claude._base_cmd("/repo", "Review")
        for forbidden in (
            "--max-turns",
            "--max-budget-usd",
            "--no-session-persistence",
            "--agent",
            "--agents",
        ):
            self.assertNotIn(forbidden, cmd)

    def test_run_lock_is_private_and_released(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(ask_claude, "STATE_DIR", td), \
             patch.object(ask_claude, "_project_key", return_value="abc"):
            with ask_claude._run_lock():
                lock_path = ask_claude._run_lock_path()
                self.assertTrue(os.path.exists(lock_path))
                self.assertEqual(os.stat(lock_path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
