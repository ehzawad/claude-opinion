"""Unit tests for ask_claude.py.

Runs without the Claude Code CLI installed. Covers helper behavior:
auth env stripping, instruction composition, result parsing, stale-resume
detection (both exit paths), is_error surfacing, session key handling,
project key computation, state file I/O, and command shape.

Run from repo root:
    python3 -m unittest discover -s tests -p 'test_*.py'
"""

import io
import hashlib
import json
import os
import subprocess
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

    def test_clear_with_matching_expected_removes(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
                ask_claude.save_session("xxx")
                ask_claude.clear_session(expected_session_id="xxx")
                sid, _ = ask_claude.load_session()
                self.assertIsNone(sid)

    def test_load_quarantines_corrupt_state(self):
        # Corrupt state must NOT silently doom-loop subsequent calls;
        # quarantine the bad file, warn with the full path, return None,
        # and let the next save persist a fresh session_id normally.
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
                state_path = ask_claude._state_path()
                with open(state_path, "w") as f:
                    f.write("not valid json {{{")
                stderr = io.StringIO()
                with patch("sys.stderr", stderr):
                    sid, meta = ask_claude.load_session()
                self.assertIsNone(sid)
                self.assertIsNone(meta)
                self.assertFalse(os.path.exists(state_path))
                quarantined = [n for n in os.listdir(td)
                               if n.startswith(os.path.basename(state_path) + ".corrupt.")]
                self.assertEqual(len(quarantined), 1)
                with open(os.path.join(td, quarantined[0])) as f:
                    self.assertEqual(f.read(), "not valid json {{{")
                err = stderr.getvalue()
                self.assertIn(state_path, err)
                self.assertIn("corrupt", err.lower())

    def test_load_after_quarantine_then_save_persists_fresh(self):
        # End-to-end: corrupt state on disk → load returns None & quarantines →
        # subsequent save with expected_prior=None succeeds (no doom loop).
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True), \
                 patch("sys.stderr", io.StringIO()):
                with open(ask_claude._state_path(), "w") as f:
                    f.write("garbage")
                ask_claude.load_session()  # triggers quarantine
                wrote = ask_claude.save_session("fresh", expected_prior=None)
                self.assertTrue(wrote)
                sid, _ = ask_claude.load_session()
                self.assertEqual(sid, "fresh")

    def test_clear_with_mismatched_expected_keeps(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
                ask_claude.save_session("yyy")
                ask_claude.clear_session(expected_session_id="xxx")
                sid, _ = ask_claude.load_session()
                self.assertEqual(sid, "yyy")


class CompareAndSaveTests(unittest.TestCase):
    """Generation-aware save_session — closes save-races on both fresh and
    resume-success paths where parallel invocations would otherwise
    overwrite each other's session IDs."""

    def _ctx(self, td):
        return [
            patch.object(ask_claude, "STATE_DIR", td),
            patch.object(ask_claude, "_project_key", return_value="abc"),
            patch.object(ask_claude, "_project_root", return_value="/p"),
            patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True),
        ]

    def test_unconditional_save_overwrites(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
                self.assertTrue(ask_claude.save_session("first"))
                self.assertTrue(ask_claude.save_session("second"))
                sid, _ = ask_claude.load_session()
                self.assertEqual(sid, "second")

    def test_save_with_matching_prior_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
                ask_claude.save_session("old")
                wrote = ask_claude.save_session("new", expected_prior="old")
                self.assertTrue(wrote)
                sid, _ = ask_claude.load_session()
                self.assertEqual(sid, "new")

    def test_save_with_mismatched_prior_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
                ask_claude.save_session("recent-other")  # concurrent winner
                wrote = ask_claude.save_session("mine", expected_prior="old")
                self.assertFalse(wrote)
                sid, _ = ask_claude.load_session()
                self.assertEqual(sid, "recent-other")  # not clobbered

    def test_save_with_empty_state_and_any_prior_succeeds(self):
        # Empty state means "no concurrent writer", regardless of what we
        # observed at entry.
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
                wrote = ask_claude.save_session("mine", expected_prior="anything")
                self.assertTrue(wrote)
                sid, _ = ask_claude.load_session()
                self.assertEqual(sid, "mine")

    def test_save_with_none_prior_and_empty_state_succeeds(self):
        # No prior observed AND no state → first writer wins
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
                wrote = ask_claude.save_session("mine", expected_prior=None)
                self.assertTrue(wrote)
                sid, _ = ask_claude.load_session()
                self.assertEqual(sid, "mine")

    def test_save_with_none_prior_and_other_state_refuses(self):
        # We saw nothing at entry but someone else has written → refuse
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
                ask_claude.save_session("recent-other")
                wrote = ask_claude.save_session("mine", expected_prior=None)
                self.assertFalse(wrote)
                sid, _ = ask_claude.load_session()
                self.assertEqual(sid, "recent-other")

    def test_save_with_current_matching_session_id_is_noop_success(self):
        # Sibling racers landing on the same rotated ID: state already
        # holds what we'd write, so accept it as a no-op success rather
        # than firing the "concurrent overwrite" warning.
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
                ask_claude.save_session("rotated")  # racer A wrote first
                # Racer B observed prior="old", got rotated_id="rotated", state already holds it.
                wrote = ask_claude.save_session("rotated", expected_prior="old")
                self.assertTrue(wrote)
                sid, _ = ask_claude.load_session()
                self.assertEqual(sid, "rotated")

    def test_save_refuses_on_corrupt_state_with_prior(self):
        # Atomic os.replace means this script cannot produce invalid JSON
        # itself. If we encounter it, refuse rather than silently overwrite —
        # external interference shouldn't be masked.
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
                with open(ask_claude._state_path(), "w") as f:
                    f.write("not valid json {{{")
                stderr = io.StringIO()
                with patch("sys.stderr", stderr):
                    wrote = ask_claude.save_session("mine", expected_prior="old")
                self.assertFalse(wrote)
                self.assertIn("unreadable", stderr.getvalue().lower())
                # Corrupt file must be left in place for human inspection.
                with open(ask_claude._state_path()) as f:
                    self.assertEqual(f.read(), "not valid json {{{")

    def test_unconditional_save_ignores_corrupt_existing_state(self):
        # When called without expected_prior the function bypasses the read,
        # so corruption isn't a concern — the atomic write replaces it.
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
                with open(ask_claude._state_path(), "w") as f:
                    f.write("garbage")
                wrote = ask_claude.save_session("clean")
                self.assertTrue(wrote)
                sid, _ = ask_claude.load_session()
                self.assertEqual(sid, "clean")


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
    def test_no_args_returns_default_plus_safety(self):
        result = ask_claude._instruction_from_args([])
        self.assertIn(ask_claude.DEFAULT_INSTRUCTION, result)
        self.assertIn(ask_claude.SAFETY_DIRECTIVE, result)

    def test_custom_instruction_keeps_custom_plus_safety(self):
        result = ask_claude._instruction_from_args(["review", "the", "diff"])
        self.assertIn("review the diff", result)
        self.assertIn(ask_claude.SAFETY_DIRECTIVE, result)

    def test_no_default_flag_returns_empty(self):
        # Explicit opt-out of any system prompt — including safety
        self.assertEqual(
            ask_claude._instruction_from_args([ask_claude.NO_DEFAULT_FLAG]),
            "",
        )

    def test_no_default_with_custom_appends_safety(self):
        # --no-default-instruction is moot when a custom instruction is
        # provided; safety still applies because the user didn't ask for
        # raw passthrough.
        result = ask_claude._instruction_from_args([ask_claude.NO_DEFAULT_FLAG, "override"])
        self.assertIn("override", result)
        self.assertIn(ask_claude.SAFETY_DIRECTIVE, result)

    def test_allow_edit_strips_safety_from_default(self):
        result = ask_claude._instruction_from_args([ask_claude.ALLOW_EDIT_FLAG])
        self.assertEqual(result, ask_claude.DEFAULT_INSTRUCTION)
        self.assertNotIn(ask_claude.SAFETY_DIRECTIVE, result)

    def test_allow_edit_strips_safety_from_custom(self):
        result = ask_claude._instruction_from_args([
            ask_claude.ALLOW_EDIT_FLAG, "review", "and", "fix",
        ])
        self.assertEqual(result, "review and fix")
        self.assertNotIn(ask_claude.SAFETY_DIRECTIVE, result)

    def test_allow_edit_with_no_default_returns_empty(self):
        # Both opt-outs together → fully raw passthrough
        result = ask_claude._instruction_from_args([
            ask_claude.NO_DEFAULT_FLAG, ask_claude.ALLOW_EDIT_FLAG,
        ])
        self.assertEqual(result, "")

    def test_safety_directive_actually_says_no_modify(self):
        lowered = ask_claude.SAFETY_DIRECTIVE.lower()
        self.assertIn("not modify files", lowered)
        self.assertIn("analysis only", lowered)


class SubprocessTimeoutTests(unittest.TestCase):
    def test_default_timeout_when_unset(self):
        with patch.dict(os.environ, _env_without("CLAUDE_OPINION_TIMEOUT"), clear=True):
            self.assertEqual(ask_claude._subprocess_timeout(), ask_claude._DEFAULT_TIMEOUT_SECONDS)

    def test_timeout_override(self):
        with patch.dict(os.environ, {"CLAUDE_OPINION_TIMEOUT": "120"}, clear=True):
            self.assertEqual(ask_claude._subprocess_timeout(), 120)

    def test_invalid_timeout_falls_back_to_default(self):
        with patch.dict(os.environ, {"CLAUDE_OPINION_TIMEOUT": "abc"}, clear=True):
            self.assertEqual(ask_claude._subprocess_timeout(), ask_claude._DEFAULT_TIMEOUT_SECONDS)

    def test_zero_or_negative_falls_back_to_default(self):
        for value in ("0", "-5"):
            with patch.dict(os.environ, {"CLAUDE_OPINION_TIMEOUT": value}, clear=True):
                self.assertEqual(ask_claude._subprocess_timeout(), ask_claude._DEFAULT_TIMEOUT_SECONDS)

    def test_run_claude_proc_kills_process_group_on_timeout(self):
        # Popen + start_new_session=True + os.killpg → bounded process tree.
        import signal as _signal

        fake_proc = MagicMock()
        fake_proc.pid = 99999
        fake_proc.returncode = -9
        # First communicate (with input) raises; second (drain after kill)
        # returns cleanly.
        fake_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd=["claude"], timeout=1),
            ("", ""),
        ]

        killpg_calls = []
        stderr = io.StringIO()
        with patch("ask_claude.subprocess.Popen", return_value=fake_proc), \
             patch("ask_claude.os.killpg", side_effect=lambda pid, sig: killpg_calls.append((pid, sig))), \
             patch.dict(os.environ, {"CLAUDE_OPINION_TIMEOUT": "1"}, clear=True), \
             patch("sys.stderr", stderr):
            with self.assertRaises(SystemExit) as cm:
                ask_claude._run_claude_proc(["claude", "-p"], "ctx")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("timeout", stderr.getvalue().lower())
        self.assertEqual(killpg_calls, [(99999, _signal.SIGKILL)])

    def test_claude_help_text_returns_empty_on_timeout(self):
        # `claude --help` must not be allowed to wedge the script before the
        # protected `claude -p` invocation.
        ask_claude._claude_help_text.cache_clear()
        try:
            with patch("ask_claude.subprocess.run",
                       side_effect=subprocess.TimeoutExpired(
                           cmd=["claude", "--help"], timeout=10,
                       )):
                self.assertEqual(ask_claude._claude_help_text(), "")
        finally:
            ask_claude._claude_help_text.cache_clear()

    def test_run_claude_proc_uses_start_new_session(self):
        # Process-tree timeout depends on Popen creating a new process group
        # (start_new_session=True); without it os.killpg can't reach
        # grandchildren spawned by claude during -p execution.
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.communicate.return_value = ("out", "")
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured.update(kwargs)
            return fake_proc

        with patch("ask_claude.subprocess.Popen", side_effect=fake_popen), \
             patch.dict(os.environ, _env_without("CLAUDE_OPINION_TIMEOUT"), clear=True):
            ask_claude._run_claude_proc(["claude", "-p"], "ctx")
        self.assertTrue(captured.get("start_new_session"))


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

    def test_success_with_session_id_saves_and_returns_text(self):
        fake_stdout = json.dumps({
            "result": "hello", "is_error": False, "session_id": "fresh-uuid",
        })
        fake_proc = self._fake_proc(fake_stdout)
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.object(ask_claude, "_run_claude_proc", return_value=fake_proc):
                text = ask_claude.run_claude("ctx", "directive")
                self.assertEqual(text, "hello")
                sid, _ = ask_claude.load_session()
                self.assertEqual(sid, "fresh-uuid")

    def test_success_without_session_id_warns_and_returns_text(self):
        # Don't discard the user's answer just because Claude didn't surface a
        # session_id; warn, return text, skip persistence.
        fake_stdout = json.dumps({"result": "hello", "is_error": False})
        fake_proc = self._fake_proc(fake_stdout)
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.object(ask_claude, "_run_claude_proc", return_value=fake_proc), \
                 patch("sys.stderr", stderr):
                text = ask_claude.run_claude("ctx", "directive")
                self.assertEqual(text, "hello")
                sid, _ = ask_claude.load_session()
                self.assertIsNone(sid)
        self.assertIn("no session_id", stderr.getvalue())

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

    def test_fresh_save_refuses_to_clobber_concurrent_writer(self):
        # Simulate a concurrent invocation: while our claude -p is running,
        # another process writes a different fresh session_id. We should
        # return our text but NOT overwrite the state.
        fake_stdout = json.dumps({
            "result": "mine", "is_error": False, "session_id": "fresh-mine",
        })
        fake_proc = self._fake_proc(fake_stdout)

        def side_effect_with_concurrent_write(*args, **kwargs):
            ask_claude.save_session("fresh-other")
            return fake_proc

        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.object(ask_claude, "_run_claude_proc",
                              side_effect=side_effect_with_concurrent_write), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True), \
                 patch("sys.stderr", stderr):
                text = ask_claude.run_claude("ctx", "directive")
                self.assertEqual(text, "mine")
                sid, _ = ask_claude.load_session()
                self.assertEqual(sid, "fresh-other")  # not clobbered
        self.assertIn("Concurrent invocation", stderr.getvalue())


class RunClaudeResumeSuccessTests(unittest.TestCase):
    """Resume-success path also uses compare-and-save so a sibling
    invocation that has written a different ID isn't clobbered."""

    def _fake_proc(self, stdout, returncode=0, stderr=""):
        m = MagicMock()
        m.stdout = stdout
        m.returncode = returncode
        m.stderr = stderr
        return m

    def test_resume_success_save_succeeds_when_state_unchanged(self):
        # Vanilla resume-success: state still holds "old" at exit, so the
        # compare-and-save with expected_prior="old" passes.
        fake_stdout = json.dumps({
            "result": "resumed answer", "is_error": False, "session_id": "old-rotated",
        })
        fake_proc = self._fake_proc(fake_stdout)
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.object(ask_claude, "_run_claude_proc", return_value=fake_proc), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True):
                ask_claude.save_session("old")
                text = ask_claude.run_claude("ctx", "directive")
                self.assertEqual(text, "resumed answer")
                sid, _ = ask_claude.load_session()
                self.assertEqual(sid, "old-rotated")

    def test_resume_success_refuses_to_clobber_concurrent_writer(self):
        # While we're blocked in claude --resume, another invocation persists
        # a different fresh/resumed ID. We return our text but must NOT
        # overwrite the state with our (now-shadowed) resume result.
        fake_stdout = json.dumps({
            "result": "resumed answer", "is_error": False, "session_id": "old-rotated",
        })
        fake_proc = self._fake_proc(fake_stdout)

        def side_effect_with_concurrent_write(*args, **kwargs):
            ask_claude.save_session("fresh-other")
            return fake_proc

        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ask_claude, "STATE_DIR", td), \
                 patch.object(ask_claude, "_project_key", return_value="abc"), \
                 patch.object(ask_claude, "_project_root", return_value="/p"), \
                 patch.object(ask_claude, "_run_claude_proc",
                              side_effect=side_effect_with_concurrent_write), \
                 patch.dict(os.environ, _env_without("CLAUDE_OPINION_SESSION_KEY"), clear=True), \
                 patch("sys.stderr", stderr):
                ask_claude.save_session("old")
                text = ask_claude.run_claude("ctx", "directive")
                self.assertEqual(text, "resumed answer")
                sid, _ = ask_claude.load_session()
                self.assertEqual(sid, "fresh-other")  # not clobbered
        self.assertIn("Concurrent invocation", stderr.getvalue())


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
        fresh_stdout = json.dumps({
            "result": "new answer", "is_error": False, "session_id": "fresh-uuid",
        })
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
                self.assertEqual(sid, "fresh-uuid")

    def test_stale_via_result_errors_exit_zero_falls_through_to_fresh(self):
        stale_stdout = json.dumps({
            "is_error": True,
            "errors": ["No conversation found with session ID: old"],
            "subtype": "error_during_execution",
        })
        stale_proc = self._fake_proc(stale_stdout, returncode=0)
        fresh_stdout = json.dumps({
            "result": "new answer", "is_error": False, "session_id": "fresh-uuid",
        })
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
                sid, _ = ask_claude.load_session()
                self.assertEqual(sid, "fresh-uuid")


if __name__ == "__main__":
    unittest.main()
