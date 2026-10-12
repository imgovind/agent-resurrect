---
name: resume-by-text
description: Use when the user pastes a snippet from an earlier Claude Code conversation and wants to find which session it came from, or wants a command to resume that session.
argument-hint: <pasted text from the old conversation>
---

# Resume by text

Finds the session whose transcript contains the pasted text and prepares the `claude --resume <id>` command for it. **Print the command; never run it.** It starts an interactive session, possibly with permission checks off, so the user launches it themselves.

The search is built into `agent-resurrect`, which this plugin comes with, so do not hand-roll it with `grep` or `rg`.

## Steps

1. Take everything after the skill name as the pasted text. If there is none, ask for it.
2. Pipe it to the finder on stdin with a quoted heredoc, so quotes, backticks and `$` in the paste are not interpreted by the shell:

```bash
agent-resurrect find <<'PASTED_TEXT_END'
<pasted text, verbatim>
PASTED_TEXT_END
```

   If the shell says `agent-resurrect: command not found`, run the same command with `"$HOME/.local/bin/agent-resurrect"` in its place.

3. Reply with the best match's title, opening prompt, last-active date, and the resume command from the output (`cd ... && claude --resume ...`) in a code block, ready to copy. The `cd` matters: sessions are looked up per directory.
4. If the output calls the match weak or ambiguous, or another candidate matches as many probes as the best one, show the candidate list and ask which one in prose. Do not guess.

## Reading the output

| Output | Meaning |
|---|---|
| Text appears in `Claude x2, you x1` | Where the text appears. Only `tool output` or `injected context` means that session merely handled the text (for example, grepped for it), not that it is the conversation. |
| Fewer probes found than searched | Normal for text copied from the terminal: rendering strips markdown, so some probes miss. Still trust the match unless it is flagged as weak or ambiguous. |
| A warning that the directory no longer exists | Removed worktree. Say so; the `cd` will fail. |
| `No session contains that text.` or the text is too short | Ask for a different snippet: plain prose from a single line, not a table or code block. |

The current session contains the pasted text too, so it is excluded automatically. Scope: top-level session transcripts only. Subagent logs are not searched.
