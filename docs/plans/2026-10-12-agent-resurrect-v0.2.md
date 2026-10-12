# agent-resurrect v0.2.0 Implementation Plan

> **For agentic workers:** wave + integrator. Each task runs in its own git worktree on branch `v0.2/<task>` with the disjoint file ownership listed. Read the v1 plan's Global Constraints too (`docs/plans/2026-10-12-agent-resurrect-v1.md`): they still apply.

**Goal:** Fix every confirmed robustness bug from the 0.1.0 review, make the tool fully usable by AI agents (`--json`, exit codes, no launching without a terminal, `simulate`, `adopt`), make the Claude Code plugin a complete install path, and bring docs and repo hygiene to a high standard.

**Sources (read the parts named in your task):** review reports kept outside the repository (not published):
- `01-code-robustness.md`: C1, H1-H3, M1-M8, Low.
- `02-developer-experience.md`: README, docs/code mismatches, OSS files, release workflow.
- `03-agent-usability.md`: P0/P1/P2, the setup skill, and the machine interface spec. The "Machine interface spec (proposed)" section is **normative** for this release.

## Global constraints added in 0.2.0

- **Exit codes:** use `agent_resurrect.exitcodes` (OK 0, ERROR 1, USAGE 2, NOT_FOUND 3, NEEDS_TTY 4, BUSY 5, GONE 6, INTERRUPTED 130). Never use bare integers.
- **`--json` output:** build every document with `agent_resurrect.jsonout.document(command, code, message, **fields)` and print it with `jsonout.emit`. Exactly one object goes to stdout; diagnostics go to stderr.
- **Never launch an agent without a terminal** (stdin and stdout both TTYs). Print the command and return `NEEDS_TTY` instead.
- **tmux and other multiplexers:** a pane is recorded and restored only when `TERM_PROGRAM == "iTerm.app"` and none of `TMUX`, `STY`, `ZELLIJ` is set. This applies in Python and in both shell snippets.
- **Version:** stays `0.1.0` in your branch. The integrator bumps it to `0.2.0` everywhere.
- **Safety:** never modify the real system. Tests use `tests.helpers.temp_home()` and `tests/fakes`, never a real `claude` launch. tmux experiments use a fake `claude` on PATH and `env -i`. A real claude once started in a sandbox during review: do not repeat that.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Wave 0 (done on main by the lead)

```python
exitcodes: OK ERROR USAGE NOT_FOUND NEEDS_TTY BUSY GONE INTERRUPTED; NAMES; name(code)
jsonout: document(command, code=OK, message=None, **fields) -> dict; emit(doc, out=None); iso_from_epoch(seconds)
store.claim(pane_key) -> Optional[Record]    # atomic live->consumed; exactly one racer wins; None otherwise
store.gc(max_age_days, now=None, is_running=None)   # is_running(record)->bool keeps running sessions
store: state and panes dirs are 0700
procs: ps always runs with LC_ALL=C (H1 fixed)
model.Record.resume_env: Optional[Dict[str, str]]   # e.g. {"CLAUDE_CONFIG_DIR": "..."}; additive, schema stays 1
AgentAdapter.adopt(ctx: HookContext) -> Optional[Record]   # default None
Setting.applicable() -> bool                               # default True
```

## Wave 1 tasks (parallel; worktrees `~/Developer/imgovind/agent-resurrect-v02/<task>`)

### T1 `restore`: restore logic, iTerm2 adapter, shell snippets

**Owns:** `src/agent_resurrect/restore.py`, `src/agent_resurrect/terminals/iterm.py`, `shell/agent-resurrect.zsh`, `shell/agent-resurrect.bash`, `tests/test_restore.py`, `tests/test_iterm.py`, `tests/test_shell_snippets.py`.

- **C1:** apply the multiplexer rule in `ITermAdapter.detect` and `pane_key`, and in both snippets. Update the contract test, and add tmux, screen, zellij and vscode cases.
- **H2:** `auto` claims with `store.claim(key)` after the countdown or ask, not `set_status`. When the claim returns None, another shell won: return OK silently and log the event `lost-race`.
- **A1:** add an `isatty=None` keyword to `resume_record`. Without a terminal it prints the resume command (sanitised, with `format_command`) and returns `NEEDS_TTY`, changing no state and appending no event. When it does launch, it claims the record if the record is stored and live. Records that aren't stored or aren't live are launched without a claim.
- **M2:** add `env: Dict[str, str]` (default empty) to `ResumePlan`. `plan_resume` copies `record.resume_env`, and `launch` uses `os.execvpe` with `os.environ` updated by `plan.env`. `format_command` prefixes `VAR=value` for the plan env.
- **M4:** on a failed launch, set the status to `skipped` (not `live`) so new shells do not retry forever. Print a hint naming the executable and suggesting `agent-resurrect resume <id>` once it is on PATH, and log the event `launch-failed`.
- **New:** `simulate(record, agent, procs, config, assume_restart=False, now=None) -> Decision`, the same rules as `check_eligible`. With `assume_restart`, the agent-running step is skipped, because a restart kills every agent.
- **Keep** all existing behaviour and tests passing: exec-based launch, sanitising, and announce-once.

### T2 `claude`: Claude Code adapter and finder

**Owns:** `src/agent_resurrect/agents/claude.py`, `src/agent_resurrect/finder.py`, `tests/test_claude_adapter.py`, `tests/test_finder.py`.

- **M2:** on SessionStart, set `resume_env={"CLAUDE_CONFIG_DIR": value}` when `ctx.env` has it. `session_exists`, `describe`, `session_record` and `find_by_text` look in the record's or environment's config dir: `paths.claude_config_dir()` honours the env var.
- **M8, the SessionEnd fast path:** first load the record. Return immediately unless the reason is deliberate, the record exists, and the session ids match. Only then walk `ps` for the nested check.
- **`adopt(ctx)`:**
  - The session id comes from `ctx.env["CLAUDE_CODE_SESSION_ID"]`.
  - The agent is the first claude ancestor of `ctx.pid`, and the same nested and interactivity rules as SessionStart apply.
  - The transcript comes from `finder.find_transcript`. The cwd is the transcript's trusted cwd, or else `ctx.env["PWD"]`. The permission mode comes from the transcript.
  - Set `source="adopt"` and `resume_env` as in M2.
  - Save the record and return it, or return None (with a log line) when any piece is missing.
- **M4:** `resume_argv[0]` is `"claude"` when `shutil.which("claude")` finds it, else `~/.claude/local/claude` if it is executable, else `"claude"`.
- **Low:**
  - `describe` must not read whole transcripts (about 0.5 s at 440 MB). Read the head (64 KB) for the first prompt and start time, and the tail (256 KB) for the last ai-title.
  - Make the agent-team teammate exclusion explicit. Investigate read-only which environment marks a teammate process, and document it in a comment.
- **Never** take the permission mode from command-line text. The regression test exists; keep it.

### T3 `cli`: commands, flags, JSON documents

**Owns:** `src/agent_resurrect/cli.py`, a new `src/agent_resurrect/views.py` (`Session` and `Pane` JSON objects), `tests/test_cli.py`, a new `tests/test_json_contract.py`, and new `docs/json/*.schema.json` files.

- Implement the "Machine interface spec (proposed)" in report 03 exactly, covering:
  - the general rules, the exit code table, and the new and changed flags;
  - `ls --ids`;
  - `find` text candidates without numbers;
  - `resume` and `here` with `--print`;
  - `simulate`, `adopt` and `version`;
  - `install` and `uninstall` with `--dry-run`, `--json` and `--yes`; `--no-plugin` filters out the setting `claude.plugin`;
  - `config --json`.
- `doctor` delegates to `doctor.run(as_json=..., strict=...)` (T4).
- **Too-short find text** (a ValueError from the finder) returns USAGE. When nothing matches, return NOT_FOUND.
- **Calls into other tasks:**
  - `restore.check_eligible`, `restore.simulate`, `restore.resume_record(..., isatty=...)`, `restore.plan_resume`, `restore.format_command` and `restore.auto` (T1);
  - `installer.install(settings=None, dry_run=False)` and `installer.uninstall(settings=None, purge=False, dry_run=False)` (T6);
  - `AgentAdapter.adopt(HookContext)` (T2).

  Where a parallel module is absent in your worktree, inject stand-ins **in tests only**, conditionally, as in v0.1.
- **Entry point:** T5 moves `bin/agent-resurrect.py` to `libexec/agent-resurrect.py`. Entry-point tests must find whichever of the two exists.
- **Contract tests:** every `--json` path parses as exactly one ASCII object, `error.code` equals `exitcodes.name(exit code)`, and the document validates against `docs/json/` (implement a tiny stdlib validator for required keys and types; no jsonschema dependency).
- **TTY handling:** `resume`/`here` with stdin from `/dev/null` exit 4 and leave the record file byte-identical.

### T4 `doctor`

**Owns:** `src/agent_resurrect/doctor.py` and `tests/test_doctor.py`.

- **Interface:** `run(out=None, *, procs=None, boot_time=None, as_json=False, strict=False) -> int`.
- **Schema:** implement `doctor --json` per report 03, with stable check ids, `fix.kind` (`command`, `new-shell`, `restart-agent`, `user-action`, `install-app`), `fix.agent_may_run` and `argv` (full launcher path), the `ready` verdict, and `records` and `since_boot` counts. `--strict` makes a WARN exit ERROR.
- **M6 checks:**
  - A login shell (`$SHELL`) other than zsh or bash is a FAIL with kind `user-action`.
  - Running inside tmux, screen or zellij (current env) is a WARN: such panes are not recorded.
  - An un-exported `ZDOTDIR` is a WARN when the rc block's file and zsh's file differ.
  - A `.zshrc` that runs `exec tmux` (or `tmux attach`/`new`) before the agent-resurrect block is a WARN.
- **Sanitising:** keep sanitising with `display.printable`.

### T5 `plugin`: self-contained plugin, launcher layout, install.sh, release workflow

**Owns:**
- **Moved:** `plugin/**` to the repo root as `.claude-plugin/plugin.json`, `hooks/hooks.json` and `skills/**` (delete `plugin/`). Also moved: `bin/agent-resurrect.py` to `libexec/agent-resurrect.py`.
- **New:** `bin/agent-resurrect` (a POSIX sh wrapper), `skills/setup/SKILL.md` and `.github/workflows/release.yml`.
- **Modified:** `.claude-plugin/marketplace.json`, `install.sh`, `tests/test_install_sh.py`, and a new `tests/test_plugin_layout.py`.

**Work:**
- **Layout:** follow report 03 "A setup skill" and P0-3.
  - The marketplace `source` is `"./"`.
  - Hooks run `"${CLAUDE_PLUGIN_ROOT}/bin/agent-resurrect" hook claude <Event>`, always exit 0 and print nothing.
  - `bin/agent-resurrect` locates a Python 3.9+ (`/usr/bin/python3` only when `xcode-select -p` succeeds, else `python3`) and execs `-I -X pycache_prefix=$HOME/.cache/agent-resurrect/pycache <its dir>/../libexec/agent-resurrect.py "$@"`.
  - `libexec/agent-resurrect.py` keeps the `__main__` guard and finds `src/` relative to itself.
- **Skills:** move `resume-by-text` to `skills/resume-session/` and broaden its triggers (the report 03 draft, adapted to real `find` output). Add `skills/setup/SKILL.md` with `disable-model-invocation: true`. It installs from `${CLAUDE_PLUGIN_ROOT}/install.sh` with no download, shows `install --dry-run --json` first, then runs doctor and `adopt`.
- **install.sh:**
  - Copy the new layout: `src libexec bin shell hooks skills .claude-plugin install.sh README.md LICENSE`.
  - The launcher in `~/.local/bin` execs `libexec/agent-resurrect.py`.
  - Add `--dry-run` (stage to a temp dir, run `install --dry-run`, write nothing under HOME).
  - **M7:** download the release tarball by an immutable commit when `AGENT_RESURRECT_COMMIT` is set in the script. `release.yml` publishes an `install.sh` release asset with that variable filled in, plus `SHA256SUMS`, and the README one-liner will use `releases/latest/download/install.sh` (T7 documents it).
- **release.yml:** on a `v*` tag push, check that the versions in `__init__.py`, `plugin.json`, `marketplace.json` and `install.sh` match the tag. Then take the notes from the CHANGELOG section and create the release with the pinned `install.sh` and `SHA256SUMS`.
- **Validate:** `claude plugin validate . --strict` must pass, and `test_plugin_layout.py` asserts the structure and hook commands.

### T6 `install`: settings and installer robustness

**Owns:** `src/agent_resurrect/settings/*.py` (all of them), `src/agent_resurrect/installer.py`, `tests/test_settings.py`, `tests/test_installer.py`, `tests/test_registries.py`, `tests/fakes/*`.

- **H3:** an unreadable, corrupt or newer-version manifest makes `uninstall` refuse. It returns a `failed` StepResult, reverts nothing and deletes nothing; the CLI then exits ERROR.
- **M1:** when any revert fails, keep the launcher, data dir and manifest entries for the failed settings, so a retry works.
- **M5:**
  - Replace `macos.keep_windows` with `iterm.keep_windows`, which writes `NSQuitAlwaysKeepsWindows` in the `com.googlecode.iterm2` domain only. Cite the iTerm2 source or AppKit behaviour.
  - **Legacy migration:** if the manifest has a `macos.keep_windows` entry with status `changed`, install reverts it (restoring the user's global value) and drops the entry, and uninstall does the same.
- **Dry run:** `install(settings=None, dry_run=False)` and `uninstall(settings=None, purge=False, dry_run=False)`. Dry-run statuses are `would-change`, `unchanged`, `skipped` and `would-revert`; nothing is written. Implement `applicable()` per setting: iTerm2 settings need iTerm2 installed, `claude.plugin` needs `claude` on PATH, and the bash rc needs a file to exist.
- **M6 (install side):** don't create `~/.zshrc` when the login shell isn't zsh and the file is absent; skip it instead.
- **Low:** the rc block's PATH line appends `~/.local/bin` instead of prepending it. Upgrades replace the old block in place (this already works; keep it).
- **Plugin setting:** keep working with the repo-root plugin (the marketplace dir is still `paths.data_dir()`). Never add a second marketplace with the same name. If `agent-resurrect@agent-resurrect` is already installed from GitHub, leave it alone and return None.
- **Registries test:** update `test_registries.py` for the new setting ids and order: `iterm.keep_windows`, `iterm.restore_policy`, `iterm.launch_at_login`, `shell.zsh`, `shell.bash`, `claude.plugin`.

### T7 `docs`: README, OSS files, agent docs

**Owns:** `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, `AGENTS.md`, `llms.txt`, `Makefile`, `.github/ISSUE_TEMPLATE/*`, `.github/PULL_REQUEST_TEMPLATE.md`, `docs/install-with-claude.md`, `docs/specs/2026-10-12-agent-resurrect-design.md` (add a "Changes in 0.2.0" section and mark superseded statements), `tests/test_docs.py`, and a new `tests/test_version.py`.

- **README:** rewrite per report 02 (top 10 plus the mismatch list) and report 03 (the "For AI coding agents" section and FAQ). It needs:
  - a Claude-Code-first tagline and a "What it supports today" table;
  - a placeholder slot for the demo GIF: `docs/demo.gif`, which the lead records;
  - sections for install (one-liner from `releases/latest/download/install.sh`, plugin route `/plugin marketplace add imgovind/agent-resurrect` then `/agent-resurrect:setup`), sessions already running (`adopt`), try it without restarting (`simulate --assume-restart`, and the kill-and-new-shell test), troubleshooting (each doctor check, restore event names, log path), upgrade, uninstall, an exit code table and a JSON pointer, why `claude --continue` is not enough, and similar projects (add claude-hibernate, Quil and claude-sessions per report 04).
- **Every command and flag** must match T3's spec. Write against the spec; the integrator reconciles.
- **docs/install-with-claude.md:** a new safe prompt (report 03 P1-1): one download to a temp file, read it, run that same file, consent pause, `doctor --json`, and act only on `agent_may_run` fixes.
- **Repo files:** CONTRIBUTING, SECURITY (private vulnerability reporting), and issue and PR templates (report 02 drafts). Add AGENTS.md and llms.txt (report 03 drafts, updated to the 0.2.0 commands) and a Makefile with `test`, `lint` and `install-dev`.
- **Tests:** `test_version.py` asserts the same version in `__init__.py`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, the `install.sh` default REF, and the CHANGELOG top entry. It is expected to fail until the integrator bumps the version, so mark the expectation in a comment. Keep `test_docs.py` pinning the prompt (now in README and the guide) and the em-dash rule.
- **CHANGELOG:** a 0.2.0 entry listing everything this plan ships.
- **Style:** no em dashes and no ALL-CAPS headings. Claim only what ships: macOS, iTerm2 and Claude Code.

## Integration (single integrator; branch `v0.2/integration` from main)

1. Merge T1 to T7 with `--no-ff`. A conflict means an ownership violation: report it.
2. Bump the version to `0.2.0` everywhere: `__init__.py`, `plugin.json`, `marketplace.json`, the `install.sh` REF, and the CHANGELOG date.
3. **Glue:**
   - Make the entry-point paths, `test_docs` (README shell block equal to `ShellRcBlock` output, prompt identity) and `test_version` agree.
   - Check that the README command table matches `agent-resurrect --help` output.
4. **Verify:**
   - The full suite passes on 3.9 and 3.12 with nothing skipped. Shellcheck and `zsh -n` pass, and so does `claude plugin validate . --strict`.
   - Temp-HOME smoke: install, `doctor --json`, `simulate --assume-restart --json`, `ls --json`, and `uninstall --yes`, followed by an empty diff.
   - The `install.sh --dry-run` writes nothing.
   - A tmux sandbox (fake claude, `env -i`) records nothing inside tmux.
5. Fast-forward main and report. Do not push.

## Lead verification (after integration)

1. Upgrade the real install from 0.1.0 to 0.2.0 via `./install.sh` and check that the legacy `macos.keep_windows` entry migrates cleanly.
2. Rerun the live end-to-end driver, adding `adopt` and `simulate`.
3. Record `docs/demo.gif` with vhs.
4. Ask the user before pushing, tagging, releasing, or changing repo settings.
