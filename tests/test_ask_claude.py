"""Unit tests for ask_claude.py.

Runs without the Claude Code CLI installed. Covers helper behavior:
auth env stripping, instruction composition, result parsing, stale-resume
detection (both exit paths), is_error surfacing, session key handling,
project key computation, state file I/O, and command shape.

Run from repo root:
    python3 -m unittest discover -s tests -p 'test_*.py'
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

SCRIPTS_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "scripts",
))
sys.path.insert(0, SCRIPTS_DIR)

import ask_claude  # noqa: E402


FIXED_PROJECT_ROOT = "/fixed/project/root"
FIXED_PROJECT_HASH = hashlib.sha256(FIXED_PROJECT_ROOT.encode()).hexdigest()[:16]


def _env_without(*names):
    return {k: v for k, v in os.environ.items() if k not in names}


class SessionKeyTests(unittest.TestCase):
    def test_unset_returns_empty(self):
        with patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
            self.assertEqual(ask_claude._session_key(), "")

    def test_value_returned(self):
        with patch.dict(os.environ, {"CLAUDE_OPINION_SESSION_KEY": "foo"}, clear=False):
            self.assertEqual(ask_claude._session_key(), "foo")

    def test_whitespace_stripped(self):
        with patch.dict(os.environ, {"CLAUDE_OPINION_SESSION_KEY": "  spaced  "}, clear=False):
            self.assertEqual(ask_claude._session_key(), "spaced")

    def test_whitespace_only_is_empty(self):
        with patch.dict(os.environ, {"CLAUDE_OPINION_SESSION_KEY": "   "}, clear=False):
            self.assertEqual(ask_claude._session_key(), "")


class ProjectKeyTests(unittest.TestCase):
    def test_no_session_key_just_project_hash(self):
        ask_claude._project_root.cache_clear()
        with patch.object(ask_claude, "_project_root", return_value=FIXED_PROJECT_ROOT), \
             patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
            self.assertEqual(ask_claude._project_key(), FIXED_PROJECT_HASH)

    def test_with_session_key_appends_suffix(self):
        ask_claude._project_root.cache_clear()
        with patch.object(ask_claude, "_project_root", return_value=FIXED_PROJECT_ROOT), \
             patch.dict(os.environ, {"CLAUDE_OPINION_SESSION_KEY": "branch-x"}, clear=False):
            key = ask_claude._project_key()
            self.assertTrue(key.startswith(FIXED_PROJECT_HASH + "-"))
            self.assertEqual(len(key), 16 + 1 + 16)

    def test_different_session_keys_produce_different_suffixes(self):
        ask_claude._project_root.cache_clear()
        with patch.object(ask_claude, "_project_root", return_value=FIXED_PROJECT_ROOT):
            with patch.dict(os.environ, {"CLAUDE_OPINION_SESSION_KEY": "a"}, clear=False):
                k1 = ask_claude._project_key()
            with patch.dict(os.environ, {"CLAUDE_OPINION_SESSION_KEY": "b"}, clear=False):
                k2 = ask_claude._project_key()
        self.assertNotEqual(k1, k2)


class StateFileRoundtripTests(unittest.TestCase):
    def test_save_then_load(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc123"), \
                 patch.object(ask_claude, "_project_root", return_value="/tmp/x"), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
                ask_claude.save_session("sess-1")
                sid, meta = ask_claude.load_session()
                self.assertEqual(sid, "sess-1")
                self.assertEqual(meta["session_id"], "sess-1")
                self.assertEqual(meta["project_path"], "/tmp/x")
                self.assertIn("updated_at", meta)

    def test_clear_removes_state(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc123"), \
                 patch.object(ask_claude, "_project_root", return_value="/tmp/x"), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
                ask_claude.save_session("sess-1")
                ask_claude.clear_session()
                sid, meta = ask_claude.load_session()
                self.assertIsNone(sid)


class SubprocessEnvTests(unittest.TestCase):
    def test_strips_api_key(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret", "FOO": "bar"}, clear=True):
            env = ask_claude._subprocess_env()
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env.get("FOO"), "bar")

    def test_strips_auth_token(self):
        with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "secret"}, clear=True):
            env = ask_claude._subprocess_env()
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)

    def test_strips_base_url(self):
        with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://proxy"}, clear=True):
            env = ask_claude._subprocess_env()
        self.assertNotIn("ANTHROPIC_BASE_URL", env)

    def test_keep_anthropic_env_opt_out_preserves_vars(self):
        patched = {
            "ANTHROPIC_API_KEY": "secret",
            "ANTHROPIC_AUTH_TOKEN": "token",
            "ANTHROPIC_BASE_URL": "url",
            "CLAUDE_OPINION_KEEP_ANTHROPIC_ENV": "1",
        }
        with patch.dict(os.environ, patched, clear=True):
            env = ask_claude._subprocess_env()
        self.assertEqual(env.get("ANTHROPIC_API_KEY"), "secret")
        self.assertEqual(env.get("ANTHROPIC_AUTH_TOKEN"), "token")
        self.assertEqual(env.get("ANTHROPIC_BASE_URL"), "url")

    def test_keep_anthropic_env_whitespace_treated_as_off(self):
        patched = {
            "ANTHROPIC_API_KEY": "secret",
            "CLAUDE_OPINION_KEEP_ANTHROPIC_ENV": "   ",
        }
        with patch.dict(os.environ, patched, clear=True):
            env = ask_claude._subprocess_env()
        self.assertNotIn("ANTHROPIC_API_KEY", env)


class InstructionCompositionTests(unittest.TestCase):
    def test_no_args_returns_default(self):
        self.assertEqual(
            ask_claude._instruction_from_args([]),
            ask_claude.DEFAULT_INSTRUCTION,
        )

    def test_custom_instruction_overrides_default(self):
        self.assertEqual(
            ask_claude._instruction_from_args(["review", "the", "diff"]),
            "review the diff",
        )

    def test_no_default_flag_returns_empty(self):
        self.assertEqual(
            ask_claude._instruction_from_args([ask_claude.NO_DEFAULT_FLAG]),
            "",
        )

    def test_no_default_with_custom_keeps_custom(self):
        self.assertEqual(
            ask_claude._instruction_from_args([ask_claude.NO_DEFAULT_FLAG, "override"]),
            "override",
        )


class EffortSelectionTests(unittest.TestCase):
    def test_prefers_max_when_available(self):
        help_text = "--effort <level> Effort level (low, medium, high, xhigh, max)"
        with patch.object(ask_claude, "_claude_help_text", return_value=help_text):
            self.assertEqual(ask_claude._best_effort_level(), "max")

    def test_falls_back_to_highest_available_effort(self):
        help_text = "--effort <level> Effort level (low, medium, high, xhigh)"
        with patch.object(ask_claude, "_claude_help_text", return_value=help_text):
            self.assertEqual(ask_claude._best_effort_level(), "xhigh")

    def test_reads_wrapped_effort_choices(self):
        help_text = "\n".join((
            "--effort <level> Effort level for the current session",
            "  (low, medium, high)",
            "--max-budget-usd <amount> Maximum dollar amount to spend",
        ))
        with patch.object(ask_claude, "_claude_help_text", return_value=help_text):
            self.assertEqual(ask_claude._best_effort_level(), "high")

    def test_omits_effort_when_cli_has_no_effort_flag(self):
        with patch.object(ask_claude, "_claude_help_text", return_value="Usage: claude"):
            self.assertIsNone(ask_claude._best_effort_level())

    def test_does_not_confuse_unrelated_max_flag_for_effort_level(self):
        help_text = "\n".join((
            "--effort <level> Effort level for the current session",
            "--max-budget-usd <amount> Maximum dollar amount to spend",
        ))
        with patch.object(ask_claude, "_claude_help_text", return_value=help_text):
            self.assertIsNone(ask_claude._best_effort_level())


class BaseCmdShapeTests(unittest.TestCase):
    def test_core_flags_present(self):
        with patch.object(ask_claude, "_best_effort_level", return_value="max"):
            cmd = ask_claude._base_cmd("/proj", "Review this")
        self.assertIn("claude", cmd)
        self.assertIn("-p", cmd)
        self.assertIn("--output-format", cmd)
        self.assertIn("json", cmd)
        self.assertIn("--effort", cmd)
        effort_idx = cmd.index("--effort")
        self.assertEqual(cmd[effort_idx + 1], "max")
        self.assertNotIn("--model", cmd)
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertIn("--add-dir", cmd)
        self.assertIn("/proj", cmd)
        self.assertNotIn("--bare", cmd)

    def test_append_system_prompt_included_when_provided(self):
        with patch.object(ask_claude, "_best_effort_level", return_value="max"):
            cmd = ask_claude._base_cmd("/proj", "Review this")
        self.assertIn("--append-system-prompt", cmd)
        idx = cmd.index("--append-system-prompt")
        self.assertEqual(cmd[idx + 1], "Review this")

    def test_append_system_prompt_omitted_when_empty(self):
        with patch.object(ask_claude, "_best_effort_level", return_value="max"):
            cmd = ask_claude._base_cmd("/proj", "")
        self.assertNotIn("--append-system-prompt", cmd)

    def test_does_not_include_verbose(self):
        # --output-format json does not require --verbose
        with patch.object(ask_claude, "_best_effort_level", return_value="max"):
            cmd = ask_claude._base_cmd("/proj", "foo")
        self.assertNotIn("--verbose", cmd)

    def test_omits_effort_when_no_supported_level_found(self):
        with patch.object(ask_claude, "_best_effort_level", return_value=None):
            cmd = ask_claude._base_cmd("/proj", "foo")
        self.assertNotIn("--effort", cmd)


class ResultParsingTests(unittest.TestCase):
    def test_valid_json_returns_dict(self):
        stdout = json.dumps({"result": "hi", "is_error": False, "session_id": "x"})
        self.assertEqual(ask_claude._parse_result(stdout)["result"], "hi")

    def test_invalid_json_returns_none(self):
        self.assertIsNone(ask_claude._parse_result("not json"))

    def test_non_object_returns_none(self):
        self.assertIsNone(ask_claude._parse_result(json.dumps([1, 2, 3])))


class StaleResumeDetectionTests(unittest.TestCase):
    def test_stale_marker_in_stderr(self):
        self.assertTrue(ask_claude._stale_marker_match(
            "No conversation found with session ID: abc"
        ))

    def test_stale_marker_variant_conversation_not_found(self):
        self.assertTrue(ask_claude._stale_marker_match("Conversation not found"))

    def test_stale_marker_variant_session_not_found(self):
        self.assertTrue(ask_claude._stale_marker_match("session not found"))

    def test_unrelated_error_not_stale(self):
        self.assertFalse(ask_claude._stale_marker_match("Credit balance is too low"))

    def test_is_stale_resume_detects_from_result_errors_array(self):
        result = {
            "is_error": True,
            "errors": ["No conversation found with session ID: xxx"],
        }
        self.assertTrue(ask_claude._is_stale_resume(result))

    def test_is_stale_resume_requires_is_error_flag(self):
        result = {
            "is_error": False,
            "errors": ["No conversation found with session ID: xxx"],
        }
        self.assertFalse(ask_claude._is_stale_resume(result))

    def test_is_stale_resume_on_non_stale_error(self):
        result = {"is_error": True, "result": "Credit balance is too low"}
        self.assertFalse(ask_claude._is_stale_resume(result))

    def test_empty_stdin_returns_none_from_parse(self):
        self.assertIsNone(ask_claude._parse_result(""))


class RunClaudeFreshPathTests(unittest.TestCase):
    """Exercise run_claude's fresh-session branch end-to-end with a mocked subprocess."""

    def _fake_proc(self, stdout, returncode=0, stderr=""):
        m = MagicMock()
        m.stdout = stdout
        m.returncode = returncode
        m.stderr = stderr
        return m

    def test_success_saves_session_and_returns_text(self):
        fake_stdout = json.dumps({"result": "hello", "is_error": False})
        fake_proc = self._fake_proc(fake_stdout)
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.object(ask_claude, "_run_claude_proc", return_value=fake_proc):
                text = ask_claude.run_claude("ctx", "directive")
                self.assertEqual(text, "hello")
                sid, _ = ask_claude.load_session()
                self.assertTrue(sid)

    def test_is_error_exits_non_zero_and_does_not_save(self):
        fake_stdout = json.dumps({"result": "Credit balance is too low", "is_error": True})
        fake_proc = self._fake_proc(fake_stdout)
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.object(ask_claude, "_run_claude_proc", return_value=fake_proc):
                with self.assertRaises(SystemExit) as cm:
                    ask_claude.run_claude("ctx", "directive")
                self.assertEqual(cm.exception.code, 1)
                sid, _ = ask_claude.load_session()
                self.assertIsNone(sid)  # session NOT saved on error

    def test_non_zero_exit_surfaces(self):
        fake_proc = self._fake_proc("", returncode=2, stderr="something went wrong")
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.object(ask_claude, "_run_claude_proc", return_value=fake_proc):
                with self.assertRaises(SystemExit) as cm:
                    ask_claude.run_claude("ctx", "directive")
                self.assertEqual(cm.exception.code, 1)


class RunClaudeResumeStaleFallbackTests(unittest.TestCase):
    """Both stale-resume signatures (exit 1 stderr vs exit 0 result.is_error) must fall through to fresh."""

    def _fake_proc(self, stdout, returncode=0, stderr=""):
        m = MagicMock()
        m.stdout = stdout
        m.returncode = returncode
        m.stderr = stderr
        return m

    def test_stale_via_stderr_exit_one_falls_through_to_fresh(self):
        stale_proc = self._fake_proc("", returncode=1, stderr="No conversation found with session ID: old")
        fresh_stdout = json.dumps({"result": "new answer", "is_error": False})
        fresh_proc = self._fake_proc(fresh_stdout)
        procs = iter([stale_proc, fresh_proc])
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.object(ask_claude, "_run_claude_proc", side_effect=lambda *a, **kw: next(procs)), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
                # Seed state with the stale session
                ask_claude.save_session("old")
                text = ask_claude.run_claude("ctx", "directive")
                self.assertEqual(text, "new answer")
                # Fresh UUID should now be stored (not "old")
                sid, _ = ask_claude.load_session()
                self.assertIsNotNone(sid)
                self.assertNotEqual(sid, "old")

    def test_stale_via_result_errors_exit_zero_falls_through_to_fresh(self):
        stale_stdout = json.dumps({
            "is_error": True,
            "errors": ["No conversation found with session ID: old"],
            "subtype": "error_during_execution",
        })
        stale_proc = self._fake_proc(stale_stdout, returncode=0)
        fresh_stdout = json.dumps({"result": "new answer", "is_error": False})
        fresh_proc = self._fake_proc(fresh_stdout)
        procs = iter([stale_proc, fresh_proc])
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.object(ask_claude, "_run_claude_proc", side_effect=lambda *a, **kw: next(procs)), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
                ask_claude.save_session("old")
                text = ask_claude.run_claude("ctx", "directive")
                self.assertEqual(text, "new answer")


if __name__ == "__main__":
    unittest.main()
