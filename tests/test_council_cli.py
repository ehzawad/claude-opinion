"""CLI tests for the bounded multi-agent Claude council."""

from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, SCRIPTS_DIR)

import claude_council as council  # noqa: E402
import _council_state as state  # noqa: E402


class CliTests(unittest.TestCase):
    def test_main_runs_panel_then_chair_and_prints_report(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        context = io.StringIO("shared context")

        def fake_panel(panel, task, shared, root, max_parallel, registry, session_key):
            return [
                council.RoleOutcome(role, True, text=f"report from {role.role_id}")
                for role in panel.roles
            ]

        def fake_chair(panel, task, shared, outcomes, root, registry, session_key):
            return council.RoleOutcome(
                council.RoleSpec("council-chair", "Council Chair", "reconcile"),
                True,
                text="reconciled answer",
            )

        with tempfile.TemporaryDirectory() as state_dir, \
             patch.object(state, "COUNCIL_STATE_DIR", state_dir), \
             patch("claude_council.shutil.which", return_value="/usr/bin/claude"), \
             patch.object(council, "_canonical_project_root", return_value="/project"), \
             patch.object(council, "run_panel", side_effect=fake_panel), \
             patch.object(council, "run_reconciler", side_effect=fake_chair), \
             patch("sys.stdin", context), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = council.main(["--panel", "minimal", "Review this"] )
        self.assertEqual(code, 0)
        self.assertIn("reconciled answer", stdout.getvalue())
        self.assertIn("report from systems-architect", stdout.getvalue())
        self.assertIn("3 roles", stderr.getvalue())

    def test_list_panels_does_not_require_claude_or_stdin(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = council.main(["--list-panels"])
        self.assertEqual(code, 0)
        self.assertIn("engineering", stdout.getvalue())

    def test_parallel_parser_rejects_zero_and_excessive_values(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            council._positive_parallel("0")
        with self.assertRaises(argparse.ArgumentTypeError):
            council._positive_parallel(str(council.MAX_PARALLEL_HARD_LIMIT + 1))


if __name__ == "__main__":
    unittest.main()
