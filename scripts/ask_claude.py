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

Set CLAUDE_OPINION_SESSION_KEY in the environment to isolate a
session's Claude thread from the project-wide thread.

Usage:
    echo "<context>" | python3 ask_claude.py
    echo "<context>" | python3 ask_claude.py "Custom instruction"
    echo "<context>" | python3 ask_claude.py --no-default-instruction
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from functools import cache

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

NO_DEFAULT_FLAG = "--no-default-instruction"

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


def load_session():
    try:
        with open(_state_path()) as f:
            meta = json.load(f)
            sid = meta.get("session_id")
            if sid:
                return sid, meta
    except (OSError, json.JSONDecodeError):
        pass
    return None, None


def save_session(session_id):
    os.makedirs(STATE_DIR, exist_ok=True)
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


def clear_session():
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
        )
    except OSError:
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


def _run_claude_proc(cmd, prompt):
    return subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, env=_subprocess_env(),
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
    session_id, meta = load_session()

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
            current_id, _ = load_session()
            if current_id == session_id:
                clear_session()
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
            save_session(str(result.get("session_id") or session_id))
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

    session_id = result.get("session_id")
    if not session_id:
        print(
            "[claude-opinion] Claude succeeded but did not return a session_id.",
            file=sys.stderr,
        )
        sys.exit(1)

    save_session(str(session_id))
    return text


def _instruction_from_args(args):
    no_default = False
    parts = []
    for arg in args:
        if arg == NO_DEFAULT_FLAG:
            no_default = True
        else:
            parts.append(arg)
    custom = " ".join(parts).strip()
    if custom:
        return custom
    if no_default:
        return ""
    return DEFAULT_INSTRUCTION


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
