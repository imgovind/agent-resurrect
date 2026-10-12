---
name: setup
description: Installs or repairs agent-resurrect from this plugin's own files, so Claude Code sessions come back in their iTerm2 panes after a restart. It changes iTerm2 and shell settings and adds a login item, so it runs only when the user asks for it.
disable-model-invocation: true
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/agent-resurrect install --dry-run *), Bash(${CLAUDE_PLUGIN_ROOT}/bin/agent-resurrect doctor *)
---

# Set up agent-resurrect

This plugin's hooks already record which iTerm2 pane each Claude Code session runs in. Bringing the sessions back after a restart also needs the `agent-resurrect` command in `~/.local/bin`, a block in the shell's rc file that runs it at a new shell's first prompt, and iTerm2 settings that reopen its windows. These steps install all of that from the files this plugin already has: no download and no sudo. `agent-resurrect uninstall` reverts every change.

Run the commands exactly as written. Until the user opens a new terminal tab, `agent-resurrect` may not be on your PATH, so use this plugin's path.

## Steps

1. Show the plan. This changes nothing:

   ```bash
   ${CLAUDE_PLUGIN_ROOT}/bin/agent-resurrect install --dry-run --json
   ```

   List, as bullets, the `description` of every step whose `status` is `would-change` or `would-revert`, and each `skipped` step with its `detail`. Say which ones change settings outside agent-resurrect's own files: iTerm2's preferences, the login item that opens iTerm2, the shell rc file and the Claude Code plugin. Then ask the user, in prose, whether to go ahead, and stop until they answer.

2. If they agree, run the installer that came with this plugin. It copies these files to `~/.local/share/agent-resurrect`, writes `~/.local/bin/agent-resurrect`, applies the settings and runs a check. Allow it up to 5 minutes. Never use sudo.

   ```bash
   bash ${CLAUDE_PLUGIN_ROOT}/install.sh
   ```

   If it fails, show the user the last 40 lines of `~/.local/state/agent-resurrect/log` and stop.

3. Check the result:

   ```bash
   ${CLAUDE_PLUGIN_ROOT}/bin/agent-resurrect doctor --json
   ```

   For each check whose `status` is `fail` or `warn`, read its `fix`:

   - `fix.agent_may_run` is true: run `fix.argv` once, then run doctor again. Do not repeat a fix that did not help.
   - Otherwise, do not attempt it; tell the user `fix.text`. `new-shell` clears by itself in a new terminal tab. `restart-agent` means restarting the Claude Code sessions that are running now. `user-action` and `install-app` need the user: never install apps, click through dialogs or change System Settings yourself.

   Report whether `ready` is true.

4. Record this session, which may have started before the plugin was installed:

   ```bash
   ${CLAUDE_PLUGIN_ROOT}/bin/agent-resurrect adopt --json
   ```

   Exit 3 means this session is not running in an iTerm2 pane, so it cannot be restored in place; say so and continue.

5. Tell the user:
   - what changed, from step 1;
   - to open a new terminal tab, which loads the shell block and puts `agent-resurrect` on PATH;
   - that Claude Code sessions already running in other panes are recorded once they are restarted (or after running `agent-resurrect adopt` inside each);
   - how to undo it: `agent-resurrect uninstall` in a terminal, or `~/.local/bin/agent-resurrect uninstall --yes` if you run it for them.
