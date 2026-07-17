"""Runtime, fan-out, reconciliation, and cancellation tests."""

from __future__ import annotations

import io
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, SCRIPTS_DIR)

import claude_council as council  # noqa: E402
import _council_orchestrator as orchestrator  # noqa: E402
import _council_process as process  # noqa: E402
import _council_state as state  # noqa: E402


class CommandAndInvocationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.state_patch = patch.object(state, "COUNCIL_STATE_DIR", self.tempdir.name)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.role = council.RoleSpec("correctness", "Correctness", "Find bugs")
        self.registry = process.ProcessRegistry()

    def _proc(self, payload, returncode=0, stderr=""):
        return process.ProcessResult(returncode, json.dumps(payload), stderr)

    def test_command_uses_one_top_level_agent_without_subagent_or_budget_flags(self):
        with patch.object(process._transport, "_best_effort_level", return_value="max"):
            cmd = process._base_command("/project", "system")
        self.assertEqual(cmd.count("claude"), 1)
        self.assertIn("-p", cmd)
        self.assertIn("--output-format", cmd)
        for forbidden in (
            "--agent", "--agents", "--max-turns", "--max-budget-usd", "--no-session-persistence"
        ):
            self.assertNotIn(forbidden, cmd)

    def test_fresh_then_resume_persists_role_thread(self):
        commands = []
        results = iter([
            self._proc({"result": "fresh report", "is_error": False, "session_id": "s1"}),
            self._proc({"result": "resumed report", "is_error": False, "session_id": "s1"}),
        ])

        def fake_run(invocation_id, cmd, prompt, project_root, registry):
            commands.append(cmd)
            return next(results)

        with patch.object(process, "_run_process", side_effect=fake_run), \
             patch.object(process._transport, "_best_effort_level", return_value=None):
            first = process._invoke_role_once(
                self.role, "task", "ctx", "/project", self.registry, "key"
            )
            second = process._invoke_role_once(
                self.role, "task2", "ctx2", "/project", self.registry, "key"
            )
        self.assertTrue(first.ok)
        self.assertFalse(first.resumed)
        self.assertTrue(second.ok)
        self.assertTrue(second.resumed)
        self.assertNotIn("--resume", commands[0])
        self.assertEqual(commands[1][-2:], ["--resume", "s1"])

    def test_stale_resume_falls_back_once_to_fresh(self):
        state.save_role_state(self.role, "old", "/project", None, "key")
        commands = []
        results = iter([
            process.ProcessResult(1, "", "No conversation found with session ID: old"),
            self._proc({"result": "new report", "is_error": False, "session_id": "new"}),
        ])

        def fake_run(invocation_id, cmd, prompt, project_root, registry):
            commands.append(cmd)
            return next(results)

        with patch.object(process, "_run_process", side_effect=fake_run), \
             patch.object(process._transport, "_best_effort_level", return_value=None), \
             patch("sys.stderr", io.StringIO()):
            outcome = process._invoke_role_once(
                self.role, "task", "ctx", "/project", self.registry, "key"
            )
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.stale_restarted)
        self.assertEqual(commands[0][-2:], ["--resume", "old"])
        self.assertNotIn("--resume", commands[1])
        self.assertEqual(
            state.load_role_state(self.role, "/project", "key")["session_id"],
            "new",
        )

    def test_non_stale_error_is_contained_in_role_outcome(self):
        with patch.object(
            process,
            "_run_process",
            return_value=self._proc({"is_error": True, "result": "bad credentials"}),
        ), patch.object(process._transport, "_best_effort_level", return_value=None):
            outcome = process._invoke_role_once(
                self.role, "task", "ctx", "/project", self.registry, "key"
            )
        self.assertFalse(outcome.ok)
        self.assertIn("bad credentials", outcome.error)

    def test_run_process_has_no_timeout_and_uses_project_cwd(self):
        fake_proc = MagicMock()
        fake_proc.pid = 123
        fake_proc.returncode = 0
        fake_proc.communicate.return_value = ("out", "err")
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured.update(kwargs)
            return fake_proc

        with patch("_council_process.subprocess.Popen", side_effect=fake_popen):
            result = process._run_process(
                "x", ["claude", "-p"], "prompt", "/project", self.registry
            )
        self.assertEqual(result.stdout, "out")
        self.assertEqual(captured["cwd"], "/project")
        self.assertTrue(captured["start_new_session"])
        fake_proc.communicate.assert_called_once_with(input="prompt")


class BoundedFanOutTests(unittest.TestCase):
    def test_parallelism_never_exceeds_bound_and_preserves_panel_order(self):
        roles = tuple(
            council.RoleSpec(f"role-{index}", f"Role {index}", "review")
            for index in range(6)
        )
        panel = council.PanelSpec("test", "Test", roles)
        lock = threading.Lock()
        active = 0
        peak = 0

        def fake_role(role, task, context, project_root, registry, session_key):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return council.RoleOutcome(role=role, ok=True, text=role.role_id)

        with patch.object(orchestrator, "_invoke_role_once", side_effect=fake_role):
            outcomes = orchestrator.run_panel(panel, "task", "ctx", "/project", 2)
        self.assertEqual(peak, 2)
        self.assertEqual([outcome.role.role_id for outcome in outcomes], [r.role_id for r in roles])

    def test_parallelism_hard_limit_is_enforced(self):
        panel = council.PanelSpec(
            "test", "Test", (council.RoleSpec("a", "A", "review"),)
        )
        with self.assertRaises(council.CouncilError):
            orchestrator.run_panel(
                panel,
                "task",
                "ctx",
                "/project",
                council.MAX_PARALLEL_HARD_LIMIT + 1,
            )


class ReconciliationAndReportTests(unittest.TestCase):
    def setUp(self):
        self.panel = council.PanelSpec(
            "test",
            "Test Panel",
            (
                council.RoleSpec("a", "Architect", "architecture"),
                council.RoleSpec("b", "Skeptic", "skepticism"),
            ),
        )
        self.outcomes = [
            council.RoleOutcome(self.panel.roles[0], True, text="Use a lock."),
            council.RoleOutcome(self.panel.roles[1], False, error="session failed"),
        ]

    def test_reconciliation_context_preserves_success_and_failure(self):
        prompt = orchestrator.build_reconciliation_context(
            self.panel, "Review", "Shared evidence", self.outcomes
        )
        self.assertIn("Use a lock.", prompt)
        self.assertIn("ROLE FAILED: session failed", prompt)
        self.assertIn("preserve", prompt.lower())

    def test_report_contains_chair_answer_and_individual_reports(self):
        chair = council.RoleOutcome(
            council.RoleSpec("council-chair", "Council Chair", "reconcile"),
            True,
            text="Final decision.",
        )
        report = orchestrator.render_report(
            self.panel, "Review", self.outcomes, chair, "/project", 2
        )
        self.assertIn("# Claude Council Report", report)
        self.assertIn("Final decision.", report)
        self.assertIn("Use a lock.", report)
        self.assertIn("session failed", report)

    def test_reconciler_uses_persistent_chair_role(self):
        captured = {}

        def fake_invoke(role, task, context, project_root, registry, session_key, **kwargs):
            captured.update(
                role=role,
                task=task,
                context=context,
                project_root=project_root,
                session_key=session_key,
                kwargs=kwargs,
            )
            return council.RoleOutcome(role, True, text="final")

        with patch.object(orchestrator, "_invoke_role_once", side_effect=fake_invoke):
            outcome = orchestrator.run_reconciler(
                self.panel,
                "Review",
                "Shared evidence",
                self.outcomes,
                "/project",
                process.ProcessRegistry(),
                "key",
            )
        self.assertTrue(outcome.ok)
        self.assertEqual(captured["role"].role_id, "council-chair")
        self.assertIn("Use a lock.", captured["context"])
        self.assertEqual(captured["session_key"], "key")
        self.assertEqual(captured["kwargs"]["user_prompt_override"], captured["context"])
        self.assertIn("Reconcile", captured["kwargs"]["system_prompt_override"])
        self.assertNotIn(
            "Do not attempt to synthesize",
            captured["kwargs"]["user_prompt_override"],
        )


class CancellationTests(unittest.TestCase):
    def test_registry_terminates_all_process_groups(self):
        proc = MagicMock()
        proc.pid = 999
        registry = process.ProcessRegistry()
        registry.add("role", proc)
        with patch("_council_process.os.killpg") as killpg:
            registry.terminate_all()
        killpg.assert_called_once_with(999, signal.SIGKILL)

    def test_process_added_after_cancellation_is_terminated_immediately(self):
        registry = process.ProcessRegistry()
        registry.terminate_all()
        proc = MagicMock()
        proc.pid = 1001
        with patch("_council_process.os.killpg") as killpg:
            registry.add("late-role", proc)
        killpg.assert_called_once_with(1001, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
