"""Shared council data types, constants, and errors."""
from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass
from typing import Any

import ask_claude as _transport

PROGRAM = "claude-council"
STATE_VERSION = 1
DEFAULT_MAX_PARALLEL = 4
MAX_PARALLEL_HARD_LIMIT = 16
ROLE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")

COUNCIL_STATE_DIR = os.path.join(_transport.STATE_DIR, "council")

ANALYSIS_ONLY_DIRECTIVE = (
    "Do not modify files or run mutating commands. This council is analysis-only; "
    "return findings, evidence, trade-offs, and recommendations."
)

ROLE_SYSTEM_BASE = """You are one independent member of a Claude Code review council.
Work from your assigned lens rather than trying to imitate the other roles. Inspect the
project directly when useful. Be concrete: cite files, functions, commands, invariants,
and failure paths. Distinguish verified facts from hypotheses. Surface disagreements you
expect other reviewers to miss. Do not merely summarize the supplied context."""

CHAIR_SYSTEM_PROMPT = """You are the chair of a Claude Code review council. Reconcile
independent role reports into one rigorous answer. Do not vote by majority or average
confidence mechanically. Check claims against the shared context and project when useful;
resolve contradictions where evidence permits, preserve material dissent where it does
not, and prioritize the smallest actionable set of decisions. Clearly separate consensus,
disagreement, rejected claims, residual uncertainty, and next steps."""

DEFAULT_TASK = (
    "Give a thorough multi-perspective second opinion on the supplied context. "
    "Identify wrong, missing, or incomplete assumptions, trade-offs, risks, and the "
    "highest-leverage next actions."
)


@dataclass(frozen=True, slots=True)
class RoleSpec:
    role_id: str
    name: str
    instruction: str


@dataclass(frozen=True, slots=True)
class PanelSpec:
    panel_id: str
    name: str
    roles: tuple[RoleSpec, ...]
    reconciler_instruction: str = ""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class RoleOutcome:
    role: RoleSpec
    ok: bool
    text: str = ""
    error: str = ""
    session_id: str | None = None
    resumed: bool = False
    stale_restarted: bool = False
    duration_seconds: float = 0.0
    result_meta: dict[str, Any] = dataclasses.field(default_factory=dict)


class CouncilError(RuntimeError):
    """Expected orchestration or configuration failure."""


class CorruptRoleStateError(CouncilError):
    """Role state was corrupt and could not be quarantined safely."""
