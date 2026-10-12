# Changelog

All notable changes to agent-resurrect are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). Before 1.0, a minor release may change the command line, the config keys or the installer's behaviour; each such change is listed here.

## [Unreleased]

## [0.2.1] - 2026-10-13

### Added
- When `agent-resurrect` finds nothing recorded for a pane, it offers to search that pane's recent scrollback (iTerm2) for the session that ran there, shows the match, and asks before resuming it: yes by default for a strong match, no for a weak or ambiguous one. No copying and pasting needed.
- `agent-resurrect -y` (or `here --yes`) searches the scrollback without asking and resumes a strong match straight away. A weak or ambiguous match is still asked about, and the search still needs a terminal.
- `agent-resurrect find --from-pane` does the same search and only prints the result. Its JSON output marks `query.source` as `pane`.
- `find` and the pane search list another session only when it matches at least half as many search probes as the best match. A match on every probe is shown alone, instead of next to sessions that share a single line of the text, such as subagents the session started or a session the text was pasted into. `--json` still lists every candidate.
- While the scrollback is read and the sessions are searched, a one-line spinner with the elapsed seconds shows that it is working. It appears only in a terminal, never in `--json` output or pipes.

### Safety
- The scrollback is read only when a person types the command in a terminal, only for that pane, and only for the search. It is never stored, logged, printed or sent anywhere. Without a terminal, `find --from-pane` exits with status 4 and reads nothing.
- A match that is still running in another pane is refused (status 5) instead of being opened twice.

## [0.2.0] - 2026-10-12

Makes agent-resurrect safe and predictable for AI agents and scripts, turns the Claude Code plugin into a complete install route, changes only iTerm2's window setting instead of a macOS-wide one, and fixes every confirmed bug from the 0.1.0 review. Still macOS, iTerm2 and Claude Code only.

### Upgrading from 0.1.0

- Run the install line again. Your records, config and saved original settings are kept.
- 0.1.0 turned off the macOS-wide "Close windows when quitting an application" setting. 0.2.0 puts your original value back and sets the same preference in iTerm2's own domain instead, so other apps behave as they did before you installed.
- Exit codes changed: "not found" is now 3 instead of 1, and a session that is still running or whose transcript is gone now exits 5 or 6. Scripts that checked for 1 need updating; see the README's exit code table.
- `install` run without a terminal now needs `--yes`, as `uninstall` already did; 0.1.0 went ahead without asking.
- The `resume-by-text` skill is now `resume-session` (`/agent-resurrect:resume-session`).
- The one-line installer moved to `https://github.com/imgovind/agent-resurrect/releases/latest/download/install.sh`.

### Added

- `--json` on `ls`, `find`, `doctor`, `install`, `uninstall`, `config`, `resume`, `here`, `simulate`, `adopt` and `version`: exactly one ASCII JSON object on stdout, with `schema`, `version`, `ok` and `error` in every document. JSON Schemas for each command are in `docs/json/`.
- Documented exit codes: 0 `ok`, 1 `error`, 2 `usage`, 3 `not-found`, 4 `needs-tty`, 5 `busy`, 6 `gone`, 130 `interrupted`.
- `agent-resurrect simulate [--assume-restart] [--pane-key KEY]`: shows what each recorded pane would do at its next first prompt, or after a restart, without launching or changing anything.
- `agent-resurrect adopt`: records the Claude Code session it runs under, for sessions that were already running when you installed.
- `agent-resurrect version`.
- `ls --ids` shows session ids.
- `resume --print` and `here --print` print the resume command without running it.
- `install --dry-run`, `uninstall --dry-run` and `install.sh --dry-run` show what would change and write nothing. `install --no-plugin` leaves the Claude Code plugin out.
- `doctor --json` with a stable id per check, a fix `kind` and `agent_may_run` flag for each problem, and a `ready` verdict. `doctor --strict` also fails on warnings.
- New doctor checks for setups where a restore can never fire: a login shell other than zsh or bash, running inside tmux, screen or zellij, a `ZDOTDIR` zsh does not see at startup, and a `.zshrc` that starts tmux before the agent-resurrect block.
- Install from inside Claude Code: `/plugin marketplace add imgovind/agent-resurrect`, `/plugin install agent-resurrect@agent-resurrect`, then `/agent-resurrect:setup`. The plugin now contains everything it runs, so its hooks record sessions without the one-line installer, and the setup skill runs the installer from the plugin's own files with no download.
- The `setup` skill, which runs only when you type it.
- Releases attach an `install.sh` pinned to the release's exact commit, and `SHA256SUMS`. A release workflow checks that every version string matches the tag.
- A `restores.jsonl` result `lost-race`, for a shell that found its record already claimed by another shell in the same pane.
- README sections: what it supports today, sessions that are already running, try it without restarting, troubleshooting, upgrade, for AI coding agents, and why `claude --continue` is not enough. More similar projects: claude-hibernate, claude-sessions and Quil.
- `CONTRIBUTING.md`, `SECURITY.md`, `AGENTS.md`, `llms.txt`, a `Makefile` (`test`, `lint`, `install-dev`), and issue and pull request templates.

### Changed

- The window setting is now `iterm.keep_windows`, which writes `NSQuitAlwaysKeepsWindows` in iTerm2's preferences only. It replaces `macos.keep_windows`, and both install and uninstall migrate a 0.1.0 manifest entry by restoring your global value.
- `resume`, `here` and the bare command never start an agent without a terminal on stdin and stdout. They print the resume command, change nothing, and exit 4.
- `--json` on `resume`, `here` and the bare command implies `--print`: it prints one document with `action` `printed` and the resume command, exits 0 when the session can be resumed, and never starts anything, with or without a terminal. A session that is still running or gone, or an unknown id, still exits 5, 6 or 3.
- `install` and `uninstall` need `--yes` when they cannot ask (no terminal, or `--json`), and exit 4 otherwise. `--dry-run` never asks.
- `here` (and the bare command) in a pane with nothing recorded still lists what can be resumed, and now exits 3 instead of 0.
- `resume` and `here` print "still running" and "no longer exists" refusals on stderr, with exit 5 and 6, instead of on stdout with exit 1.
- `find` lists other candidates by title and session id, without numbers, because `resume <n>` reads numbers from `ls`.
- The resume skill is now `resume-session` and also handles "my sessions are gone after a restart" by listing what died. It prints commands and never runs them.
- The install prompt for Claude Code downloads the installer once, runs a dry run, waits for your OK, runs the same file it read, and acts only on doctor fixes marked safe for an agent.
- The shell block adds `~/.local/bin` to the end of PATH instead of the front, so tools you already have keep priority.
- `~/.zshrc` is no longer created when your login shell is not zsh and the file does not exist.
- The plugin moved from `plugin/` to the repository root, its hooks run `${CLAUDE_PLUGIN_ROOT}/bin/agent-resurrect`, and the Python entry point moved from `bin/agent-resurrect.py` to `libexec/agent-resurrect.py`.
- The state folders are readable only by you.

### Fixed

- Claude Code sessions inside tmux, screen or zellij in an iTerm2 pane all shared the outer pane's record, so they overwrote each other and resumed outside the multiplexer. They are no longer recorded or restored.
- Process start times were read in the user's locale. In Chinese, Japanese and Korean locales nothing was recorded, and other locale mismatches made running sessions look dead. `ps` now always runs in the C locale.
- Two shells in the same pane could both resume one record. The record is now claimed in one atomic step, and only one shell launches.
- Uninstall with an unreadable, corrupt or newer-version manifest reverted nothing, deleted the program files and exited 0. It now refuses without changing anything.
- A failed revert during uninstall deleted the program files, so uninstall could not be retried. The program files and the failed setting's entry are now kept.
- Sessions started with a custom `CLAUDE_CONFIG_DIR` are now found and resumed with the same value.
- When `claude` is not on PATH, resuming falls back to `~/.claude/local/claude` if it exists. A failed launch now marks the record skipped, so new shells stop retrying, and prints what to do.
- The SessionEnd hook returns at once unless a deliberate exit ends the pane's recorded session, so it stays well inside Claude Code's time limit for that hook.
- Opening a pane or running `ls` read whole transcripts, which took about half a second for very large ones. Only the start and end of a transcript are read now.
- Agent-team teammate processes are excluded from recording on purpose, not by accident.
- `ls`, `resume <n>`, `doctor` and automatic restore deleted records older than `max_age_days` even while their session was still running, so a session that ran for weeks without restarting lost its record. Records of running sessions are now kept.

## [0.1.0] - 2026-10-12

First release. Supports Claude Code in iTerm2 on macOS.

### Added

- Automatic resume after a restart: each iTerm2 pane that was running a Claude Code session resumes that same session in the same pane and folder, at the shell's first prompt.
- Four restore behaviours through `auto_resume`: `countdown` (default, 3 seconds, any key skips), `immediate`, `ask` and `off`.
- Permission mode mirroring through `permission_flag` (`mirror`, `always`, `never`): a session that ran with `--dangerously-skip-permissions` comes back with it by default.
- Commands: `agent-resurrect` (resume this pane now), `ls`, `resume <n or session-id>`, `find [text] [--clipboard]`, `doctor`, `config [key [value]]`, `install` and `uninstall [--purge]`, with `resurrect` as a short alias.
- Text search over Claude Code transcripts (`find`) for sessions that have no record, such as those started before install or outside iTerm2.
- A Claude Code plugin with SessionStart and SessionEnd hooks that record which pane each interactive session runs in, and a `resume-by-text` skill.
- zsh and bash first-prompt snippets that start no process when the pane has nothing to restore.
- A one-line installer, plus a copy-paste prompt for installing with Claude Code.
- Installer settings, each with an exact revert: macOS keeps windows on quit, iTerm2 restores windows at launch, iTerm2 opens at login (LaunchAgent), the shell rc block, and the Claude Code plugin. Original values are saved in a manifest before anything changes, and `agent-resurrect uninstall` restores every one.
- Adapter interfaces for agents and terminals, so other agents and terminals can be added later.

[Unreleased]: https://github.com/imgovind/agent-resurrect/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/imgovind/agent-resurrect/releases/tag/v0.2.1
[0.2.0]: https://github.com/imgovind/agent-resurrect/releases/tag/v0.2.0
[0.1.0]: https://github.com/imgovind/agent-resurrect/releases/tag/v0.1.0
