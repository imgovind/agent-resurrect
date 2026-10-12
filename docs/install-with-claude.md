# Install agent-resurrect with Claude Code

If you would rather have Claude Code read the installer and walk you through it, paste the prompt below into a Claude Code session on the Mac where you want agent-resurrect. It works the same way in Codex, Cursor's agent and Gemini CLI; see [Other agents](#other-agents).

## The prompt

Copy all of it, exactly as it is:

```text
Install agent-resurrect (https://github.com/imgovind/agent-resurrect) on this Mac. Do these steps in order. 1) Download the installer once, to a file: `curl -fsSL https://github.com/imgovind/agent-resurrect/releases/latest/download/install.sh -o "${TMPDIR:-/tmp}/agent-resurrect-install.sh"`. Read all of it, then run `bash "${TMPDIR:-/tmp}/agent-resurrect-install.sh" --dry-run`, which shows every change without making any. 2) Tell me in a few bullets what it will change, including the iTerm2 settings, the LaunchAgent and my shell rc file, then stop and wait for my OK. 3) After I agree, run the same file you read, not a new download: `bash "${TMPDIR:-/tmp}/agent-resurrect-install.sh"`, allowing it up to 5 minutes. Do not use sudo. 4) Run `~/.local/bin/agent-resurrect doctor --json`. For each check whose status is "fail": if fix.agent_may_run is true, run fix.argv once, then run doctor again; otherwise tell me fix.text and do not try it yourself. Leave warnings alone. If a step failed, show me the end of ~/.local/state/agent-resurrect/log. 5) Run `~/.local/bin/agent-resurrect adopt` so this session is recorded too; exit status 3 only means this is not an iTerm2 pane. 6) Finish by telling me what changed; that I should open a new terminal tab and restart any other Claude Code sessions that are running now, because sessions started before the install are not recorded; and how to uninstall: `agent-resurrect uninstall` in a terminal, or `~/.local/bin/agent-resurrect uninstall --yes` if you run it for me.
```

## What Claude Code will do

1. Download `install.sh` once, from the latest release, to a temporary file, and read all of it.
2. Run that file with `--dry-run`. This downloads the release's code to a temporary folder and prints every change the installer would make, without writing anything under your home folder. The real changes are made by Python code inside the release, not by `install.sh` itself, so this is the only way to see them before they happen.
3. Summarise the changes for you and stop. Nothing is installed until you say yes. Compare its summary with [What the installer changes](../README.md#what-the-installer-changes).
4. Run the same file it read. It does not download the installer a second time, and it does not pipe anything into `bash`.
5. Run `~/.local/bin/agent-resurrect doctor --json` by its full path, because the shell Claude Code runs commands in may not have `~/.local/bin` on its PATH yet. For each failed check it runs the suggested command only when doctor marks it as safe for an agent (`agent_may_run`); anything that needs you, such as a click in System Settings or installing an app, it tells you about instead.
6. Run `~/.local/bin/agent-resurrect adopt`, which records the Claude Code session it is running in, so this session also comes back after a restart.
7. Tell you what changed, what to do next, and how to uninstall.

Claude Code asks for your approval before running commands, as usual. The prompt rules out `sudo`, and the installer never needs it.

## Why the prompt is written this way

- **One download, read and run.** An older version of this prompt read one copy of the installer and then ran another with `curl ... | bash`. Reading a file and then running a different download defeats the point of reading it, so the prompt now runs the file it read.
- **A pause for your consent.** In auto-accept or bypass permissions mode, Claude Code would otherwise go straight from the summary to the install. Step 2 makes it wait for you.
- **Only safe fixes.** `doctor --json` labels each fix with a `kind` and with `agent_may_run`. An agent may re-run the installer step that failed; it should not edit your rc files by hand, install apps, or click through dialogs for you.
- **Warnings are expected.** In Claude Code's own shell, doctor usually warns that `~/.local/bin` is not on PATH (Claude Code took a snapshot of your shell before the install), and that it is not running in an iTerm2 pane when Claude Code runs in an editor. Both are fine; neither has a fix an agent should try.

## Other agents

The prompt works in Codex, Cursor's agent and Gemini CLI too. The installer writes to `~/.local`, `~/Library/LaunchAgents` and your user defaults, so an agent that runs commands in a sandbox must run it outside the sandbox:

- Codex sandboxes commands by default, blocking network access and writes outside the workspace. Approve running the installer outside the sandbox when Codex asks.
- Gemini CLI does the same when its sandbox is turned on.

Step 5 (`adopt`) records Claude Code sessions only. In another agent it exits with status 3, which is fine.

## After it finishes

- Open a new terminal tab. New shells pick up `~/.local/bin` on your PATH and the first-prompt check.
- Claude Code sessions you start from now on are recorded. Sessions that were already running in other panes are not; restart them, or type `! ~/.local/bin/agent-resurrect adopt` in each one.
- Check the setup any time with:

  ```bash
  agent-resurrect doctor
  ```

- To see what a restart would do right now, without restarting:

  ```bash
  agent-resurrect simulate --assume-restart
  ```

## If something is missing

The installer does not fail when iTerm2 or Claude Code is missing; it skips the related steps and `doctor` reports them. Install the missing app, then run:

```bash
agent-resurrect install
```

If the installer stops with "Python 3.9+ is required", install Apple's command line tools and run the prompt again. This opens a dialog that you have to click yourself; an agent cannot do it for you:

```bash
xcode-select --install
```

If a step failed, the details are in the log:

```bash
tail -n 40 ~/.local/state/agent-resurrect/log
```

## Install the plugin instead

If you prefer to start from inside Claude Code, install the plugin and then run its setup skill, which does the same steps from the plugin's own files, with no download:

```text
/plugin marketplace add imgovind/agent-resurrect
/plugin install agent-resurrect@agent-resurrect
/agent-resurrect:setup
```

See [Install from inside Claude Code](../README.md#install-from-inside-claude-code).

## Uninstall

Run this in a terminal:

```bash
agent-resurrect uninstall
```

It asks before changing anything, then restores every original value the installer changed. Add `--dry-run` to only see what it would revert, or `--purge` to also delete the session records, the log and your config.

If you ask Claude Code to uninstall it instead, it should run the command by its full path and with `--yes`, because it cannot answer the question:

```bash
~/.local/bin/agent-resurrect uninstall --yes
```
