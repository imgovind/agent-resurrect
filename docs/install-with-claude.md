# Install agent-resurrect with Claude Code

If you would rather have Claude Code read the installer and walk you through it, paste the prompt below into a Claude Code session on the Mac where you want agent-resurrect.

## The prompt

Copy all of it, exactly as it is:

```text
Install agent-resurrect from https://github.com/imgovind/agent-resurrect on this Mac. First download install.sh from that repo, read it, and tell me in a few bullets what it will change. Then run `curl -fsSL https://raw.githubusercontent.com/imgovind/agent-resurrect/main/install.sh | bash`. After it finishes, run `~/.local/bin/agent-resurrect doctor`; for anything not marked OK, run the fix it suggests and run doctor again. Do not use sudo. Finish by telling me what changed and the uninstall command.
```

## What Claude Code will do

1. Download `install.sh` and summarise what it will change, before running anything. Compare its summary with [What the installer changes](../README.md#what-the-installer-changes).
2. Run the one-line installer. Claude Code asks for your approval before running commands, as usual.
3. Run `~/.local/bin/agent-resurrect doctor` (by its full path, because the shell Claude Code runs commands in may not have `~/.local/bin` on its PATH yet) and, for any check that is not OK, run the suggested fix and check again.
4. Tell you what changed and how to uninstall.

The prompt rules out `sudo`, and the installer never needs it.

## After it finishes

- Open a new terminal tab. New shells pick up `~/.local/bin` on your PATH and the first-prompt check.
- Claude Code sessions you start from now on are recorded. Sessions that were already running when you installed are not; if you need one of them after a restart, use `agent-resurrect find` with some text from that conversation.
- Check the setup any time with:

  ```bash
  agent-resurrect doctor
  ```

## If something is missing

The installer does not fail when iTerm2 or Claude Code is missing; it skips the related steps and `doctor` reports them. Install the missing app, then run:

```bash
agent-resurrect install
```

If the installer stops with "Python 3.9+ is required", install Apple's command line tools and run the prompt again:

```bash
xcode-select --install
```

## Uninstall

Run this in a terminal:

```bash
agent-resurrect uninstall
```

It asks before changing anything, then restores every original value the installer changed. Add `--purge` to also delete the session records, the log and your config.

If you ask Claude Code to uninstall it instead, it should run the command by its full path and with `--yes`, because it cannot answer the question:

```bash
~/.local/bin/agent-resurrect uninstall --yes
```
