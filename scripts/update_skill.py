#!/usr/bin/env python3
"""Fast-forward this skill checkout to the latest remote default branch."""

import argparse
import os
import subprocess
import sys


def _repo_root(script_path=None):
    script = script_path or __file__
    return os.path.dirname(os.path.dirname(os.path.realpath(script)))


def _git(repo_root, args, check=True):
    proc = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip() or "git command failed"
        raise RuntimeError(msg)
    return proc


def _default_branch_from_remote_head(remote_head):
    head = (remote_head or "").strip()
    if not head or "/" not in head:
        return None
    return head.split("/", 1)[1]


def _is_dirty(status_output):
    return bool((status_output or "").strip())


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Update this claude-opinion checkout with git pull --ff-only.",
    )
    parser.add_argument("--remote", default="origin", help="Git remote to update from.")
    parser.add_argument(
        "--branch",
        help="Branch to update from. Defaults to the remote's HEAD branch.",
    )
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    if not os.path.isdir(os.path.join(repo_root, ".git")):
        print(
            f"[claude-opinion] Expected a git checkout at {repo_root}.",
            file=sys.stderr,
        )
        sys.exit(1)

    status = _git(repo_root, ["status", "--porcelain"]).stdout
    if _is_dirty(status):
        print(
            "[claude-opinion] Local changes detected. Commit or stash them before updating.",
            file=sys.stderr,
        )
        sys.exit(1)

    current_branch = _git(
        repo_root,
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
    ).stdout.strip()
    remote_head = _git(
        repo_root,
        ["symbolic-ref", "--quiet", "--short", f"refs/remotes/{args.remote}/HEAD"],
    ).stdout.strip()
    default_branch = args.branch or _default_branch_from_remote_head(remote_head)
    if not default_branch:
        print(
            f"[claude-opinion] Could not determine default branch for remote '{args.remote}'.",
            file=sys.stderr,
        )
        sys.exit(1)

    if current_branch != default_branch:
        print(
            f"[claude-opinion] Checkout is on '{current_branch}', not '{default_branch}'. "
            f"Switch branches or rerun with --branch {current_branch}.",
            file=sys.stderr,
        )
        sys.exit(1)

    before = _git(repo_root, ["rev-parse", "--short", "HEAD"]).stdout.strip()
    _git(repo_root, ["fetch", args.remote, "--tags"])
    pull = _git(repo_root, ["pull", "--ff-only", args.remote, default_branch])
    after = _git(repo_root, ["rev-parse", "--short", "HEAD"]).stdout.strip()
    version = _git(repo_root, ["describe", "--tags", "--always"]).stdout.strip()

    if before == after:
        print(f"[claude-opinion] Already up to date at {version} ({after}).")
    else:
        summary = pull.stdout.strip() or "Updated successfully."
        print(f"[claude-opinion] Updated {before} -> {after} ({version}).")
        print(summary)


if __name__ == "__main__":
    main()
