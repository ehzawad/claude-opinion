"""Unit tests for the persistent bounded Claude council."""

from __future__ import annotations

import argparse
import io
import json
import os
import signal
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, SCRIPTS_DIR)

import claude_council as council  # noqa: E402


class PanelCompositionTests(unittest.TestCase):
    def test_auto_panel_adds_domain_roles(self):
        panel = council.compose_panel(
            "auto",
            "Review this authentication session migration and concurrency tests",
            "The implementation uses locks, tokens, retries, and integration tests.",
        )
        ids = [role.role_id for role in panel.roles]
        self.assertEqual(ids[:2], ["systems-architect", "correctness-reviewer"])
        self.assertIn("reliability-operator", ids)
        self.assertIn("security-reviewer", ids)
        self.assertIn("test-strategist", ids)
        self.assertIn("product-maintainer", ids)
        self.assertEqual(ids[-1], "adversarial-skeptic")
        self.assertEqual(len(ids), len(set(ids)))

    def test_builtin_panel_is_deterministic(self):
        first = council.compose_panel("engineering", "one", "context")
        second = council.compose_panel("engineering", "two", "other")
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first.roles), 5)

    def test_custom_roles_file(self):
        payload = {
            "id": "migration-panel",
            "name": "Migration Panel",
            "reconciler_instruction": "Prefer reversible steps.",
            "roles": [
                {"id": "schema", "name": "Schema", "instruction": "Review schema."},
                {"id": "rollback", "name": "Rollback", "instruction": "Review rollback."},
            ],
        }
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            json.dump(payload, handle)
            path = handle.name
        try:
            panel = council.compose_panel("auto", "task", "ctx", path)
        finally:
            os.remove(path)
        self.assertEqual(panel.panel_id, "migration-panel")
        self.assertEqual([role.role_id for role in panel.roles], ["schema", "rollback"])
        self.assertEqual(panel.reconciler_instruction, "Prefer reversible steps.")

    def test_custom_roles_file_rejects_non_string_instruction(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            json.dump({"roles": [{"id": "x", "instruction": None}]}, handle)
            path = handle.name
        try:
            with self.assertRaises(council.CouncilError):
                council.compose_panel("auto", "task", "ctx", path)
        finally:
            os.remove(path)

    def test_duplicate_and_unsafe_ids_rejected(self):
        duplicate = council.PanelSpec(
            "panel",
            "Panel",
            (
                council.RoleSpec("same", "A", "one"),
                council.RoleSpec("same", "B", "two"),
            ),
        )
        with self.assertRaises(council.CouncilError):
            council.validate_panel(duplicate)
        unsafe = council.PanelSpec(
            "panel",
            "Panel",
            (council.RoleSpec("../escape", "A", "one"),),
        )
        with self.assertRaises(council.CouncilError):
            council.validate_panel(unsafe)

    def test_word_boundaries_avoid_api_false_positive(self):
        panel = council.compose_auto_panel(
            "Review capitalization", "No product-facing change"
        )
        self.assertNotIn("product-maintainer", [role.role_id for role in panel.roles])


class PrivateRunDirectoryTests(unittest.TestCase):
    def test_private_modes(self):
        with tempfile.TemporaryDirectory() as parent:
            with council.PrivateRunDirectory(parent, keep=True) as run_dir:
                path = run_dir.write_text("roles/a.md", "secret")
                self.assertEqual(stat.S_IMODE(os.stat(run_dir.path).st_mode), 0o700)
                self.assertEqual(
                    stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700
                )
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertTrue(os.path.isdir(run_dir.path))
            import shutil

            shutil.rmtree(run_dir.path)

    def test_path_traversal_rejected(self):
        with council.PrivateRunDirectory() as run_dir:
            with self.assertRaises(council.CouncilError):
                run_dir.write_text("../outside", "bad")

    def test_default_directory_removed(self):
        with council.PrivateRunDirectory() as run_dir:
            path = run_dir.path
            run_dir.write_text("x", "y")
        self.assertFalse(os.path.exists(path))


class CouncilStateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.patch_state = patch.object(council, "STATE_DIR", self.tempdir.name)
        self.patch_state.start()
        self.addCleanup(self.patch_state.stop)
        self.root = "/tmp/project"
        self.role = council.RoleSpec("architect", "Architect", "Review architecture")

    def test_state_path_is_project_session_role_scoped(self):
        a = council.role_state_path("architect", self.root, "s1")
        b = council.role_state_path("security", self.root, "s1")
        c = council.role_state_path("architect", self.root, "s2")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(a, council.role_state_path("architect", self.root, "s1"))

    def test_session_key_fallback(self):
        with patch.dict(
            os.environ,
            {
                "CLAUDE_OPINION_SESSION_KEY": "fallback",
                "CLAUDE_COUNCIL_SESSION_KEY": "",
            },
            clear=True,
        ):
            self.assertEqual(council.session_key(), "fallback")
        with patch.dict(
            os.environ,
            {
                "CLAUDE_OPINION_SESSION_KEY": "fallback",
                "CLAUDE_COUNCIL_SESSION_KEY": "preferred",
            },
            clear=True,
        ):
            self.assertEqual(council.session_key(), "preferred")

    def test_save_load_compare_clear(self):
        self.assertTrue(
            council.save_role_state(self.role, "s1", self.root, None, "key")
        )
        loaded = council.load_role_state(self.role, self.root, "key")
        self.assertEqual(loaded["session_id"], "s1")
        self.assertEqual(
            loaded["role_fingerprint"], council.role_fingerprint(self.role)
        )
        self.assertFalse(
            council.clear_role_state(self.role, self.root, "other", "key")
        )
        self.assertTrue(council.clear_role_state(self.role, self.root, "s1", "key"))
        self.assertIsNone(council.load_role_state(self.role, self.root, "key"))

    def test_compare_save_preserves_newer_state(self):
        council.save_role_state(self.role, "newer", self.root, None, "key")
        self.assertFalse(
            council.save_role_state(self.role, "mine", self.root, "older", "key")
        )
        self.assertEqual(
            council.load_role_state(self.role, self.root, "key")["session_id"],
            "newer",
        )

    def test_changed_mandate_quarantines_old_thread(self):
        council.save_role_state(self.role, "old", self.root, None, "key")
        changed = council.RoleSpec("architect", "Architect", "Different mandate")
        with patch("sys.stderr", io.StringIO()):
            self.assertIsNone(council.load_role_state(changed, self.root, "key"))
        self.assertTrue(
            any(".corrupt." in name for name in os.listdir(self.tempdir.name))
        )

    def test_corrupt_state_quarantined(self):
        path = council.role_state_path(self.role.role_id, self.root, "key")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("not json")
        with patch("sys.stderr", io.StringIO()):
            self.assertIsNone(council.load_role_state(self.role, self.root, "key"))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(
            len(
                [
                    name
                    for name in os.listdir(self.tempdir.name)
                    if ".corrupt." in name
                ]
            ),
            1,
        )

    def test_run_lock_private(self):
        path = council.run_lock_path(self.root, "key")
        with council.council_run_lock(self.root, "key"):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_state_symlink_not_followed(self):
        path = council.role_state_path(self.role.role_id, self.root, "key")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        target = os.path.join(self.tempdir.name, "target.json")
        with open(target, "w") as handle:
            json.dump({"session_id": "stolen"}, handle)
        os.symlink(target, path)
        if hasattr(os, "O_NOFOLLOW"):
            with self.assertRaises(council.CouncilError):
                council.load_role_state(self.role, self.root, "key")


class InvocationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.patch_state = patch.object(council, "STATE_DIR", self.tempdir.name)
        self.patch_state.start()
        self.addCleanup(self.patch_state.stop)
        self.role = council.RoleSpec("correctness", "Correctness", "Find bugs")
        self.registry = council.ProcessRegistry()

    @staticmethod
    def proc(payload, returncode=0, stderr=""):
        return council.ProcessResult(returncode, json.dumps(payload), stderr)

    def test_command_omits_fanout_budget_flags(self):
        with patch.object(council.transport, "_best_effort_level", return_value="max"):
            cmd = council.base_command("/project", "system")
        self.assertEqual(cmd.count("claude"), 1)
        self.assertIn("-p", cmd)
        for flag in council.FORBIDDEN_CLI_FLAGS:
            self.assertNotIn(flag, cmd)

    def test_fresh_then_resume(self):
        commands = []
        responses = iter(
            [
                self.proc(
                    {"result": "fresh", "is_error": False, "session_id": "s1"}
                ),
                self.proc(
                    {"result": "resume", "is_error": False, "session_id": "s1"}
                ),
            ]
        )

        def fake_run(invocation_id, cmd, prompt, project_root, registry):
            commands.append(cmd)
            return next(responses)

        with patch.object(council, "run_process", side_effect=fake_run), patch.object(
            council.transport, "_best_effort_level", return_value=None
        ):
            first = council.invoke_role(
                self.role, "task", "ctx", "/project", self.registry, "key"
            )
            second = council.invoke_role(
                self.role, "task2", "ctx2", "/project", self.registry, "key"
            )
        self.assertTrue(first.ok)
        self.assertFalse(first.resumed)
        self.assertTrue(second.ok)
        self.assertTrue(second.resumed)
        self.assertNotIn("--resume", commands[0])
        self.assertEqual(commands[1][-2:], ["--resume", "s1"])

    def test_stale_resume_retries_fresh(self):
        council.save_role_state(self.role, "old", "/project", None, "key")
        commands = []
        responses = iter(
            [
                council.ProcessResult(
                    1, "", "No conversation found with session ID: old"
                ),
                self.proc(
                    {"result": "new", "is_error": False, "session_id": "new"}
                ),
            ]
        )

        def fake_run(invocation_id, cmd, prompt, project_root, registry):
            commands.append(cmd)
            return next(responses)

        with patch.object(council, "run_process", side_effect=fake_run), patch.object(
            council.transport, "_best_effort_level", return_value=None
        ), patch("sys.stderr", io.StringIO()):
            outcome = council.invoke_role(
                self.role, "task", "ctx", "/project", self.registry, "key"
            )
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.stale_restarted)
        self.assertEqual(commands[0][-2:], ["--resume", "old"])
        self.assertNotIn("--resume", commands[1])
        self.assertEqual(
            council.load_role_state(self.role, "/project", "key")["session_id"],
            "new",
        )

    def test_non_stale_error_contained(self):
        with patch.object(
            council,
            "run_process",
            return_value=self.proc({"is_error": True, "result": "bad auth"}),
        ), patch.object(council.transport, "_best_effort_level", return_value=None):
            outcome = council.invoke_role(
                self.role, "task", "ctx", "/project", self.registry, "key"
            )
        self.assertFalse(outcome.ok)
        self.assertIn("bad auth", outcome.error)

    def test_invalid_json_error_is_clear(self):
        with patch.object(
            council,
            "run_process",
            return_value=council.ProcessResult(0, "not json", ""),
        ), patch.object(council.transport, "_best_effort_level", return_value=None):
            outcome = council.invoke_role(
                self.role, "task", "ctx", "/project", self.registry, "key"
            )
        self.assertFalse(outcome.ok)
        self.assertIn("valid JSON", outcome.error)

    def test_run_process_has_no_timeout_and_sets_cwd(self):
        proc = MagicMock()
        proc.pid = 123
        proc.returncode = 0
        proc.communicate.return_value = ("out", "err")
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured.update(kwargs)
            return proc

        with patch("claude_council.subprocess.Popen", side_effect=fake_popen):
            result = council.run_process(
                "role", ["claude", "-p"], "prompt", "/project", self.registry
            )
        self.assertEqual(result.stdout, "out")
        self.assertEqual(captured["cwd"], "/project")
        self.assertTrue(captured["start_new_session"])
        proc.communicate.assert_called_once_with(input="prompt")


class BoundedFanOutTests(unittest.TestCase):
    def test_peak_concurrency_never_exceeds_bound(self):
        roles = tuple(
            council.RoleSpec(f"role-{index}", f"Role {index}", "review")
            for index in range(6)
        )
        panel = council.PanelSpec("panel", "Panel", roles)
        lock = threading.Lock()
        active = 0
        peak = 0

        def fake_invoke(role, task, context, project_root, registry, key, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return council.RoleOutcome(role, True, text=role.role_id)

        with patch.object(council, "invoke_role", side_effect=fake_invoke):
            outcomes = council.run_panel(panel, "task", "ctx", "/project", 2)
        self.assertEqual(peak, 2)
        self.assertEqual(
            [outcome.role.role_id for outcome in outcomes],
            [role.role_id for role in roles],
        )

    def test_hard_limit_enforced(self):
        panel = council.PanelSpec(
            "panel", "Panel", (council.RoleSpec("a", "A", "review"),)
        )
        with self.assertRaises(council.CouncilError):
            council.run_panel(
                panel, "task", "ctx", "/project", council.MAX_PARALLEL + 1
            )


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.panel = council.PanelSpec(
            "panel",
            "Panel",
            (
                council.RoleSpec("a", "Architect", "architecture"),
                council.RoleSpec("b", "Skeptic", "skepticism"),
            ),
        )
        self.outcomes = [
            council.RoleOutcome(self.panel.roles[0], True, text="Use a lock."),
            council.RoleOutcome(self.panel.roles[1], False, error="session failed"),
        ]

    def test_context_preserves_success_failure_and_dissent_contract(self):
        prompt = council.build_reconciliation_context(
            self.panel, "Review", "Shared evidence", self.outcomes
        )
        self.assertIn("Use a lock.", prompt)
        self.assertIn("ROLE FAILED: session failed", prompt)
        self.assertIn("minority dissent", prompt)

    def test_chair_uses_prompt_overrides(self):
        captured = {}

        def fake_invoke(role, task, context, project_root, registry, key, **kwargs):
            captured.update(role=role, context=context, kwargs=kwargs, key=key)
            return council.RoleOutcome(role, True, text="final")

        with patch.object(council, "invoke_role", side_effect=fake_invoke):
            outcome = council.run_reconciler(
                self.panel,
                "Review",
                "Shared evidence",
                self.outcomes,
                "/project",
                council.ProcessRegistry(),
                "key",
            )
        self.assertTrue(outcome.ok)
        self.assertEqual(captured["role"].role_id, "council-chair")
        self.assertEqual(
            captured["kwargs"]["user_prompt_override"], captured["context"]
        )
        self.assertIn("Reconcile", captured["kwargs"]["system_prompt_override"])
        self.assertNotIn(
            "Do not synthesize", captured["kwargs"]["user_prompt_override"]
        )
        self.assertEqual(captured["key"], "key")

    def test_report_contains_chair_and_individual_reports(self):
        chair = council.RoleOutcome(
            council.RoleSpec("council-chair", "Council Chair", "reconcile"),
            True,
            text="Final decision.",
        )
        report = council.render_report(
            self.panel, "Review", self.outcomes, chair, "/project", 2
        )
        self.assertIn("# Claude Council Report", report)
        self.assertIn("Final decision.", report)
        self.assertIn("Use a lock.", report)
        self.assertIn("session failed", report)


class CancellationTests(unittest.TestCase):
    def test_registry_kills_process_group(self):
        proc = MagicMock()
        proc.pid = 999
        proc.communicate.return_value = ("", "")
        registry = council.ProcessRegistry()
        registry.add("role", proc)
        with patch("claude_council.os.killpg") as killpg:
            registry.terminate_all()
        killpg.assert_called_once_with(999, signal.SIGKILL)


class CliTests(unittest.TestCase):
    def test_list_panels_needs_no_claude_or_stdin(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = council.main(["--list-panels"])
        self.assertEqual(code, 0)
        self.assertIn("engineering", stdout.getvalue())

    def test_parallel_parser_rejects_out_of_range(self):
        for raw in ("0", str(council.MAX_PARALLEL + 1), "not-int"):
            with self.assertRaises(argparse.ArgumentTypeError):
                council.positive_parallel(raw)

    def test_main_runs_panel_then_chair(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        def fake_panel(panel, task, context, root, max_parallel, registry, key):
            return [
                council.RoleOutcome(role, True, text=f"report from {role.role_id}")
                for role in panel.roles
            ]

        def fake_chair(panel, task, context, outcomes, root, registry, key):
            return council.RoleOutcome(
                council.RoleSpec("council-chair", "Council Chair", "reconcile"),
                True,
                text="reconciled answer",
            )

        with tempfile.TemporaryDirectory() as state_dir, patch.object(
            council, "STATE_DIR", state_dir
        ), patch("claude_council.shutil.which", return_value="/usr/bin/claude"), patch.object(
            council, "canonical_project_root", return_value="/project"
        ), patch.object(council, "run_panel", side_effect=fake_panel), patch.object(
            council, "run_reconciler", side_effect=fake_chair
        ), patch("sys.stdin", io.StringIO("shared context")), patch(
            "sys.stdout", stdout
        ), patch("sys.stderr", stderr):
            code = council.main(["--panel", "minimal", "Review this"])
        self.assertEqual(code, 0)
        self.assertIn("reconciled answer", stdout.getvalue())
        self.assertIn("report from systems-architect", stdout.getvalue())
        self.assertIn("3 roles", stderr.getvalue())

    def test_all_role_failures_skip_chair_and_exit_one(self):
        stdout = io.StringIO()

        def fake_panel(panel, task, context, root, max_parallel, registry, key):
            return [
                council.RoleOutcome(role, False, error="failed")
                for role in panel.roles
            ]

        with tempfile.TemporaryDirectory() as state_dir, patch.object(
            council, "STATE_DIR", state_dir
        ), patch("claude_council.shutil.which", return_value="/usr/bin/claude"), patch.object(
            council, "canonical_project_root", return_value="/project"
        ), patch.object(council, "run_panel", side_effect=fake_panel), patch.object(
            council, "run_reconciler"
        ) as chair, patch("sys.stdin", io.StringIO("shared context")), patch(
            "sys.stdout", stdout
        ), patch("sys.stderr", io.StringIO()):
            code = council.main(["--panel", "minimal"])
        self.assertEqual(code, 1)
        chair.assert_not_called()
        self.assertIn("Role failed", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
