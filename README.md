# claude-opinion

An OpenAI Codex CLI skill that brings Anthropic's Claude Code into your work as a distinct second model. You, Codex, and Claude in the loop. Install once; invoke from any Codex project.

Mirror of [ehzawad/codex-opinion](https://github.com/ehzawad/codex-opinion) (which brings Codex into Claude Code); this reverses the direction.

## Prerequisites

- [OpenAI Codex CLI](https://developers.openai.com/codex/cli) — authenticated (`codex` in terminal)
- [Claude Code](https://claude.ai/code) — authenticated (`claude auth status` should show logged in)

Both must be logged in and working in your terminal before using this skill.

## Install

For a first-time install, clone the skill directly into Codex's user-level skills directory:

```bash
mkdir -p ~/.agents/skills
git clone https://github.com/ehzawad/claude-opinion.git ~/.agents/skills/claude-opinion
```

This is a user-level install: Codex reads user skills from `$HOME/.agents/skills` and follows symlinked skill folders. If you are developing the skill from a separate checkout, symlink that checkout into the same user-level directory:

```bash
cd /path/to/claude-opinion
mkdir -p ~/.agents/skills
ln -s "$(pwd)" ~/.agents/skills/claude-opinion
```

If you previously installed this skill under `~/.codex/skills/claude-opinion`, install it again under `~/.agents/skills/claude-opinion`; the older path is not the current documented user-level skills location.

Codex usually detects newly installed skills automatically. Restart Codex if `$claude-opinion` does not appear.

## Usage

```text
$claude-opinion
```

Or naturally via phrases like "ask claude," "second opinion," "another perspective."

Use `$claude-opinion` for deterministic skill invocation. Codex reserves `/` for built-in slash commands; named skills are invoked via `$`.

## How it works

The script ships stdin to Claude via `claude -p --output-format json`, with a short generic review directive riding on `--append-system-prompt`. Stdin stays as pure context. On the first call per project, a fresh session UUID is pre-generated and saved after Claude produces a final answer. Follow-up calls resume the same session via `--resume <uuid>`, so Claude carries accumulated project knowledge across Codex sessions.

Codex reconciles Claude's response against its own assessment and reports the reconciled output to the user.

```mermaid
sequenceDiagram
    participant U as User
    participant C as Codex CLI
    participant S as ask_claude.py
    participant X as Claude Code

    U->>C: $claude-opinion (or natural trigger)
    C->>C: Compose adaptive context
    C->>S: Pipe context via stdin
    S->>X: claude -p --output-format json<br/>(env stripped, session id or resume)
    X-->>S: {result, is_error, session_id, ...}
    S->>S: Parse outer JSON, check is_error
    S-->>C: Claude's analysis via stdout
    C-->>U: Reconciles and reports
```

## Session management

One Claude session per project, stored at `$XDG_STATE_HOME/claude-opinion/{project-hash}.json` (default `~/.local/state/claude-opinion/...`). When a saved session is no longer resumable (Claude reports *"no conversation found with session ID …"*), the script clears the state, logs a notice, and starts fresh. Other resume failures surface as hard errors with Claude's stderr.

Set `CLAUDE_OPINION_SESSION_KEY` before launching Codex to scope state to that session — the state file becomes `{project-hash}-{session-hash}.json` and the session gets its own Claude thread.

See [DESIGN.md](DESIGN.md) for the session-management flowchart and JSON protocol diagram.

## Security

Claude runs with `--dangerously-skip-permissions` — no approval prompts. This gives Claude full read/write access to your machine so it can thoroughly inspect and analyze the current project. Do not use this skill on untrusted projects or with untrusted input.

## Configuration

The script uses your Claude Code defaults — model, effort, and other settings come from Claude Code's configuration. No model is hardcoded. Permission bypass is overridden by the skill (see Security above).

No subprocess timeout is enforced. Real failures surface via non-zero exit or a clean exit with no final message (both handled).

## Subprocess auth routing

The script strips `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, and `ANTHROPIC_BASE_URL` from the child `claude` process's environment so Claude.ai subscription auth wins over API-key or proxy-gateway routing (see [anthropics/claude-code#2051](https://github.com/anthropics/claude-code/issues/2051)). Without stripping, a present `ANTHROPIC_API_KEY` routes billing to the API key — which may be a different, possibly-empty balance than the subscription.

If you specifically *want* API-key or proxy routing for this skill, set `CLAUDE_OPINION_KEEP_ANTHROPIC_ENV=1` in your environment to skip the strip.

## License

MIT
