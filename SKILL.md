---
name: claude-opinion
description: Persistent single-agent second opinion from Claude Code — Codex and Claude in the same project loop. Invoke $claude-opinion, or naturally via “ask Claude”, “second opinion”, “another perspective”, “Claude weigh in”, or “reconcile with Claude”.
---

# Claude Second Opinion

Ask one persistent Claude Code agent to review the current work, then reconcile its response with your own assessment.

## Invocation contract

Call the script synchronously and wait for it to exit:

```bash
printf '%s\n' "<gathered context>" \
  | python3 ~/.agents/skills/claude-opinion/scripts/ask_claude.py
```

Do not wrap the command in `timeout`, do not set a short outer shell-tool deadline, and do not launch it in the background. The wrapper has no normal wall-clock timeout and is intended to run until Claude completes. An external host may still enforce its own hard limit; use the longest/disabled command timeout available to the host.

The wrapper also imposes no stdin-size, stdout-size, turn, or budget cap. Do not add `head`, `tail`, byte truncation, token truncation, `--max-turns`, or `--max-budget-usd` around the call merely to make it finish sooner.

Keep this workflow single-agent: invoke one top-level `ask_claude.py` process for the task. Do not fan out multiple Claude roles, pass `--agent`/`--agents`, or run concurrent copies against the same project/session key. Sequential follow-up turns are allowed when they materially improve the answer.

## Building context

When the target is the working-tree change set:

```bash
{
  git diff HEAD
  git ls-files --others --exclude-standard | while IFS= read -r f; do
    file --mime "$f" | grep -q 'charset=binary' && continue
    printf '\n=== %s ===\n' "$f"
    cat "$f"
  done
} | python3 ~/.agents/skills/claude-opinion/scripts/ask_claude.py
```

There is intentionally no per-file byte guard. Binary untracked files are skipped because stdin is textual, not because of a size quota. When binary evidence matters, describe it and provide a relevant textual inspection instead of dumping arbitrary bytes.

When the repository is clean or the request names a specific target, gather the narrowest complete context implied by the user. Do not enumerate unrelated wrappers, generated directories, or documentation merely to inflate context. If no Git root exists and the user gave no target, ask what should be reviewed rather than guessing a nearby project.

Never pipe an empty string. Supply a concrete prompt when `git diff HEAD` is empty:

```bash
printf '%s\n' "Review this codebase. Focus on: ..." \
  | python3 ~/.agents/skills/claude-opinion/scripts/ask_claude.py
```

Custom instruction:

```bash
printf '%s\n' "<context>" \
  | python3 ~/.agents/skills/claude-opinion/scripts/ask_claude.py \
      "Evaluate the design for correctness, failure modes, and migration risk"
```

The default directive requests a thorough second opinion and appends an analysis-only safety rule. Pass `--allow-edit` only when file mutation is explicitly desired. `--no-default-instruction` is raw passthrough.

## Session and context continuity

The default is one Claude session per canonical Git worktree/project. Calls from any subdirectory in that project use the same state key, run Claude with the project root as `cwd`, and resume the stored session with `--resume`. The transcript and accumulated project context therefore continue across Codex sessions.

Set `CLAUDE_OPINION_SESSION_KEY` before launching Codex when an independent thread is required within the same project. Calls sharing a project/session key are serialized by a per-thread run lock; do not bypass that ordering with parallel invocations.

If Claude reports that the stored session is stale, the wrapper logs the event, clears only the matching state generation, and starts fresh. Other resume errors are surfaced rather than silently discarding context.

When the task framing changes substantially, state the new framing explicitly so inherited context does not bias the review.

## After Claude responds

Reconcile rather than relay. Check Claude’s claims against the repository and your own reasoning, then report what you accept, what you reject, what changed your assessment, and what remains uncertain.
