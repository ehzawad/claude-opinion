---
name: claude-opinion
description: Second opinion from Claude Code — you, Codex, and Claude in the loop. Invoke $claude-opinion, or naturally via phrases like "ask claude," "second opinion," "another perspective," "claude weigh in," "reconcile with claude."
---

# Claude Second Opinion

Get a second opinion from Claude Code on your current work. Claude uses your configured model, while the script selects the highest effort level supported by the installed Claude CLI.
Prefer `max` when Claude advertises explicit effort levels; treat `auto` as a fallback, not the preferred choice.

## How to call

Call with your context on stdin and **wait for the script to exit synchronously**. Codex CLI does not have a "fire and forget + notify on completion" shell mechanism (unlike Claude Code's `Bash run_in_background`); just run the call and let it block. A Claude call with `--effort max` and full file access often takes 1–5 minutes, occasionally longer. The script enforces its own subprocess timeout (`CLAUDE_OPINION_TIMEOUT` env var, default 600s) so it cannot wedge forever — but if Codex's outer per-command timeout is shorter than that, the work will be killed mid-flight. If your Codex environment caps shell commands aggressively, raise that cap before invoking this skill.

The script spawns `claude` with `--dangerously-skip-permissions` — Claude can read and write files during the opinion, so don't use this skill on untrusted projects. The default system-prompt directive instructs Claude to provide analysis only and not modify files. Custom instructions override that directive — when you pass one, restate the read-only constraint if you want it preserved.

To update the installed skill itself, run `python3 ~/.agents/skills/claude-opinion/scripts/update_skill.py`.

```bash
echo "<gathered context>" | python3 ~/.agents/skills/claude-opinion/scripts/ask_claude.py
```

The script appends a default review directive to Claude's system prompt. Pass a positional arg to override it, or `--no-default-instruction` to skip the directive.

## Building the context

If there are uncommitted changes:

```bash
git diff HEAD | python3 ~/.agents/skills/claude-opinion/scripts/ask_claude.py
```

If there are also untracked files that matter, include them (with binary/size guards):

```bash
{ git diff HEAD
  git ls-files --others --exclude-standard | while IFS= read -r f; do
      file --mime "$f" | grep -q 'charset=binary' && continue
      [ $(wc -c <"$f") -gt 32768 ] && continue
      printf '\n=== %s ===\n' "$f"
      cat "$f"
  done
} | python3 ~/.agents/skills/claude-opinion/scripts/ask_claude.py
```

If `git rev-parse --show-toplevel` fails and the user gave no target, ask the user what to review. Do not pick a nearby repo by recency, parent directory, or any other heuristic.

If the repo is clean and the user gave no scope, ask whether they want the staged/unstaged diff, a specific file/path, or a full-codebase opinion. Only auto-gather files when the target is explicit or the diff is non-empty.

When gathering context yourself with an explicit target, prefer the narrowest slice the user's request implies. Avoid enumerating `bin/`, `README`, and wrappers unless the user asked for a full review.

```bash
echo "Review this codebase. Key files: ..." | python3 ~/.agents/skills/claude-opinion/scripts/ask_claude.py
```

With a custom instruction:

```bash
echo "<context>" | python3 ~/.agents/skills/claude-opinion/scripts/ask_claude.py "user's instruction"
```

**Never pipe an empty string.** If `git diff HEAD` would be empty, use echo with gathered context instead.

## Session continuity

One Claude session per project, persisted across Codex sessions. Fresh calls let Claude allocate the session ID, and follow-up calls resume the prior Claude session so it keeps its accumulated codebase knowledge. Reframe when the task shifts so prior framing doesn't bias later turns. If the session has expired, the script logs a notice and starts fresh.

Set `CLAUDE_OPINION_SESSION_KEY` before launching Codex to isolate a session from the project-wide thread.

## After Claude responds

Reconcile Claude's findings with your own read, then tell the user what changed, what you accept, what you challenge, and what remains uncertain.

Multi-turn is available when a reply warrants follow-up (Codex → Claude → Codex → Claude → reconcile); default is single-turn.
