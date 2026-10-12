# agent-resurrect v1 Implementation Plan

> **For agentic workers:** Execution uses the wave + integrator pattern. Each wave-1 task runs in its own git worktree on its own branch with the disjoint file ownership listed below. Steps use checkbox (`- [ ]`) syntax for tracking. Read the spec first.

**Goal:** After a restart, each iTerm2 pane that was running a Claude Code session resumes that session by itself, and a one-line installer sets up everything needed.

**Architecture:** Claude Code plugin hooks record `pane key -> session` into one JSON file per pane. A shell snippet, on the first prompt of a new shell, checks for its pane's file and, if eligible, runs `agent-resurrect auto`. That command counts down and then `exec`s `claude --resume`. Agents and terminals sit behind adapter interfaces. Every system change is a `Setting` with check, apply and revert, which gives an exact uninstall.

**Tech Stack:** Python 3.9+ standard library only (run as `python3 -I`), zsh and bash snippets, bash `install.sh`, a Claude Code plugin (JSON), and `unittest`.

**Spec:** `docs/specs/2026-10-12-agent-resurrect-design.md`. Every task implicitly includes its requirements.

## Global Constraints

- Python 3.9 compatible and stdlib only. Use `typing.Optional`/`List`/`Dict`; no `X | Y` unions, no `match`.
- All code runs under `python3 -I`: never rely on the current directory, `PYTHONPATH` or user site-packages at runtime.
- Tests: `./run_tests.sh` must pass on `/usr/bin/python3` (3.9) and `python3` (3.12). Any test touching files uses `tests.helpers.temp_home()`.
- **Never touch the real system in tests or during development.** That means no real HOME, no real `defaults write`, no real `launchctl`, and no real `claude plugin install/uninstall/marketplace add`. Settings call executables by name through `PATH`, and tests put fakes first on `PATH`. Against the real `claude` CLI, only `--help` and `claude plugin validate <path>` are allowed.
- The hook command always exits 0 and never writes to stdout or stderr.
- Pane-key contract: iTerm2 key = `"iterm-"` + the text after the first `:` in `ITERM_SESSION_ID`. Python and the shell snippets compute it identically, and Python additionally requires a GUID-shaped value.
- Command names are exact: `agent-resurrect` (alias `resurrect`), and subcommands `here`, `ls`, `resume`, `find`, `auto`, `hook`, `doctor`, `install`, `uninstall`, `config`.
- User-facing text has no em dashes and no ALL-CAPS headings.
- Foundation files are frozen: `paths.py`, `model.py`, `store.py`, `procs.py`, `config.py`, `logs.py`, `agents/base.py`, `terminals/base.py`, `settings/base.py`, `tests/helpers.py`. If you need a change there, do not edit them. Describe it in your final report, and the integrator applies it.
- Registries `agents/__init__.py` and `terminals/__init__.py` are owned by the integrator only.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Foundation API (Task 0, done, commit 3a5957f)

```python
# paths: home(), state_dir(), config_dir(), data_dir(), bin_dir(), panes_dir(), manifest_path(),
#        log_path(), restore_events_path(), config_path(), claude_config_dir()   (all honour HOME/XDG_*/CLAUDE_CONFIG_DIR)
# model: Record(terminal, pane_key, agent, session_id, cwd, pane_label=None, transcript_path=None,
#               permission_mode=None, source=None, agent_pid=None, agent_started=None, status="live",
#               recorded_at="", updated_at="", schema=1); .to_dict(); Record.from_dict(d) (ValueError if bad)
#        SessionInfo(title, first_prompt, started, last_active); Candidate(session_id, transcript_path, cwd,
#               cwd_trusted, hits, total_probes, roles, info); STATUS_LIVE/CONSUMED/SKIPPED; utc_now_iso(); parse_iso()
# store: valid_pane_key(k), record_path(k), load(k)->Optional[Record], save(r)->Record (atomic), delete(k)->bool,
#        set_status(k, s), list_records() (newest first), gc(max_age_days)->List[str],
#        append_restore_event(k, result, detail=""), read_restore_events()->List[dict]
# procs: ProcInfo(pid, ppid, stat, tty, started, comm, args) .name .has_tty .foreground;
#        Procs(run=None).get(pid), .ancestors(pid) (self first), .is_alive(pid, started)
# config: Config(auto_resume, countdown_seconds, permission_flag, max_age_days); load_config(); set_value(k, v); DEFAULTS; CHOICES
# logs: log(message)
# agents.base: HookContext(env, terminal, pane_key, pane_label, procs, pid); AgentAdapter (on_hook, session_exists,
#        resume_argv, describe, session_record, find_by_text)
# terminals.base: TerminalAdapter (name, restores_in_place, detect, pane_key, pane_label, settings)
# settings.base: Check(ok, detail); Setting (id, description, fix_hint, check, apply->Optional[dict], revert(original))
# tests.helpers: temp_home(), proc(...), FakeProcs(*procs), FakeTerminal (FAKE_PANE env), FakeAgent(exists=True), make_record(**kw)
```

## Review Focus

1. **Ctrl-C or Ctrl-Z in a resumed agent.** The signal must reach only the agent. `restore.launch` therefore `os.execvp`s the agent, so no Python parent remains to die on SIGINT and hand the terminal back to the shell while the agent still runs. Pinned in Task B (`test_launch_execs_without_spawning`).
2. **A non-interactive or piped shell in a restored pane** (iTerm "send text at start", `zsh -i -c ...`) must not block on a countdown. `auto` returns immediately when stdin or stdout is not a TTY. Pinned in Task B (`test_auto_never_waits_without_a_tty`).
3. **A nested `claude -p`** started by an agent's own tool call in the same pane must never overwrite the pane's record, and its SessionEnd must never delete it. Pinned in Task A (`test_nested_claude_is_not_recorded`, `test_session_end_for_another_session_is_ignored`).
4. **The user's rc file is a symlink into a dotfiles repo,** lacks a trailing newline, or already has our block. The block must be added to and removed from the symlink target in place, leaving the symlink and every surrounding byte unchanged. Pinned in Task C (`test_rc_symlink_and_bytes_preserved`).
5. **A reinstall or upgrade must keep the original values captured by the first install,** so uninstall restores the true originals, not the already-changed values. Pinned in Task C (`test_reinstall_keeps_first_originals`).

---

## Wave 1 (parallel). Worktrees: `~/Developer/imgovind/agent-resurrect-wt/<task>`, branches `wave1/<task>`

### Task A (`claude`): Claude Code adapter, text finder, plugin

**Files (owned):**
- Create: `src/agent_resurrect/finder.py`, `src/agent_resurrect/agents/claude.py`
- Create: `plugin/.claude-plugin/plugin.json`, `plugin/hooks/hooks.json`, `plugin/skills/resume-by-text/SKILL.md`, `.claude-plugin/marketplace.json`
- Test: `tests/test_finder.py`, `tests/test_claude_adapter.py`

**Interfaces:**
- Consumes: the foundation API above.
- Produces:
  ```python
  # finder.py
  def build_probes(text: str, max_probes: int = 10) -> List[str]          # ValueError("...too short...") if < 8 chars of text
  def search(text: str, projects_dir: Path, exclude: Optional[str] = None,
             limit: int = 5, max_probes: int = 10) -> List[Candidate]      # best first; [] if nothing
  def find_transcript(session_id: str, projects_dir: Path) -> Optional[Path]  # <projects_dir>/*/<id>.jsonl
  def transcript_meta(path: Path) -> TranscriptMeta
  @dataclass class TranscriptMeta: cwd: Optional[str]; cwd_trusted: bool; info: SessionInfo; permission_mode: Optional[str]
  def last_permission_mode(path: Path) -> Optional[str]                  # last "permissionMode" in the final 256 KB
  # agents/claude.py
  def is_claude_process(p: ProcInfo) -> bool
  class ClaudeAdapter(AgentAdapter): name = "claude"
  ```

**Source to port:** `~/.claude/skills/resume-by-text/scripts/find_session.py` is tested and tuned. Keep `PLAIN_RUN`, `PROBE_WIDTH = 40`, the `--probes` default of 10, `ECHO_TYPES`, the role ranking and the cwd-trust rule. Replace all printing with returned `Candidate` objects, and add a sequential fallback if `ProcessPoolExecutor` raises `OSError`. `scan` must stay a module-level function so it can be pickled.

**ClaudeAdapter behaviour:**
- `is_claude_process(p)`: true if `p.name == "claude"`; or `p.name in ("node", "bun")` and `"claude-code"` appears in `p.args`; or the basename of the first token of `p.args` is `claude`. The hook's own `python3 ... hook claude SessionStart` must be false.
- `on_hook(event, payload, ctx)`:
  1. If `payload` is not a dict or has no `session_id`: log and return.
  2. Walk `ctx.procs.ancestors(ctx.pid)` to the first process where `is_claude_process` is true. If there is none: log and return.
  3. SessionStart: skip with a log line when any of these hold:
     - another claude process appears further up the chain (nested);
     - the agent has no tty;
     - the agent is not foreground;
     - its args contain the token `-p`, the token `--print`, or a token starting with `--print=`.
     Otherwise save `Record(terminal=ctx.terminal.name, pane_key=ctx.pane_key, pane_label=ctx.pane_label, agent="claude", session_id, cwd=payload.get("cwd") or "", transcript_path, permission_mode, source, agent_pid=agent.pid, agent_started=agent.started, status="live")`. Keep `recorded_at` from the existing record only if it has the same `session_id`.
  4. SessionEnd: delete the record only if `reason in ("prompt_input_exit", "logout")` and the existing record's `session_id` equals the payload's. Otherwise do nothing, which covers `other`, `clear`, `resume` and a missing reason.
- `session_exists(r)`: `r.transcript_path` exists, or `find_transcript(r.session_id, paths.claude_config_dir() / "projects")` is found.
- `resume_argv(r, cfg)`: `["claude", "--resume", r.session_id]`. Append `--dangerously-skip-permissions` when `cfg.permission_flag == "always"`, or when it is `"mirror"` and the current mode is `"bypassPermissions"`. The current mode is `last_permission_mode(transcript)` if readable, else `r.permission_mode`.
- `describe(r)`: `transcript_meta(path).info`, or `SessionInfo()` if there is no transcript.
- `session_record(id)`: find the transcript and use `transcript_meta`, giving `Record(terminal="", pane_key="", agent="claude", session_id=id, cwd=meta.cwd or "", transcript_path=str(path), permission_mode=meta.permission_mode)`. Otherwise `None`.
- `find_by_text(text, exclude=None, limit=5)`: `finder.search(text, paths.claude_config_dir() / "projects", exclude=exclude or os.environ.get("CLAUDE_CODE_SESSION_ID"), limit=limit)`.

**Plugin files** (validate with `claude plugin validate plugin` and `claude plugin validate .`):

`plugin/.claude-plugin/plugin.json`
```json
{
  "name": "agent-resurrect",
  "version": "0.1.0",
  "description": "Records which terminal pane each Claude Code session runs in, so agent-resurrect can resume it there after a restart.",
  "author": { "name": "imgovind" },
  "homepage": "https://github.com/imgovind/agent-resurrect",
  "license": "MIT"
}
```
`plugin/hooks/hooks.json`
```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "timeout": 10,
      "command": "[ -x \"$HOME/.local/bin/agent-resurrect\" ] && \"$HOME/.local/bin/agent-resurrect\" hook claude SessionStart </dev/stdin >/dev/null 2>&1 || true" }] }],
    "SessionEnd": [{ "hooks": [{ "type": "command", "timeout": 5,
      "command": "[ -x \"$HOME/.local/bin/agent-resurrect\" ] && \"$HOME/.local/bin/agent-resurrect\" hook claude SessionEnd </dev/stdin >/dev/null 2>&1 || true" }] }]
  }
}
```
`.claude-plugin/marketplace.json`
```json
{
  "name": "agent-resurrect",
  "owner": { "name": "imgovind" },
  "plugins": [{ "name": "agent-resurrect", "source": "./plugin", "version": "0.1.0",
    "description": "Resume Claude Code sessions in their iTerm2 panes after a restart." }]
}
```
If `claude plugin validate` reports a schema difference, follow the validator and record the change in your report.

`plugin/skills/resume-by-text/SKILL.md`: adapt `~/.claude/skills/resume-by-text/SKILL.md`. Step 2 becomes:
```bash
agent-resurrect find <<'PASTED_TEXT_END'
<pasted text, verbatim>
PASTED_TEXT_END
```
Keep the "print the command, never run it" rule and the output-reading table. Remove the "do not hand-roll" section; mention once that the search is built in.

- [ ] **Step 1: Write failing tests.** `tests/test_finder.py` builds synthetic transcripts under a temp `CLAUDE_CONFIG_DIR` with a helper that writes JSONL lines. Required tests:
  ```python
  def write_session(projects, folder, session_id, records):   # records: list of dicts -> one JSON line each
      path = projects / folder / f"{session_id}.jsonl"; path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text("".join(json.dumps(r) + "\n" for r in records)); return path

  def user(text, cwd="/Users/me/proj", sid="s1", mode=None): ...   # {"type":"user","message":{"role":"user","content":text},"cwd":cwd,"sessionId":sid,"timestamp":"2026-10-10T10:00:00Z", "permissionMode": mode}
  def assistant(text, sid="s1"): ...                                 # {"type":"assistant","message":{"content":[{"type":"text","text":text}]},"sessionId":sid}
  def tool_result(text, sid="s1"): ...                               # {"type":"user","message":{"content":[{"type":"tool_result","content":text}]},"sessionId":sid}
  ```
  - `test_finds_multiline_paste_with_quotes`: the transcript contains `He said "deploy the uat2 worker first" and then\nwe merged`; the pasted text is the same with real newlines and quotes. Expect `search` to return session `s1` first.
  - `test_wrapped_and_markdown_stripped_paste`: the transcript has `` the `cloudbuild_worker_uat2.yaml` file deploys to UAT's agent-worker service, which is wrong for this rollout plan entirely ``. The paste is the same text with backticks removed, wrapped at 30 columns, and indented with `"⏺ "`. Expect `s1` first, with `hits >= 2`.
  - `test_excludes_current_session`: two sessions contain the text and `exclude="s1"`. Only `s2` is returned.
  - `test_ranks_conversation_over_tool_output`: `s1` has the text only in a tool_result (newer mtime); `s2` has it in a user message (older). Expect `s2` first.
  - `test_cwd_trusted_only_when_it_encodes_to_folder`: folder `-Users-me-proj` with cwd `/Users/me/proj` gives `cwd_trusted` True. Folder `-Users-me-other` with the same cwd gives False and still reports the cwd.
  - `test_too_short_text`: `build_probes("ok")` raises ValueError.
  - `test_last_permission_mode`: the transcript has an early `permissionMode: default` and a later `bypassPermissions`. Expect `bypassPermissions`.
  - `test_title_and_first_prompt`: records `{"type":"ai-title","aiTitle":"Fix login"}` and a user message `hello there`. `transcript_meta` gives title `Fix login` and first_prompt `hello there`.

  `tests/test_claude_adapter.py`. Note: the tree helper must be defined so that `chain[0]` is the hook.
  ```python
  def tree(*extra, agent_args="claude", agent_stat="S+", tty="ttys029"):
      # pid 300 = hook (python3), 200 = sh -c, 100 = claude, 50 = -zsh (pane shell)
      return FakeProcs(proc(300, 200, "/usr/bin/python3", "python3 -I x hook claude SessionStart"),
                       proc(200, 100, "/bin/sh", "sh -c ..."),
                       proc(100, 50, "claude", agent_args, stat=agent_stat, tty=tty),
                       proc(50, 1, "-zsh"), *extra)
  def ctx(procs): return HookContext(env={"FAKE_PANE": "p1"}, terminal=FakeTerminal(), pane_key="fake-p1",
                                     pane_label="p1", procs=procs, pid=300)
  START = {"session_id": "s1", "cwd": "/Users/me/proj", "transcript_path": "/x/s1.jsonl",
           "permission_mode": "bypassPermissions", "source": "startup"}
  ```
  - `test_session_start_records_pane`: after `on_hook("SessionStart", START, ctx(tree()))`, `store.load("fake-p1")` has session `s1`, `agent_pid == 100`, the agent's `started`, status `live` and terminal `fake`.
  - `test_nested_claude_is_not_recorded`: insert `proc(50, 40, "claude")` so the pane shell's parent is another claude, and add `proc(40, 1, "-zsh")`. Nothing is saved.
  - `test_print_mode_background_and_no_tty_are_not_recorded`: three cases, `agent_args="claude -p hi"`, `agent_stat="S"` and `tty="??"`. Nothing is saved in any of them.
  - `test_node_installed_claude_is_recognised`: the agent is `proc(100, 50, "node", "node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js")`. The record is saved.
  - `test_clear_replaces_session_id`: SessionStart `s1`, then SessionStart with `session_id s2` and `source clear`. The record has `s2`.
  - `test_deliberate_exit_deletes_and_other_keeps`: `prompt_input_exit` for `s1` deletes the record. For a fresh record, `other` keeps it, and so do `clear` and a missing reason.
  - `test_session_end_for_another_session_is_ignored`: the record is `s1`; SessionEnd `prompt_input_exit` for `s9` leaves `s1` in place.
  - `test_bad_payloads_never_raise`: `on_hook("SessionStart", {}, ...)`, `on_hook("SessionStart", "x", ...)` and `on_hook("Bogus", START, ...)` raise nothing and save nothing.
  - `test_resume_argv_permission_flag`: the transcript's last mode is `bypassPermissions`. `mirror` gives the flag, `never` doesn't, and `always` always does. When the last mode is `default`, `mirror` gives no flag.
  - `test_session_record_rebuilds_from_transcript`: `session_record("s1")` has the cwd and mode from the transcript. `session_record("nope")` is None.
- [ ] **Step 2:** Run `./run_tests.sh -k Finder -k Claude`. Expect a failure because the modules don't exist yet.
- [ ] **Step 3:** Implement `finder.py` and `agents/claude.py` as specified.
- [ ] **Step 4:** Run `./run_tests.sh`. Everything passes on 3.9 and 3.12.
- [ ] **Step 5:** Write the plugin files and run `claude plugin validate plugin` and `claude plugin validate .`. Both must pass.
- [ ] **Step 6:** Commit to `wave1/claude`.

### Task B (`restore`): iTerm2 adapter, restore logic, shell snippets

**Files (owned):**
- Create: `src/agent_resurrect/terminals/iterm.py`, `src/agent_resurrect/restore.py`
- Create: `shell/agent-resurrect.zsh`, `shell/agent-resurrect.bash`
- Test: `tests/test_iterm.py`, `tests/test_restore.py`, `tests/test_shell_snippets.py`

**Interfaces:**
- Consumes: the foundation API above. `ITermAdapter.settings()` returns `[ITermRestorePolicy(), ITermLaunchAtLogin()]` from `agent_resurrect.settings.iterm` (Task C), imported inside the method so this task's tests never import it.
- Produces:
  ```python
  # terminals/iterm.py
  class ITermAdapter(TerminalAdapter): name = "iterm"; restores_in_place = True
  # restore.py
  @dataclass class Decision: eligible: bool; reason: str
      # reasons: "ok", "not-live", "too-old", "agent-running", "session-gone"
  @dataclass class ResumePlan: argv: List[str]; cwd: Optional[str]; warning: Optional[str]
  def check_eligible(record, agent, procs, config, now=None, require_live=True) -> Decision
  def plan_resume(record, agent, config) -> ResumePlan
  def format_command(plan: ResumePlan) -> str      # "cd '/a b' && claude --resume x ..." (shlex.quote each part)
  def launch(plan: ResumePlan) -> None             # os.chdir(cwd) if cwd; os.execvp(argv[0], argv). Raises OSError if not found.
  def wait_for_key(seconds: int, stdin=None, stdout=None) -> bool   # countdown; True if a key was pressed
  def ask_yes_no(prompt: str, stdin=None, stdout=None) -> bool      # default yes
  def resume_record(record, agent, config, *, stdout=None, launcher=launch) -> int
  def auto(env, *, terminal=None, agent_lookup=None, procs=None, config=None, stdin=None, stdout=None,
           keypress=wait_for_key, ask=ask_yes_no, launcher=launch, isatty=None, now=None) -> int
  ```

**Behaviour:**
- `ITermAdapter.pane_key(env)`: `sid = env.get("ITERM_SESSION_ID", "")`, then `_, sep, guid = sid.partition(":")`. Return `"iterm-" + guid` only if `sep` is non-empty and `guid` matches `^[0-9A-Fa-f]{8}-([0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}$`; otherwise None. Do not change the case.
- `pane_label`: the text before the first `:`, or None. `detect(env)`: `pane_key(env) is not None`.
- `check_eligible`, in this order:
  1. `require_live` and status is not `live` gives `not-live`.
  2. `updated_at` older than `max_age_days` gives `too-old`.
  3. `procs.is_alive(agent_pid, agent_started)` gives `agent-running`.
  4. `not agent.session_exists(record)` gives `session-gone`.
  5. Otherwise `ok`.
- `plan_resume`: the argv comes from `agent.resume_argv`. If `record.cwd` is an existing directory, cwd is set to it; otherwise cwd is None and the warning is `"working directory <cwd> no longer exists; resuming from the current directory"`.
- `resume_record`:
  1. Print `Resurrecting "<title or first prompt or session id>" (<agent>) in <cwd>`.
  2. If the record has a valid pane key and is stored, call `store.set_status(key, "consumed")`.
  3. Call `store.append_restore_event(key, "resumed", session_id)`.
  4. Print the warning, if any.
  5. Call `launcher(plan)`. On `OSError`: print `agent-resurrect: could not start <argv[0]>: <err>`, set the status back to `live`, append event `launch-failed`, and return 1.
- `auto`:
  1. `terminal = terminal or detect_terminal(env)`, then the pane key. If either is None, return 0 silently.
  2. `record = store.load(key)`. If None, return 0.
  3. If stdin or stdout is not a TTY (`isatty` injectable): append event `not-a-tty` and return 0. Never read input.
  4. Run `store.gc(config.max_age_days)`. Look up the agent; if it is unknown, append event `unknown-agent` and return 0.
  5. Call `check_eligible`. If the reason is `session-gone`: print one line, delete the record, append the event and return 0. Any other ineligible reason: append the event and return 0 silently.
  6. Act on `config.auto_resume`:
     - `off`: print `agent-resurrect: resume with: <format_command>`, set status `consumed`, return 0.
     - `ask`: if `ask(...)` is False, set status `skipped`, append event `skipped`, print the hint, return 0.
     - `countdown`: print `Resurrecting "<title>" (<agent>) in <cwd>. Press any key to skip.` If `keypress(config.countdown_seconds)` is True, set status `skipped`, append event `skipped`, print `Skipped. Run "agent-resurrect" in this pane to resume it later.`, and return 0.
     - `immediate`: continue without waiting.
  7. Finally `return resume_record(...)`.
- `wait_for_key`:
  - Put the terminal in cbreak mode with `tty.setcbreak`, and always restore it in a `finally`.
  - Each second, rewrite the countdown with `\r` (`in 3s`, `in 2s`, ...).
  - Use `select` on the stdin fd until the deadline. If a key is read, consume up to 64 bytes and return True. Return False at timeout.
- `launch` must use `os.execvp`, never `subprocess`, so no Python parent remains to receive Ctrl-C.

**Snippets:**

`shell/agent-resurrect.zsh`
```zsh
# agent-resurrect: on the first prompt of an interactive shell in a restored terminal pane,
# resume the agent session that was running there. Spawns nothing when the pane has no record.
[[ -o interactive ]] || return 0
typeset -g _agent_resurrect_done=0
_agent_resurrect_first_prompt() {
  (( _agent_resurrect_done )) && return 0
  _agent_resurrect_done=1
  add-zsh-hook -d precmd _agent_resurrect_first_prompt
  local key=""
  [[ -n "${ITERM_SESSION_ID:-}" ]] && key="iterm-${ITERM_SESSION_ID#*:}"
  [[ -n "$key" && -f "${XDG_STATE_HOME:-$HOME/.local/state}/agent-resurrect/panes/$key.json" ]] || return 0
  "${AGENT_RESURRECT_BIN:-$HOME/.local/bin/agent-resurrect}" auto
}
autoload -Uz add-zsh-hook
add-zsh-hook precmd _agent_resurrect_first_prompt
```
`shell/agent-resurrect.bash`. This must work on macOS's bash 3.2 and pass `shellcheck`.
```bash
# agent-resurrect: on the first prompt of an interactive shell in a restored terminal pane,
# resume the agent session that was running there. Spawns nothing when the pane has no record.
case $- in *i*) ;; *) return 0 ;; esac
_agent_resurrect_done=0
_agent_resurrect_first_prompt() {
  [ "$_agent_resurrect_done" = 1 ] && return 0
  _agent_resurrect_done=1
  PROMPT_COMMAND="${PROMPT_COMMAND#_agent_resurrect_first_prompt; }"
  PROMPT_COMMAND="${PROMPT_COMMAND#_agent_resurrect_first_prompt}"
  local key=""
  [ -n "${ITERM_SESSION_ID:-}" ] && key="iterm-${ITERM_SESSION_ID#*:}"
  [ -n "$key" ] && [ -f "${XDG_STATE_HOME:-$HOME/.local/state}/agent-resurrect/panes/$key.json" ] || return 0
  "${AGENT_RESURRECT_BIN:-$HOME/.local/bin/agent-resurrect}" auto
}
PROMPT_COMMAND="_agent_resurrect_first_prompt${PROMPT_COMMAND:+; $PROMPT_COMMAND}"
```

- [ ] **Step 1: Write failing tests.**
  - `tests/test_iterm.py`:
    - `test_pane_key_and_label`: `ITERM_SESSION_ID=w0t12p0:10FE02F5-C525-405E-9E86-C8255ED17032` gives key `iterm-10FE02F5-C525-405E-9E86-C8255ED17032` and label `w0t12p0`.
    - `test_rejects_malformed`: `""`, `"w0t0p0"`, `"w0t0p0:"`, `"w0t0p0:../../etc"` and `"w0t0p0:not-a-guid"` all give None, and `detect` is False.
    - `test_key_is_a_valid_store_key`.
  - `tests/test_restore.py`, using `FakeAgent`, `FakeProcs`, `FakeTerminal`, `temp_home`, `make_record` and `env={"FAKE_PANE": "p1"}`:
    - `test_eligibility_order_and_reasons`: one case per reason. Alive means `FakeProcs(proc(4242, 1, "claude"))` with matching `started`; a reused pid (`started` differs) counts as not alive and gives `ok`.
    - `test_require_live_false_ignores_status`.
    - `test_plan_uses_cwd_or_warns`: an existing temp dir becomes the cwd; a missing dir gives cwd None plus the warning text.
    - `test_format_command_quotes_spaces_and_quotes`: cwd `/tmp/a b'c` gives `cd '/tmp/a b'"'"'c' && fake-agent --resume s-1`.
    - `test_auto_no_record_is_silent`: returns 0, and nothing is written to stdout.
    - `test_auto_never_waits_without_a_tty`: `isatty=lambda f: False`. `keypress` must not be called (have it raise), the launcher is not called, and the event is `not-a-tty`.
    - `test_auto_countdown_resumes`: `isatty` True and `keypress` returns False. The launcher receives a plan whose argv is `["fake-agent", "--resume", "s-1"]`, the status is `consumed` before the launcher runs (assert inside the launcher), and the event is `resumed`.
    - `test_auto_countdown_skip`: `keypress` returns True. Status `skipped`, the launcher is not called, and the hint is printed.
    - `test_auto_modes`: `immediate` doesn't call keypress. `ask` with False gives `skipped`. `off` prints `resume with:`, gives `consumed`, and doesn't call the launcher.
    - `test_auto_ignores_running_agent_and_consumed_record`: neither case calls the launcher.
    - `test_auto_session_gone_deletes_record`.
    - `test_launch_failure_restores_live`: the launcher raises `FileNotFoundError`. Return 1, status `live`, event `launch-failed`.
    - `test_launch_execs_without_spawning`: patch `os.execvp` and `os.chdir` to record their calls, and patch `subprocess.Popen` to raise. `launch(ResumePlan(["claude", "--resume", "x"], "/tmp", None))` calls chdir("/tmp"), then execvp("claude", [...]).
  - `tests/test_shell_snippets.py`. Each case writes a fake `AGENT_RESURRECT_BIN` script that appends `ran` to a marker file, and uses a temp `XDG_STATE_HOME`. Skip a case when its shell is missing.
    - zsh, record present: `zsh -f -i -c 'source shell/agent-resurrect.zsh; for f in $precmd_functions; do $f; done; for f in $precmd_functions; do $f; done'`. The marker contains exactly one `ran`.
    - zsh, no record: no marker. zsh, `ITERM_SESSION_ID` unset: no marker.
    - bash: the same three cases with `bash --norc --noprofile -i -c 'source shell/agent-resurrect.bash; eval "$PROMPT_COMMAND"; eval "$PROMPT_COMMAND"'`.
    - bash: a pre-existing `PROMPT_COMMAND="echo other"` still runs after the first prompt, and our function is gone from `PROMPT_COMMAND`.
    - Contract: for several `ITERM_SESSION_ID` values, the key the shell computes (echo it from a copy of the key line) equals `ITermAdapter().pane_key()` whenever the latter is not None.
- [ ] **Step 2:** Run `./run_tests.sh -k ITerm -k Restore -k Shell`. Expect failures.
- [ ] **Step 3:** Implement `terminals/iterm.py`, `restore.py` and both snippets.
- [ ] **Step 4:** Run `./run_tests.sh` and pass. Run `zsh -n shell/agent-resurrect.zsh`, and run `shellcheck shell/agent-resurrect.bash` if shellcheck is installed (do not install it).
- [ ] **Step 5:** Commit to `wave1/restore`.

### Task C (`install`): settings, installer, install.sh

**Files (owned):**
- Modify: `src/agent_resurrect/settings/__init__.py` (only `all_settings`)
- Create: `src/agent_resurrect/settings/defaults.py`, `settings/macos.py`, `settings/iterm.py`, `settings/shellrc.py`, `settings/claude_plugin.py`, `src/agent_resurrect/installer.py`, `install.sh`
- Create: `tests/fakes/defaults`, `tests/fakes/launchctl`, `tests/fakes/claude` (executable Python scripts)
- Test: `tests/test_settings.py`, `tests/test_installer.py`, `tests/test_install_sh.py`

**Before writing the iTerm setting, verify (spec open item 1).** Find out from iTerm2 source on GitHub (`gnachman/iTerm2`, look for `OpenArrangementAtStartup`, `OpenNoWindowsAtStartup` and `NoSyncIgnoreSystemWindowRestoration`) which keys make iTerm2 restore windows at launch using the system restoration setting. Encode exactly that, and cite the source file in a code comment.

**Also verify (open item 2).** Run `claude plugin install --help`, `claude plugin marketplace add --help`, `claude plugin list --help` and `claude plugin uninstall --help` to learn the non-interactive flags (`--scope`, `-y`, `--json`). Use only `--help`.

**Interfaces:**
- Consumes: `Setting`, `Check` (`settings/base.py`) and `paths`. `all_terminals()` from `agent_resurrect.terminals` is imported inside `all_settings()`.
- Produces:
  ```python
  # settings/defaults.py  (runs `defaults` from PATH)
  def read(domain: str, key: str) -> Optional[Tuple[str, str]]   # (type, value) e.g. ("boolean", "1"); None if absent
  def write(domain: str, key: str, type_: str, value: str) -> None  # type_: boolean|integer|string
  def delete(domain: str, key: str) -> None
  # settings/macos.py   class KeepWindowsOnQuit(Setting): id = "macos.keep_windows"
  # settings/iterm.py   class ITermRestorePolicy(Setting): id = "iterm.restore_policy"
  #                     class ITermLaunchAtLogin(Setting): id = "iterm.launch_at_login"
  #                         LABEL = "io.github.imgovind.agent-resurrect.iterm"
  # settings/shellrc.py class ShellRcBlock(Setting): __init__(self, shell: str)   # "zsh" | "bash"; id = f"shell.{shell}"
  #                     BEGIN = "# >>> agent-resurrect >>>"; END = "# <<< agent-resurrect <<<"
  # settings/claude_plugin.py class ClaudePlugin(Setting): id = "claude.plugin"
  # settings/__init__.py def all_settings() -> List[Setting]:
  #     [KeepWindowsOnQuit(), *[s for t in all_terminals() for s in t.settings()], ShellRcBlock("zsh"), ShellRcBlock("bash"), ClaudePlugin()]
  # installer.py
  @dataclass class StepResult: id: str; status: str; detail: str = ""   # changed | unchanged | skipped | failed | reverted
  def load_manifest() -> dict      # {"version": 1, "installed_at": iso, "settings": {id: {"status": "changed"|"unchanged", "original": dict|None}}}
  def install(settings: Optional[List[Setting]] = None) -> List[StepResult]
  def uninstall(settings: Optional[List[Setting]] = None, purge: bool = False) -> List[StepResult]
  ```

**Behaviour:**
- **Defaults-backed settings** store `{"keys": {key: {"existed": bool, "type": t, "value": v}}}` as the original. Revert deletes keys that did not exist and rewrites the rest with their original type and value. The global domain is `NSGlobalDomain`.
  - `KeepWindowsOnQuit`: `NSQuitAlwaysKeepsWindows` = boolean true.
- **`ITermLaunchAtLogin`:**
  - Writes `~/Library/LaunchAgents/<LABEL>.plist` with `plistlib`: `ProgramArguments ["/usr/bin/open", "-a", "iTerm"]`, `RunAtLoad true`, `LimitLoadToSessionType "Aqua"`.
  - Then runs `launchctl bootstrap gui/<uid> <plist>`; a non-zero exit because it is already loaded is fine.
  - The original is `{"existed": false}`. Revert runs `launchctl bootout gui/<uid>/<LABEL>` (ignoring failure) and deletes the plist.
  - Check passes if the plist exists with that content.
  - If `/Applications/iTerm.app` is missing (overridable via env `AGENT_RESURRECT_ITERM_APP` for tests): apply returns None and check fails with "iTerm2 is not installed".
- **`ShellRcBlock`:**
  - Target: zsh uses `${ZDOTDIR:-$HOME}/.zshrc`, created if missing. Bash uses `~/.bash_profile` if it exists, else `~/.bashrc` if it exists. If neither exists, apply returns None and check reports `ok=True` with detail "bash not configured".
  - Block content:
    ```
    # >>> agent-resurrect >>>
    # Managed by agent-resurrect. Remove with: agent-resurrect uninstall
    case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
    [ -f "<data_dir>/shell/agent-resurrect.<shell>" ] && . "<data_dir>/shell/agent-resurrect.<shell>"
    # <<< agent-resurrect <<<
    ```
    `<data_dir>` is `paths.data_dir()` with the `$HOME` prefix rewritten as `$HOME`.
  - Edit the file in place through its path. Never `os.replace` it, so a symlink stays a symlink and the target is edited.
  - If the content doesn't end with a newline, add one before the block. If an identical block is present, return None. If a different block is present, replace it in place and return None.
  - The original is `{"existed": bool}`. Revert removes exactly the block lines and the single newline the installer added. If the file didn't exist before and is now empty, delete it.
- **`ClaudePlugin`:**
  - If `claude` isn't on PATH: apply returns None and check fails with fix "install Claude Code, then run: agent-resurrect install".
  - Apply runs `claude plugin marketplace add <data_dir>`, then `claude plugin install agent-resurrect@agent-resurrect` with the non-interactive flags found above.
  - If the plugin is already installed: run `claude plugin marketplace update agent-resurrect` and `claude plugin update agent-resurrect@agent-resurrect`, then return None.
  - The original is `{"installed": false}`. Revert runs uninstall, then `marketplace remove`.
  - Check uses `claude plugin list` output (JSON if supported) and looks for `agent-resurrect`.
- **`installer.install`:**
  - Apply each setting in order. An exception becomes status `failed`, gets logged, and installation continues.
  - Manifest merge rule: a setting already recorded as `changed` keeps its first `original` forever.
  - Write the manifest atomically (temp file plus `os.replace`).
- **`installer.uninstall`:**
  - Revert settings with status `changed` in reverse order, then remove the manifest entries.
  - Remove `paths.data_dir()`, `bin_dir()/agent-resurrect`, and `bin_dir()/resurrect` only if it's a symlink to `agent-resurrect`.
  - With `purge`, also remove `state_dir()` and `config_dir()`. Remove the manifest last.
- **Fakes:**
  - `tests/fakes/defaults` keeps a JSON store at `$FAKE_DEFAULTS_DB` and supports `read <domain> <key>`, `read-type <domain> <key>` (output `Type is boolean`), `write <domain> <key> -bool|-int|-string <v>` and `delete <domain> <key>`.
  - `tests/fakes/launchctl` and `tests/fakes/claude` append each argv as a JSON line to `$FAKE_CALLS_LOG`. Fake `claude plugin list` prints the installed state, which it keeps in `$FAKE_CLAUDE_DB`.

**`install.sh` (bash 3.2 compatible, `shellcheck` clean, `set -euo pipefail`, all logic in functions with a final `main "$@"` so a truncated download runs nothing):**
1. `preflight`: macOS only (`uname` is `Darwin`), otherwise exit 1 with a message. Not root.
2. `find_python`: use `/usr/bin/python3` if `xcode-select -p` succeeds and it is 3.9 or newer; else `python3` from PATH if 3.9 or newer. Otherwise exit 1 with "Python 3.9+ is required. Install Apple's command line tools with: xcode-select --install".
3. `fetch`: if the script's own directory contains `src/agent_resurrect/__init__.py`, copy from it. Otherwise download `https://codeload.github.com/imgovind/agent-resurrect/tar.gz/${AGENT_RESURRECT_REF:-v0.1.0}` with `curl -fsSL` into `mktemp -d`, extract, and copy from there. Copy `src bin shell plugin .claude-plugin install.sh README.md LICENSE` (whichever exist) into `$DATA.new`, then swap it in with `rm -rf "$DATA"; mv "$DATA.new" "$DATA"`. `DATA="${XDG_DATA_HOME:-$HOME/.local/share}/agent-resurrect"`.
4. `write_launchers`: write `$HOME/.local/bin/agent-resurrect` (mode 755):
   ```sh
   #!/bin/sh
   exec "<python>" -I "<DATA>/bin/agent-resurrect.py" "$@"
   ```
   Create the symlink `resurrect -> agent-resurrect` unless a non-symlink `resurrect` already exists, in which case print a note.
5. Run `"$HOME/.local/bin/agent-resurrect" install --yes`, then `"$HOME/.local/bin/agent-resurrect" doctor || true`.
6. Print `Installed. Open a new terminal tab to activate. Uninstall any time with: agent-resurrect uninstall`.

- [ ] **Step 1: Write failing tests.**
  - `tests/test_settings.py`, with the fakes on `PATH` and a temp HOME:
    - `test_keep_windows_apply_check_revert_absent_key`: the key starts absent. Apply returns an original with `existed` False, check passes, and revert deletes the key.
    - `test_keep_windows_already_true_returns_none`.
    - `test_keep_windows_revert_restores_false`.
    - `test_iterm_restore_policy` with the same three cases, for the keys you verified.
    - `test_launch_agent_plist_and_bootstrap`: the plist contents are correct, the calls log contains `bootstrap`, and revert issues `bootout` and deletes the plist.
    - `test_launch_agent_skipped_without_iterm`.
    - `test_rc_block_added_once_and_removed_exactly`: the original rc content is `export A=1` with no trailing newline. After apply, apply, revert, the bytes equal the original.
    - `test_rc_symlink_and_bytes_preserved`: `~/.zshrc` is a symlink to `~/dotfiles/zshrc`. After apply it is still a symlink and the target contains the block. After revert the target's bytes equal the original.
    - `test_rc_created_then_removed_when_absent`.
    - `test_bash_prefers_bash_profile_and_skips_when_unconfigured`.
    - `test_claude_plugin_install_update_uninstall`: check the calls log sequence for a fresh install, then an update when already installed, then revert.
    - `test_claude_plugin_without_claude_is_skipped`.
  - `tests/test_installer.py`, with in-memory fake `Setting` subclasses defined in the test:
    - `test_install_records_changed_and_unchanged`.
    - `test_failure_is_reported_and_install_continues`.
    - `test_reinstall_keeps_first_originals`: the first apply returns `{"v": "orig"}` and the second returns None. The manifest still holds `{"v": "orig"}`, and uninstall passes it to `revert`.
    - `test_uninstall_reverses_order_and_removes_files`: create `data_dir`, the launcher and the `resurrect` symlink. All are gone afterwards; `state_dir` survives without `purge` and is removed with `purge`.
  - `tests/test_install_sh.py`:
    - `test_install_sh_from_clone`: skipped unless `bin/agent-resurrect.py` exists, since it is provided by Task E and the integrator un-skips it. Run `bash install.sh` with a temp HOME, the fakes on PATH and `AGENT_RESURRECT_ITERM_APP` pointing at a temp dir. The launcher exists and is executable, `DATA/src/agent_resurrect` exists, and the manifest exists.
    - `test_install_sh_rejects_non_macos`: run with a fake `uname` that prints Linux. The exit is 1.
    - `test_install_sh_shellcheck`: skipped if shellcheck is missing.
- [ ] **Step 2:** Run `./run_tests.sh -k Setting -k Installer -k InstallSh`. Expect failures.
- [ ] **Step 3:** Implement as specified.
- [ ] **Step 4:** Run `./run_tests.sh` and pass on both interpreters.
- [ ] **Step 5:** Commit to `wave1/install`. Report the verified iTerm keys and plugin flags.

### Task D (`docs`): README, LICENSE, CHANGELOG, install prompt

**Files (owned):** Create `README.md`, `LICENSE` (MIT, `Copyright (c) 2026 Govindarajan Panneerselvam`), `CHANGELOG.md`, `docs/install-with-claude.md`.

**README sections, in order:**
1. Title `agent-resurrect`, then the one-line description from spec section 13.
2. **The problem.** Restart, panes come back, agents gone.
3. **Install.** The one-liner, plus "Install with Claude Code", which is the exact prompt from spec 9.1 in a fenced block.
4. **What happens after a restart.** The countdown text exactly as Task B prints it.
5. **Commands.** A table of `agent-resurrect`, `ls`, `resume <n|session-id>`, `find [text] [--clipboard]`, `doctor`, `config [key [value]]` and `uninstall [--purge]`, with one line each, matching Task E.
6. **Configuration.** The config file path, and the four keys with their values and defaults from spec section 8.
7. **What the installer changes.** The table from spec 9.3, in plain words, plus "Uninstall restores every original value."
8. **How it works.** A short paragraph plus the record and restore diagram.
9. **Permission mode.** Explain `mirror`, `always` and `never`.
10. **FAQ.**
    - "Does it work after a crash?" Yes, when iTerm2 restores the panes.
    - "What if I exited on purpose?" It is never restored.
    - "Sessions from before I installed?" Use `find`.
    - "Privacy?" Everything stays local; nothing is sent anywhere.
11. **Roadmap.** Codex, Cursor agent, opencode, Qoder; Warp, Ghostty, WezTerm, Kitty, tmux.
12. **Similar projects.** A respectful comparison crediting cc-session-restore, claude-iterm2-resume-sessions, cc-sessions, PonyMux and Agent-Resume.
13. **Development.** `./run_tests.sh`, then installing from a clone with `./install.sh`.
14. **License.**

Use headings that match search queries, for example "Resume Claude Code after a restart" as an H2 under "The problem". No em dashes and no ALL-CAPS headings. Every command must be copy-paste ready.

- [ ] **Step 1:** Write the files.
- [ ] **Step 2:** Check that every command in the README appears in the Global Constraints command list, and check for em dashes with `grep -c '—' README.md docs/install-with-claude.md CHANGELOG.md`, which must print 0 for each.
- [ ] **Step 3:** Commit to `wave1/docs`.

### Task E (`cli`): CLI, doctor, entry point

**Files (owned):** Create `bin/agent-resurrect.py`, `src/agent_resurrect/cli.py`, `src/agent_resurrect/doctor.py`. Test: `tests/test_cli.py`, `tests/test_doctor.py`.

**Interfaces:**
- Consumes:
  - The foundation API.
  - `restore.check_eligible`, `restore.plan_resume`, `restore.format_command`, `restore.resume_record` and `restore.auto` (Task B).
  - `installer.install`, `installer.uninstall` and `StepResult` (Task C).
  - `settings.all_settings` (Task C).
  - `agents.all_agents`/`get_agent` and `terminals.detect_terminal`.
  - `AgentAdapter.find_by_text`/`session_record`/`describe`.

  Tasks B and C are being written in parallel, so import them at module level by name. Tests patch `agent_resurrect.cli.<name>` with fakes; create the minimal stub modules **only inside your test setup** via `sys.modules` if a real module is absent. Do not create files owned by other tasks.
- Produces: `cli.main(argv: Optional[List[str]] = None) -> int` and `doctor.run(out=None) -> int`.

**`bin/agent-resurrect.py`:**
```python
#!/usr/bin/env python3
"""Entry point. The launcher in ~/.local/bin runs this file with `python3 -I`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

if __name__ == "__main__":  # required: text search uses multiprocessing (spawn re-imports this file)
    from agent_resurrect.cli import main
    sys.exit(main())
```

**Commands (argparse subparsers; no subcommand means `here`; `--version` prints `agent-resurrect 0.1.0`):**
- `here`:
  - Detect the terminal and pane key. If there's no record: print `Nothing recorded for this pane.`, then the `ls` output, and return 0.
  - Otherwise run `check_eligible(require_live=False)`:
    - `agent-running`: print `That session is still running (pid N).` and return 1.
    - `session-gone`: print the message and return 1.
    - Anything else: `return resume_record(...)`.
- `ls [--all]`:
  - Run `store.gc`.
  - List records whose agent is not running; `--all` includes running ones.
  - Print one numbered row per record, newest first: `n  agent  "title"  cwd  last-active(local time)  pane-label  status`.
  - If there are none, print `No sessions to resurrect.`
- `resume <n|session-id>`:
  - A number indexes the default `ls` ordering.
  - Otherwise look for a stored record with that `session_id`, else the first non-None `agent.session_record(id)`.
  - Unknown ids print an error and return 1. A running agent prints the same message as `here`.
  - Otherwise `return resume_record(...)`.
- `find [TEXT ...] [--clipboard] [--limit N]`:
  - The text is the joined args. With `--clipboard`, use `pbpaste` output. With no args and stdin not a TTY, read stdin.
  - Empty text is an error and returns 2.
  - Gather `find_by_text` results from all agents, best first.
  - Print a `BEST MATCH` block with session, title, opening prompt, cwd and "text appears in" roles, followed by `Resume command:` and `format_command(plan_resume(record, agent, cfg))`. Then `Other candidates:` as numbered lines.
  - Print `No session contains that text.` with the hint from the old script when there are none.
  - Never launches anything.
- `auto`: `return restore.auto(os.environ)`.
- `hook <agent> <event>`:
  - Read stdin, capped at 1 MB, and parse it as JSON. Detect the terminal and pane key.
  - Build `HookContext(env, terminal, key, label, Procs(), os.getpid())` and call `agent.on_hook`.
  - Any exception or missing piece is logged. **Always return 0 and print nothing.**
- `doctor`: `return doctor.run()`.
- `install [--yes]`:
  - Without `--yes` on a TTY: list each `setting.description` and ask `Proceed? [Y/n]`.
  - Run `installer.install()` and print `changed/unchanged/skipped/failed  <id>  <detail>`.
  - Return 1 if any step failed.
- `uninstall [--purge] [--yes]`: confirm unless `--yes`, run `installer.uninstall(purge=...)`, and print the results.
- `config [KEY [VALUE]]`:
  - No args: print all four effective values.
  - A key: print its value.
  - A key and value: call `config.set_value`. Validation errors print and return 2.

**`doctor.run`:** print one line per check, as `OK    <text>`, `WARN  <text>` or `FAIL  <text>`. A FAIL is followed by an indented `fix: <hint>`. Return 1 if any FAIL, else 0.
1. Python version is 3.9 or newer.
2. `shutil.which("agent-resurrect")` is found; WARN with the PATH hint if not.
3. For each `all_settings()`: check, then OK or FAIL with `fix_hint`.
4. This pane: OK `iterm pane w0t12p0` when a terminal is detected, else WARN `not running inside a supported terminal (iTerm2)`.
5. Records: `<n> recorded sessions (<live> live, <restorable> restorable)`.
6. Since the last boot:
   - Read boot time from `sysctl -n kern.boottime` with the regex `\{ sec = (\d+),` (not a bare `sec =`, which also matches `usec`).
   - Count restore events after boot by result.
   - Orphans are live records whose agent isn't running and whose `updated_at` is before boot. If there are any: WARN `<k> sessions died in panes that did not come back; run: agent-resurrect ls`.

- [ ] **Step 1: Write failing tests.**
  - `tests/test_cli.py`, patching `cli.detect_terminal` to return `FakeTerminal()`, `cli.get_agent` and `cli.all_agents` to use `FakeAgent`, and `restore.launch` to record its calls. Use `temp_home`, and capture stdout with `contextlib.redirect_stdout`.
    - `test_version`.
    - `test_hook_always_exits_zero_and_prints_nothing`: garbage stdin, an empty payload, an unknown agent, and no terminal detected. All return 0 with empty stdout and stderr.
    - `test_hook_passes_context_to_agent`: FakeAgent's `hook_calls` gets `("SessionStart", payload, ctx)` with `ctx.pane_key == "fake-p1"` and `ctx.pid == os.getpid()`.
    - `test_here_without_record_lists`.
    - `test_here_resumes`: the launcher is called with the FakeAgent argv.
    - `test_ls_orders_newest_first_and_hides_running`.
    - `test_resume_by_number_and_by_session_id`.
    - `test_find_reads_stdin_and_prints_command`: FakeAgent's `find_by_text` returns one Candidate. The output contains `BEST MATCH` and `Resume command:`, and the launcher is not called.
    - `test_find_empty_text_returns_2`.
    - `test_config_get_set_and_invalid`.
    - `test_install_and_uninstall_delegate`: patch `cli.installer` with a stub that records calls; `--yes` doesn't prompt.
  - `tests/test_doctor.py`:
    - `test_all_ok_returns_0`: one passing fake setting, plus FakeTerminal detected.
    - `test_failing_setting_returns_1_and_prints_fix`.
    - `test_boot_time_parsing_uses_sec_not_usec`: feed `{ sec = 1791785041, usec = 851234 } Mon Oct 12 11:34:01 2026` and get 1791785041.
    - `test_orphans_warn`: a live record updated before the boot time, with its agent not running.
- [ ] **Step 2:** Run `./run_tests.sh -k Cli -k Doctor`. Expect failures.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `./run_tests.sh` and pass.
- [ ] **Step 5:** Commit to `wave1/cli`.

---

## Wave 1 integration (single integrator, worktree `agent-resurrect-wt/integration`, branch `wave1/integration` from `main`)

- [ ] **Step 1:** Merge `wave1/claude`, `wave1/restore`, `wave1/install`, `wave1/docs` and `wave1/cli` with `--no-ff`. Ownership is disjoint, so a conflict means an ownership violation: report it and do not resolve it silently.
- [ ] **Step 2:** Wire the registries:
  ```python
  # src/agent_resurrect/agents/__init__.py
  def all_agents() -> List[AgentAdapter]:
      from .claude import ClaudeAdapter
      return [ClaudeAdapter()]
  # src/agent_resurrect/terminals/__init__.py
  def all_terminals() -> List[TerminalAdapter]:
      from .iterm import ITermAdapter
      return [ITermAdapter()]
  ```
- [ ] **Step 3:** Apply any foundation changes requested in the wave reports, minimally and with tests.
- [ ] **Step 4:** Run `./run_tests.sh` on both interpreters. `test_install_sh_from_clone` must now run, not skip. Fix integration glue only, and list every fix.
- [ ] **Step 5:** Run `claude plugin validate plugin`, `claude plugin validate .`, `zsh -n shell/agent-resurrect.zsh`, and `shellcheck install.sh shell/agent-resurrect.bash` if shellcheck is installed.
- [ ] **Step 6:** Smoke test in a temp HOME with the fakes on PATH:
  - run `bash install.sh`, then `$HOME/.local/bin/agent-resurrect doctor`;
  - pipe a SessionStart payload into `hook claude SessionStart` while setting `ITERM_SESSION_ID`; no record is expected, because the fake process chain has no claude;
  - run `agent-resurrect ls` and `agent-resurrect uninstall --yes`.
  Then assert that the temp HOME is back to its original files.
- [ ] **Step 7:** Merge `wave1/integration` into `main` (fast-forward if possible) and report.

## Lead verification (after integration, on the real machine; lead only)

- [ ] Install from the clone into the real HOME: `./install.sh`. Read the change summary, then run `agent-resurrect doctor`.
- [ ] In a new iTerm2 pane, run `claude`. The pane's record appears in `agent-resurrect ls --all` with the right session, cwd and `permission_mode`.
- [ ] Kill that claude with SIGTERM. The record is kept and `ls` shows it as restorable.
- [ ] Run `ITERM_SESSION_ID=<that pane's id> zsh -i`. The countdown appears and the same session resumes with the right flag. Ctrl-C inside Claude doesn't return to the shell.
- [ ] Run `/exit`. The record is deleted.
- [ ] Remove the superseded personal skill `~/.claude/skills/resume-by-text` once the plugin skill is confirmed working.
