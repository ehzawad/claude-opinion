"""Claude subprocess supervision and one-role invocation protocol."""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any

import ask_claude as _transport
from _council_state import clear_role_state, load_role_state, save_role_state
from _council_types import (
    ANALYSIS_ONLY_DIRECTIVE, PROGRAM, ROLE_SYSTEM_BASE, CouncilError,
    ProcessResult, RoleOutcome, RoleSpec,
)


class ProcessRegistry:
    """Track active Claude subprocesses so explicit cancellation can stop all trees."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._terminating = False

    def add(self, invocation_id: str, proc: subprocess.Popen[str]) -> None:
        # Closing the add-after-snapshot race is essential during Ctrl-C: a
        # queued worker can begin between terminate_all() taking its snapshot
        # and the executor cancelling pending futures. Such a late process must
        # be killed immediately rather than escaping the cancellation sweep.
        with self._lock:
            terminate_immediately = self._terminating
            if not terminate_immediately:
                self._processes[invocation_id] = proc
        if terminate_immediately:
            _terminate_process_group(proc)

    def discard(self, invocation_id: str, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            if self._processes.get(invocation_id) is proc:
                self._processes.pop(invocation_id, None)

    def terminate_all(self) -> None:
        with self._lock:
            self._terminating = True
            processes = list(self._processes.values())
            self._processes.clear()
        for proc in processes:
            _terminate_process_group(proc)


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(OSError):
            proc.kill()
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        proc.communicate()


def _run_process(
    invocation_id: str,
    cmd: list[str],
    prompt: str,
    project_root: str,
    registry: ProcessRegistry,
) -> ProcessResult:
    """Run one Claude CLI process with no normal wrapper timeout or truncation."""

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_transport._subprocess_env(),
        cwd=project_root,
        start_new_session=True,
    )
    registry.add(invocation_id, proc)
    try:
        stdout, stderr = proc.communicate(input=prompt)
    except KeyboardInterrupt:
        _terminate_process_group(proc)
        raise
    finally:
        registry.discard(invocation_id, proc)
    return ProcessResult(proc.returncode, stdout, stderr)


def _base_command(project_root: str, system_prompt: str) -> list[str]:
    cmd = _transport._base_cmd(project_root, system_prompt)
    forbidden = {
        "--agent",
        "--agents",
        "--max-turns",
        "--max-budget-usd",
        "--no-session-persistence",
    }
    present = forbidden.intersection(cmd)
    if present:
        raise CouncilError(f"Transport unexpectedly added forbidden flags: {sorted(present)}")
    return cmd


def _role_system_prompt(role: RoleSpec) -> str:
    return (
        f"{ROLE_SYSTEM_BASE}\n\n"
        f"Council role: {role.name} ({role.role_id})\n"
        f"Role mandate: {role.instruction}\n\n"
        f"{ANALYSIS_ONLY_DIRECTIVE}"
    )


def _role_user_prompt(role: RoleSpec, task: str, context: str) -> str:
    return f"""# Council task

{task}

# Your assigned lens

{role.instruction}

# Shared context

{context}

# Deliverable

Produce an independent report with: material findings ordered by impact; concrete evidence;
trade-offs and counterarguments; actionable recommendations; and explicit uncertainty. Do not
attempt to synthesize the other roles—the chair will do that after all reports complete.
"""


def _result_error(result: dict[str, Any] | None, stderr: str, returncode: int) -> str:
    if result:
        message = result.get("result") or " ".join(
            str(item) for item in (result.get("errors") or [])
        )
        if message:
            return str(message)
    if stderr.strip():
        return stderr.strip()
    return f"Claude exited with status {returncode}"


def _invoke_role_once(
    role: RoleSpec,
    task: str,
    context: str,
    project_root: str,
    registry: ProcessRegistry,
    session_key: str | None = None,
    *,
    system_prompt_override: str | None = None,
    user_prompt_override: str | None = None,
) -> RoleOutcome:
    started = time.monotonic()
    try:
        state = load_role_state(role, project_root, session_key)
    except CouncilError as exc:
        return RoleOutcome(
            role=role,
            ok=False,
            error=str(exc),
            duration_seconds=time.monotonic() - started,
        )
    prior_session_id = str(state["session_id"]) if state else None
    prompt = (
        user_prompt_override
        if user_prompt_override is not None
        else _role_user_prompt(role, task, context)
    )
    system_prompt = (
        system_prompt_override
        if system_prompt_override is not None
        else _role_system_prompt(role)
    )
    base_cmd = _base_command(project_root, system_prompt)
    stale_restarted = False

    def run(cmd: list[str], suffix: str) -> tuple[ProcessResult, dict[str, Any] | None]:
        proc_result = _run_process(
            f"{role.role_id}:{suffix}", cmd, prompt, project_root, registry
        )
        parsed = _transport._parse_result(proc_result.stdout)
        return proc_result, parsed

    if prior_session_id:
        proc_result, parsed = run(base_cmd + ["--resume", prior_session_id], "resume")
        stale = (
            _transport._stale_marker_match(proc_result.stderr)
            or _transport._is_stale_resume(parsed)
        )
        if stale:
            stale_restarted = True
            clear_role_state(
                role, project_root, expected_session_id=prior_session_id,
                session_key=session_key,
            )
            print(
                f"[{PROGRAM}] Role {role.role_id} session was stale; starting fresh.",
                file=sys.stderr,
            )
        elif proc_result.returncode != 0 or not parsed or parsed.get("is_error"):
            return RoleOutcome(
                role=role,
                ok=False,
                error=_result_error(parsed, proc_result.stderr, proc_result.returncode),
                session_id=prior_session_id,
                resumed=True,
                duration_seconds=time.monotonic() - started,
                result_meta=parsed or {},
            )
        else:
            text = parsed.get("result")
            if not text:
                return RoleOutcome(
                    role=role,
                    ok=False,
                    error="Claude returned no final role message",
                    session_id=prior_session_id,
                    resumed=True,
                    duration_seconds=time.monotonic() - started,
                    result_meta=parsed,
                )
            new_session_id = str(parsed.get("session_id") or prior_session_id)
            saved = save_role_state(
                role, new_session_id, project_root, prior_session_id, session_key
            )
            if not saved:
                print(
                    f"[{PROGRAM}] Role {role.role_id} state changed concurrently; "
                    "preserving the newer state.",
                    file=sys.stderr,
                )
            return RoleOutcome(
                role=role,
                ok=True,
                text=str(text),
                session_id=new_session_id,
                resumed=True,
                duration_seconds=time.monotonic() - started,
                result_meta=parsed,
            )

    proc_result, parsed = run(base_cmd, "fresh")
    if proc_result.returncode != 0 or not parsed or parsed.get("is_error"):
        return RoleOutcome(
            role=role,
            ok=False,
            error=_result_error(parsed, proc_result.stderr, proc_result.returncode),
            resumed=False,
            stale_restarted=stale_restarted,
            duration_seconds=time.monotonic() - started,
            result_meta=parsed or {},
        )
    text = parsed.get("result")
    if not text:
        return RoleOutcome(
            role=role,
            ok=False,
            error="Claude returned no final role message",
            resumed=False,
            stale_restarted=stale_restarted,
            duration_seconds=time.monotonic() - started,
            result_meta=parsed,
        )
    new_session_id = parsed.get("session_id")
    if new_session_id:
        saved = save_role_state(
            role, str(new_session_id), project_root, None, session_key
        )
        if not saved:
            print(
                f"[{PROGRAM}] Role {role.role_id} state changed concurrently; "
                "preserving the newer state.",
                file=sys.stderr,
            )
    else:
        print(
            f"[{PROGRAM}] Role {role.role_id} returned no session_id; "
            "its next turn will start fresh.",
            file=sys.stderr,
        )
    return RoleOutcome(
        role=role,
        ok=True,
        text=str(text),
        session_id=str(new_session_id) if new_session_id else None,
        resumed=False,
        stale_restarted=stale_restarted,
        duration_seconds=time.monotonic() - started,
        result_meta=parsed,
    )
