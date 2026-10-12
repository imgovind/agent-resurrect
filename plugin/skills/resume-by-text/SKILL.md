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

3. Reply with the best match's `title`, `opens` (its opening prompt) and `active` (first and last activity) lines, and the command printed under `Resume command:`, copied exactly, in a code block, ready to copy. When the session's directory still exists the command starts with `cd '<dir>' &&`, so it resumes where the session ran.
4. If a `NOTE` line calls the match weak or ambiguous, show the `Other candidates:` list and ask which one in prose. Do not guess.

## Reading the output

| Output | Meaning |
|---|---|
| `text appears in: Claude x2, you x1` on the `BEST MATCH` line, `[tool output x1]` on a candidate | Where the text appears. Only `tool output` or `injected context` means that session merely handled the text (for example, grepped for it), not that it is the conversation. |
| `(3/4 probes; ...)`: fewer probes found than searched | Normal for text copied from the terminal: rendering strips markdown, so some probes miss. Still trust the match unless a `NOTE` flags it. |
| `NOTE    : weak match, only 1 probe found` or `NOTE    : ambiguous, another session matches equally well` | Not safe to pick on your own: show the candidates and ask. |
| `WARNING : working directory <dir> no longer exists; resuming from the current directory` | Removed worktree or folder. Say so. The command has no `cd`; `claude --resume` still finds the session, but it runs in the current directory. |
| `WARNING : the recorded cwd does not match the transcript's project folder` | The directory may have been moved or renamed. If resume says no conversation was found, run it from the folder's original directory. |
| `No session contains that text.` or `Pasted text is too short to identify a session` | Ask for a different snippet: plain prose from a single line or two, not a table or code block. |

The current session contains the pasted text too, so it is excluded automatically. Scope: top-level session transcripts only. Subagent logs are not searched.
