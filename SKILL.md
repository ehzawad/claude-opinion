---
name: claude-opinion
description: Persistent Claude second opinion with either a bounded multi-role council or an explicit single-agent path. Invoke $claude-opinion, or naturally via “ask Claude”, “Claude council”, “panel review”, “second opinion”, “another perspective”, or “reconcile with Claude”.
---

# Claude Council Opinion

Use a task-specific panel of persistent Claude Code roles, then reconcile their independent reports. The single-agent transport remains available when the user explicitly asks for one Claude perspective.

## Default invocation contract

For a council review, pipe complete shared context to:

```bash
printf '%s\n' "<gathered context>" \
  | python3 ~/.agents/skills/claude-opinion/scripts/claude_council.py \
      "<task instruction>"
```

Run it synchronously. Do not wrap it in `timeout`, apply a short outer shell deadline, truncate stdin/stdout, or launch it in the background. Each role and chair process is intentionally unbounded by this wrapper; the council bounds only concurrent fan-out.

Do not start multiple copies for the same project/session key. The orchestrator already launches the bounded internal panel and holds a council run lock across the complete turn.

## Panel selection

Use `--panel auto` unless the task clearly calls for a deterministic panel:

- `--panel minimal` for a small architect/correctness/skeptic review;
- `--panel engineering` for implementation, reliability, security, and testing;
- `--panel architecture` for system boundaries, operations, migration, and maintenance;
- `--panel research` for evidence, methods, reproducibility, and application.

Set `--max-parallel N` when the user requests a smaller or larger concurrency bound. The default is four and the supported range is 1–16.

Use `--roles-file <json>` only when the user supplies a panel or the task needs domain roles not covered by the built-ins. Role IDs must be unique lowercase path-safe identifiers. Each role needs an explicit mandate; avoid redundant personas that would produce correlated reports without adding a distinct lens.

## Building shared context

When the target is the working-tree change set:

```bash
{
  git diff HEAD
  git ls-files --others --exclude-standard | while IFS= read -r f; do
    file --mime "$f" | grep -q 'charset=binary' && continue
    printf '\n=== %s ===\n' "$f"
    cat "$f"
  done
} | python3 ~/.agents/skills/claude-opinion/scripts/claude_council.py \
      "Review the current changes"
```

There is no per-file byte guard. Binary files are skipped because stdin is textual, not because of a quota. When binary evidence matters, provide a textual inspection or let Claude examine the project directly.

When the repository is clean or the request names a specific target, gather the narrowest complete evidence implied by the user. Do not enumerate unrelated generated directories or wrappers merely to inflate context. Never pipe an empty string.

## Session and role continuity

The council uses one session per canonical project, optional council session key, and role ID. Calls from sibling subdirectories of the same Git worktree use the same state namespace and run Claude from the canonical project root.

Use an independent thread family within the same project with:

```bash
export CLAUDE_COUNCIL_SESSION_KEY=architecture-review
```

If unset, `CLAUDE_OPINION_SESSION_KEY` is used as a fallback. Changing a role's name or mandate invalidates its fingerprint and starts a fresh role thread rather than resuming a different persona under the same ID.

A stale role or chair session is compare-and-cleared and retried once fresh. Other errors are retained as failed role reports rather than aborting the entire panel. If at least one role succeeds, the chair receives both successes and failures.

## Reconciliation path

The script's persistent `council-chair` receives:

1. the original task;
2. the complete shared context;
3. the panel manifest;
4. every role report;
5. every contained role failure.

The chair must not vote mechanically. It should verify claims, distinguish consensus from proof, resolve contradictions where possible, preserve material dissent, reject weak claims, and prioritize next actions.

After the script returns, Codex remains the final host-side arbiter. Check the chair's claims against the repository and your own reasoning. Report what you accept, what you reject, what changed your assessment, and what remains uncertain; do not merely relay the council report.

## Private run directory

The orchestrator stages `context.md`, `panel.json`, role outputs, and `report.md` in a private `0700` temporary directory with `0600` files. It deletes the directory normally. Use `--keep-run-dir` only when diagnostics or provenance require preserving it, and surface the path to the user.

## Cancellation and failure handling

Ctrl-C terminates all active role or chair process groups. Partial role failures are included in the report. If all roles fail, the script exits non-zero without running the chair. A failed chair also returns the independent reports and exits non-zero.

## Explicit single-agent path

When the user specifically wants one Claude perspective, use:

```bash
printf '%s\n' "<gathered context>" \
  | python3 ~/.agents/skills/claude-opinion/scripts/ask_claude.py \
      "<task instruction>"
```

Do not use the single-agent path merely to save effort when the user requested a panel or council.
