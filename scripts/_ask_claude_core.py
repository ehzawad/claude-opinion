#!/usr/bin/env python3
"""Route a prompt to Claude Code.

Invokes `claude -p --output-format json` with the highest supported
`--effort` level and lets Claude assign the session ID on fresh calls,
then uses `--resume <uuid>` for follow-ups. Stdin passes verbatim as the prompt body;
the review directive rides on `--append-system-prompt` so stdin stays as
pure context.

Strips ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL
from the subprocess env so the Claude.ai subscription wins routing
over an API-key or proxy-gateway setup that would otherwise bill a
possibly-empty balance. See anthropics/claude-code#2051.

An optional positional argument overrides DEFAULT_INSTRUCTION. Pass
--no-default-instruction to skip the system-prompt directive entirely.
By default, a short safety line ("Do not modify files...") is appended
to whatever instruction is active; pass --allow-edit to opt out (only
do this if you want Claude to make changes).

Set CLAUDE_OPINION_SESSION_KEY in the environment to isolate a
session's Claude thread from the project-wide thread.

Usage:
    echo "<context>" | python3 ask_claude.py
    echo "<context>" | python3 ask_claude.py "Custom instruction"
    echo "<context>" | python3 ask_claude.py --no-default-instruction
    echo "<context>" | python3 ask_claude.py --allow-edit "fix the bug"
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from functools import cache

# Sentinel for save_session's optional generation check. distinct from None
# so callers can pass `expected_prior=None` to mean "expect empty state"
# without colliding with "skip the check entirely".
_UNCHECKED = object()

STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "claude-opinion",
)

DEFAULT_INSTRUCTION = (
    "Give a thorough second opinion on the context below. "
    "Surface wrong, missing, or incomplete assumptions, trade-offs, and risks. "
    "Prioritize actionable findings; if nothing material stands out, say so clearly. "
    "Thoroughness beats speed."
)

# Always appended to the active instruction unless --allow-edit is passed.
# The script invokes claude with --dangerously-skip-permissions, so this is
# the only thing keeping benign custom instructions ("focus on test
# coverage", etc.) from accidentally inviting mutating runs.
SAFETY_DIRECTIVE = "Do not modify files or run mutating commands; provide analysis only."

NO_DEFAULT_FLAG = "--no-default-instruction"
ALLOW_EDIT_FLAG = "--allow-edit"

# Default subprocess timeout. Long enough for `claude -p --effort max` work
# (file reads, web fetches, tool calls) without leaving the script wedged
# forever if the child hangs. Override via CLAUDE_OPINION_TIMEOUT env var.
_DEFAULT_TIMEOUT_SECONDS = 600

# Bound on the one-shot `claude --help` probe. Help should return instantly;
# 10s is generous and prevents the script from hanging before the protected
# `claude -p` call ever runs.
_HELP_TIMEOUT_SECONDS = 10

# Env vars that route away from the Claude.ai subscription. Stripped
# before every claude -p spawn so subscription auth wins over API-key
# or proxy routing. See anthropics/claude-code#2051.
_STRIP_ENV_VARS = frozenset({
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
})

# Substring matches (stderr or result.errors) that indicate the stored
# session can no longer be resumed. On match we clear state and restart.
# "no conversation found" is the verified wording; the others are cheap
# future-proofing against Claude CLI rewording.
_STALE_RESUME_MARKERS = (
    "no conversation found",
    "conversation not found",
    "session not found",
)

_EFFORT_LEVELS = ("auto", "low", "medium", "high", "xhigh", "max")
# Prefer the highest explicit effort when available; only fall back to
# auto if the installed Claude CLI does not advertise the explicit levels.
_EFFORT_PREFERENCE = ("max", "xhigh", "high", "medium", "low", "auto")


@cache
def _project_root():
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True,
        ).stdout.strip()
    except OSError:
        root = ""
    return root or os.getcwd()


def _session_key():
    return os.environ.get("CLAUDE_OPINION_SESSION_KEY", "").strip()


def _project_key():
    base = hashlib.sha256(_project_root().encode()).hexdigest()[:16]
    session_key = _session_key()
    if session_key:
        suffix = hashlib.sha256(session_key.encode()).hexdigest()[:16]
        return f"{base}-{suffix}"
    return base


def _state_path():
    return os.path.join(STATE_DIR, f"{_project_key()}.json")


class _CorruptStateUnrecoverable(Exception):
    """Corrupt state was detected but quarantine failed (e.g. directory
    perms blocked the rename). Safe recovery requires human inspection;
    proceeding to spawn `claude -p` would burn a call we can't persist."""


def load_session():
    """Load session metadata, or quarantine and return None on corruption.

    A corrupt state file (e.g. JSON garbled by an external writer or a
    crash in another tool) would otherwise wedge every subsequent call
    into a fresh-then-refuse-save loop: load_session returns None silently,
    a fresh `claude -p` runs successfully, then save_session refuses to
    overwrite the unreadable file. We break that doom loop here by renaming
    the corrupt file aside (preserving evidence for inspection) so the
    follow-up save can persist a new session_id normally.

    The whole open → parse → quarantine sequence runs under `_state_lock`
    so a sibling save can't slip a *valid* fresh state in between our
    failed parse and our rename — without the lock, that interleaving
    would cause us to quarantine the sibling's good data.

    Plain I/O errors (permissions on the file itself, etc.) stay silent —
    they're outside our recovery scope. A failed quarantine raises
    `_CorruptStateUnrecoverable` so the caller can abort before spending
    on a `claude -p` call that won't be able to persist anyway.
    """
    state_path = _state_path()
    with _state_lock():
        try:
            with open(state_path) as f:
                meta = json.load(f)
                sid = meta.get("session_id")
                if sid:
                    return sid, meta
        except FileNotFoundError:
            pass
        except OSError:
            pass
        except json.JSONDecodeError:
            # time.time_ns + pid: nanosecond resolution + per-process
            # suffix makes target-name collisions practically impossible
            # even under burst corruption from multiple processes. Without
            # both, two events in the same second on POSIX would let
            # os.rename silently overwrite preserved evidence.
            quarantine_path = (
                f"{state_path}.corrupt.{time.time_ns()}.{os.getpid()}"
            )
            try:
                os.rename(state_path, quarantine_path)
            except OSError as e:
                raise _CorruptStateUnrecoverable(
                    f"State file at {state_path} is corrupt and could not "
                    f"be quarantined ({e}); manual cleanup required."
                )
            print(
                f"[claude-opinion] State file at {state_path} was corrupt; "
                f"moved to {quarantine_path} and starting fresh.",
                file=sys.stderr,
            )
    return None, None


@contextlib.contextmanager
def _state_lock():
    """Hold an exclusive flock during state I/O.

    Serializes concurrent save_session / clear_session invocations on the
    same project so the `read state -> branch -> write state` sequence in
    run_claude doesn't race. The lock file lives next to the state file
    and is released automatically when the fd closes.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    lock_path = os.path.join(STATE_DIR, ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def save_session(session_id, expected_prior=_UNCHECKED):
    """Persist session metadata atomically; optionally guard against races.

    When `expected_prior` is left as `_UNCHECKED` (the default), the save is
    unconditional — last writer wins.

    When `expected_prior` is provided (including `None`), the save is a
    compare-and-save: it only writes if the current state's `session_id`
    matches `expected_prior` *or* the state file is missing (treated as
    "no concurrent writer"). This is used on both fresh and resume-success
    paths so a parallel invocation that has already persisted a different
    ID isn't clobbered. A current value equal to `session_id` itself is also
    accepted as a no-op success.

    A corrupt or unreadable existing state file does NOT collapse to the
    "missing" case: with atomic os.replace writes this script cannot produce
    invalid JSON itself, so corruption implies external interference and we
    refuse to overwrite rather than masking the problem.

    Returns True if the write happened (or the no-op match), False if it was
    skipped because a concurrent invocation already wrote a different
    session_id or the existing state was unreadable.
    """
    with _state_lock():
        if expected_prior is not _UNCHECKED:
            try:
                with open(_state_path()) as f:
                    current = json.load(f).get("session_id")
            except FileNotFoundError:
                current = None
            except (OSError, json.JSONDecodeError) as e:
                print(
                    f"[claude-opinion] State file unreadable ({type(e).__name__}); "
                    "refusing to overwrite. Inspect or remove it manually.",
                    file=sys.stderr,
                )
                return False
            # current==None: empty state, no concurrent writer → proceed.
            # current==expected_prior: state matches what we observed → proceed.
            # current==session_id: someone wrote the same ID → no-op success.
            # otherwise: a concurrent invocation persisted a different ID → refuse.
            if (current is not None
                    and current != expected_prior
                    and current != session_id):
                return False
        meta = {
            "session_id": session_id,
            "project_path": _project_root(),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        session_key = _session_key()
        if session_key:
            meta["session_key"] = session_key
        path = _state_path()
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp.", dir=STATE_DIR)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(meta, f, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
        return True


def clear_session(expected_session_id=None):
    """Remove the session state file.

    When `expected_session_id` is provided, this is a compare-and-clear:
    the file is only removed if its current `session_id` matches. Closes
    the TOCTOU window where a concurrent invocation may have replaced the
    stale ID with a fresh one between our read and our delete.
    """
    with _state_lock():
        if expected_session_id is not None:
            try:
                with open(_state_path()) as f:
                    meta = json.load(f)
                if meta.get("session_id") != expected_session_id:
                    return
            except (OSError, json.JSONDecodeError):
                return
        try:
            os.remove(_state_path())
        except OSError:
            pass


def _subprocess_env():
    if os.environ.get("CLAUDE_OPINION_KEEP_ANTHROPIC_ENV", "").strip():
        return dict(os.environ)
    return {k: v for k, v in os.environ.items() if k not in _STRIP_ENV_VARS}


@cache
def _claude_help_text():
    try:
        proc = subprocess.run(
            ["claude", "--help"],
            capture_output=True, text=True, env=_subprocess_env(),
            timeout=_HELP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return "\n".join(part for part in (proc.stdout, proc.stderr) if part)


def _supported_efforts_from_help(help_text):
    if "--effort" not in help_text:
        return ()

    lines = help_text.splitlines()
    for idx, line in enumerate(lines):
        if "--effort" not in line:
            continue
        option_lines = [line]
        for continuation in lines[idx + 1:idx + 3]:
            if re.match(r"\s*(?:-[A-Za-z0-9],\s*)?--?[A-Za-z]", continuation):
                break
            option_lines.append(continuation)
        option_help = " ".join(option_lines).lower()
        match = re.search(r"\(([^)]*)\)", option_help)
        choices_text = match.group(1) if match else option_help
        tokens = re.split(r"[^a-z]+", choices_text)
        efforts = tuple(level for level in _EFFORT_LEVELS if level in tokens)
        if efforts:
            return efforts
        return ()
    return ()


def _best_effort_level():
    supported = set(_supported_efforts_from_help(_claude_help_text()))
    for level in _EFFORT_PREFERENCE:
        if level in supported:
            return level
    return None


def _subprocess_timeout():
    """Return the timeout in seconds for the claude -p subprocess.

    Defaults to 600s. Override via CLAUDE_OPINION_TIMEOUT (must be a
    positive integer; non-numeric or non-positive values fall back to the
    default rather than disabling the timeout, since Codex's outer shell
    tool is not a reliable backstop).
    """
    raw = os.environ.get("CLAUDE_OPINION_TIMEOUT", "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        val = int(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_SECONDS
    return val if val > 0 else _DEFAULT_TIMEOUT_SECONDS


def _run_claude_proc(cmd, prompt):
    """Run `claude -p` with a process-group-bounded timeout.

    `start_new_session=True` puts claude in its own process group so a
    timeout can SIGKILL the whole tree (claude itself plus any tool
    subprocesses it spawned during -p execution); plain `subprocess.run`
    timeouts only signal the direct child.
    """
    timeout = _subprocess_timeout()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_subprocess_env(),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        print(
            f"[claude-opinion] claude -p exceeded {timeout}s timeout. "
            "Raise CLAUDE_OPINION_TIMEOUT or check whether claude itself is hung.",
            file=sys.stderr,
        )
        sys.exit(1)

    return subprocess.CompletedProcess(
        args=cmd, returncode=proc.returncode,
        stdout=stdout, stderr=stderr,
    )


def _base_cmd(project_root, append_system_prompt):
    cmd = [
        "claude", "-p",
        "--output-format", "json",
    ]
    effort = _best_effort_level()
    if effort:
        cmd.extend(["--effort", effort])
    cmd.extend([
        "--dangerously-skip-permissions",
        "--add-dir", project_root,
    ])
    if append_system_prompt:
        cmd.extend(["--append-system-prompt", append_system_prompt])
    return cmd


def _parse_result(stdout):
    """Parse `--output-format json` stdout. Returns the outer dict or None."""
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _stale_marker_match(text):
    lowered = (text or "").lower()
    return any(m in lowered for m in _STALE_RESUME_MARKERS)


def _is_stale_resume(result):
    """True if the result dict indicates the resumed session is gone."""
    if not result or not result.get("is_error"):
        return False
    errors = result.get("errors") or []
    combined = " ".join(str(e) for e in errors)
    return _stale_marker_match(combined)


def run_claude(stdin_content, instruction):
    root = _project_root()
    try:
        session_id, meta = load_session()
    except _CorruptStateUnrecoverable as e:
        # Don't burn a `claude -p` call we can't persist anyway.
        print(f"[claude-opinion] {e}", file=sys.stderr)
        sys.exit(1)
    # Capture the state generation we observed at entry. Used for the
    # compare-and-save guard on the fresh path so a parallel invocation
    # that has already written a different fresh ID isn't clobbered.
    prior_session_id = session_id

    if session_id:
        cmd = _base_cmd(root, instruction) + ["--resume", session_id]
        proc = _run_claude_proc(cmd, stdin_content)
        stderr = proc.stderr.strip()
        result = _parse_result(proc.stdout)

        # Stale resume can surface as exit 1 with stderr marker (output-format
        # json) or exit 0 with result.is_error + errors[] (stream-json).
        # Check both paths before any hard failure.
        stale = _stale_marker_match(stderr) or _is_stale_resume(result)

        if stale:
            updated = (meta or {}).get("updated_at", "unknown")
            print(
                f"[claude-opinion] Session {session_id} (last used {updated}) is stale — starting fresh.",
                file=sys.stderr,
            )
            # Compare-and-clear: if a concurrent invocation already wrote a
            # fresh session_id, leave it alone.
            clear_session(expected_session_id=session_id)
            # fall through to fresh branch below

        elif proc.returncode != 0:
            if stderr:
                print(stderr, file=sys.stderr)
            print(f"[claude resume exited {proc.returncode}]", file=sys.stderr)
            sys.exit(1)

        elif result and result.get("is_error"):
            msg = str(
                result.get("result")
                or " ".join(str(e) for e in (result.get("errors") or []))
                or "claude reported error"
            )
            print(f"[claude-opinion] Claude reported error: {msg}", file=sys.stderr)
            sys.exit(1)

        elif result and result.get("result"):
            # Resume-success: also compare-and-save. Claude can rotate the
            # session ID on resume, and a parallel invocation may have
            # written a different fresh/resumed ID while we were blocked
            # in claude --resume — we must not clobber it.
            new_id = str(result.get("session_id") or session_id)
            if not save_session(new_id, expected_prior=prior_session_id):
                print(
                    "[claude-opinion] Concurrent invocation already updated session "
                    "state; not overwriting. Next call will use theirs.",
                    file=sys.stderr,
                )
            return result["result"]

        else:
            print(
                "[claude-opinion] Claude exited cleanly but produced no final message.",
                file=sys.stderr,
            )
            if stderr:
                print(stderr, file=sys.stderr)
            sys.exit(1)

    # Fresh session: let Claude allocate the session ID. Current Claude Code
    # builds can hang in print mode when a fresh call is forced with
    # --session-id, while resume-by-ID remains healthy.
    cmd = _base_cmd(root, instruction)
    proc = _run_claude_proc(cmd, stdin_content)
    stderr = proc.stderr.strip()

    if proc.returncode != 0:
        if stderr:
            print(stderr, file=sys.stderr)
        print(f"[claude exited {proc.returncode}]", file=sys.stderr)
        sys.exit(1)

    result = _parse_result(proc.stdout)
    if not result:
        print("[claude-opinion] Could not parse claude JSON output.", file=sys.stderr)
        if stderr:
            print(stderr, file=sys.stderr)
        sys.exit(1)

    if result.get("is_error"):
        msg = str(
            result.get("result")
            or " ".join(str(e) for e in (result.get("errors") or []))
            or "claude reported error"
        )
        print(f"[claude-opinion] Claude reported error: {msg}", file=sys.stderr)
        sys.exit(1)

    text = result.get("result")
    if not text:
        print(
            "[claude-opinion] Claude exited cleanly but produced no final message.",
            file=sys.stderr,
        )
        if stderr:
            print(stderr, file=sys.stderr)
        sys.exit(1)

    new_session_id = result.get("session_id")
    if new_session_id:
        # Compare-and-save: refuse to overwrite if a concurrent invocation
        # has already written a different session_id.
        if not save_session(str(new_session_id), expected_prior=prior_session_id):
            print(
                "[claude-opinion] Concurrent invocation already updated session "
                "state; not overwriting. Next call will use theirs.",
                file=sys.stderr,
            )
    else:
        # Don't discard the user's answer just because resume isn't possible
        # next time. Surface it as a warning and skip persistence.
        print(
            "[claude-opinion] Claude returned a result but no session_id; "
            "next call will start a fresh session.",
            file=sys.stderr,
        )
    return text


def _instruction_from_args(args):
    no_default = False
    allow_edit = False
    parts = []
    for arg in args:
        if arg == NO_DEFAULT_FLAG:
            no_default = True
        elif arg == ALLOW_EDIT_FLAG:
            allow_edit = True
        else:
            parts.append(arg)
    custom = " ".join(parts).strip()
    if custom:
        instruction = custom
    elif no_default:
        # Explicit opt-out of any system prompt at all — including the
        # safety directive. The user is in raw-passthrough mode.
        return ""
    else:
        instruction = DEFAULT_INSTRUCTION
    if not allow_edit:
        instruction = f"{instruction} {SAFETY_DIRECTIVE}"
    return instruction


def main():
    if not shutil.which("claude"):
        print(
            "Claude Code CLI not found. Install from https://claude.ai/code",
            file=sys.stderr,
        )
        sys.exit(1)

    if sys.stdin.isatty():
        print(
            "No input piped. Usage: echo 'context' | python3 ask_claude.py",
            file=sys.stderr,
        )
        sys.exit(1)

    stdin_content = sys.stdin.read()
    if not stdin_content.strip():
        print("Empty input — pipe a complete prompt instead.", file=sys.stderr)
        sys.exit(1)

    instruction = _instruction_from_args(sys.argv[1:])
    print(run_claude(stdin_content, instruction))


if __name__ == "__main__":
    main()
