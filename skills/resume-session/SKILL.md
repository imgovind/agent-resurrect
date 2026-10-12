---
name: resume-session
description: Finds a Claude Code session that was lost to a restart, crash or closed tab, and gives the user the exact command to resume it. Lists the sessions agent-resurrect recorded, or searches every transcript for text the user remembers or pastes. Prints the command and never starts the session.
when_to_use: Use when the user says things like "I lost my Claude session", "my Mac restarted and my sessions are gone", "resume where I left off", "which session was I working on X in", "find the session id for this conversation", or pastes text from an earlier conversation and asks where it came from. Not for resuming git operations, containers, downloads or anything that is not an agent conversation.
argument-hint: "[text you remember from the conversation]"
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/agent-resurrect ls *), Bash(${CLAUDE_PLUGIN_ROOT}/bin/agent-resurrect find *)
---

# Resume a session

Find the session, then give the user the command that resumes it. **Never run that command yourself.** Never run `agent-resurrect` with no arguments, `agent-resurrect here` or `agent-resurrect resume` either: on a terminal each one starts an interactive agent in place of the command. The two commands below give you everything you need.

Run the tool exactly as written below, by this plugin's own path. It works even when `agent-resurrect` is not on your PATH, and its `--json` output is the same in every version of this skill. Both commands only read: they change nothing.

## Steps

1. If the user gave text (after the skill name, or pasted in the conversation), go to step 4.
2. List the sessions that died:

   ```bash
   ${CLAUDE_PLUGIN_ROOT}/bin/agent-resurrect ls --json
   ```

3. Show `entries` as a short table: `session.title`, `session.cwd`, `session.last_active_at` and `pane.label`. An entry whose `auto_restore.eligible` is true comes back by itself the next time its pane opens a shell, so the user may only need to reopen iTerm2. For the session the user wants, give them `session.resume.command` in a code block, copied exactly, to run in the terminal pane where they want it. If `session.resume.warning` is not null, say what it says. In a terminal where agent-resurrect is installed, `agent-resurrect resume <session_id>` does the same; name sessions by `session.session_id`, not by `number`, because numbers change between listings. If the list is empty or the session is not in it, ask for a sentence they remember from that conversation and continue with step 4.
4. Search the transcripts for the text. Pass it on stdin with a quoted heredoc, so quotes, backticks and `$` in it are not interpreted:

   ```bash
   ${CLAUDE_PLUGIN_ROOT}/bin/agent-resurrect find --json <<'PASTED_TEXT_END'
   <the user's text, verbatim>
   PASTED_TEXT_END
   ```

5. Reply with the best match: `best.session.title`, its opening prompt (`best.session.first_prompt`), when it was active (`best.session.started_at` to `best.session.last_active_at`), and `best.session.resume.command` copied exactly into a code block. When the folder still exists, the command starts with `cd '<folder>' &&`.
6. If `confidence` is `weak` or `ambiguous`, do not guess: list `best` and each of `candidates` by `session.title` and `session.session_id`, and ask the user which one.

## Reading the result

| Result | Meaning |
|---|---|
| Exit 0, `confidence` is `strong` | Use `best`. |
| `confidence` is `weak` or `ambiguous` | Only part of the text matched, or another session matches as well. Show the candidates and ask. |
| `best.in_conversation` is false | The text appears only in tool output or injected context, so that session handled the text rather than discussed it. Say so. |
| `best.session.resume.warning` is not null | Usually the folder no longer exists, so the command has no `cd` and resumes from the current directory. Say so. |
| Exit 3, `error.code` is `not-found` | No transcript contains the text. Ask for a different snippet. |
| Exit 2, `error.code` is `usage` | The text is too short to identify a session. Ask for plain prose from one or two lines, not a table or code. |
| `agent-resurrect: Python 3.9 or newer is required` | Tell the user to install Apple's command line tools (`xcode-select --install` opens a dialog they must click through), and offer `claude --resume`, Claude Code's own session picker, meanwhile. |

The current session is excluded from the search automatically. Only top-level session transcripts are searched, not subagent logs. If the user expected their sessions to come back by themselves after a restart and `~/.local/bin/agent-resurrect` does not exist, the restore part is not installed yet: suggest `/agent-resurrect:setup`.
