---
name: claude-opinion
description: Second opinion from Claude Code — you, Codex, and Claude in the loop. Invoke $claude-opinion, or naturally via phrases like "ask claude," "second opinion," "another perspective," "claude weigh in," "reconcile with claude."
---

# Claude Second Opinion

Get a second opinion from Claude Code on your current work. Claude uses your configured model, while the script selects the highest effort level supported by the installed Claude CLI.
Prefer `max` when Claude advertises explicit effort levels; treat `auto` as a fallback, not the preferred choice.

## How to call

Call with your context on stdin. A Claude call takes as long as the work takes; run it in the background (`codex` shell tooling will wait for the exit event) rather than sleep-polling. The script spawns `claude` with `--dangerously-skip-permissions` — Claude can read and write files during the opinion, so don't use this skill on untrusted projects.
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

If the tree is clean, gather context yourself:

```bash
echo "Review this codebase. Key files: ..." | python3 ~/.agents/skills/claude-opinion/scripts/ask_claude.py
```

With a custom instruction:

```bash
echo "<context>" | python3 ~/.agents/skills/claude-opinion/scripts/ask_claude.py "user's instruction"
```

**Never pipe an empty string.** If `git diff HEAD` would be empty, use echo with gathered context instead.

## Session continuity

One Claude session per project, persisted across Codex sessions. Follow-up calls resume the prior Claude session so it keeps its accumulated codebase knowledge. Reframe when the task shifts so prior framing doesn't bias later turns. If the session has expired, the script logs a notice and starts fresh.

Set `CLAUDE_OPINION_SESSION_KEY` before launching Codex to isolate a session from the project-wide thread.

## After Claude responds

Reconcile Claude's findings with your own read, then tell the user what changed, what you accept, what you challenge, and what remains uncertain.

Multi-turn is available when a reply warrants follow-up (Codex → Claude → Codex → Claude → reconcile); default is single-turn.
