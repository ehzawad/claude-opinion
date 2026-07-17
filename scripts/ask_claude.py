#!/usr/bin/env python3
"""Unbounded, project-scoped single-agent entry point for claude-opinion.

The historical transport implementation lives in ``_ask_claude_core.py``.
This entry point keeps its mature JSON/session/error handling while replacing
its execution policy:

* no wrapper wall-clock timeout for ``claude --help`` or ``claude -p``;
* no wrapper input/output, turn, or budget cap;
* one canonical working directory per Git worktree/project;
* one serialized top-level Claude process per project/session key.

The only normal completion conditions are Claude exiting or the caller
explicitly interrupting the process. Host, OS, Claude CLI, account, and model
limits still exist outside this wrapper.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import subprocess
import sys
from functools import cache
from typing import Iterator

import _ask_claude_core as _core


_ORIGINAL_RUN_CLAUDE = _core.run_claude

# Preserve the original preference for maximum explicit effort while accepting
# newer CLI help text that advertises ultracode.
_core._EFFORT_LEVELS = (
    "auto",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultracode",
)
_core._EFFORT_PREFERENCE = (
    "max",
    "ultracode",
    "xhigh",
    "high",
    "medium",
    "low",
    "auto",
)


@cache
def _project_root() -> str:
    """Return the canonical Git worktree root, or canonical cwd outside Git."""

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        root = proc.stdout.strip() if proc.returncode == 0 else ""
    except OSError:
        root = ""
    return os.path.realpath(root or os.getcwd())


@cache
def _claude_help_text() -> str:
    """Read Claude CLI help without imposing a wrapper timeout."""

    try:
        proc = subprocess.run(
            ["claude", "--help"],
            capture_output=True,
            text=True,
            env=_core._subprocess_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # TimeoutExpired is retained only for compatibility with custom/mocked
        # subprocess implementations. This wrapper never supplies a timeout.
        return ""
    return "\n".join(part for part in (proc.stdout, proc.stderr) if part)


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    """Terminate the complete Claude process tree after explicit cancellation."""

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.communicate()
    except (OSError, subprocess.TimeoutExpired):
        pass


def _project_cwd_from_cmd(cmd: list[str]) -> str:
    """Recover the canonical project root already embedded by ``_base_cmd``.

    Reading the existing ``--add-dir`` value avoids a second Git subprocess at
    launch time. It also makes the transport robust when callers replace
    ``subprocess.Popen`` for testing or instrumentation.
    """

    try:
        index = cmd.index("--add-dir")
        value = cmd[index + 1]
    except (ValueError, IndexError):
        value = os.getcwd()
    return os.path.realpath(value)


def _run_claude_proc(cmd: list[str], prompt: str) -> subprocess.CompletedProcess[str]:
    """Run one Claude process with no normal wall-clock timeout.

    ``cwd`` is the canonical project root carried in ``--add-dir`` by
    ``_base_cmd``. Claude Code scopes resumable sessions by project
    directory/worktree, so this keeps resume behavior stable when the caller
    moves between subdirectories of the same project.
    """

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_core._subprocess_env(),
        cwd=_project_cwd_from_cmd(cmd),
        start_new_session=True,
    )
    try:
        # Deliberately no timeout argument and no output-size bound.
        stdout, stderr = proc.communicate(input=prompt)
    except subprocess.TimeoutExpired:
        # No timeout is configured here. This branch supports callers/tests
        # that inject a Popen implementation which raises TimeoutExpired.
        _terminate_process_group(proc)
        print(
            "[claude-opinion] subprocess reported an unexpected timeout even "
            "though the wrapper configured none.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    except KeyboardInterrupt:
        _terminate_process_group(proc)
        raise

    return subprocess.CompletedProcess(
        args=cmd,
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _run_lock_path() -> str:
    """One lock per canonical project and optional session key."""

    return f"{_core._state_path()}.run.lock"


@contextlib.contextmanager
def _run_lock() -> Iterator[None]:
    """Serialize load -> resume/fresh call -> save for one Claude thread."""

    os.makedirs(_core.STATE_DIR, exist_ok=True)
    fd = os.open(_run_lock_path(), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def run_claude(stdin_content: str, instruction: str) -> str:
    """Run exactly one top-level Claude agent for this project/session key."""

    with _run_lock():
        return _ORIGINAL_RUN_CLAUDE(stdin_content, instruction)


# Patch the transport module so its existing helpers, tests, and ``main`` all
# observe the new policy. When imported, expose the patched core module itself;
# this preserves the public/internal surface used by existing unit tests.
_core._project_root = _project_root
_core._claude_help_text = _claude_help_text
_core._project_cwd_from_cmd = _project_cwd_from_cmd
_core._run_claude_proc = _run_claude_proc
_core._run_lock_path = _run_lock_path
_core._run_lock = _run_lock
_core.run_claude = run_claude


if __name__ == "__main__":
    _core.main()
else:
    sys.modules[__name__] = _core
