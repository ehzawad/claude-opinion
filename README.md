# claude-opinion

An OpenAI Codex CLI skill that asks one persistent Claude Code agent for a second opinion. It keeps Codex and Claude in the same project loop without turning the workflow into a panel or fan-out system.

This repository mirrors the direction of [`ehzawad/codex-opinion`](https://github.com/ehzawad/codex-opinion): Codex is the host, and Claude Code is the consulted model.

## What this version guarantees

The `scripts/ask_claude.py` entry point deliberately adds no wrapper-level wall-clock timeout, turn limit, budget limit, stdin-size cap, or stdout-size cap. It blocks synchronously until Claude exits or the caller explicitly interrupts it.

Those guarantees apply to this wrapper. The operating system, invoking shell/tool, Claude Code CLI, account, network, and model still have their own intrinsic limits. In particular, an outer Codex command timeout can still terminate a subprocess; the skill instructs Codex not to impose one.

The runtime launches exactly one top-level `claude -p` process. It does not pass `--agent`, `--agents`, `--max-turns`, `--max-budget-usd`, or `--no-session-persistence`, and it does not fan work out internally.

## Prerequisites

- [OpenAI Codex CLI](https://developers.openai.com/codex/cli), authenticated
- [Claude Code](https://code.claude.com/docs/en/overview), authenticated (`claude auth status`)
- POSIX-compatible file locking (`fcntl`), so Linux and macOS are supported by this implementation

## Install

```bash
mkdir -p ~/.agents/skills
git clone https://github.com/ehzawad/claude-opinion.git ~/.agents/skills/claude-opinion
```

For development from another checkout:

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

## Usage

Invoke the skill deterministically:

```text
$claude-opinion
```

Natural requests such as “ask Claude”, “get a second opinion”, and “reconcile with Claude” can also activate it.

Direct transport usage:

```bash
printf '%s\n' "<complete context>" \
  | python3 ~/.agents/skills/claude-opinion/scripts/ask_claude.py
```

Use a custom instruction:

```bash
printf '%s\n' "<complete context>" \
  | python3 ~/.agents/skills/claude-opinion/scripts/ask_claude.py \
      "Review this migration for correctness and operational risk"
```

Pass `--allow-edit` only when Claude should modify files. Pass `--no-default-instruction` for raw stdin passthrough.

## Project-scoped context resume

A fresh call lets Claude allocate its session ID. The wrapper stores that ID under:

```text
$XDG_STATE_HOME/claude-opinion/{project-hash}.json
```

The default state root is `~/.local/state/claude-opinion/`. The key is derived from the canonical Git worktree root; outside Git, it is derived from the canonical current directory. Therefore, calls made from different subdirectories of the same checkout resolve to the same wrapper state.

The Claude subprocess itself also runs with the canonical project root as `cwd`. That detail is essential: Claude Code associates resumable sessions with project directories/worktrees. Follow-up calls use `--resume <session-id>`, so the Claude transcript and accumulated context continue across separate Codex invocations in the same project.

Set an explicit key to maintain an independent thread in the same project:

```bash
export CLAUDE_OPINION_SESSION_KEY=architecture-review
```

This produces `{project-hash}-{session-hash}.json`. Unset it to return to the project-wide default thread.

## Ordering and concurrency

The workflow remains single-agent. A per-project/per-session `.run.lock` is held across:

```text
load session -> resume or start fresh -> receive final result -> save session
```

That serialization prevents two invocations from writing interleaved messages into the same Claude session. Different projects or explicit session keys can proceed independently.

State writes retain the existing atomic replace, corruption quarantine, generation-aware compare-and-save, compare-and-clear, and stale-session fallback behavior. If a stored session no longer exists, the wrapper clears only the matching stale generation and starts a fresh Claude session.

## Input and output behavior

`stdin` is read in full and sent as the prompt body. Claude’s `result` string is returned in full. The wrapper does not truncate either side and does not apply the previous 32 KiB guard when the skill gathers untracked text files.

This does not make the model context window infinite. For very large repositories, context selection still matters: send the narrowest complete evidence set that supports the requested review, and let Claude inspect the project directly when appropriate.

## Execution and cancellation

`claude --help` capability detection and the main `claude -p` call both run without wrapper timeouts. `CLAUDE_OPINION_TIMEOUT` is no longer consulted by the active entry point.

Pressing Ctrl-C is treated as explicit cancellation. The wrapper terminates Claude’s complete process group so child tool processes do not remain orphaned.

## Security and authentication routing

Claude runs with `--dangerously-skip-permissions`. The default system directive says not to modify files or run mutating commands; `--allow-edit` removes that guard. Do not use the skill on untrusted repositories or prompts.

The child environment strips `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, and `ANTHROPIC_BASE_URL` by default so an authenticated Claude.ai session is not silently displaced by API-key or proxy routing. Set `CLAUDE_OPINION_KEEP_ANTHROPIC_ENV=1` to preserve those variables intentionally.

The wrapper does not use `--bare`, so normal project instructions and Claude Code configuration remain available.

## Implementation note

`scripts/_ask_claude_core.py` preserves the earlier, well-tested transport and session implementation. `scripts/ask_claude.py` is the active policy entry point: it canonicalizes the project root, removes normal timeout enforcement, fixes the child working directory, and adds the per-thread run lock. Do not invoke the internal core file directly.

See [DESIGN.md](DESIGN.md) for the state and launch flow.

## Test

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

## License

MIT
