"""Task-specific council panel definitions and validation."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable

from _council_types import CouncilError, PanelSpec, ROLE_ID_RE, RoleSpec

_ROLE_LIBRARY: dict[str, RoleSpec] = {
    "systems-architect": RoleSpec(
        "systems-architect",
        "Systems Architect",
        "Evaluate boundaries, ownership, state, interfaces, coupling, evolution paths, "
        "and whether the design's complexity is justified.",
    ),
    "correctness-reviewer": RoleSpec(
        "correctness-reviewer",
        "Correctness Reviewer",
        "Trace invariants and control flow. Find logical errors, hidden assumptions, edge "
        "cases, data-loss paths, and mismatches between stated and actual behavior.",
    ),
    "reliability-operator": RoleSpec(
        "reliability-operator",
        "Reliability and Operations Reviewer",
        "Analyze concurrency, cancellation, partial failure, retries, recovery, resource "
        "use, observability, upgrade behavior, and operational failure modes.",
    ),
    "security-reviewer": RoleSpec(
        "security-reviewer",
        "Security Reviewer",
        "Inspect trust boundaries, permissions, prompt and command injection, secret and "
        "state handling, filesystem safety, dependency risk, and abuse cases.",
    ),
    "test-strategist": RoleSpec(
        "test-strategist",
        "Test Strategist",
        "Assess verification quality. Propose high-value unit, integration, concurrency, "
        "property, fault-injection, and end-to-end tests with concrete oracles.",
    ),
    "research-methodologist": RoleSpec(
        "research-methodologist",
        "Research Methodologist",
        "Examine evidence quality, experimental design, measurement validity, statistics, "
        "alternative hypotheses, reproducibility, and provenance.",
    ),
    "product-maintainer": RoleSpec(
        "product-maintainer",
        "Product and Maintenance Reviewer",
        "Evaluate user workflow, compatibility, migration cost, documentation, defaults, "
        "maintenance burden, and whether the result is understandable in practice.",
    ),
    "adversarial-skeptic": RoleSpec(
        "adversarial-skeptic",
        "Adversarial Skeptic",
        "Challenge the framing and apparent consensus. Look for simpler alternatives, "
        "category errors, overengineering, omitted constraints, and reasons the proposal "
        "could fail even if each local component works.",
    ),
}

_BUILTIN_PANELS: dict[str, PanelSpec] = {
    "minimal": PanelSpec(
        "minimal",
        "Minimal Critical Review",
        tuple(_ROLE_LIBRARY[key] for key in (
            "systems-architect",
            "correctness-reviewer",
            "adversarial-skeptic",
        )),
    ),
    "engineering": PanelSpec(
        "engineering",
        "Engineering Review Council",
        tuple(_ROLE_LIBRARY[key] for key in (
            "systems-architect",
            "correctness-reviewer",
            "reliability-operator",
            "security-reviewer",
            "test-strategist",
            "adversarial-skeptic",
        )),
    ),
    "architecture": PanelSpec(
        "architecture",
        "Architecture and Operations Council",
        tuple(_ROLE_LIBRARY[key] for key in (
            "systems-architect",
            "correctness-reviewer",
            "reliability-operator",
            "product-maintainer",
            "security-reviewer",
            "adversarial-skeptic",
        )),
    ),
    "research": PanelSpec(
        "research",
        "Research and Evidence Council",
        tuple(_ROLE_LIBRARY[key] for key in (
            "research-methodologist",
            "correctness-reviewer",
            "systems-architect",
            "product-maintainer",
            "adversarial-skeptic",
        )),
    ),
}

_AUTO_SIGNALS: dict[str, tuple[str, ...]] = {
    "reliability-operator": (
        "concurrency", "parallel", "thread", "process", "session", "resume", "lock",
        "timeout", "retry", "failure", "recovery", "deploy", "production", "daemon",
        "queue", "worker", "database", "distributed", "network", "stream",
    ),
    "security-reviewer": (
        "security", "auth", "authentication", "authorization", "permission",
        "secret", "token", "credential", "injection",
        "sandbox", "untrusted", "privilege", "access control", "encryption", "privacy",
    ),
    "test-strategist": (
        "test", "bug", "fix", "implementation", "code", "refactor", "migration",
        "regression", "unit", "integration", "property", "coverage",
    ),
    "research-methodologist": (
        "research", "paper", "study", "experiment", "dataset", "benchmark", "metric",
        "statistics", "hypothesis", "evidence", "evaluation", "model quality",
    ),
    "product-maintainer": (
        "user", "workflow", "api", "cli", "compatibility", "migration", "documentation",
        "maintain", "release", "adoption", "product", "interface", "default",
    ),
}


def _validate_role(role: RoleSpec) -> RoleSpec:
    if not isinstance(role.role_id, str) or not ROLE_ID_RE.fullmatch(role.role_id):
        raise CouncilError(
            f"Invalid role id {role.role_id!r}; use lowercase letters, digits, '.', '_' or '-'."
        )
    if not isinstance(role.name, str) or not role.name.strip():
        raise CouncilError(f"Role {role.role_id!r} has an empty name")
    if not isinstance(role.instruction, str) or not role.instruction.strip():
        raise CouncilError(f"Role {role.role_id!r} has an empty instruction")
    # Names are presentation metadata and must remain one line; mandates may be
    # intentionally multi-line.
    name = " ".join(role.name.split())
    return RoleSpec(role.role_id, name, role.instruction.strip())


def _validate_panel(panel: PanelSpec) -> PanelSpec:
    if not panel.roles:
        raise CouncilError("A council panel must contain at least one role")
    roles = tuple(_validate_role(role) for role in panel.roles)
    ids = [role.role_id for role in roles]
    duplicates = sorted({role_id for role_id in ids if ids.count(role_id) > 1})
    if duplicates:
        raise CouncilError(f"Duplicate role ids: {', '.join(duplicates)}")
    if not isinstance(panel.panel_id, str):
        raise CouncilError(f"Invalid panel id: {panel.panel_id!r}")
    panel_id = panel.panel_id.strip() or "custom"
    if not ROLE_ID_RE.fullmatch(panel_id):
        raise CouncilError(f"Invalid panel id: {panel.panel_id!r}")
    if not isinstance(panel.name, str):
        raise CouncilError("Panel name must be a string")
    if not isinstance(panel.reconciler_instruction, str):
        raise CouncilError("reconciler_instruction must be a string")
    name = " ".join(panel.name.split()) or panel_id
    return PanelSpec(panel_id, name, roles, panel.reconciler_instruction.strip())


def _load_roles_file(path: str) -> PanelSpec:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise CouncilError(f"Could not load roles file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CouncilError("Roles file must contain a JSON object")
    raw_roles = payload.get("roles")
    if not isinstance(raw_roles, list):
        raise CouncilError("Roles file must contain a 'roles' array")
    roles: list[RoleSpec] = []
    for index, item in enumerate(raw_roles):
        if not isinstance(item, dict):
            raise CouncilError(f"roles[{index}] must be an object")
        try:
            role_id = item["id"]
            instruction = item["instruction"]
        except KeyError as exc:
            raise CouncilError(f"roles[{index}] is missing {exc.args[0]!r}") from exc
        name = item.get("name") or role_id
        if not isinstance(role_id, str):
            raise CouncilError(f"roles[{index}].id must be a string")
        if not isinstance(name, str):
            raise CouncilError(f"roles[{index}].name must be a string")
        if not isinstance(instruction, str):
            raise CouncilError(f"roles[{index}].instruction must be a string")
        roles.append(RoleSpec(role_id, name, instruction))
    panel_id = payload.get("id") or "custom"
    panel_name = payload.get("name") or "Custom Claude Council"
    reconciler_instruction = payload.get("reconciler_instruction") or ""
    if not isinstance(panel_id, str) or not isinstance(panel_name, str):
        raise CouncilError("Panel id and name must be strings")
    if not isinstance(reconciler_instruction, str):
        raise CouncilError("reconciler_instruction must be a string")
    panel = PanelSpec(
        panel_id,
        panel_name,
        tuple(roles),
        reconciler_instruction,
    )
    return _validate_panel(panel)


def _contains_any(text: str, signals: Iterable[str]) -> bool:
    for signal in signals:
        if " " in signal:
            if signal in text:
                return True
        elif re.search(rf"\b{re.escape(signal)}\b", text):
            return True
    return False


def _compose_auto_panel(task: str, context: str) -> PanelSpec:
    text = f"{task}\n{context}".lower()
    role_ids = ["systems-architect", "correctness-reviewer"]
    for role_id, signals in _AUTO_SIGNALS.items():
        if _contains_any(text, signals):
            role_ids.append(role_id)
    role_ids.append("adversarial-skeptic")
    # Preserve insertion order when one role is selected by multiple domains.
    unique_ids = tuple(dict.fromkeys(role_ids))
    return _validate_panel(PanelSpec(
        "auto",
        "Task-Composed Claude Council",
        tuple(_ROLE_LIBRARY[role_id] for role_id in unique_ids),
    ))


def compose_panel(
    panel_name: str,
    task: str,
    context: str,
    roles_file: str | None = None,
) -> PanelSpec:
    """Compose a validated role panel from a custom manifest or built-in policy."""

    if roles_file:
        return _load_roles_file(roles_file)
    normalized = panel_name.strip().lower()
    if normalized == "auto":
        return _compose_auto_panel(task, context)
    try:
        return _validate_panel(_BUILTIN_PANELS[normalized])
    except KeyError as exc:
        choices = ", ".join(("auto", *_BUILTIN_PANELS.keys()))
        raise CouncilError(f"Unknown panel {panel_name!r}; choose one of: {choices}") from exc
