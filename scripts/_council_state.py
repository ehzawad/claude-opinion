"""Per-project, session, and role persistence for Claude council threads."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from typing import Any

import ask_claude as _transport
from _council_panels import _validate_role
from _council_types import (
    COUNCIL_STATE_DIR, PROGRAM, STATE_VERSION, CorruptRoleStateError,
    CouncilError, RoleSpec,
)


def _canonical_project_root() -> str:
    return os.path.realpath(_transport._project_root())


def _session_key() -> str:
    return (
        os.environ.get("CLAUDE_COUNCIL_SESSION_KEY", "").strip()
        or os.environ.get("CLAUDE_OPINION_SESSION_KEY", "").strip()
    )


def _hash_prefix(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _scope_key(project_root: str | None = None, session_key: str | None = None) -> str:
    root = os.path.realpath(project_root or _canonical_project_root())
    key = _hash_prefix(root)
    effective_session = _session_key() if session_key is None else session_key.strip()
    if effective_session:
        key = f"{key}-{_hash_prefix(effective_session)}"
    return key


def _role_state_path(
    role_id: str,
    project_root: str | None = None,
    session_key: str | None = None,
) -> str:
    safe_role = _validate_role(RoleSpec(role_id, role_id, "state key")).role_id
    role_hash = _hash_prefix(safe_role, 12)
    return os.path.join(
        COUNCIL_STATE_DIR,
        f"{_scope_key(project_root, session_key)}--{safe_role}-{role_hash}.json",
    )


def _council_run_lock_path(
    project_root: str | None = None,
    session_key: str | None = None,
) -> str:
    return os.path.join(
        COUNCIL_STATE_DIR,
        f"{_scope_key(project_root, session_key)}.run.lock",
    )


@contextlib.contextmanager
def _exclusive_lock(path: str) -> Iterator[None]:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    os.chmod(os.path.dirname(path), 0o700)
    fd = os.open(
        path,
        os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def council_run_lock(
    project_root: str | None = None,
    session_key: str | None = None,
) -> Iterator[None]:
    """Serialize a complete panel turn for one project/session namespace."""

    with _exclusive_lock(_council_run_lock_path(project_root, session_key)):
        yield


def _state_lock_path(state_path: str) -> str:
    return f"{state_path}.lock"


def _quarantine_state(state_path: str) -> str:
    target = (
        f"{state_path}.corrupt.{time.time_ns()}.{os.getpid()}."
        f"{threading.get_ident()}"
    )
    try:
        os.rename(state_path, target)
    except OSError as exc:
        raise CorruptRoleStateError(
            f"Role state at {state_path} is corrupt and could not be quarantined: {exc}"
        ) from exc
    return target


def _read_state_unlocked(state_path: str) -> dict[str, Any] | None:
    try:
        fd = os.open(state_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        if isinstance(exc, OSError) and not isinstance(exc, FileNotFoundError):
            raise CouncilError(f"Could not read role state {state_path}: {exc}") from exc
        quarantined = _quarantine_state(state_path)
        print(
            f"[{PROGRAM}] Corrupt role state moved to {quarantined}; starting fresh.",
            file=sys.stderr,
        )
        return None
    if not isinstance(payload, dict):
        quarantined = _quarantine_state(state_path)
        print(
            f"[{PROGRAM}] Invalid role state moved to {quarantined}; starting fresh.",
            file=sys.stderr,
        )
        return None
    return payload


def _role_fingerprint(role: RoleSpec) -> str:
    return _hash_prefix(f"{role.name}\n{role.instruction}", 24)


def _valid_state_payload(
    payload: dict[str, Any],
    role: RoleSpec,
    project_root: str,
) -> bool:
    stored_project = payload.get("project_path")
    return (
        payload.get("version") == STATE_VERSION
        and payload.get("role_id") == role.role_id
        and payload.get("role_fingerprint") == _role_fingerprint(role)
        and isinstance(stored_project, str)
        and bool(stored_project)
        and os.path.realpath(stored_project) == os.path.realpath(project_root)
        and isinstance(payload.get("session_id"), str)
        and bool(payload.get("session_id"))
    )


def load_role_state(
    role: RoleSpec,
    project_root: str,
    session_key: str | None = None,
) -> dict[str, Any] | None:
    path = _role_state_path(role.role_id, project_root, session_key)
    with _exclusive_lock(_state_lock_path(path)):
        payload = _read_state_unlocked(path)
        if not payload:
            return None
        if _valid_state_payload(payload, role, project_root):
            return payload
        quarantined = _quarantine_state(path)
        print(
            f"[{PROGRAM}] Mismatched role state moved to {quarantined}; starting fresh.",
            file=sys.stderr,
        )
        return None


def save_role_state(
    role: RoleSpec,
    session_id: str,
    project_root: str,
    expected_prior: str | None,
    session_key: str | None = None,
) -> bool:
    path = _role_state_path(role.role_id, project_root, session_key)
    with _exclusive_lock(_state_lock_path(path)):
        current_payload = _read_state_unlocked(path)
        if current_payload and not _valid_state_payload(current_payload, role, project_root):
            quarantined = _quarantine_state(path)
            print(
                f"[{PROGRAM}] Mismatched role state moved to {quarantined}; replacing it.",
                file=sys.stderr,
            )
            current_payload = None
        current = current_payload.get("session_id") if current_payload else None
        if current not in (None, expected_prior, session_id):
            return False
        os.makedirs(COUNCIL_STATE_DIR, mode=0o700, exist_ok=True)
        os.chmod(COUNCIL_STATE_DIR, 0o700)
        payload: dict[str, Any] = {
            "version": STATE_VERSION,
            "session_id": session_id,
            "project_path": os.path.realpath(project_root),
            "role_id": role.role_id,
            "role_name": role.name,
            "role_fingerprint": _role_fingerprint(role),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        effective_key = _session_key() if session_key is None else session_key.strip()
        if effective_key:
            payload["session_key"] = effective_key
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp.", dir=COUNCIL_STATE_DIR)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_path, path)
            os.chmod(path, 0o600)
        except Exception:
            with contextlib.suppress(OSError):
                os.remove(tmp_path)
            raise
        return True


def clear_role_state(
    role: RoleSpec,
    project_root: str,
    expected_session_id: str | None = None,
    session_key: str | None = None,
) -> bool:
    path = _role_state_path(role.role_id, project_root, session_key)
    with _exclusive_lock(_state_lock_path(path)):
        if expected_session_id is not None:
            payload = _read_state_unlocked(path)
            if (
                not payload
                or not _valid_state_payload(payload, role, project_root)
                or payload.get("session_id") != expected_session_id
            ):
                return False
        try:
            os.remove(path)
        except FileNotFoundError:
            return False
        return True
