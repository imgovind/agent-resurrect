# agent-resurrect

Resume your Claude Code sessions in the same iTerm2 panes after your Mac restarts.

[![test](https://github.com/imgovind/agent-resurrect/actions/workflows/test.yml/badge.svg)](https://github.com/imgovind/agent-resurrect/actions/workflows/test.yml)
[![release](https://img.shields.io/github/v/release/imgovind/agent-resurrect)](https://github.com/imgovind/agent-resurrect/releases)
![macOS](https://img.shields.io/badge/platform-macOS-lightgrey)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

![iTerm2 panes resuming their own Claude Code sessions after iTerm2 reopens](docs/demo.gif)

```text
Resurrecting "Fix the login redirect" (claude) in /Users/me/project. Press any key to skip.
```

```bash
curl -fsSL https://github.com/imgovind/agent-resurrect/releases/latest/download/install.sh | bash
```

macOS, iTerm2 and Claude Code for now. [What it supports](#what-it-supports-today) · [Install](#install) · [Try it without restarting](#try-it-without-restarting) · [Troubleshooting](#troubleshooting) · [For AI coding agents](#for-ai-coding-agents) · [Uninstall](#uninstall)

## Resume Claude Code after a restart

Your Mac restarts: an update, a crash, a flat battery. iTerm2 brings back every window, tab and pane, even the old scrollback. But each pane now holds a fresh shell, and the Claude Code sessions that were running there are gone.

The conversations are not lost, because Claude Code keeps them on disk. Getting them back is the tedious part: work out which session ran in which pane, find its id, go to the right folder, and type `claude --resume <id> --dangerously-skip-permissions` into each pane, one at a time.

agent-resurrect does that for you. Every pane that was running a Claude Code session resumes that same session by itself: same pane, same folder, same permission mode.

- Panes that were not running an agent are left alone.
- Sessions you quit on purpose stay quit.
- When a session cannot come back on its own, one command lists it and resumes it.

## What it supports today

| | Supported in 0.2 | Not yet |
|---|---|---|
| Operating system | macOS | Linux, Windows |
| Terminal | iTerm2 | Terminal.app, Ghostty, Warp, WezTerm, Kitty |
| Inside a pane | A plain shell | tmux, screen or zellij: Claude Code sessions inside them are not recorded |
| Agent | Claude Code | Codex, Cursor agent, opencode, Qoder |
| Login shell | zsh, bash (including macOS bash 3.2) | fish, nushell |
| Python | 3.9 or newer (Apple's command line tools include one) | |

Two more limits:

- Sessions that were already running when you installed are not recorded until you adopt or restart them. See [Sessions that are already running](#sessions-that-are-already-running).
- Panes that iTerm2 does not restore (a closed tab, a window that did not come back) are not reopened automatically. `agent-resurrect ls` lists their sessions and `agent-resurrect resume <session-id>` resumes one in the current pane.

## Install

You need macOS, [iTerm2](https://iterm2.com), [Claude Code](https://claude.com/claude-code), and Python 3.9 or newer. Apple's command line tools include a suitable Python; install them with `xcode-select --install` if you don't have them.

Paste this into a terminal:

```bash
curl -fsSL https://github.com/imgovind/agent-resurrect/releases/latest/download/install.sh | bash
```

Then open a new iTerm2 tab. Claude Code sessions you start from now on are recorded.

The installer uses no `sudo` and only touches your home folder and your own preferences. Its window setting applies to iTerm2 only; other apps keep their own behaviour. At the end it prints everything it changed and runs `agent-resurrect doctor`. Details are in [What the installer changes](#what-the-installer-changes).

The install line always fetches the latest release. The `install.sh` attached to each release downloads the code of that release by its exact commit, so what you install cannot change after the release is published.

To read the script and see every change before anything is made:

```bash
curl -fsSL https://github.com/imgovind/agent-resurrect/releases/latest/download/install.sh -o install.sh
less install.sh
bash install.sh --dry-run
bash install.sh
```

`--dry-run` downloads the release to a temporary folder and prints what the installer would change, writing nothing under your home folder. Each release also lists checksums in `SHA256SUMS`; to check the file you downloaded:

```bash
curl -fsSL https://github.com/imgovind/agent-resurrect/releases/latest/download/SHA256SUMS -o SHA256SUMS
shasum -a 256 -c SHA256SUMS --ignore-missing
```

### Install from inside Claude Code

Run these three commands in a Claude Code session:

```text
/plugin marketplace add imgovind/agent-resurrect
/plugin install agent-resurrect@agent-resurrect
/agent-resurrect:setup
```

The first two install the plugin. From the next Claude Code session on, its hooks record which pane each session runs in, and `agent-resurrect ls` and `find` work in Claude Code's shell.

The plugin alone does not restore anything: the first-prompt check, the iTerm2 settings and the LaunchAgent come from the installer. `/agent-resurrect:setup` runs it. Claude only runs this skill when you type it. It shows you what the installer would change, asks before going ahead, installs from the plugin's own files (no download, no `sudo`), checks the result with `doctor`, and records the current session with `adopt`.

If you already used the one-line installer, skip this: it installed the plugin too, from a local marketplace that is also named `agent-resurrect`, so adding the GitHub marketplace as well would clash with it.

### Install with Claude Code

Paste this prompt into Claude Code. It downloads the installer once, shows you what it will change and waits for your OK, runs the same file it read, and checks the result.

```text
Install agent-resurrect (https://github.com/imgovind/agent-resurrect) on this Mac. Do these steps in order. 1) Download the installer once, to a file: `curl -fsSL https://github.com/imgovind/agent-resurrect/releases/latest/download/install.sh -o "${TMPDIR:-/tmp}/agent-resurrect-install.sh"`. Read all of it, then run `bash "${TMPDIR:-/tmp}/agent-resurrect-install.sh" --dry-run`, which shows every change without making any. 2) Tell me in a few bullets what it will change, including the iTerm2 settings, the LaunchAgent and my shell rc file, then stop and wait for my OK. 3) After I agree, run the same file you read, not a new download: `bash "${TMPDIR:-/tmp}/agent-resurrect-install.sh"`, allowing it up to 5 minutes. Do not use sudo. 4) Run `~/.local/bin/agent-resurrect doctor --json`. For each check whose status is "fail": if fix.agent_may_run is true, run fix.argv once, then run doctor again; otherwise tell me fix.text and do not try it yourself. Leave warnings alone. If a step failed, show me the end of ~/.local/state/agent-resurrect/log. 5) Run `~/.local/bin/agent-resurrect adopt` so this session is recorded too; exit status 3 only means this is not an iTerm2 pane. 6) Finish by telling me what changed; that I should open a new terminal tab and restart any other Claude Code sessions that are running now, because sessions started before the install are not recorded; and how to uninstall: `agent-resurrect uninstall` in a terminal, or `~/.local/bin/agent-resurrect uninstall --yes` if you run it for me.
```

More about this route, including Codex, Cursor and Gemini CLI: [docs/install-with-claude.md](docs/install-with-claude.md).

### Check that it works

The installer finishes by running `agent-resurrect doctor`. Each line starts with `OK`, `WARN` or `FAIL`, and a `FAIL` line is followed by the command that fixes it. Right after installing, a `WARN` that `~/.local/bin/agent-resurrect` is not on PATH is expected; it goes away in a new tab. If you installed from a terminal other than iTerm2, a `WARN` that this is not an iTerm2 pane is expected too.

In a new iTerm2 tab, start Claude Code, then run this in another tab:

```bash
agent-resurrect ls --all
```

Your session is listed with the status `running`, which means it is recorded. To see what a restart would do right now, without restarting:

```bash
agent-resurrect simulate --assume-restart
```

### Sessions that are already running

Claude Code loads plugins when a session starts, so a session that was already running when you installed has no record. To cover one before you restart, type this into that session (the `!` runs it as a shell command in the session's own pane):

```text
! ~/.local/bin/agent-resurrect adopt
```

`adopt` records the Claude Code session it runs under, for the pane it runs in, and nothing else. Because that session started before the plugin's hooks were loaded, a later `/exit` in it is not noticed: if you quit an adopted session on purpose, it may still be offered after the next restart, and any key skips it. Sessions you start after installing do not have this caveat.

## Try it without restarting

You don't need to restart your Mac to see agent-resurrect work.

### Dry check: what a restart would do

```bash
agent-resurrect simulate --assume-restart
```

For every recorded pane, this prints whether it would resume after a restart and with which command, or why not. It treats every recorded Claude Code process as gone, the way a restart would, and it launches nothing and changes nothing. Without `--assume-restart` it reports what each pane would do at its next first prompt right now.

### Quick test: one pane

This test ends one Claude Code process the way a crash would. The conversation stays on disk.

1. Open a new iTerm2 tab and note its terminal name:

   ```bash
   tty
   ```

   It prints something like `/dev/ttys004`.

2. In that tab, start Claude Code in a project folder and send it one message, so the session is saved to disk:

   ```bash
   claude
   ```

3. In a different tab, end that Claude Code with a signal it cannot catch. Use the name from step 1 without `/dev/`:

   ```bash
   pkill -9 -t ttys004 -f claude
   ```

   Quitting with `/exit` would not work for this test: a deliberate exit deletes the record, because you meant to quit.

4. Back in the first tab, start a fresh shell. This is what iTerm2 does in each pane after a restart:

   ```bash
   exec "$SHELL" -l
   ```

   You see `Resurrecting "..." (claude) in ... Press any key to skip.`, a 3 second countdown, and then the same conversation.

### Full test: quit and reopen iTerm2

This runs the same sequence as a restart, minus the reboot.

1. Start Claude Code in two or three panes and send each one a message.
2. Quit iTerm2 with Cmd-Q and confirm that it may close the running sessions.
3. Open iTerm2 again. It restores your windows and panes, and each pane that was running Claude Code counts down and resumes its own session.

If a pane does not resume, run `agent-resurrect doctor` in it and see [Troubleshooting](#troubleshooting).

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

`resurrect` is a short alias for `agent-resurrect`; every command works with either name, unless that name was already taken on your Mac (the installer says so).

| Command | What it does |
|---|---|
| `agent-resurrect` or `agent-resurrect here` | Resume the session that died in this pane, now, with no countdown. Works after you skipped the countdown. If this pane has nothing recorded, it lists what can be resumed. `--print` prints the resume command instead of running it. |
| `agent-resurrect ls [--all] [--ids]` | List sessions you can resume, numbered, newest first: agent, title, folder, last active, pane and status. `--all` includes sessions that are still running; `--ids` adds each session id. |
| `agent-resurrect resume <session-id or n> [--print]` | Resume any Claude Code session by its id, or a listed session by its number, in the current pane. Numbers come from the last `ls` and can change when another pane records a session; ids never change. `--print` prints the command instead. |
| `agent-resurrect find [text] [--clipboard] [--limit N]` | Find the session that contains some text you remember or copied, and print its resume command. Shows up to `N` candidates (default 5), by title and session id. It never starts anything. |
| `agent-resurrect simulate [--assume-restart] [--pane-key KEY]` | Show what each recorded pane, or one pane, would do at its next first prompt, without launching or changing anything. `--assume-restart` treats every recorded agent as stopped, as a restart would. |
| `agent-resurrect adopt` | Record the Claude Code session this command runs under. For sessions that were already running when you installed. |
| `agent-resurrect doctor [--strict]` | Check every setting agent-resurrect needs, the plugin and your PATH, and report restores since the last boot. Prints a fix for anything that fails. `--strict` also fails on warnings. See [Troubleshooting](#troubleshooting). |
| `agent-resurrect config [key [value]]` | Show all settings, show one, or change one. |
| `agent-resurrect install [--yes] [--dry-run] [--no-plugin]` | Apply the settings agent-resurrect needs, for example after you install iTerm2 or Claude Code later. It lists the changes and asks first; `--yes` skips the question. `--dry-run` shows what would change and changes nothing. `--no-plugin` leaves the Claude Code plugin out. |
| `agent-resurrect uninstall [--yes] [--dry-run] [--purge]` | Undo everything the installer changed. It asks first; `--yes` skips the question. `--dry-run` shows what would be reverted. `--purge` also deletes records, logs and config. |
| `agent-resurrect version` | Print the installed version. `agent-resurrect --version` works too. |

Every command above also takes `--json`; see [For AI coding agents](#for-ai-coding-agents). The `auto` and `hook` commands are used by the shell snippet and the plugin; you don't run them yourself.

Examples you can paste:

```bash
agent-resurrect ls --ids
agent-resurrect resume 11111111-2222-3333-4444-555555555555
agent-resurrect find --clipboard
agent-resurrect find "rename the users table migration"
pbpaste | agent-resurrect find
agent-resurrect config auto_resume ask
```

`find` searches your Claude Code transcripts, so it also covers sessions started before you installed agent-resurrect or outside iTerm2. Inside Claude Code, the plugin's `resume-session` skill does the same work when you say things like "I lost my Claude session" or paste text from an old conversation; you can also run it as `/agent-resurrect:resume-session` followed by the text. It prints the resume command for you to run; it never starts the session itself.

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

## Troubleshooting

Start with:

```bash
agent-resurrect doctor
```

Run it inside an iTerm2 pane. Each line starts with `OK`, `WARN` or `FAIL`. A `FAIL` line is followed by a `fix:` line with what to do. Doctor exits with status 1 when anything failed, and with `--strict` also when anything warned.

### What doctor checks

| Check | What it means when it is not OK |
|---|---|
| `python` | The Python agent-resurrect runs on is older than 3.9. Install Apple's command line tools with `xcode-select --install` (it opens a dialog you have to click), then run the install line again. |
| `launcher.on_path` | `~/.local/bin` is not on your PATH in this shell. Open a new tab; the shell block adds it. |
| `iterm.keep_windows` | iTerm2 closes its windows when it quits, so there is nothing to restore after a restart. |
| `iterm.restore_policy` | iTerm2 is set to open no windows, or a fixed arrangement, at launch, instead of restoring. |
| `iterm.launch_at_login` | The LaunchAgent that opens iTerm2 at login is missing, or iTerm2 is not installed. |
| `shell.zsh`, `shell.bash` | Your rc file does not load the first-prompt check, or has an outdated copy of the block. |
| `claude.plugin` | The Claude Code plugin that records sessions is missing or disabled, so new sessions are not recorded. |
| `terminal.pane` | Doctor is not running in an iTerm2 pane. Not a problem in itself, but sessions in this terminal are not recorded. |
| `records` | How many panes have a record, how many are live (not resumed or skipped yet), how many would resume right now, and how many died in panes that did not come back. |
| `restores.since_boot` | What each pane did since the last boot, as counts of the results below. |

For the settings checks, `~/.local/bin/agent-resurrect install` puts everything back the way it needs to be.

Doctor also warns about setups where a restore can never fire, even though every setting is in place:

- Your login shell is not zsh or bash (for example fish). This is a `FAIL`: the first-prompt check only exists for zsh and bash.
- Doctor is running inside tmux, screen or zellij. Sessions in such panes are not recorded.
- `ZDOTDIR` is set in a way zsh does not see at startup, so the block was written to a different `.zshrc` than the one zsh reads.
- Your `.zshrc` starts tmux (`exec tmux`, `tmux attach` or `tmux new`) before the agent-resurrect block, so the block never runs in the iTerm2 pane itself.

### What each restore result means

Every automatic restore decision is written to `~/.local/state/agent-resurrect/restores.jsonl`. Doctor's `since boot` line counts them by result:

| Result | Meaning |
|---|---|
| `resumed` | The pane resumed its session. |
| `skipped` | You pressed a key during the countdown, or answered no. Run `agent-resurrect` in that pane to resume it. |
| `offered` | `auto_resume` is `off`, so the pane printed the resume command instead of running it. |
| `agent-running` | The recorded Claude Code process is still running, so there was nothing to resume. |
| `not-live` | This record was already resumed or skipped earlier. |
| `lost-race` | Another shell in the same pane claimed the record first and resumed it, so this one did nothing. |
| `too-old` | The record is older than `max_age_days`. |
| `session-gone` | The session's transcript no longer exists, so the record was dropped. |
| `not-a-tty` | The shell had no terminal for input and output, so nothing was attempted and the record was left as it was. |
| `launch-failed` | `claude` could not be started. The record is set to skipped so new shells stop retrying. Check that `command -v claude` finds it in a new tab, then run `agent-resurrect resume <session-id>`. |
| `unknown-agent` | The record names an agent this version does not know. Upgrade agent-resurrect. |

### Logs and files

| Path | What is in it |
|---|---|
| `~/.local/state/agent-resurrect/log` (and `log.1`) | Errors and decisions from hooks, restores and the installer. Capped at 256 KB. |
| `~/.local/state/agent-resurrect/restores.jsonl` | One line per automatic restore decision. |
| `~/.local/state/agent-resurrect/panes/` | One JSON record per iTerm2 pane. |
| `~/.local/state/agent-resurrect/manifest.json` | The original value of every setting the installer changed. |
| `~/.config/agent-resurrect/config` | Your settings. |
| `~/.cache/agent-resurrect/` | A bytecode cache that keeps each hook fast. Safe to delete. |

The state folders are readable only by you. If you set `XDG_STATE_HOME` or `XDG_CONFIG_HOME`, these live under those folders instead.

```bash
tail -n 40 ~/.local/state/agent-resurrect/log
tail -n 20 ~/.local/state/agent-resurrect/restores.jsonl
```

### Common problems

**iTerm2 opened with one empty window, not my old panes.**
iTerm2 did not save or restore its windows. Run `agent-resurrect doctor`; `iterm.keep_windows` and `iterm.restore_policy` must be `OK`. Panes that were closed before the restart are not restored by iTerm2 either; `agent-resurrect ls` still lists their sessions.

**My panes came back, but nothing resumed.**
Run `agent-resurrect doctor` in one of them and read the `since boot` line. The usual causes:

- The session was started before you installed agent-resurrect, or before the plugin was installed, so it has no record. Use `agent-resurrect find` with some text from the conversation.
- The shell block is not loaded (`shell.zsh` or `shell.bash` is not `OK`), or the pane runs a shell agent-resurrect does not support, such as fish.
- Claude Code was running inside tmux, screen or zellij, which is not supported yet.
- The pane was skipped, or `auto_resume` is `off`.

In any of these cases, `agent-resurrect` resumes the pane's session right away if it has one, and otherwise lists what can be resumed.

**`agent-resurrect: command not found`.**
Open a new tab, or use the full path: `~/.local/bin/agent-resurrect`.

**Resuming says no conversation was found.**
Claude Code looks up a session by its folder. If the folder was moved or renamed, run `agent-resurrect find` with some text from the conversation and run the command it prints from the original folder.

**I use a custom `CLAUDE_CONFIG_DIR`.**
Sessions started with `CLAUDE_CONFIG_DIR` set are recorded with it and resumed with the same value, so Claude Code finds them in the right place.

**It resumed without bypass permissions mode, or with it when I did not want it.**
See [Permission mode](#permission-mode): `agent-resurrect config permission_flag mirror`, `always` or `never`.

**macOS showed "Background Items Added" after I logged in.**
That is the LaunchAgent that opens iTerm2 at login, `io.github.imgovind.agent-resurrect.iterm`. You can see it in System Settings, General, Login Items. `agent-resurrect uninstall` removes it.

### Reporting a bug

[Open an issue](https://github.com/imgovind/agent-resurrect/issues/new/choose) with the output of `agent-resurrect --version`, `agent-resurrect doctor` and `claude --version`, your macOS and iTerm2 versions, and the last lines of the log. The log can contain folder paths and session ids; check it before you paste it. For a security problem, follow [SECURITY.md](SECURITY.md) instead.

## What the installer changes

For panes to come back after a restart, iTerm2 has to keep and restore its windows, and it has to open at login. The installer sets that up, then adds the pieces that record and resume sessions.

| What | Change | What uninstall does |
|---|---|---|
| iTerm2 keeps its windows | Tells iTerm2 to keep its windows when it quits or the Mac restarts (`NSQuitAlwaysKeepsWindows` in iTerm2's own preferences). Other apps are not affected; the macOS-wide "Close windows when quitting an application" setting is left as it is. | Puts back iTerm2's original value, or removes it if it was never set. |
| iTerm2 startup | Sets iTerm2's window restoration policy to "Use System Window Restoration Setting" (Settings, General, Startup), so it reopens your windows and panes. | Puts back your original iTerm2 values. |
| iTerm2 at login | Adds a LaunchAgent, `~/Library/LaunchAgents/io.github.imgovind.agent-resurrect.iterm.plist`, that opens iTerm2 when you log in. macOS loads it at your next login, so installing it does not open or switch to iTerm2. It asks for no Automation permission. Skipped if iTerm2 is not installed. | Unloads it if it is loaded, and deletes the file. |
| Shell startup | Adds a marked block to `~/.zshrc` (or `$ZDOTDIR/.zshrc`), and to `~/.bash_profile` or else `~/.bashrc` if you have one. A `~/.zshrc` is only created when zsh is your login shell. The block adds `~/.local/bin` to your PATH if it is missing and loads the first-prompt check. A symlinked rc file stays a symlink; its target is edited in place. | Removes exactly that block, leaving every other byte of the file as it was. |
| Claude Code plugin | Adds a local plugin marketplace from `~/.local/share/agent-resurrect` and installs the `agent-resurrect` plugin: two session hooks that record which pane a session runs in, and the `resume-session` and `setup` skills. Skipped if `claude` is not installed, if you pass `--no-plugin`, or if you already installed the plugin from GitHub. | Uninstalls the plugin and removes the marketplace, unless the plugin was already there before. |
| Program files | Code in `~/.local/share/agent-resurrect/`, the commands `~/.local/bin/agent-resurrect` and `~/.local/bin/resurrect`, and a bytecode cache in `~/.cache/agent-resurrect/`. | Deletes them. |

The shell block looks like this:

```bash
# >>> agent-resurrect >>>
# Managed by agent-resurrect. Remove with: agent-resurrect uninstall
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
[ -f "$HOME/.local/share/agent-resurrect/shell/agent-resurrect.zsh" ] && . "$HOME/.local/share/agent-resurrect/shell/agent-resurrect.zsh"
# <<< agent-resurrect <<<
```

Before changing anything, the installer saves each original value in `~/.local/state/agent-resurrect/manifest.json`. A setting that is already the way it needs to be is left alone and is never reverted. Running the installer again, for example to upgrade, keeps the originals from the first install.

### Upgrade

Run the install line again:

```bash
curl -fsSL https://github.com/imgovind/agent-resurrect/releases/latest/download/install.sh | bash
```

It replaces the program files and keeps your session records, your config and the original settings it saved the first time, so `agent-resurrect uninstall` still restores what you had before you ever installed it. An existing shell block is updated in place. Open a new tab afterwards.

Upgrading from 0.1.0 also puts back your macOS-wide "Close windows when quitting an application" setting, which 0.1.0 changed for every app. 0.2.0 sets iTerm2's own preference instead.

Check which version you have:

```bash
agent-resurrect --version
```

To install a specific release, use the `install.sh` attached to it:

```bash
curl -fsSL https://github.com/imgovind/agent-resurrect/releases/download/v0.2.0/install.sh | bash
```

### Uninstall

```bash
agent-resurrect uninstall
```

It lists what it will revert and asks first. Then it restores every original value and deletes the program files, including the bytecode cache. Add `--dry-run` to only see the list, or `--purge` to also delete the session records, the log and your config (`~/.local/state/agent-resurrect/` and `~/.config/agent-resurrect/`).

Without a terminal it does not ask; add `--yes`. In a script, or when Claude Code runs it for you:

```bash
~/.local/bin/agent-resurrect uninstall --yes
```

If the manifest of original values cannot be read, uninstall stops before changing anything and says why, so nothing is lost. If one setting fails to revert, the others are still reverted, and the program files and the failed entry are kept so you can run uninstall again.

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
  ┌──────────────────────────────┐   reads, claims         │
  │ ~/.local/state/              │◄────────────────────────┤
  │   agent-resurrect/panes/     │                         ▼
  │   iterm-<pane id>.json       │          ┌──────────────────────────────┐
  └──────────────────────────────┘          │ countdown, then              │
                                            │ claude --resume <id>         │
                                            │ in the same pane and folder  │
                                            └──────────────────────────────┘
```

Recording:

- A record holds the session id, folder, transcript path, permission mode, the Claude Code process id with its start time, and `CLAUDE_CONFIG_DIR` when it is set.
- `/clear`, `/resume` and similar commands start a new session id in the same pane; the newest one wins.
- Quitting from the prompt (for example `/exit`) or `/logout` deletes the record. A restart, crash or closed tab keeps it.
- Only the interactive session in the pane is recorded. A `claude -p` run, a background process, an agent-team teammate, or a Claude Code started by another agent's tool call never overwrites the pane's record.
- Panes inside tmux, screen or zellij are not recorded, because every pane inside them shares the outer iTerm2 pane's id.
- The hooks always exit successfully and print nothing, so they never block or slow down Claude Code.

Restoring: a pane resumes its record automatically only when all of these hold.

1. The record is live (not already resumed or skipped).
2. The recorded Claude Code process is gone. A new process that reused the same pid does not count, because the start time must match too.
3. The session's transcript is still on disk.
4. The record is newer than `max_age_days`.

Before launching, the shell claims the record in one atomic step, so if two shells in the same pane start at once, only one resumes it, and a crash loop cannot keep re-triggering it. Claude Code then replaces the `agent-resurrect` process, so no helper sits between your shell and Claude Code. A shell without a terminal for input and output, such as one started by a script, never resumes or counts down: it only logs `not-a-tty` and leaves the record as it was.

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

## For AI coding agents

If you are an AI agent acting for a user, or a script, these rules keep you safe:

- Run the command by its full path, `~/.local/bin/agent-resurrect`, until the user opens a new terminal tab. In a Claude Code session where the plugin is loaded, `agent-resurrect` is on your PATH too.
- Add `--json` and branch on the exit code, not on the text.
- Safe to run yourself: `ls`, `find`, `simulate`, `doctor`, `config` (reading), `version`, `adopt` (it records only the session you run under), and `install --dry-run` and `uninstall --dry-run`. Run `install --yes` or `uninstall --yes` only when the user asked for it.
- Never use `agent-resurrect` with no arguments, `here` or `resume` to start a session for the user. Without a terminal they refuse: they print the resume command, change nothing, and exit with status 4. Add `--print` to get the command with status 0, then give it to the user to run.
- Refer to sessions by session id, never by list number: `ls` numbers can change between two calls, and `find` does not number its candidates.
- In `doctor --json`, run a check's `fix.argv` only when `fix.agent_may_run` is true, once, then run doctor again. Otherwise tell the user `fix.text`.
- After installing, run `adopt` to record your own session, and tell the user that other sessions running now are recorded only after they restart.
- The installer writes to `~/.local`, `~/Library/LaunchAgents` and user defaults. An agent that runs commands in a sandbox (Codex by default, Gemini CLI when its sandbox is on) must run it outside the sandbox, with the user's approval. The install prompt is in [docs/install-with-claude.md](docs/install-with-claude.md).

### Exit codes

| Code | Name | When |
|---|---|---|
| 0 | `ok` | Success. Also `find` with any match (the confidence is in the output), `doctor` with no failure, and `ls` with an empty list. |
| 1 | `error` | An unexpected error, an install or uninstall step failed, or `doctor` found a failure (or a warning, with `--strict`). |
| 2 | `usage` | Bad arguments, an unknown config key or invalid value, or `find` text that is missing or too short. |
| 3 | `not-found` | No session for that number or id, `find` found nothing, `here` has nothing recorded for this pane, or `adopt` is not running inside a Claude Code session in an iTerm2 pane. |
| 4 | `needs-tty` | It would start an agent or ask a question, but stdin or stdout is not a terminal. The output contains the command to run. Use `--print` to get it with status 0, or `--yes` for `install` and `uninstall`. |
| 5 | `busy` | The session's agent process is still running. |
| 6 | `gone` | The session's transcript no longer exists. |
| 130 | `interrupted` | Ctrl-C. |

After a successful launch, the exit status is Claude Code's own, because Claude Code replaces the `agent-resurrect` process. The `hook` command always exits 0 and prints nothing, whatever happens.

### JSON output

With `--json`, a command prints exactly one JSON object on stdout, ending with a newline and escaped to plain ASCII. Diagnostics go to stderr, and it never asks a question. Every object has:

- `schema`: `agent-resurrect.<command>/1`, for example `agent-resurrect.doctor/1`. Within version 1, fields are only ever added, never renamed or removed.
- `version`: the agent-resurrect version.
- `ok`: `true` or `false`.
- `error`: `null`, or `{"code": "<name from the table above>", "message": "..."}`.

Times are ISO 8601 in UTC (`2026-10-12T10:54:16Z`) and paths are absolute. Text taken from transcripts, such as titles, is returned as it is; JSON escaping makes it safe to print.

Sessions appear as one shared object with `session_id`, `title`, `cwd`, `last_active_at` and a `resume` object holding `argv`, `cwd` and the ready-to-copy `command`. Each check in `doctor --json` has an `id`, a `status` (`ok`, `warn` or `fail`) and a `fix` whose `kind` says who can act on it:

| `fix.kind` | Meaning |
|---|---|
| `command` | Run `fix.argv` once, then run doctor again. `agent_may_run` is true. |
| `new-shell` | Fixes itself in a new terminal tab. |
| `restart-agent` | Restart the Claude Code sessions that are running, or run `/reload-plugins` in them. |
| `user-action` | Needs a person: a click in System Settings or a dialog, or changing the login shell. |
| `install-app` | iTerm2, Claude Code or the command line tools are missing. Ask the user first. |

The top-level `ready` field is `true` when every setting is in place and the plugin is enabled. The JSON Schema for each command's output is in [docs/json/](docs/json/).

## Why `claude --continue` is not enough

`claude --continue` resumes the most recent session in the current folder. When several panes were working in the same repository, every pane gets the same, most recent conversation, and the others have to be found by hand in the `claude --resume` picker. You also have to remember `--dangerously-skip-permissions` for each session that used it.

agent-resurrect records which pane each session ran in, so after a restart every pane gets its own session back, in its own folder, with the same permission mode, without you typing anything. For a single session in a fresh terminal, `claude --resume` is all you need.

## Frequently asked questions

**How do I resume Claude Code after my Mac restarts?**
Install agent-resurrect before the restart. After it, each iTerm2 pane that was running Claude Code resumes its own session at the first prompt. Without it, run `claude --resume` in each pane and pick the session from the list.

**Why does `claude --continue` resume the wrong session?**
It picks the most recent session in the folder, not the one that ran in that pane. See [Why `claude --continue` is not enough](#why-claude---continue-is-not-enough).

**How do I find which Claude Code session was in which terminal?**
`agent-resurrect ls --ids` lists every recorded session with its pane, folder and session id. For a session with no record, `agent-resurrect find` searches your transcripts for text you remember or copy from the pane's scrollback.

**Does tmux-resurrect restore Claude Code?**
tmux-resurrect restores tmux sessions, panes and folders, but it does not know which Claude Code session ran where. agent-resurrect does not support tmux yet either: sessions inside tmux are not recorded.

**Does it work after a crash?**
Yes, when iTerm2 restores the panes. A crash does not end the session on purpose, so the record is kept.

**What if I exited a session on purpose?**
It is never restored. Quitting from the prompt (for example `/exit`) deletes the pane's record.

**What about sessions from before I installed?**
They have no record, because Claude Code loads the plugin when a session starts. Run `! ~/.local/bin/agent-resurrect adopt` inside each one to record it, or restart it. For a session that has already died, use `agent-resurrect find` with some text you remember; it prints the resume command.

**I closed a tab that was running Claude Code. Can I get it back?**
Yes. The pane will not come back, so nothing happens automatically, but `agent-resurrect ls` lists the session and `agent-resurrect resume <session-id>` resumes it in the current pane.

**Does it slow down my shell?**
No. The check runs once, at the first prompt, as a file test inside the shell. When the pane has nothing to restore, no process is started.

**Which shells are supported?**
zsh and bash, including macOS's bash 3.2. It runs after frameworks such as oh-my-zsh and powerlevel10k have finished loading. fish and nushell are not supported.

**Does it change settings for other apps?**
No. It changes only iTerm2's preferences, your shell rc file and a LaunchAgent for iTerm2. Version 0.1.0 changed the macOS-wide "Close windows when quitting an application" setting; upgrading to 0.2.0 puts your original value back.

**Does it need special permissions?**
No. There is no `sudo`, no Accessibility or Automation prompt, and nothing is written outside your home folder and your own preferences.

**Privacy?**
Everything stays on your Mac. agent-resurrect itself makes no network requests: the only download is the release the installer fetches from GitHub, and re-running the installer asks Claude Code to refresh its plugin, which contacts GitHub only if you installed the plugin from there. `find` reads your Claude Code transcripts locally. There is no telemetry.

## Similar projects

Other people have solved parts of this problem, and agent-resurrect builds on their ideas. Each is worth a look; one of them may fit your setup better.

| Project | What it does |
|---|---|
| [cc-session-restore](https://github.com/asadtariq96/cc-session-restore) by asadtariq96 | A LaunchAgent snapshots your open Claude Code sessions every 60 seconds. After a reboot, `cc-session-restore restore` reopens them as tabs in one iTerm2 window, each resumed by its exact session id. One-line installer. |
| [claude-iterm2-resume-sessions](https://github.com/adrianschmidt/claude-iterm2-resume-sessions) by adrianschmidt | A Claude Code plugin whose SessionStart and SessionEnd hooks record each session's iTerm2 pane. At login, a LaunchAgent types `claude --resume <id>` back into the original pane, or opens it in a tab of a new window when that pane is gone or busy. The closest design to this one. |
| [claude-hibernate](https://github.com/SteveVitali/claude-hibernate) by SteveVitali | Snapshots your running Claude Code sessions before a shutdown and wakes them after the reboot, each in its original folder with the same session id and launch flags. Works with nine terminal backends, including tmux, iTerm2 and kitty, on macOS and Linux. Installs with Homebrew, a curl script, or as one bash script. |
| [claude-sessions](https://github.com/DenverLifeSciences/claude-sessions) by daksh-gargas | A launchd job snapshots every iTerm2 window and tab every 5 minutes, with each tab's folder and Claude Code session. After a crash you choose when to restore, with `--restore-crash` or a picker; it never restores on its own. |
| [Quil](https://quil.cc) | An open-source terminal multiplexer for AI coding agents that survives reboots, on macOS, Linux and Windows. It records a session id per pane and resumes Claude Code conversations with `claude --resume` after a restart. Also runs Codex and OpenCode. |
| [cc-sessions](https://github.com/shsunmoonlee/cc-sessions) by shsunmoonlee | A broader session manager for Claude Code in iTerm2: a ledger written by Claude Code hooks, a daemon that hibernates idle sessions under memory pressure and wakes them when you click their tab, and restore after a crash or shutdown. |
| [PonyMux](https://github.com/heylittlepan/ponymux) | A native macOS terminal app, free but not open source, that organizes Claude Code, Codex and Grok sessions into lists and folders, searches their transcripts, and resumes or forks past sessions. |
| [Agent-Resume](https://github.com/fukuyori/Agent-Resume) (`agres`) by fukuyori | A cross-platform Go CLI with an interactive picker that resumes Claude Code, Codex, OpenCode, Aider and Antigravity CLI sessions from the current folder or all projects, with search and cleanup. |

What agent-resurrect focuses on:

- **Each pane resumes itself.** The pane's own shell resumes the session at its first prompt. Nothing types into panes from outside, so no Automation permission is needed.
- **Every change can be undone.** The installer records the original value of each setting it changes, and `agent-resurrect uninstall` puts each one back.
- **The permission mode comes back too.** A session that was running with `--dangerously-skip-permissions` comes back with it, and you can change that.
- **Agents can drive it.** Every command has `--json` output and documented exit codes, and nothing starts a session without a terminal.

Where others do more today: claude-hibernate and Quil work on Linux and with tmux, claude-hibernate also brings back other launch flags, claude-iterm2-resume-sessions reopens sessions in a new window when their panes are gone, cc-sessions also manages memory, and Quil, Agent-Resume and PonyMux already work with several agents.

## Roadmap

Version 0.2 supports Claude Code in iTerm2 on macOS. Agents and terminals sit behind separate adapter interfaces, so each of these is a separate, separately tested addition:

- Agents: Codex, Cursor agent, opencode, Qoder. These will be recorded from the shell, since they may not offer Claude-style session hooks.
- Terminals: Ghostty, Warp, WezTerm, Kitty, tmux, Terminal.app.
- Reopening sessions whose panes were not restored in new tabs.
- A Homebrew tap, and a signed, notarized `.pkg` installer.

Want one of these sooner? [Request an agent or terminal](https://github.com/imgovind/agent-resurrect/issues/new/choose).

## Development

agent-resurrect is plain Python 3.9+ with the standard library only, plus small zsh and bash snippets and a bash installer.

```bash
git clone https://github.com/imgovind/agent-resurrect.git
cd agent-resurrect
make test
```

- `make test` runs the unit tests on `/usr/bin/python3` (3.9) and on `python3` from your PATH. `make lint` runs the shell checks CI runs. `make install-dev` installs your working tree on your own Mac.
- Tests never touch your real HOME, `defaults`, `launchctl` or Claude Code plugins; they use a temporary home and fake executables.
- [CONTRIBUTING.md](CONTRIBUTING.md) has the ground rules and how to add an agent or terminal. Coding agents should read [AGENTS.md](AGENTS.md).

The original design is in [docs/specs/2026-10-12-agent-resurrect-design.md](docs/specs/2026-10-12-agent-resurrect-design.md).

## License

[MIT](LICENSE). Copyright (c) 2026 Govindarajan Panneerselvam.
