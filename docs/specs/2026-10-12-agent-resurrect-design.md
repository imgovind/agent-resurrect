# agent-resurrect: design spec

Date: 2026-10-12
Status: implemented in 0.1.0; revised in 0.2.0

> This is the original design. Where it and the code disagree, the README and the code are the source of truth. Statements that 0.2.0 changed are marked **Superseded in 0.2.0** below, and [Changes in 0.2.0](#15-changes-in-020) lists everything that changed. Contributor rules are in AGENTS.md and CONTRIBUTING.md.

Repo: `imgovind/agent-resurrect` (public, MIT). Command: `agent-resurrect`, alias `resurrect`.

## 1. Problem

When a Mac restarts (or the terminal crashes), every AI coding agent running in a terminal pane dies. The terminal restores the windows, panes and even the old scrollback, but each pane now holds a fresh shell. Today the recovery is manual: copy text out of the scrollback, open a new agent, ask it to find the session id, then type `claude --resume <id> --dangerously-skip-permissions` in each pane, one by one.

## 2. Goal

After a restart, every pane that was running an agent session resumes that session by itself, in the same pane, in the same working directory, with the same permission mode. When automatic restore is not possible, one command lists and resumes what died.

Success criteria:

1. Restart the Mac with N panes running Claude Code. On login, each of those N panes shows a short countdown and resumes its own session. Panes that were not running an agent do nothing.
2. A session the user exited on purpose is never resurrected.
3. Install is one pasted line (or one pasted prompt into Claude Code) and needs no manual settings changes. Uninstall restores every changed setting to its original value.
4. A shell that starts in a pane with nothing to restore pays no measurable cost (no process spawned).

## 3. Scope

**v1:** macOS, iTerm2, Claude Code. Both adapter interfaces (agent, terminal) exist from day one so later support is additive.

**Deferred (each a separate, separately tested adapter):**
- Agents: Codex, Cursor agent, opencode, Qoder. These need a shell-level recorder (section 6.4) because they may lack Claude-style session hooks; its design is fixed here, its implementation ships with the first non-Claude adapter.
- Terminals: Warp, Ghostty, WezTerm, Kitty, tmux, Terminal.app.
- A signed `.pkg` installer (needs an Apple Developer ID and notarization to pass Gatekeeper).
- Reopening orphaned sessions in new tabs when panes are not restored.

**Non-goals:** keeping agent processes alive across a restart (impossible), restoring an interrupted tool call (the agent decides that), Linux/Windows.

## 4. Verified facts this design rests on

| Fact | Evidence |
|---|---|
| iTerm2 pane GUID survives a reboot | On the author's Mac, pane `w0t12p0:10FE02F5-C525-405E-9E86-C8255ED17032` appears in a process environment captured 2026-10-10 and is the same pane's `ITERM_SESSION_ID` after the 2026-10-12 11:34 reboot. iTerm2 fixed a bug where Quit regenerated the id (iterm2-discuss). One data point: `doctor` reports GUID matches after each restore so regressions are visible. |
| Hooks inherit the terminal environment | `ITERM_SESSION_ID` is visible inside Claude Code tool and hook subprocesses (Claude Code hooks docs: hooks inherit the parent environment). |
| SessionStart stdin carries `session_id`, `cwd`, `transcript_path`, `permission_mode`, `source` (`startup`, `resume`, `clear`, `compact`, `fork`) | Claude Code hooks reference. |
| SessionEnd stdin carries `reason`: `clear`, `resume`, `logout`, `prompt_input_exit`, `other`; default timeout 1.5 s | Claude Code hooks reference. A reboot ends sessions with `other` per prior art; the design keeps the record whether SessionEnd fires or not. |
| A session that ended in `bypassPermissions` resumes in default mode unless the flag is passed again | Claude Code sessions docs, "Permission mode on resume". |
| `claude --resume <id>` searches the current project, its worktrees, then all projects | Claude Code CLI reference (since v2.1.223). |
| The interactive `claude` process is the foreground job of its tty (`ps` stat contains `+`) and is the parent of hook/tool shells | Observed: `-zsh` > `claude` (`S+`, ttys029) > tool shell. |
| Claude Code plugins can be installed non-interactively | `claude plugin marketplace add <source>`, `claude plugin install <plugin>@<marketplace>`. |
| `/usr/bin/python3` on a current Mac with Command Line Tools is 3.9 | Observed 3.9.6. |
| Pasted text is JSON-escaped in transcripts; terminal paste re-renders markdown and wraps lines | Verified while building the text finder (section 6.6). |

## 5. Architecture

```
          RECORD                                    RESTORE
  ┌──────────────────────────┐          ┌────────────────────────────────┐
  │ agent adapter: claude    │          │ shell trigger (zsh/bash)       │
  │  plugin hooks ──► `agent-│          │  first prompt of a new shell:  │
  │  resurrect hook claude`  │          │  record file for my pane key?  │
  └────────────┬─────────────┘          │  no ──► nothing (no process)   │
               │ upsert / delete        │  yes ─► `agent-resurrect auto` │
               ▼                        └───────────────┬────────────────┘
  ┌──────────────────────────┐                          │ read, decide, mark
  │ store: one JSON record   │◄─────────────────────────┘
  │ per pane key             │          ┌────────────────────────────────┐
  └──────────────────────────┘◄─────────│ CLI: here, ls, resume, find,   │
  ┌──────────────────────────┐          │ doctor, install, uninstall     │
  │ terminal adapter: iterm  │          └────────────────────────────────┘
  │  pane key + settings     │
  └──────────────────────────┘
```

### 5.1 Units

| Unit | Responsibility | Depends on |
|---|---|---|
| `store` | Read, atomically write (temp file + rename), list and delete pane records; garbage-collect old ones | filesystem only |
| `procs` | Process inspection behind one small interface: parent chain, command name, argv, stat flags, start time, liveness check of (pid, start time) | `ps` |
| `terminals/base`, `terminals/iterm` | Detect the terminal from the environment; produce the pane key; declare the settings it needs | env, `settings` |
| `agents/base`, `agents/claude` | Turn a hook event into a record or a deletion; say whether a session still exists; build the resume argv; describe a session (title, first prompt, last active); find sessions by text | `store`, `procs`, `finder` |
| `restore` | Eligibility rules (section 7) and the countdown/ask/immediate behaviour | `store`, `procs`, agents |
| `finder` | Text search over Claude transcripts (the tested `find_session.py`, moved in) | filesystem only |
| `settings` | Setting objects with `check`, `apply` (returns the original value), `revert(original)`; the install manifest | `defaults`, `launchctl`, `claude`, files |
| `installer` | Ordered apply/revert of settings, idempotent; writes the manifest | `settings` |
| `cli` | Argument parsing and output only | everything above |

All Python is stdlib only, 3.9 compatible, run with `-I` (isolated mode).

### 5.2 Adapter interfaces

```python
class TerminalAdapter(Protocol):
    name: str                          # "iterm"
    restores_in_place: bool            # True for iTerm2
    def detect(self, env) -> bool: ...
    def pane_key(self, env) -> Optional[str]: ...      # "iterm-<GUID>"
    def pane_label(self, env) -> Optional[str]: ...    # "w0t12p0", display only
    def settings(self) -> List[Setting]: ...

class AgentAdapter(Protocol):
    name: str                          # "claude"
    def on_hook(self, event: str, payload: dict, ctx) -> None: ...
    def session_exists(self, record) -> bool: ...
    def resume_argv(self, record, config) -> List[str]: ...
    def describe(self, record) -> SessionInfo: ...
    def find_by_text(self, text: str, exclude: Optional[str]) -> List[Candidate]: ...
```

> **Superseded in 0.2.0:** `AgentAdapter` also has `session_record(session_id)`, `find_by_text` takes a `limit`, and 0.2.0 adds `adopt(ctx)` (record the session a command runs under). `Setting` gains `applicable()`. See `agents/base.py` and `settings/base.py`.

The pane key format is a contract shared with the shell trigger, which computes it without spawning a process: `iterm-` followed by the part of `ITERM_SESSION_ID` after the first `:`.

> **Superseded in 0.2.0:** a pane is recorded and restored only when `TERM_PROGRAM` is `iTerm.app` and none of `TMUX`, `STY` and `ZELLIJ` is set. Panes inside tmux, screen or zellij inherit the outer pane's `ITERM_SESSION_ID`, so they would all share one key.

## 6. Flows

### 6.1 Recording (Claude Code)

The plugin's `hooks/hooks.json` registers SessionStart and SessionEnd with command `"$HOME/.local/bin/agent-resurrect" hook claude <event> || true`. The hook:

> **Superseded in 0.2.0:** the plugin is self-contained at the repository root, and its hooks run `"${CLAUDE_PLUGIN_ROOT}/bin/agent-resurrect" hook claude <event>`, so a plugin-only install records sessions without the curl installer. SessionStart also records `CLAUDE_CONFIG_DIR` (as `resume_env`) when it is set, and SessionEnd loads the record and compares session ids before it walks the process table.

1. Exits immediately if no terminal adapter detects a pane key (not iTerm2, or no `ITERM_SESSION_ID`).
2. Finds the agent process: walks up the parent chain to the nearest ancestor whose command name is `claude`.
3. Skips recording (exit 0) when that process is not interactive: its stat lacks `+`, it has no tty, its argv contains `-p` or `--print`, or another `claude` process is further up the chain (a nested agent started by an agent's tool call would otherwise overwrite the pane's record).
4. SessionStart (any `source`): upserts the pane's record with status `live`, the payload fields, the agent pid and its start time. `clear`, `resume` and `fork` change the session id, so the newest SessionStart always wins.
5. SessionEnd: `prompt_input_exit` or `logout` deletes the record (a deliberate exit). `clear` and `resume` do nothing (the following SessionStart updates the record). `other` or a missing reason keeps the record (reboot, crash, closed tab).
6. Always exits 0, prints nothing, finishes well inside the 1.5 s SessionEnd budget (target under 150 ms), and logs errors to the log file.

### 6.2 Automatic restore

The installer adds a marked block to `~/.zshrc` (and `~/.bashrc` when present; **superseded:** the code uses `~/.bash_profile`, else `~/.bashrc`, and since 0.2.0 does not create `~/.zshrc` when zsh is not the login shell). In interactive shells only, it registers a one-shot first-prompt hook (`precmd` in zsh, `PROMPT_COMMAND` in bash) so it runs after frameworks such as oh-my-zsh and powerlevel10k instant prompt have finished. The hook removes itself, computes the pane key, and runs `agent-resurrect auto` only if `~/.local/state/agent-resurrect/panes/<key>.json` exists. No record means no process is spawned.

`auto` applies the eligibility rules (section 7). If eligible, it behaves per `auto_resume`:

- `countdown` (default): prints `Resurrecting "<title>" (<agent>, <cwd>) in 3s, press any key to skip`. (The shipped text is `Resurrecting "<title>" (<agent>) in <cwd>. Press any key to skip.`)
- `immediate`: no countdown.
- `ask`: `[Y/n]`.
- `off`: prints the resume command only.

Before launching, it marks the record `consumed` so a crash loop cannot re-trigger. It then runs the agent as a child of the shell (not `exec`), so quitting the agent leaves a normal shell. The resumed session's own SessionStart hook rewrites the record as `live` with the new pid. Skipping marks the record `skipped`.

> **Superseded in 0.2.0:** the agent is `exec`ed in place of `agent-resurrect`, so no Python parent sits between the shell and the agent (quitting the agent still returns to the shell). `auto` claims the record with one atomic `live` to `consumed` step after the countdown, so when two shells race only one launches; the loser logs `lost-race`. A failed launch sets the record to `skipped` and logs `launch-failed`, so new shells stop retrying.

### 6.3 Manual commands

| Command | Behaviour |
|---|---|
| `agent-resurrect` | Resume what died in this pane, no countdown, ignoring `consumed`/`skipped`. If this pane has no record, print `ls` output instead. |
| `agent-resurrect ls` | List restorable records: agent, title, cwd, last active, pane label, whether that pane exists now. Numbered, newest `updated_at` first. (**Superseded:** it shows status, not whether the pane exists.) |
| `agent-resurrect resume <n or session-id>` | Resume a listed record or any session id, in this pane. |
| `agent-resurrect find [text] [--clipboard]` | Text search (section 6.6); prints matches and the resume command. |
| `agent-resurrect doctor` | Check every required setting, the hook installation, PATH, and the last restore's GUID matches; print a fix for each failure. |
| `agent-resurrect uninstall [--purge]` | Revert everything from the manifest; `--purge` also deletes records and logs. |

> **Superseded in 0.2.0:** the command set, flags, exit codes and `--json` documents are in the README (Commands, For AI coding agents). New in 0.2.0: `simulate`, `adopt`, `version`, `ls --ids`, `--print` on `resume` and `here`, `--dry-run` on `install` and `uninstall`, `install --no-plugin`, `doctor --strict`, and `--json` on every user command. `resume`, `here` and the bare command never launch an agent without a terminal: they print the command and exit 4.

### 6.4 Shell-level recorder (designed now, built with the first non-Claude agent)

A zsh `preexec` / bash `DEBUG` hook in the same rc block notes `(pane key, agent command, cwd, start time)` when the command line starts with a known agent executable. After a restart, that agent's adapter resolves the session id from the agent's own session store by cwd and start time and builds its resume command (for example `codex resume <id>`, `opencode --session <id>`). Claude keeps hook-based recording because it is exact across `/clear` and `/resume`.

### 6.5 Resume command and permission mode

`resume_argv` for Claude is `claude --resume <session_id>`, plus `--dangerously-skip-permissions` according to `permission_flag`:

- `mirror` (default): added only if the recorded `permission_mode` was `bypassPermissions`. Other modes are restored by Claude Code itself.
- `always`: always added.
- `never`: never added.

The command runs with the recorded `cwd` as working directory. If that directory no longer exists, it runs from the current directory and prints a warning; `--resume` still finds the session across projects.

### 6.6 Text search fallback

`finder` is the tested `find_session.py`:

- It scans top-level transcripts under `$CLAUDE_CONFIG_DIR/projects` (default `~/.claude`) in parallel with stdlib `mmap`.
- It searches with up to 10 probes, each at most 40 characters of plain prose taken from within single lines and JSON-escaped. These widths were measured: 60-character probes straddle stripped markdown, 30-character probes match around 1,600 sessions by chance.
- It ranks matches by probe coverage first, then by whether the text appears in a user or Claude message rather than only in tool output, then by recency.
- It excludes `CLAUDE_CODE_SESSION_ID`.
- It trusts only a `cwd` whose dash-encoding equals the project folder name.

It covers sessions that have no record: anything started before install, or anything run outside iTerm2. The plugin also ships it as a Claude Code skill (paste text after the skill name).

## 7. Eligibility rules for automatic restore

A pane's record is restored automatically only when all of these hold:

1. Its status is `live`.
2. The recorded agent process is gone, meaning no process exists with the recorded pid and the recorded start time. A reused pid does not count as alive.
3. `session_exists` is true (for Claude, the transcript file is still on disk).
4. The record is newer than `max_age_days` (default 14). Older records are garbage-collected by `ls`, `auto` and `doctor`.

The manual commands skip rule 1.

## 8. Data

**Paths (XDG):**

| What | Path |
|---|---|
| Code | `~/.local/share/agent-resurrect/` |
| Launcher | `~/.local/bin/agent-resurrect`, and `~/.local/bin/resurrect` as a symlink |
| Records | `~/.local/state/agent-resurrect/panes/<pane key>.json` |
| Install manifest | `~/.local/state/agent-resurrect/manifest.json` |
| Log | `~/.local/state/agent-resurrect/log`, capped at 256 KB with one rotation |
| Config | `~/.config/agent-resurrect/config` (INI, `[agent-resurrect]` section) |

**Record (schema 1):**

```json
{
  "schema": 1,
  "terminal": "iterm",
  "pane_key": "iterm-10FE02F5-C525-405E-9E86-C8255ED17032",
  "pane_label": "w0t12p0",
  "agent": "claude",
  "session_id": "83fc435f-73ea-41b2-b2e3-076ee2c3f7b0",
  "cwd": "/Users/me/project",
  "transcript_path": "/Users/me/.claude/projects/-Users-me-project/83fc435f-....jsonl",
  "permission_mode": "bypassPermissions",
  "source": "startup",
  "agent_pid": 92528,
  "agent_started": "Mon Oct 12 11:40:02 2026",
  "status": "live",
  "recorded_at": "2026-10-12T06:16:16Z",
  "updated_at": "2026-10-12T06:40:00Z"
}
```

**Config keys:**

| Key | Values | Default |
|---|---|---|
| `auto_resume` | `countdown`, `immediate`, `ask`, `off` | `countdown` |
| `countdown_seconds` | integer | `3` |
| `permission_flag` | `mirror`, `always`, `never` | `mirror` |
| `max_age_days` | integer | `14` |

## 9. Installer

### 9.1 Entry points

1. **One line:** `curl -fsSL https://github.com/imgovind/agent-resurrect/releases/latest/download/install.sh | bash` (updated in 0.2.0; 0.1.0 fetched `install.sh` from `main`)
2. **Paste into Claude Code** (README section "Install with Claude Code"), copy-paste ready. Rewritten in 0.2.0: one download, read and run the same file, a dry run and a pause for consent, then `doctor --json`:

   > Install agent-resurrect (https://github.com/imgovind/agent-resurrect) on this Mac. Do these steps in order. 1) Download the installer once, to a file: `curl -fsSL https://github.com/imgovind/agent-resurrect/releases/latest/download/install.sh -o "${TMPDIR:-/tmp}/agent-resurrect-install.sh"`. Read all of it, then run `bash "${TMPDIR:-/tmp}/agent-resurrect-install.sh" --dry-run`, which shows every change without making any. 2) Tell me in a few bullets what it will change, including the iTerm2 settings, the LaunchAgent and my shell rc file, then stop and wait for my OK. 3) After I agree, run the same file you read, not a new download: `bash "${TMPDIR:-/tmp}/agent-resurrect-install.sh"`, allowing it up to 5 minutes. Do not use sudo. 4) Run `~/.local/bin/agent-resurrect doctor --json`. For each check whose status is "fail": if fix.agent_may_run is true, run fix.argv once, then run doctor again; otherwise tell me fix.text and do not try it yourself. Leave warnings alone. If a step failed, show me the end of ~/.local/state/agent-resurrect/log. 5) Run `~/.local/bin/agent-resurrect adopt` so this session is recorded too; exit status 3 only means this is not an iTerm2 pane. 6) Finish by telling me what changed; that I should open a new terminal tab and restart any other Claude Code sessions that are running now, because sessions started before the install are not recorded; and how to uninstall: `agent-resurrect uninstall` in a terminal, or `~/.local/bin/agent-resurrect uninstall --yes` if you run it for me.

3. **From a clone:** `./install.sh` copies from the working tree instead of downloading. This is also the development path.

4. **From inside Claude Code** (added in 0.2.0): `/plugin marketplace add imgovind/agent-resurrect`, `/plugin install agent-resurrect@agent-resurrect`, then `/agent-resurrect:setup`, which runs the plugin's own `install.sh` with no download.

A double-click `.pkg` or `.command` is deferred: unsigned downloads are blocked by Gatekeeper, so they would be less "one click" than a pasted line.

### 9.2 Steps (`install.sh` is a thin bash bootstrap; the logic is in Python so it is testable)

1. **Preflight.** It's macOS. A Python 3.9+ interpreter is found: `/usr/bin/python3` only if `xcode-select -p` succeeds (so it never triggers the Command Line Tools install dialog), otherwise `python3` on PATH. Missing `claude` or a missing `/Applications/iTerm.app` are warnings, not failures; the related steps are skipped and `doctor` reports them.
2. **Fetch.** It downloads the release tarball pinned in `install.sh` (`AGENT_RESURRECT_REF` overrides it), or uses the working tree when run from a clone. It installs to `~/.local/share/agent-resurrect`. (0.2.0: the `install.sh` attached to a release pins the exact commit through `AGENT_RESURRECT_COMMIT`, the release lists `SHA256SUMS`, and `install.sh --dry-run` stages to a temporary folder and runs `install --dry-run` without writing under HOME.)
3. **Launcher.** It writes `~/.local/bin/agent-resurrect`, a two-line sh script that runs `exec <resolved python> -I <share>/src/agent_resurrect/cli.py "$@"`, and adds the `resurrect` symlink. If `~/.local/bin` is not on PATH, the rc block adds it.

   > **Superseded in 0.2.0:** the launcher runs `<share>/libexec/agent-resurrect.py` (0.1.0: `<share>/bin/agent-resurrect.py`) with `-X pycache_prefix=~/.cache/agent-resurrect/pycache`. The rc block appends `~/.local/bin` to PATH instead of prepending it.

4. **`agent-resurrect install`.** It applies the settings below in order, writing each original value to the manifest before changing anything. It is idempotent: a setting already in the desired state is recorded as `unchanged` and is never reverted.
5. **Doctor.** It runs `doctor` and prints a summary of what changed plus the uninstall command.

### 9.3 Settings applied

| Id | Desired state | Apply | Revert |
|---|---|---|---|
| `macos.keep_windows` | macOS "Close windows when quitting an application" off | `defaults write -g NSQuitAlwaysKeepsWindows -bool true` | Restore the original value, or delete the key if it was absent |
| `iterm.keep_windows` (0.2.0, replaces `macos.keep_windows`) | iTerm2 keeps its windows on quit, other apps unaffected | `defaults write com.googlecode.iterm2 NSQuitAlwaysKeepsWindows -bool true` | Restore the original value, or delete the key if it was absent |
| `iterm.restore_policy` | iTerm2 restores windows at launch ("Use System Window Restoration Setting") | `defaults write com.googlecode.iterm2 OpenArrangementAtStartup -bool false` and `OpenNoWindowsAtStartup -bool false` | Restore originals |
| `iterm.launch_at_login` | iTerm2 opens at login | LaunchAgent `~/Library/LaunchAgents/io.github.imgovind.agent-resurrect.iterm.plist` running `/usr/bin/open -a iTerm` at load. Install only writes it: `launchctl bootstrap` would run it at once and pull iTerm2 to the front, so launchd loads it at the next login | `bootout` (ignoring "not loaded") and delete |
| `shell.zsh`, `shell.bash` | rc block present | Append a block between `# >>> agent-resurrect >>>` and `# <<< agent-resurrect <<<` | Remove exactly that block |
| `claude.plugin` | Plugin installed and enabled | `claude plugin marketplace add ~/.local/share/agent-resurrect`, then `claude plugin install agent-resurrect@agent-resurrect` | Uninstall the plugin and remove the marketplace |

The LaunchAgent is used instead of a System Events login item because it needs no Automation permission prompt. If macOS also reopens iTerm2, `open -a` is harmless.

> **Superseded in 0.2.0:** `macos.keep_windows` changed the setting for every app. 0.2.0 writes `NSQuitAlwaysKeepsWindows` in iTerm2's own domain only; the app domain is searched before the global one. When the manifest holds a `macos.keep_windows` entry from 0.1.0, both install and uninstall revert it (restoring the user's global value) and drop the entry. The order is now `iterm.keep_windows`, `iterm.restore_policy`, `iterm.launch_at_login`, `shell.zsh`, `shell.bash`, `claude.plugin`. `claude.plugin` leaves an `agent-resurrect` plugin that is already installed from GitHub alone and never adds a second marketplace with the same name.

## 10. Error handling and safety

- **Hooks fail open.** They always exit 0 and never print, so Claude Code is never blocked or slowed visibly.
- **Pane isolation.** `auto` never resumes a session in a pane other than its own. Cross-pane restore happens only through the manual `resume`.
- **No privilege escalation.** No `sudo` anywhere. The installer touches only the user's home directory and user defaults.
- **No agent without a terminal (0.2.0).** Commands that would launch an agent refuse when stdin or stdout is not a terminal: they print the command, change no state and exit 4.
- **Private state (0.2.0).** The state and panes directories are created with mode 0700.
- **Safe uninstall (0.2.0).** An unreadable, corrupt or newer-version manifest makes uninstall refuse without reverting or deleting anything. When a revert fails, the launcher, the code and that setting's manifest entry are kept so a retry works.
- **Explicit changes.** Every changed setting is listed at the end of install, and uninstall reverts from the manifest, not from assumptions.
- **No network at runtime.** The only network access is the tarball download during install.
- **Corruption tolerance.** Corrupt or unknown-schema records are logged and ignored, never deleted silently.

## 11. Testing

- **Unit tests** (stdlib `unittest`, run on `/usr/bin/python3` 3.9 and 3.12):
  - Hook payload fixtures mapped to records.
  - The skip rules (non-foreground, `-p`, nested) via a fake `procs`.
  - The SessionEnd reason table.
  - Eligibility, covering dead, alive and reused pid, consumed and skipped status, a missing transcript, and age.
  - `resume_argv` for every `permission_flag` value and a missing cwd.
  - The finder on synthetic transcripts, including JSON escaping, wrapped and markdown-stripped pastes, self-exclusion and cwd trust.
  - rc block insert and remove, preserving surrounding content.
  - Apply and revert for each setting, against fake `defaults`, `launchctl` and `claude` executables on PATH and a temporary HOME.
  - Install twice gives the same state, and uninstall restores every original value.
- **Pane-key contract test.** The shell snippet and `TerminalAdapter.pane_key` must produce the same key for the same environment.
- **Local end-to-end (no reboot needed):**
  1. Install from the clone on the author's Mac.
  2. Start `claude` in a pane and confirm the record.
  3. Kill it with SIGTERM and confirm the record is kept.
  4. Run `ITERM_SESSION_ID=<that pane id> zsh -i` and confirm the countdown and that the right session resumes with the right flag.
  5. Exit with `/exit` and confirm the record is deleted.
- **Real-world check.** The author's next restart, followed by `agent-resurrect doctor` reporting GUID matches.
- **CI.** GitHub Actions on `macos-latest` runs the unit tests on 3.9 and 3.12, plus `shellcheck` on `install.sh` and the bash snippet.

## 12. Repository layout

> **Superseded in 0.2.0:** the plugin now lives at the repository root (`.claude-plugin/plugin.json`, `hooks/`, `skills/resume-session/`, `skills/setup/`), `bin/agent-resurrect` is a POSIX sh wrapper, the Python entry point is `libexec/agent-resurrect.py`, and JSON Schemas live in `docs/json/`. The layout below is the 0.1.0 one; AGENTS.md has the current layout.

```
agent-resurrect/
├── README.md                     problem, 1-line install, Claude prompt, how it works, FAQ, comparison, uninstall
├── LICENSE                       MIT
├── CHANGELOG.md
├── install.sh
├── .claude-plugin/marketplace.json
├── plugin/
│   ├── .claude-plugin/plugin.json
│   ├── hooks/hooks.json
│   └── skills/resume-by-text/SKILL.md
├── src/agent_resurrect/
│   ├── cli.py  store.py  procs.py  restore.py  finder.py  settings.py  installer.py  config.py
│   ├── agents/   base.py  claude.py
│   └── terminals/ base.py  iterm.py
├── shell/agent-resurrect.zsh  shell/agent-resurrect.bash
├── tests/
├── docs/specs/
└── .github/workflows/test.yml
```

## 13. Publishing (only after the author approves the finished repo)

- > **Superseded in 0.2.0:** the description and topics now claim only what ships (Claude Code, iTerm2, macOS); topics for unsupported agents and terminals were dropped. The original plan follows.
- **GitHub description** (v1 claims only what ships): "Resurrect your AI coding agent sessions after a restart, in the same terminal pane. Claude Code + iTerm2 today; Codex, Cursor, opencode, Qoder, Ghostty, Warp next." It is updated as each adapter ships.
- **Topics:** `claude-code`, `codex`, `cursor`, `opencode`, `iterm2`, `ghostty`, `warp`, `tmux-resurrect`, `session-restore`, `ai-agents`.
- **README headings** match real queries: "Resume Claude Code after a restart", "Restore Claude Code sessions after reboot in iTerm2".
- **README comparison** with cc-session-restore, claude-iterm2-resume-sessions, cc-sessions, PonyMux and Agent-Resume, crediting prior art.
- **Release:** tag `v0.1.0`, so `install.sh` pins a tag and not `main`.

## 14. Open items to settle during implementation (each verified, not assumed)

1. **iTerm2 restoration settings.** The exact semantics of `OpenArrangementAtStartup`, `OpenNoWindowsAtStartup` and `NoSyncIgnoreSystemWindowRestoration` must be confirmed from iTerm2 source or docs before the installer writes them.
2. **Non-interactive plugin install.** Confirm whether `claude plugin install` needs a confirmation flag when run non-interactively.
3. **Live restart test.** Confirm that `NSQuitAlwaysKeepsWindows` plus the LaunchAgent restores iTerm2 windows after a real restart when macOS "Reopen windows when logging back in" is unchecked (the author's machine has it unchecked and restored anyway).
4. **Command name.** Confirm the interactive Claude process's command name is `claude` for all install methods (native installer, npm, Homebrew).

## 15. Changes in 0.2.0

0.2.0 fixes the confirmed findings of the 0.1.0 review, makes the tool usable by AI agents, and makes the Claude Code plugin a complete install route. The plan is `docs/plans/2026-10-12-agent-resurrect-v0.2.md`.

**Machine interface.**

- `--json` on `ls`, `find`, `doctor`, `install`, `uninstall`, `config`, `resume`, `here`, `simulate`, `adopt` and `version`. Exactly one ASCII JSON object on stdout with `schema` (`agent-resurrect.<command>/1`), `version`, `ok` and `error`; fields within `/1` are only added. Schemas are in `docs/json/`.
- Exit codes: 0 `ok`, 1 `error`, 2 `usage`, 3 `not-found`, 4 `needs-tty`, 5 `busy`, 6 `gone`, 130 `interrupted` (`agent_resurrect.exitcodes`). `hook` always exits 0 and prints nothing.
- `resume`, `here` and the bare command never launch without a terminal (exit 4, command printed, no state change); `--print` returns the command with exit 0.
- `find` no longer numbers its candidates (the numbers meant something else to `resume <n>`); `ls --ids` shows session ids.
- New commands: `simulate [--assume-restart] [--pane-key KEY]` runs the eligibility rules of section 7 without launching or changing anything; with `--assume-restart` rule 2 is skipped, because a restart ends every agent. `adopt` records the Claude Code session the command runs under, through the same path as SessionStart (session id from `CLAUDE_CODE_SESSION_ID`, agent from the first `claude` ancestor, cwd and permission mode from the transcript, `source` `adopt`). `version` prints the version.
- `doctor --json` gives each check a stable id and a `fix` with `kind` (`command`, `new-shell`, `restart-agent`, `user-action`, `install-app`), `agent_may_run` and `argv`, plus a `ready` verdict; `--strict` turns warnings into failures. New checks: a login shell other than zsh or bash, running inside tmux, screen or zellij, an un-exported `ZDOTDIR`, and a `.zshrc` that starts tmux before the agent-resurrect block.
- `install` and `uninstall` take `--dry-run` (statuses `would-change`, `would-revert`, `unchanged`, `skipped`), and `install` takes `--no-plugin`.

**Robustness.**

- Multiplexers: no recording or restoring inside tmux, screen or zellij (section 5.2 note).
- `ps` runs with `LC_ALL=C`, so process start times compare correctly in every locale.
- Atomic claim before launch; `lost-race` event for the shell that loses.
- `CLAUDE_CONFIG_DIR` is recorded as `resume_env` and passed back on resume; record schema stays 1 (the field is additive).
- When `claude` is not on PATH, the resume argv falls back to `~/.claude/local/claude` if it is executable. A failed launch sets the record to `skipped`.
- Reading a session's title and first prompt reads only the head and tail of the transcript.
- Uninstall refuses on a bad manifest and keeps what it needs for a retry when a revert fails.
- `iterm.keep_windows` replaces the global `macos.keep_windows`, with a migration for 0.1.0 manifests.

**Distribution.**

- The repository root is the plugin. `/agent-resurrect:setup` (user-invoked only) shows `install --dry-run --json`, asks, runs `${CLAUDE_PLUGIN_ROOT}/install.sh` with no download, then runs `doctor` and `adopt`. The `resume-by-text` skill became `resume-session`, which also covers listing what died.
- A release workflow checks that every version string matches the tag, takes the notes from the CHANGELOG, and publishes `install.sh` (pinned to the release commit) and `SHA256SUMS`. The documented one-liner uses `releases/latest/download/install.sh`.
