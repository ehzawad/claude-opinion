# claude-opinion

An OpenAI Codex CLI skill that can ask either one persistent Claude Code agent or a persistent multi-role Claude council for a second opinion on the current project.

The council path mirrors the supplied Codex Council architecture in the opposite direction: Codex is the host, multiple independent Claude Code sessions review the same evidence through different roles, and a final Claude chair reconciles their findings before Codex performs its own verification.

## Execution modes

The multi-agent entry point is:

```text
scripts/claude_council.py
```

It implements:

- task-specific panel composition;
- one persistent Claude session per project, session namespace, and role;
- bounded parallel fan-out across role runners;
- private staging for context, panel metadata, role outputs, and the report;
- partial-failure containment;
- a persistent council-chair reconciliation pass;
- a complete aggregated Markdown report.

The prior single-agent entry point remains available:

```text
scripts/ask_claude.py
```

Use the single-agent path when one continuous Claude perspective is explicitly preferred. Use the council when independent lenses, dissent, and synthesis are valuable.

## Prerequisites

- [OpenAI Codex CLI](https://developers.openai.com/codex/cli), authenticated
- [Claude Code](https://code.claude.com/docs/en/overview), authenticated (`claude auth status`)
- Linux or macOS/POSIX file locking (`fcntl`)

## Install

```bash
mkdir -p ~/.agents/skills
git clone https://github.com/ehzawad/claude-opinion.git ~/.agents/skills/claude-opinion
```

For development from a separate checkout:

```bash
cd /path/to/claude-opinion
mkdir -p ~/.agents/skills
ln -s "$(pwd)" ~/.agents/skills/claude-opinion
```

Restart Codex only if `$claude-opinion` is not detected automatically.

## Update

```bash
python3 ~/.agents/skills/claude-opinion/scripts/update_skill.py
```

The updater refuses to overwrite local edits and uses a fast-forward-only pull.

## Council usage

Review the current working-tree changes:

```bash
{
  git diff HEAD
  git ls-files --others --exclude-standard | while IFS= read -r f; do
    file --mime "$f" | grep -q 'charset=binary' && continue
    printf '\n=== %s ===\n' "$f"
    cat "$f"
  done
} | python3 ~/.agents/skills/claude-opinion/scripts/claude_council.py
```

Give the council an explicit task:

```bash
printf '%s\n' "$(git diff HEAD)" \
  | python3 ~/.agents/skills/claude-opinion/scripts/claude_council.py \
      "Review this migration for correctness, rollback safety, and operational risk"
```

Choose a built-in panel and concurrency bound:

```bash
cat architecture-notes.md \
  | python3 ~/.agents/skills/claude-opinion/scripts/claude_council.py \
      --panel architecture \
      --max-parallel 3 \
      "Challenge this system design"
```

List built-in panels:

```bash
python3 ~/.agents/skills/claude-opinion/scripts/claude_council.py --list-panels
```

Skip the chair and return only the role reports:

```bash
cat context.md \
  | python3 ~/.agents/skills/claude-opinion/scripts/claude_council.py \
      --no-reconcile
```

Preserve the private run directory or also write the final report to a chosen path:

```bash
cat context.md \
  | python3 ~/.agents/skills/claude-opinion/scripts/claude_council.py \
      --keep-run-dir \
      --report-file ./claude-council-report.md
```

## Panel composition

`--panel auto` is the default. It always includes architecture, correctness, and adversarial-skeptic roles, then adds reliability, security, testing, research, and product-maintenance roles when the task and shared context contain corresponding signals.

The deterministic built-in panels are:

| Panel | Roles |
|---|---|
| `minimal` | systems architect, correctness reviewer, adversarial skeptic |
| `engineering` | architect, correctness, reliability, security, testing, skeptic |
| `architecture` | architect, correctness, reliability, product/maintenance, security, skeptic |
| `research` | research methodologist, correctness, architect, product/maintenance, skeptic |

A custom JSON panel overrides `--panel`:

```json
{
  "id": "migration-panel",
  "name": "Migration Panel",
  "reconciler_instruction": "Prefer reversible steps and explicit rollback gates.",
  "roles": [
    {
      "id": "schema-reviewer",
      "name": "Schema Reviewer",
      "instruction": "Trace schema compatibility, data conversion, and rollback invariants."
    },
    {
      "id": "operator",
      "name": "Production Operator",
      "instruction": "Analyze rollout, observability, partial failure, and recovery."
    },
    {
      "id": "skeptic",
      "name": "Adversarial Skeptic",
      "instruction": "Challenge whether the migration is necessary and whether a simpler path exists."
    }
  ]
}
```

Invoke it with:

```bash
cat context.md \
  | python3 ~/.agents/skills/claude-opinion/scripts/claude_council.py \
      --roles-file ./migration-panel.json
```

Role and panel identifiers must be lowercase path-safe identifiers. Duplicate role IDs are rejected. Changing a role's name or instruction changes its fingerprint and starts a fresh role session instead of resuming a thread with a different mandate.

## Bounded parallel fan-out

The council launches one top-level `claude -p --output-format json` process per role. It does not use Claude Code's `--agent` or `--agents` flags; process-level role isolation and persistence are controlled by this wrapper.

The default concurrency bound is four role processes. Configure it with:

```bash
export CLAUDE_COUNCIL_MAX_PARALLEL=4
```

or `--max-parallel N`. The supported bound is 1–16. Total panel size can exceed the concurrency bound; excess roles wait in the executor queue.

One project/session council run lock covers private staging, role fan-out, state persistence, and chair reconciliation. That prevents two council invocations from interleaving turns into the same role sessions. Different projects or explicit session namespaces can proceed independently.

## Per-project, session, and role continuity

Project identity is the canonical Git worktree root, falling back to the canonical current directory outside Git. Council state lives under:

```text
$XDG_STATE_HOME/claude-opinion/council/
```

The default root is:

```text
~/.local/state/claude-opinion/council/
```

Each role gets a distinct state file keyed by:

```text
project hash + optional session hash + role id + role hash
```

A fresh role call lets Claude allocate a session ID. Later council runs in the same project resume that role with:

```text
claude -p --resume <stored-session-id>
```

The chair is also a persistent role (`council-chair`). It receives the original task, complete shared context, panel manifest, every successful report, and every role failure.

Use an independent council namespace within the same project:

```bash
export CLAUDE_COUNCIL_SESSION_KEY=architecture-review
```

If that variable is unset, the council falls back to `CLAUDE_OPINION_SESSION_KEY`, then to a project-wide default namespace.

State writes are private and atomic. Stored role sessions use compare-and-save, stale sessions use compare-and-clear, malformed state is quarantined, and a stale Claude session is retried once as a fresh role thread.

## Reconciliation semantics

The chair does not mechanically vote or average confidence. Its system instruction requires it to:

- verify claims against the supplied context and project when useful;
- identify consensus without treating majority as proof;
- resolve contradictions when evidence permits;
- preserve material minority dissent when it does not;
- reject weak claims explicitly;
- return prioritized decisions, next steps, and residual uncertainty.

The final Markdown output contains the reconciled answer, panel execution table, individual role reports, and contained role failures. If some roles fail but others succeed, the chair still receives those failures as evidence. If all roles fail, reconciliation is skipped and the command exits non-zero with the diagnostic report.

## Private staging and cancellation

Every run creates a `0700` temporary directory containing:

```text
context.md
panel.json
roles/<role-id>.md
report.md
```

Files are written with mode `0600`. The directory is removed after the run unless `--keep-run-dir` is passed.

All active Claude processes run in separate process groups. Ctrl-C terminates every active role or chair process group so Claude-spawned tool processes are not left orphaned.

## No per-agent wrapper timeout or truncation

Role and chair calls use blocking `communicate(input=complete_prompt)` without a timeout argument. The wrapper does not impose a wall-clock, prompt-size, result-size, turn, or spend limit on an individual Claude process, and it does not pass `--max-turns`, `--max-budget-usd`, or `--no-session-persistence`.

The bounded part is concurrency, not per-agent runtime. The invoking Codex host, shell tool, operating system, Claude Code CLI/service, account, network, and model context window still retain their intrinsic limits.

## Security

Claude processes currently inherit the established `--dangerously-skip-permissions` transport behavior. Council prompts are analysis-only and explicitly prohibit file mutation, but that instruction is not a filesystem sandbox. Do not use this skill on untrusted repositories or prompts.

The child environment strips `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, and `ANTHROPIC_BASE_URL` by default so an authenticated Claude.ai session is not silently displaced by API-key or proxy routing. Set `CLAUDE_OPINION_KEEP_ANTHROPIC_ENV=1` to preserve those variables intentionally.

## Single-agent usage

The original persistent one-agent path remains:

```bash
cat context.md \
  | python3 ~/.agents/skills/claude-opinion/scripts/ask_claude.py
```

It retains one session per canonical project/session key and no wrapper timeout.

## Test

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

## License

MIT
