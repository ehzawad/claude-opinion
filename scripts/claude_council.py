#!/usr/bin/env python3
"""Bounded persistent multi-agent Claude council for ``claude-opinion``."""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import sys
from collections.abc import Sequence

from _council_orchestrator import render_report, run_panel, run_reconciler
from _council_panels import _BUILTIN_PANELS, compose_panel
from _council_process import ProcessRegistry
from _council_staging import PrivateRunDirectory, _write_atomic_text
from _council_state import _canonical_project_root, _session_key, council_run_lock
from _council_types import (
    DEFAULT_MAX_PARALLEL, DEFAULT_TASK, MAX_PARALLEL_HARD_LIMIT, PROGRAM,
    CouncilError, PanelSpec, RoleOutcome, RoleSpec,
)

__all__ = [
    "CouncilError",
    "PanelSpec",
    "RoleOutcome",
    "RoleSpec",
    "build_parser",
    "compose_panel",
    "main",
]

def _positive_parallel(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1 or parsed > MAX_PARALLEL_HARD_LIMIT:
        raise argparse.ArgumentTypeError(
            f"must be between 1 and {MAX_PARALLEL_HARD_LIMIT}"
        )
    return parsed


def _default_parallelism() -> int:
    raw = os.environ.get("CLAUDE_COUNCIL_MAX_PARALLEL", "").strip()
    if not raw:
        return DEFAULT_MAX_PARALLEL
    try:
        return _positive_parallel(raw)
    except argparse.ArgumentTypeError as exc:
        raise CouncilError(f"Invalid CLAUDE_COUNCIL_MAX_PARALLEL: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a persistent multi-role Claude council with bounded parallel fan-out "
            "and a final reconciliation pass."
        )
    )
    parser.add_argument(
        "task",
        nargs="*",
        help="Optional task instruction. Defaults to a thorough multi-perspective review.",
    )
    parser.add_argument(
        "--panel",
        default=os.environ.get("CLAUDE_COUNCIL_PANEL", "auto"),
        help="Panel name: auto, minimal, engineering, architecture, or research.",
    )
    parser.add_argument(
        "--roles-file",
        help="JSON panel manifest. Overrides --panel.",
    )
    parser.add_argument(
        "--max-parallel",
        type=_positive_parallel,
        help=(
            f"Maximum concurrent role processes (1-{MAX_PARALLEL_HARD_LIMIT}; "
            f"default {DEFAULT_MAX_PARALLEL} or CLAUDE_COUNCIL_MAX_PARALLEL)."
        ),
    )
    parser.add_argument(
        "--no-reconcile",
        action="store_true",
        help="Return the aggregated role reports without running the chair.",
    )
    parser.add_argument(
        "--keep-run-dir",
        action="store_true",
        help="Preserve the private staged run directory and print its path to stderr.",
    )
    parser.add_argument(
        "--run-root",
        help="Parent directory for the private 0700 run directory.",
    )
    parser.add_argument(
        "--report-file",
        help="Also write the complete markdown report atomically to this path.",
    )
    parser.add_argument(
        "--list-panels",
        action="store_true",
        help="List built-in panels and exit without reading stdin.",
    )
    return parser


def _print_panels() -> None:
    print("auto\tTask-composed from the shared context")
    for panel_id, panel in _BUILTIN_PANELS.items():
        role_ids = ", ".join(role.role_id for role in panel.roles)
        print(f"{panel_id}\t{panel.name}: {role_ids}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_panels:
        _print_panels()
        return 0
    if not shutil.which("claude"):
        print("Claude Code CLI not found. Install and authenticate it first.", file=sys.stderr)
        return 1
    if sys.stdin.isatty():
        parser.error("pipe shared context on stdin")
    context = sys.stdin.read()
    if not context.strip():
        parser.error("stdin context is empty")
    task = " ".join(args.task).strip() or DEFAULT_TASK
    try:
        max_parallel = args.max_parallel or _default_parallelism()
        panel = compose_panel(args.panel, task, context, args.roles_file)
        project_root = _canonical_project_root()
        effective_session_key = _session_key()
        registry = ProcessRegistry()
        with council_run_lock(project_root, effective_session_key):
            with PrivateRunDirectory(args.run_root, args.keep_run_dir) as run_dir:
                run_dir.write_text("context.md", context)
                run_dir.write_text(
                    "panel.json",
                    json.dumps(
                        {
                            "id": panel.panel_id,
                            "name": panel.name,
                            "roles": [dataclasses.asdict(role) for role in panel.roles],
                            "max_parallel": max_parallel,
                        },
                        indent=2,
                    ) + "\n",
                )
                print(
                    f"[{PROGRAM}] Panel {panel.panel_id}: {len(panel.roles)} roles, "
                    f"max {min(max_parallel, len(panel.roles))} concurrent.",
                    file=sys.stderr,
                )
                outcomes = run_panel(
                    panel,
                    task,
                    context,
                    project_root,
                    max_parallel,
                    registry,
                    effective_session_key,
                )
                for outcome in outcomes:
                    run_dir.write_text(
                        f"roles/{outcome.role.role_id}.md",
                        outcome.text if outcome.ok else f"ERROR: {outcome.error}\n",
                    )
                successful = [outcome for outcome in outcomes if outcome.ok]
                reconciliation: RoleOutcome | None = None
                if successful and not args.no_reconcile:
                    reconciliation = run_reconciler(
                        panel,
                        task,
                        context,
                        outcomes,
                        project_root,
                        registry,
                        effective_session_key,
                    )
                report = render_report(
                    panel,
                    task,
                    outcomes,
                    reconciliation,
                    project_root,
                    max_parallel,
                )
                run_dir.write_text("report.md", report)
                if args.report_file:
                    _write_atomic_text(args.report_file, report)
                if args.keep_run_dir and run_dir.path:
                    print(f"[{PROGRAM}] Preserved run directory: {run_dir.path}", file=sys.stderr)
    except KeyboardInterrupt:
        print(
            f"\n[{PROGRAM}] Cancelled; active Claude process groups were terminated.",
            file=sys.stderr,
        )
        return 130
    except CouncilError as exc:
        print(f"[{PROGRAM}] {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"[{PROGRAM}] Operating-system error: {exc}", file=sys.stderr)
        return 2
    print(report, end="")
    if not successful:
        return 1
    if reconciliation is not None and not reconciliation.ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
