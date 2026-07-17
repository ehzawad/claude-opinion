"""Private run staging and atomic report output."""
from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
from pathlib import Path

from _council_types import CouncilError


class PrivateRunDirectory:
    """Private staging area for context, panel manifest, role output, and report."""

    def __init__(self, run_root: str | None = None, keep: bool = False) -> None:
        self.run_root = os.path.realpath(run_root) if run_root else None
        self.keep = keep
        self.path: str | None = None

    def __enter__(self) -> "PrivateRunDirectory":
        if self.run_root:
            os.makedirs(self.run_root, mode=0o700, exist_ok=True)
        self.path = tempfile.mkdtemp(prefix="claude-council-", dir=self.run_root)
        os.chmod(self.path, 0o700)
        self._validate_private_directory(self.path)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.path and not self.keep:
            shutil.rmtree(self.path, ignore_errors=True)

    @staticmethod
    def _validate_private_directory(path: str) -> None:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise CouncilError(f"Run path is not a real directory: {path}")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise CouncilError(f"Run directory is not owned by the current user: {path}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise CouncilError(f"Run directory is not private (expected mode 0700): {path}")

    def write_text(self, relative_path: str, content: str) -> str:
        if not self.path:
            raise RuntimeError("Run directory is not active")
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise CouncilError(f"Unsafe run-directory path: {relative_path}")
        destination = Path(self.path, relative)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(destination.parent, 0o700)
        fd = os.open(
            destination,
            os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
        except Exception:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        return str(destination)


def _write_atomic_text(path: str, content: str) -> None:
    destination = os.path.realpath(path)
    parent = os.path.dirname(destination) or os.getcwd()
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".claude-council.", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_path, destination)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise
