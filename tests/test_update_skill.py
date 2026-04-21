"""Unit tests for update_skill.py."""

import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "scripts",
))
sys.path.insert(0, SCRIPTS_DIR)

import update_skill  # noqa: E402


class HelperTests(unittest.TestCase):
    def test_default_branch_from_remote_head(self):
        self.assertEqual(update_skill._default_branch_from_remote_head("origin/main"), "main")

    def test_default_branch_missing_separator(self):
        self.assertIsNone(update_skill._default_branch_from_remote_head("main"))

    def test_is_dirty(self):
        self.assertTrue(update_skill._is_dirty(" M scripts/update_skill.py\n"))
        self.assertFalse(update_skill._is_dirty(""))
        self.assertFalse(update_skill._is_dirty("   \n"))

    def test_repo_root_uses_realpath(self):
        with tempfile.TemporaryDirectory() as td:
            repo = os.path.join(td, "repo")
            scripts = os.path.join(repo, "scripts")
            os.makedirs(scripts)
            real_script = os.path.join(scripts, "update_skill.py")
            link_path = os.path.join(td, "update-link.py")
            with open(real_script, "w", encoding="utf-8") as f:
                f.write("#!/usr/bin/env python3\n")
            os.symlink(real_script, link_path)
            self.assertEqual(update_skill._repo_root(link_path), os.path.realpath(repo))


class MainFlowTests(unittest.TestCase):
    def test_main_refuses_dirty_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            os.mkdir(os.path.join(td, ".git"))

            def fake_git(repo_root, args, check=True):
                class Proc:
                    stdout = " M scripts/update_skill.py\n"
                    stderr = ""
                    returncode = 0
                return Proc()

            stderr = io.StringIO()
            with patch.object(update_skill, "_repo_root", return_value=td), \
                 patch.object(update_skill, "_git", side_effect=fake_git), \
                 patch("sys.stderr", stderr):
                with self.assertRaises(SystemExit) as cm:
                    update_skill.main([])
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("Local changes detected", stderr.getvalue())

    def test_main_reports_up_to_date(self):
        with tempfile.TemporaryDirectory() as td:
            os.mkdir(os.path.join(td, ".git"))
            responses = {
                ("status", "--porcelain"): "",
                ("symbolic-ref", "--quiet", "--short", "HEAD"): "main\n",
                ("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"): "origin/main\n",
                ("rev-parse", "--short", "HEAD"): "abc123\n",
                ("fetch", "origin", "--tags"): "",
                ("pull", "--ff-only", "origin", "main"): "Already up to date.\n",
                ("describe", "--tags", "--always"): "abc123\n",
            }

            def fake_git(repo_root, args, check=True):
                class Proc:
                    stdout = responses[tuple(args)]
                    stderr = ""
                    returncode = 0
                return Proc()

            stdout = io.StringIO()
            with patch.object(update_skill, "_repo_root", return_value=td), \
                 patch.object(update_skill, "_git", side_effect=fake_git), \
                 patch("sys.stdout", stdout):
                update_skill.main([])
            self.assertIn("Already up to date", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
