# agent-resurrect: design spec

Date: 2026-10-12
Status: draft, awaiting review
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

The pane key format is a contract shared with the shell trigger, which computes it without spawning a process: `iterm-` followed by the part of `ITERM_SESSION_ID` after the first `:`.

## 6. Flows

### 6.1 Recording (Claude Code)

The plugin's `hooks/hooks.json` registers SessionStart and SessionEnd with command `"$HOME/.local/bin/agent-resurrect" hook claude <event> || true`. The hook:

1. Exits immediately if no terminal adapter detects a pane key (not iTerm2, or no `ITERM_SESSION_ID`).
2. Finds the agent process: walks up the parent chain to the nearest ancestor whose command name is `claude`.
3. Skips recording (exit 0) when that process is not interactive: its stat lacks `+`, it has no tty, its argv contains `-p` or `--print`, or another `claude` process is further up the chain (a nested agent started by an agent's tool call would otherwise overwrite the pane's record).
4. SessionStart (any `source`): upserts the pane's record with status `live`, the payload fields, the agent pid and its start time. `clear`, `resume` and `fork` change the session id, so the newest SessionStart always wins.
5. SessionEnd: `prompt_input_exit` or `logout` deletes the record (a deliberate exit). `clear` and `resume` do nothing (the following SessionStart updates the record). `other` or a missing reason keeps the record (reboot, crash, closed tab).
6. Always exits 0, prints nothing, finishes well inside the 1.5 s SessionEnd budget (target under 150 ms), and logs errors to the log file.

### 6.2 Automatic restore

The installer adds a marked block to `~/.zshrc` (and `~/.bashrc` when present). In interactive shells only, it registers a one-shot first-prompt hook (`precmd` in zsh, `PROMPT_COMMAND` in bash) so it runs after frameworks such as oh-my-zsh and powerlevel10k instant prompt have finished. The hook removes itself, computes the pane key, and runs `agent-resurrect auto` only if `~/.local/state/agent-resurrect/panes/<key>.json` exists. No record means no process is spawned.

`auto` applies the eligibility rules (section 7). If eligible, it behaves per `auto_resume`:

- `countdown` (default): prints `Resurrecting "<title>" (<agent>, <cwd>) in 3s, press any key to skip`.
- `immediate`: no countdown.
- `ask`: `[Y/n]`.
- `off`: prints the resume command only.

Before launching, it marks the record `consumed` so a crash loop cannot re-trigger. It then runs the agent as a child of the shell (not `exec`), so quitting the agent leaves a normal shell. The resumed session's own SessionStart hook rewrites the record as `live` with the new pid. Skipping marks the record `skipped`.

### 6.3 Manual commands

| Command | Behaviour |
|---|---|
| `agent-resurrect` | Resume what died in this pane, no countdown, ignoring `consumed`/`skipped`. If this pane has no record, print `ls` output instead. |
| `agent-resurrect ls` | List restorable records: agent, title, cwd, last active, pane label, whether that pane exists now. Numbered, newest `updated_at` first. |
| `agent-resurrect resume <n or session-id>` | Resume a listed record or any session id, in this pane. |
| `agent-resurrect find [text] [--clipboard]` | Text search (section 6.6); prints matches and the resume command. |
| `agent-resurrect doctor` | Check every required setting, the hook installation, PATH, and the last restore's GUID matches; print a fix for each failure. |
| `agent-resurrect uninstall [--purge]` | Revert everything from the manifest; `--purge` also deletes records and logs. |

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

1. **One line:** `curl -fsSL https://raw.githubusercontent.com/imgovind/agent-resurrect/main/install.sh | bash`
2. **Paste into Claude Code** (README section "Install with Claude Code"), copy-paste ready:

   > Install agent-resurrect from https://github.com/imgovind/agent-resurrect on this Mac. First download install.sh from that repo, read it, and tell me in a few bullets what it will change. Then run `curl -fsSL https://raw.githubusercontent.com/imgovind/agent-resurrect/main/install.sh | bash`. After it finishes, run `~/.local/bin/agent-resurrect doctor`; for anything not marked OK, run the fix it suggests and run doctor again. Do not use sudo. Finish by telling me what changed and the uninstall command.

3. **From a clone:** `./install.sh` copies from the working tree instead of downloading. This is also the development path.

A double-click `.pkg` or `.command` is deferred: unsigned downloads are blocked by Gatekeeper, so they would be less "one click" than a pasted line.

### 9.2 Steps (`install.sh` is a thin bash bootstrap; the logic is in Python so it is testable)

1. **Preflight.** It's macOS. A Python 3.9+ interpreter is found: `/usr/bin/python3` only if `xcode-select -p` succeeds (so it never triggers the Command Line Tools install dialog), otherwise `python3` on PATH. Missing `claude` or a missing `/Applications/iTerm.app` are warnings, not failures; the related steps are skipped and `doctor` reports them.
2. **Fetch.** It downloads the release tarball pinned in `install.sh` (`AGENT_RESURRECT_REF` overrides it), or uses the working tree when run from a clone. It installs to `~/.local/share/agent-resurrect`.
3. **Launcher.** It writes `~/.local/bin/agent-resurrect`, a two-line sh script that runs `exec <resolved python> -I <share>/src/agent_resurrect/cli.py "$@"`, and adds the `resurrect` symlink. If `~/.local/bin` is not on PATH, the rc block adds it.
4. **`agent-resurrect install`.** It applies the settings below in order, writing each original value to the manifest before changing anything. It is idempotent: a setting already in the desired state is recorded as `unchanged` and is never reverted.
5. **Doctor.** It runs `doctor` and prints a summary of what changed plus the uninstall command.

### 9.3 Settings applied

| Id | Desired state | Apply | Revert |
|---|---|---|---|
| `macos.keep_windows` | macOS "Close windows when quitting an application" off | `defaults write -g NSQuitAlwaysKeepsWindows -bool true` | Restore the original value, or delete the key if it was absent |
| `iterm.restore_policy` | iTerm2 restores windows at launch ("Use System Window Restoration Setting") | `defaults write com.googlecode.iterm2 OpenArrangementAtStartup -bool false` and `OpenNoWindowsAtStartup -bool false` | Restore originals |
| `iterm.launch_at_login` | iTerm2 opens at login | LaunchAgent `~/Library/LaunchAgents/io.github.imgovind.agent-resurrect.iterm.plist` running `/usr/bin/open -a iTerm` at load. Install only writes it: `launchctl bootstrap` would run it at once and pull iTerm2 to the front, so launchd loads it at the next login | `bootout` (ignoring "not loaded") and delete |
| `shell.zsh`, `shell.bash` | rc block present | Append a block between `# >>> agent-resurrect >>>` and `# <<< agent-resurrect <<<` | Remove exactly that block |
| `claude.plugin` | Plugin installed and enabled | `claude plugin marketplace add ~/.local/share/agent-resurrect`, then `claude plugin install agent-resurrect@agent-resurrect` | Uninstall the plugin and remove the marketplace |

The LaunchAgent is used instead of a System Events login item because it needs no Automation permission prompt. If macOS also reopens iTerm2, `open -a` is harmless.

## 10. Error handling and safety

- **Hooks fail open.** They always exit 0 and never print, so Claude Code is never blocked or slowed visibly.
- **Pane isolation.** `auto` never resumes a session in a pane other than its own. Cross-pane restore happens only through the manual `resume`.
- **No privilege escalation.** No `sudo` anywhere. The installer touches only the user's home directory and user defaults.
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
