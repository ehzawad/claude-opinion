"""Panel, staging, and state tests for the Claude council."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, SCRIPTS_DIR)

import claude_council as council  # noqa: E402
import _council_panels as panels  # noqa: E402
import _council_staging as staging  # noqa: E402
import _council_state as state  # noqa: E402


class PanelCompositionTests(unittest.TestCase):
    def test_auto_panel_selects_task_specific_roles(self):
        panel = council.compose_panel(
            "auto",
            "Review this authentication session migration and its concurrency tests",
            "The implementation uses locks, tokens, retries, and integration tests.",
        )
        ids = [role.role_id for role in panel.roles]
        self.assertEqual(ids[0:2], ["systems-architect", "correctness-reviewer"])
        self.assertIn("security-reviewer", ids)
        self.assertIn("reliability-operator", ids)
        self.assertIn("test-strategist", ids)
        self.assertIn("product-maintainer", ids)
        self.assertEqual(ids[-1], "adversarial-skeptic")
        self.assertEqual(len(ids), len(set(ids)))

    def test_builtin_panel_is_valid_and_deterministic(self):
        first = council.compose_panel("engineering", "task", "context")
        second = council.compose_panel("engineering", "other", "different")
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first.roles), 5)

    def test_roles_file_supports_custom_panel_and_chair_instruction(self):
        payload = {
            "id": "migration-panel",
            "name": "Migration Panel",
            "reconciler_instruction": "Prefer reversible steps.",
            "roles": [
                {"id": "schema", "name": "Schema", "instruction": "Review schema changes."},
                {"id": "rollback", "name": "Rollback", "instruction": "Review rollback safety."},
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

    def test_roles_file_rejects_non_string_fields(self):
        payload = {"roles": [{"id": "a", "instruction": None}]}
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            json.dump(payload, handle)
            path = handle.name
        try:
            with self.assertRaises(council.CouncilError):
                council.compose_panel("auto", "task", "ctx", path)
        finally:
            os.remove(path)

    def test_duplicate_or_unsafe_role_ids_are_rejected(self):
        duplicate = council.PanelSpec(
            "x",
            "x",
            (
                council.RoleSpec("same", "A", "one"),
                council.RoleSpec("same", "B", "two"),
            ),
        )
        with self.assertRaises(council.CouncilError):
            panels._validate_panel(duplicate)
        unsafe = council.PanelSpec(
            "x",
            "x",
            (council.RoleSpec("../escape", "A", "one"),),
        )
        with self.assertRaises(council.CouncilError):
            panels._validate_panel(unsafe)


class PrivateRunDirectoryTests(unittest.TestCase):
    def test_directory_and_files_are_private(self):
        with tempfile.TemporaryDirectory() as parent:
            with staging.PrivateRunDirectory(parent, keep=True) as run_dir:
                path = run_dir.write_text("roles/a.md", "secret")
                self.assertEqual(stat.S_IMODE(os.stat(run_dir.path).st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertTrue(os.path.isdir(run_dir.path))
            import shutil
            shutil.rmtree(run_dir.path)

    def test_path_traversal_is_rejected(self):
        with staging.PrivateRunDirectory() as run_dir:
            with self.assertRaises(council.CouncilError):
                run_dir.write_text("../outside", "no")


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.state_patch = patch.object(state, "COUNCIL_STATE_DIR", self.tempdir.name)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.role = council.RoleSpec("architect", "Architect", "Review architecture")
        self.root = "/tmp/project"

    def test_role_state_is_scoped_by_project_session_and_role(self):
        a = state._role_state_path("architect", self.root, "s1")
        b = state._role_state_path("security", self.root, "s1")
        c = state._role_state_path("architect", self.root, "s2")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(a, state._role_state_path("architect", self.root, "s1"))

    def test_save_load_and_compare_clear(self):
        self.assertTrue(state.save_role_state(self.role, "sess-1", self.root, None, "key"))
        loaded = state.load_role_state(self.role, self.root, "key")
        self.assertEqual(loaded["session_id"], "sess-1")
        self.assertFalse(state.clear_role_state(self.role, self.root, "other", "key"))
        self.assertIsNotNone(state.load_role_state(self.role, self.root, "key"))
        self.assertTrue(state.clear_role_state(self.role, self.root, "sess-1", "key"))
        self.assertIsNone(state.load_role_state(self.role, self.root, "key"))

    def test_compare_save_does_not_clobber_newer_state(self):
        state.save_role_state(self.role, "newer", self.root, None, "key")
        self.assertFalse(
            state.save_role_state(self.role, "mine", self.root, "older", "key")
        )
        self.assertEqual(
            state.load_role_state(self.role, self.root, "key")["session_id"],
            "newer",
        )

    def test_changed_role_instruction_starts_a_fresh_thread(self):
        state.save_role_state(self.role, "old", self.root, None, "key")
        changed = council.RoleSpec("architect", "Architect", "A different mandate")
        with patch("sys.stderr", io.StringIO()):
            self.assertIsNone(state.load_role_state(changed, self.root, "key"))
        state_path = state._role_state_path(changed.role_id, self.root, "key")
        self.assertFalse(os.path.exists(state_path))
        self.assertTrue(any(".corrupt." in name for name in os.listdir(self.tempdir.name)))

    def test_state_reader_refuses_symlink(self):
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("O_NOFOLLOW is unavailable")
        path = state._role_state_path(self.role.role_id, self.root, "key")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        target = os.path.join(self.tempdir.name, "target.json")
        with open(target, "w") as handle:
            json.dump({"session_id": "stolen"}, handle)
        os.symlink(target, path)
        with self.assertRaises(council.CouncilError):
            state.load_role_state(self.role, self.root, "key")

    def test_corrupt_state_is_quarantined(self):
        path = state._role_state_path(self.role.role_id, self.root, "key")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("not json")
        with patch("sys.stderr", io.StringIO()):
            self.assertIsNone(state.load_role_state(self.role, self.root, "key"))
        self.assertFalse(os.path.exists(path))
        quarantined = [name for name in os.listdir(self.tempdir.name) if ".corrupt." in name]
        self.assertEqual(len(quarantined), 1)

    def test_council_run_lock_is_private(self):
        path = state._council_run_lock_path(self.root, "key")
        with state.council_run_lock(self.root, "key"):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
