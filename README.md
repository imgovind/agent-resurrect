# agent-resurrect

Resurrect your AI coding agent sessions after a restart, in the same terminal pane. Claude Code + iTerm2 today; Codex, Cursor, opencode, Qoder, Ghostty, Warp next.

[![test](https://github.com/imgovind/agent-resurrect/actions/workflows/test.yml/badge.svg)](https://github.com/imgovind/agent-resurrect/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

```text
Resurrecting "Fix the login redirect" (claude) in /Users/me/project. Press any key to skip.
```

## Resume Claude Code after a restart

Your Mac restarts: an update, a crash, a flat battery. iTerm2 brings back every window, tab and pane, even the old scrollback. But each pane now holds a fresh shell, and the Claude Code sessions that were running there are gone.

The conversations are not lost, because Claude Code keeps them on disk. Getting them back is the tedious part: work out which session ran in which pane, find its id, go to the right folder, and type `claude --resume <id> --dangerously-skip-permissions` into each pane, one at a time.

agent-resurrect does that for you. Every pane that was running a Claude Code session resumes that same session by itself: same pane, same folder, same permission mode.

- Panes that were not running an agent are left alone.
- Sessions you quit on purpose stay quit.
- When a session cannot come back on its own, one command lists it and resumes it.

## Install

You need macOS, [iTerm2](https://iterm2.com), [Claude Code](https://claude.com/claude-code), and Python 3.9 or newer. Apple's command line tools include a suitable Python; install them with `xcode-select --install` if you don't have them.

Paste this into a terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/imgovind/agent-resurrect/main/install.sh | bash
```

Then open a new terminal tab. Claude Code sessions you start from now on are recorded.

The installer uses no `sudo` and only touches your home folder and your own preferences. At the end it prints everything it changed and runs `agent-resurrect doctor`. Details are in [What the installer changes](#what-the-installer-changes).

To read the script before running it:

```bash
curl -fsSL https://raw.githubusercontent.com/imgovind/agent-resurrect/main/install.sh -o install.sh
less install.sh
bash install.sh
```

### Install with Claude Code

Paste this prompt into Claude Code. It reads the installer, tells you what will change, installs, and checks the result.

```text
Install agent-resurrect from https://github.com/imgovind/agent-resurrect on this Mac. First download install.sh from that repo, read it, and tell me in a few bullets what it will change. Then run `curl -fsSL https://raw.githubusercontent.com/imgovind/agent-resurrect/main/install.sh | bash`. After it finishes, run `agent-resurrect doctor`; for anything not marked OK, run the fix it suggests and run doctor again. Do not use sudo. Finish by telling me what changed and the uninstall command.
```

More about this route: [docs/install-with-claude.md](docs/install-with-claude.md).

## Restore Claude Code sessions after a reboot in iTerm2

After a restart, this is what happens:

1. You log in. iTerm2 opens and restores its windows and panes.
2. Each pane's new shell reaches its first prompt and checks for a record for that pane. A pane with no record does nothing more; no process is started.
3. A pane that was running a Claude Code session prints:

   ```text
   Resurrecting "Fix the login redirect" (claude) in /Users/me/project. Press any key to skip.
   ```

   It counts down 3 seconds, then runs `claude --resume <session id>` in that folder, adding `--dangerously-skip-permissions` if the session was running in that mode.
4. If you press a key during the countdown, it prints:

   ```text
   Skipped. Run "agent-resurrect" in this pane to resume it later.
   ```

When you quit the resumed session, you are back at a normal shell prompt in that pane. Ctrl-C and Ctrl-Z go to Claude Code, as usual.

A pane whose session is still running is left alone. If the session's transcript no longer exists, the pane prints one line saying so and drops the record. If a pane did not come back at all (you closed the tab, or the window was not restored), run `agent-resurrect ls` in any pane and resume it from there.

## Commands

`resurrect` is a short alias for `agent-resurrect`; every command works with either name.

| Command | What it does |
|---|---|
| `agent-resurrect` | Resume the session that died in this pane, now, with no countdown. Works after you skipped the countdown. If this pane has nothing recorded, it lists what can be resurrected. Same as `agent-resurrect here`. |
| `agent-resurrect ls` | List sessions you can resurrect, numbered, newest first: agent, title, folder, last active, pane and status. Add `--all` to include sessions that are still running. |
| `agent-resurrect resume <n or session-id>` | Resume a listed session by its number, or any Claude Code session by its id, in the current pane. |
| `agent-resurrect find [text] [--clipboard]` | Find the session that contains some text you remember or copied, and print its resume command. It never starts anything. |
| `agent-resurrect doctor` | Check every setting agent-resurrect needs, the plugin and your PATH, and report restores since the last boot. Prints a fix for anything that fails. |
| `agent-resurrect config [key [value]]` | Show all settings, show one, or change one. |
| `agent-resurrect uninstall [--purge]` | Undo everything the installer changed. `--purge` also deletes records, logs and config. |

Examples you can paste:

```bash
agent-resurrect ls
agent-resurrect resume 2
agent-resurrect find --clipboard
agent-resurrect find "rename the users table migration"
pbpaste | agent-resurrect find
agent-resurrect config auto_resume ask
```

`find` searches your Claude Code transcripts, so it also covers sessions started before you installed agent-resurrect or outside iTerm2. Inside Claude Code, the plugin's `resume-by-text` skill does the same search: run `/agent-resurrect:resume-by-text` followed by the pasted text.

`agent-resurrect install` re-applies the settings, for example after you install iTerm2 or Claude Code later; `doctor` tells you when that is needed. The `auto` and `hook` commands are used by the shell snippet and the plugin; you don't run them yourself.

## Configuration

Settings live in `~/.config/agent-resurrect/config`. The file is optional; set values with `agent-resurrect config <key> <value>` or edit it directly:

```ini
[agent-resurrect]
auto_resume = countdown
countdown_seconds = 3
permission_flag = mirror
max_age_days = 14
```

| Key | Values | Default | Meaning |
|---|---|---|---|
| `auto_resume` | `countdown`, `immediate`, `ask`, `off` | `countdown` | What a restored pane does. `countdown` waits and lets you skip; `immediate` resumes at once; `ask` asks `[Y/n]` first; `off` only prints the resume command. |
| `countdown_seconds` | integer | `3` | Length of the countdown. |
| `permission_flag` | `mirror`, `always`, `never` | `mirror` | When to add `--dangerously-skip-permissions`. See [Permission mode](#permission-mode). |
| `max_age_days` | integer | `14` | Records not updated for this many days are not restored automatically and are cleaned up. |

## What the installer changes

For panes to come back after a restart, macOS and iTerm2 have to keep and restore your windows, and iTerm2 has to open at login. The installer sets that up, then adds the pieces that record and resume sessions.

| What | Change | What uninstall does |
|---|---|---|
| macOS window restoration | Turns off "Close windows when quitting an application" (System Settings, Desktop & Dock), so windows survive a quit or restart. | Puts back your original value, or removes the setting if it was never set. |
| iTerm2 startup | Sets iTerm2's window restoration policy to "Use System Window Restoration Setting" (Settings, General, Startup), so it reopens your windows and panes. | Puts back your original iTerm2 values. |
| iTerm2 at login | Adds a LaunchAgent, `~/Library/LaunchAgents/io.github.imgovind.agent-resurrect.iterm.plist`, that opens iTerm2 when you log in. macOS loads it at your next login, so installing it does not open or switch to iTerm2. It asks for no Automation permission. Skipped if iTerm2 is not installed. | Unloads it if it is loaded, and deletes the file. |
| Shell startup | Adds a marked block to `~/.zshrc`, and to `~/.bash_profile` or `~/.bashrc` if you have one. The block puts `~/.local/bin` on your PATH if it is missing and loads the first-prompt check. A symlinked rc file stays a symlink; its target is edited in place. | Removes exactly that block, leaving every other byte of the file as it was. |
| Claude Code plugin | Adds a local plugin marketplace from `~/.local/share/agent-resurrect` and installs the `agent-resurrect` plugin: two session hooks that record which pane a session runs in, and the `resume-by-text` skill. Skipped if `claude` is not installed. | Uninstalls the plugin and removes the marketplace. |
| Program files | Code in `~/.local/share/agent-resurrect/`, the commands `~/.local/bin/agent-resurrect` and `~/.local/bin/resurrect`. | Deletes them. |

The shell block looks like this:

```bash
# >>> agent-resurrect >>>
# Managed by agent-resurrect. Remove with: agent-resurrect uninstall
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
[ -f "$HOME/.local/share/agent-resurrect/shell/agent-resurrect.zsh" ] && . "$HOME/.local/share/agent-resurrect/shell/agent-resurrect.zsh"
# <<< agent-resurrect <<<
```

Before changing anything, the installer saves each original value in `~/.local/state/agent-resurrect/manifest.json`. A setting that is already the way it needs to be is left alone and is never reverted. Running the installer again, for example to upgrade, keeps the originals from the first install.

### Uninstall

```bash
agent-resurrect uninstall
```

Uninstall restores every original value. Add `--purge` to also delete the session records, the log and your config (`~/.local/state/agent-resurrect/` and `~/.config/agent-resurrect/`).

## How it works

The Claude Code plugin's hooks run when a session starts or ends. Each one writes a small JSON record keyed by the iTerm2 pane it runs in. iTerm2 keeps a pane's id (`ITERM_SESSION_ID`) across a restart, so after the restart the same pane can find its own record.

```text
  While you work                             After a restart

  Claude Code in an iTerm2 pane              iTerm2 reopens the same pane
  (plugin hooks on session start/end)        (same pane id, fresh shell)
                 │                                         │
                 │ agent-resurrect hook claude             │ first prompt
                 ▼                                         ▼
  ┌──────────────────────────────┐          ┌──────────────────────────────┐
  │ write or delete this pane's  │          │ shell check: is there a      │
  │ record                       │          │ record for this pane?        │
  └──────────────┬───────────────┘          │   no:  nothing runs          │
                 │                          │   yes: agent-resurrect auto  │
                 ▼                          └──────────────┬───────────────┘
  ┌──────────────────────────────┐   reads, marks          │
  │ ~/.local/state/              │◄────────────────────────┤
  │   agent-resurrect/panes/     │                         ▼
  │   iterm-<pane id>.json       │          ┌──────────────────────────────┐
  └──────────────────────────────┘          │ countdown, then              │
                                            │ claude --resume <id>         │
                                            │ in the same pane and folder  │
                                            └──────────────────────────────┘
```

Recording:

- A record holds the session id, folder, transcript path, permission mode, and the Claude Code process id with its start time.
- `/clear`, `/resume` and similar commands start a new session id in the same pane; the newest one wins.
- Quitting from the prompt (for example `/exit`) or `/logout` deletes the record. A restart, crash or closed tab keeps it.
- Only the interactive session in the pane is recorded. A `claude -p` run, a background process, or a Claude Code started by another agent's tool call never overwrites the pane's record.
- The hooks always exit successfully and print nothing, so they never block or slow down Claude Code.

Restoring: a pane resumes its record automatically only when all of these hold.

1. The record is live (not already resumed or skipped).
2. The recorded Claude Code process is gone. A new process that reused the same pid does not count, because the start time must match too.
3. The session's transcript is still on disk.
4. The record is newer than `max_age_days`.

Before launching, the record is marked as used, so a crash loop cannot keep re-triggering it. Claude Code then replaces the `agent-resurrect` process, so no helper sits between your shell and Claude Code. A shell without a terminal for input and output, such as one started by a script, never waits for a countdown.

## Permission mode

A Claude Code session that was running with `--dangerously-skip-permissions` (bypass permissions mode) resumes in the default mode unless the flag is passed again. agent-resurrect handles this with `permission_flag`:

| Value | Behaviour |
|---|---|
| `mirror` (default) | Adds `--dangerously-skip-permissions` only if the session was last running in bypass permissions mode. Claude Code restores the other modes by itself. |
| `always` | Always adds the flag. |
| `never` | Never adds the flag. |

To change it:

```bash
agent-resurrect config permission_flag never
```

## Frequently asked questions

**Does it work after a crash?**
Yes, when iTerm2 restores the panes. A crash does not end the session on purpose, so the record is kept.

**What if I exited a session on purpose?**
It is never restored. Quitting from the prompt (for example `/exit`) deletes the pane's record.

**What about sessions from before I installed?**
They have no record, because Claude Code loads the plugin when a session starts. Use `agent-resurrect find` with some text you remember or copy from the pane's scrollback; it prints the resume command.

**I closed a tab that was running Claude Code. Can I get it back?**
Yes. The pane will not come back, so nothing happens automatically, but `agent-resurrect ls` lists the session and `agent-resurrect resume <n>` resumes it in the current pane.

**Does it slow down my shell?**
No. The check runs once, at the first prompt, as a file test inside the shell. When the pane has nothing to restore, no process is started.

**Which shells are supported?**
zsh and bash, including macOS's bash 3.2. It runs after frameworks such as oh-my-zsh and powerlevel10k have finished loading.

**Does it need special permissions?**
No. There is no `sudo`, no Accessibility or Automation prompt, and nothing is written outside your home folder and your own preferences.

**Privacy?**
Everything stays on your Mac. agent-resurrect makes no network requests; the only download is the release archive the installer fetches from GitHub. `find` reads your Claude Code transcripts locally.

## Roadmap

Version 0.1.0 supports Claude Code in iTerm2 on macOS. Agents and terminals sit behind separate adapter interfaces, so each of these is a separate, separately tested addition:

- Agents: Codex, Cursor agent, opencode, Qoder. These will be recorded from the shell, since they may not offer Claude-style session hooks.
- Terminals: Warp, Ghostty, WezTerm, Kitty, tmux, Terminal.app.
- Reopening sessions whose panes were not restored in new tabs.
- A signed, notarized `.pkg` installer.

## Similar projects

Other people have solved parts of this problem, and agent-resurrect builds on their ideas. Each is worth a look; one of them may fit your setup better.

| Project | What it does |
|---|---|
| [cc-session-restore](https://github.com/asadtariq96/cc-session-restore) by asadtariq96 | A LaunchAgent snapshots your open Claude Code sessions every 60 seconds. After a reboot, `cc-session-restore restore` reopens them as tabs in one iTerm2 window, each resumed by its exact session id. One-line installer. |
| [claude-iterm2-resume-sessions](https://github.com/adrianschmidt/claude-iterm2-resume-sessions) by adrianschmidt | A Claude Code plugin whose SessionStart and SessionEnd hooks record each session's iTerm2 pane. At login, a LaunchAgent types `claude --resume <id>` back into the original pane, or opens it in a tab of a new window when that pane is gone or busy. The closest design to this one. |
| [cc-sessions](https://github.com/shsunmoonlee/cc-sessions) by shsunmoonlee | A broader session manager for Claude Code in iTerm2: a ledger written by Claude Code hooks, a daemon that hibernates idle sessions under memory pressure and wakes them when you click their tab, and restore after a crash or shutdown. |
| [PonyMux](https://github.com/heylittlepan/ponymux) | A native macOS terminal app, free but not open source, that organizes Claude Code, Codex and Grok sessions into lists and folders, searches their transcripts, and resumes or forks past sessions. |
| [Agent-Resume](https://github.com/fukuyori/Agent-Resume) (`agres`) by fukuyori | A cross-platform Go CLI with an interactive picker that resumes Claude Code, Codex, OpenCode, Aider and Antigravity CLI sessions from the current folder or all projects, with search and cleanup. |

How agent-resurrect differs:

- **It resumes in the original pane, by itself.** The pane's own shell resumes the session at its first prompt. Nothing types into panes from outside, so no Automation permission is needed.
- **One line installs everything,** including the macOS and iTerm2 settings that window restoration depends on. Every original value is recorded, and `agent-resurrect uninstall` puts each one back.
- **It mirrors the permission mode.** A session that was running with `--dangerously-skip-permissions` comes back with it, and you can change that.
- **It is built around adapters.** Agents and terminals are separate interfaces, so support for other agents and terminals can be added without changing the core. Today only Claude Code and iTerm2 ship.

Where others do more today: claude-iterm2-resume-sessions reopens sessions in a new window when their panes are gone, cc-sessions also manages memory, and Agent-Resume and PonyMux already work with several agents.

## Development

agent-resurrect is plain Python 3.9+ with the standard library only, plus small zsh and bash snippets and a bash installer.

```bash
git clone https://github.com/imgovind/agent-resurrect.git
cd agent-resurrect
./run_tests.sh
./install.sh
```

- `./run_tests.sh` runs the unit tests on `/usr/bin/python3` (3.9) and on `python3` from your PATH. Extra arguments go to `unittest`, for example `./run_tests.sh -k Store`.
- `./install.sh` run from a clone installs from your working tree instead of downloading a release.
- Tests never touch your real HOME, `defaults`, `launchctl` or Claude Code plugins; they use a temporary home and fake executables.

The design is in [docs/specs/2026-10-12-agent-resurrect-design.md](docs/specs/2026-10-12-agent-resurrect-design.md).

## License

[MIT](LICENSE). Copyright (c) 2026 Govindarajan Panneerselvam.
