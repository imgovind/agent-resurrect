# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, Cursor, Gemini CLI, opencode) working on this repository. Claude Code reads this file when there is no CLAUDE.md. Human contributors: [CONTRIBUTING.md](CONTRIBUTING.md) has the same rules.

## What this is

agent-resurrect resumes AI coding agent sessions in their original terminal panes after a restart. Shipped today: Claude Code in iTerm2 on macOS. Other agents and terminals are planned behind two adapter interfaces and do not exist yet.

The README and the code are the source of truth for behaviour. `docs/specs/` records the original design; statements that 0.2.0 changed are marked there as superseded.

## Layout

- `install.sh`: thin bash bootstrap. Copies the tree to `~/.local/share/agent-resurrect`, writes the launcher `~/.local/bin/agent-resurrect`, then runs `agent-resurrect install --yes` and `doctor`. `--dry-run` stages to a temporary folder and runs `install --dry-run`.
- `bin/agent-resurrect`: POSIX sh wrapper. Finds a Python 3.9+ and runs `libexec/agent-resurrect.py` with `-I` (and `-B` for a `--dry-run`, which must write nothing under HOME). It is on Claude Code's PATH while the plugin is enabled.
- `libexec/agent-resurrect.py`: the Python entry point. It finds `src/` relative to itself.
- `src/agent_resurrect/cli.py`: argument parsing and output only. `views.py` builds the shared `Session` and `Pane` JSON objects.
- `src/agent_resurrect/exitcodes.py` and `jsonout.py`: the exit code contract and the `--json` envelope.
- `src/agent_resurrect/agents/`: agent adapters (`base.py` interface, `claude.py`).
- `src/agent_resurrect/terminals/`: terminal adapters (`base.py`, `iterm.py`).
- `src/agent_resurrect/settings/`: every system change the installer makes, each a `Setting` with `check`, `apply`, an exact `revert`, and `applicable`.
- `src/agent_resurrect/restore.py`: eligibility rules, `simulate`, the countdown and the launch. `store.py`: pane records and the atomic claim. `finder.py`: transcript text search. `doctor.py`: checks and `doctor --json`.
- `shell/`: zsh and bash first-prompt snippets. They compute the pane key without starting a process.
- `.claude-plugin/`, `hooks/`, `skills/`: the Claude Code plugin, rooted at the repository root. Hooks call `${CLAUDE_PLUGIN_ROOT}/bin/agent-resurrect`. Skills: `resume-session` and `setup`.
- `docs/json/`: JSON Schema for each command's `--json` output.
- `tests/`: unittest suite. `tests/fakes/` holds fake `claude`, `defaults` and `launchctl`.

## Commands

```bash
make test                            # ./run_tests.sh: all tests on /usr/bin/python3 (3.9) and python3 from PATH
./run_tests.sh -k Store              # a subset
PYTHONPATH=src python3 -m unittest discover -s tests -t . -v   # what CI runs
make lint                            # shellcheck and zsh -n, as CI runs them
make validate                        # claude plugin validate . --strict, if Claude Code is installed
```

Do not run `make install-dev` or `./install.sh` on the user's machine unless they ask: it changes their real settings.

## Rules

- Python standard library only. Python 3.9 is the floor: use `typing.Optional` and `typing.List`, no `X | Y` unions at runtime, no `match`.
- macOS only. Shell snippets must work in zsh and in macOS's bash 3.2.
- Tests never touch the real HOME, user defaults, launchd or Claude Code, and never start a real `claude`. Use `tests.helpers.temp_home()` and put `tests/fakes` first on PATH, as `tests/test_install_sh.py` does.
- Hooks always exit 0 and print nothing. Claude Code and Codex add SessionStart stdout to the model's context, so any output becomes prompt text.
- Never launch an agent without a terminal on stdin and stdout. Print the command and return `exitcodes.NEEDS_TTY` instead. `--json` on `resume` and `here` implies `--print`: it never launches.
- A dry run (`install --dry-run`, `uninstall --dry-run`, `install.sh --dry-run`) writes nothing under HOME: no log, no manifest, no bytecode.
- Use the names in `exitcodes.py`, never bare integers. Build `--json` documents with `jsonout.document(command, code, message, **fields)` and print them with `jsonout.emit`. Exactly one object goes to stdout; diagnostics go to stderr. Within a schema version, only add fields. Keep `docs/json/` in step.
- A pane is recorded and restored only when `TERM_PROGRAM` is `iTerm.app` and none of `TMUX`, `STY`, `ZELLIJ` is set, in Python and in both shell snippets.
- Every system change is a `Setting`. Save the original value in the manifest before changing it, and make `revert` restore it exactly.
- Pane keys come from environment variables. They must pass `store.valid_pane_key`, and shell snippets must compute the same key as the terminal adapter (`tests/test_shell_snippets.py`).
- Pass every string that came from a transcript or record through `display.printable` before printing it as text.
- No em dashes in any tracked file except `docs/plans/`, and no all-caps headings (`tests/test_docs.py` enforces both).
- The install prompt must be identical in README.md, docs/install-with-claude.md and the design spec (`tests/test_docs.py`).
- The version must agree in `__init__.py`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `install.sh` and the newest CHANGELOG entry (`tests/test_version.py`).
- Record user-visible changes in CHANGELOG.md under `## [Unreleased]` (Keep a Changelog format).

## Adding an agent adapter

1. Read `agents/base.py` and `agents/claude.py`.
2. Create `agents/<name>.py`. Decide how sessions are recorded: the agent's own lifecycle hooks if it has them, otherwise the shell recorder described in section 6.4 of the design spec.
3. Implement every method: `on_hook` must never raise; `resume_argv` returns an argv list, never a shell string; `find_by_text` raises `ValueError` for text too short to search and returns `[]` if the agent's store cannot be searched; `adopt` returns `None` when it cannot identify the session.
4. Add a fake executable to `tests/fakes/` and sanitised session files under `tests/fixtures/<name>/`.
5. Register it in `agents/__init__.py`, and add any hook configuration it needs as a `Setting` with an exact revert.
6. Add tests that cover recording, nested agent processes, missing and truncated session files, and resume argv.

## Adding a terminal adapter

1. Implement `terminals/base.py`. The pane key must survive a restart of that terminal.
2. Return the settings the terminal needs for window restoration from `settings()`.
3. Teach both shell snippets to compute the same pane key, and extend `tests/test_shell_snippets.py`.

## Running agent-resurrect as an agent

- Use `~/.local/bin/agent-resurrect` by full path, or `agent-resurrect` in a Claude Code session where the plugin is loaded.
- Add `--json` and branch on the exit code: 0 ok, 1 error, 2 usage, 3 not found, 4 needs a terminal, 5 busy, 6 gone.
- Safe to run: `ls`, `find`, `simulate`, `doctor`, `config` (reading), `version`, `adopt`, `install --dry-run`, `uninstall --dry-run`. Run `install --yes` or `uninstall --yes` only when the user asks.
- Never run the bare command, `here` or `resume` to start a session. With `--json` (which implies `--print`) or `--print` they print the resume command and exit 0; give it to the user. Without either and without a terminal they refuse with exit 4. Refer to sessions by id, not by list number.
- In `doctor --json`, run `fix.argv` only when `fix.agent_may_run` is true.
