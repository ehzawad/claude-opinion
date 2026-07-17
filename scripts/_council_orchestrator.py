"""Bounded role fan-out, chair reconciliation, and report rendering."""
from __future__ import annotations

import concurrent.futures
import json
from collections.abc import Sequence

from _council_panels import _validate_panel
from _council_process import ProcessRegistry, _invoke_role_once
from _council_types import (
    ANALYSIS_ONLY_DIRECTIVE, CHAIR_SYSTEM_PROMPT, MAX_PARALLEL_HARD_LIMIT,
    CouncilError, PanelSpec, RoleOutcome, RoleSpec,
)


def run_panel(
    panel: PanelSpec,
    task: str,
    context: str,
    project_root: str,
    max_parallel: int,
    registry: ProcessRegistry | None = None,
    session_key: str | None = None,
) -> list[RoleOutcome]:
    """Run panel roles concurrently while never exceeding ``max_parallel``."""

    panel = _validate_panel(panel)
    if max_parallel < 1:
        raise CouncilError("--max-parallel must be at least 1")
    if max_parallel > MAX_PARALLEL_HARD_LIMIT:
        raise CouncilError(
            f"--max-parallel may not exceed {MAX_PARALLEL_HARD_LIMIT}"
        )
    worker_count = min(max_parallel, len(panel.roles))
    process_registry = registry or ProcessRegistry()
    indexed: dict[str, int] = {
        role.role_id: index for index, role in enumerate(panel.roles)
    }
    outcomes: list[RoleOutcome] = []
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="claude-council-role",
    )
    futures = {
        executor.submit(
            _invoke_role_once,
            role,
            task,
            context,
            project_root,
            process_registry,
            session_key,
        ): role
        for role in panel.roles
    }
    try:
        for future in concurrent.futures.as_completed(futures):
            role = futures[future]
            try:
                outcomes.append(future.result())
            except Exception as exc:  # contain one role crash; keep the panel alive
                outcomes.append(
                    RoleOutcome(
                        role=role,
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
    except KeyboardInterrupt:
        process_registry.terminate_all()
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    outcomes.sort(key=lambda outcome: indexed[outcome.role.role_id])
    return outcomes


def _chair_system_prompt(panel: PanelSpec) -> str:
    prompt = CHAIR_SYSTEM_PROMPT
    if panel.reconciler_instruction:
        prompt += f"\n\nPanel-specific instruction: {panel.reconciler_instruction}"
    return f"{prompt}\n\n{ANALYSIS_ONLY_DIRECTIVE}"


def _chair_role(panel: PanelSpec) -> RoleSpec:
    # The instruction doubles as the persisted role fingerprint. Changing a
    # panel-specific chair mandate starts a fresh chair thread.
    return RoleSpec(
        "council-chair",
        "Council Chair",
        _chair_system_prompt(panel),
    )


def build_reconciliation_context(
    panel: PanelSpec,
    task: str,
    context: str,
    outcomes: Sequence[RoleOutcome],
) -> str:
    parts = [
        "# Original council task",
        task,
        "# Shared context",
        context,
        "# Panel manifest",
        json.dumps(
            {
                "id": panel.panel_id,
                "name": panel.name,
                "roles": [
                    {"id": role.role_id, "name": role.name, "instruction": role.instruction}
                    for role in panel.roles
                ],
            },
            indent=2,
        ),
        "# Independent role reports",
    ]
    for outcome in outcomes:
        parts.append(f"## {outcome.role.name} ({outcome.role.role_id})")
        if outcome.ok:
            parts.append(outcome.text)
        else:
            parts.append(f"ROLE FAILED: {outcome.error}")
    parts.extend([
        "# Chair deliverable",
        "Produce one reconciled answer with: executive decision; consensus findings; "
        "material disagreements and how you resolved them; rejected or weak claims; "
        "actionable next steps in priority order; and residual uncertainty. Preserve "
        "important minority dissent instead of hiding it.",
    ])
    return "\n\n".join(parts)


def run_reconciler(
    panel: PanelSpec,
    task: str,
    context: str,
    outcomes: Sequence[RoleOutcome],
    project_root: str,
    registry: ProcessRegistry,
    session_key: str | None = None,
) -> RoleOutcome:
    chair = _chair_role(panel)
    chair_context = build_reconciliation_context(panel, task, context, outcomes)
    try:
        return _invoke_role_once(
            chair,
            "Reconcile the completed Claude council into the final answer.",
            chair_context,
            project_root,
            registry,
            session_key,
            system_prompt_override=_chair_system_prompt(panel),
            user_prompt_override=chair_context,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        return RoleOutcome(
            chair,
            False,
            error=f"{type(exc).__name__}: {exc}",
        )


def _escape_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render_report(
    panel: PanelSpec,
    task: str,
    outcomes: Sequence[RoleOutcome],
    reconciliation: RoleOutcome | None,
    project_root: str,
    max_parallel: int,
) -> str:
    successful = sum(outcome.ok for outcome in outcomes)
    lines = [
        "# Claude Council Report",
        "",
        f"- **Panel:** {_escape_table(panel.name)} (`{panel.panel_id}`)",
        f"- **Project:** `{project_root}`",
        (
            f"- **Roles:** {len(outcomes)} "
            f"({successful} succeeded, {len(outcomes) - successful} failed)"
        ),
        f"- **Parallelism bound:** {min(max_parallel, max(1, len(outcomes)))}",
        f"- **Task:** {_escape_table(task)}",
        "",
    ]
    if reconciliation is not None:
        lines.extend(["## Reconciled answer", ""])
        if reconciliation.ok:
            lines.extend([reconciliation.text, ""])
        else:
            lines.extend([
                f"**Reconciliation failed:** {reconciliation.error}",
                "",
                "The independent role reports remain available below.",
                "",
            ])
    lines.extend([
        "## Panel execution",
        "",
        "| Role | Status | Session mode | Duration |",
        "|---|---|---|---:|",
    ])
    for outcome in outcomes:
        status = "ok" if outcome.ok else "failed"
        mode = "resumed" if outcome.resumed else "fresh"
        if outcome.stale_restarted:
            mode = "stale → fresh"
        lines.append(
            f"| {_escape_table(outcome.role.name)} (`{outcome.role.role_id}`) "
            f"| {status} | {mode} | {outcome.duration_seconds:.2f}s |"
        )
    lines.extend(["", "## Independent role reports", ""])
    for outcome in outcomes:
        lines.extend([
            f"### {outcome.role.name} (`{outcome.role.role_id}`)",
            "",
            outcome.text if outcome.ok else f"**Role failed:** {outcome.error}",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"
