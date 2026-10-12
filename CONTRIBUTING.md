# Contributing to agent-resurrect

Thanks for helping. agent-resurrect 0.2 supports Claude Code in iTerm2 on macOS. Support for other agents and terminals is welcome; please [open an issue](https://github.com/imgovind/agent-resurrect/issues/new/choose) first so we can agree on the approach before you write the adapter.

If you work with a coding agent, point it at [AGENTS.md](AGENTS.md); it has the same rules in a form agents read on their own.

## Set up

```bash
git clone https://github.com/imgovind/agent-resurrect.git
cd agent-resurrect
make test
```

`make test` runs `./run_tests.sh`, which runs the unit tests on `/usr/bin/python3` (the 3.9 floor that ships with macOS) and on `python3` from your PATH, if that is a different Python. Pass `unittest` arguments to the script directly, for example `./run_tests.sh -k Store`.

CI also lints the shell files. Run the same checks before you open a pull request:

```bash
make lint
```

That runs `shellcheck` on the installer, the bash snippet and the launcher wrapper, and `zsh -n` on the zsh snippet. If you changed the plugin (`.claude-plugin/`, `hooks/` or `skills/`) and have Claude Code installed, also run:

```bash
make validate
```

To try your branch on your own Mac, run `make install-dev` (the same as `./install.sh` from the clone); it installs your working tree instead of downloading a release. `./install.sh --dry-run` shows what it would change first, and `agent-resurrect uninstall` reverts it.

## Ground rules

These come from how the code is built, and the tests hold most of them in place.

- **Standard library only, Python 3.9.** The launcher runs the system Python with `-I`, so there are no dependencies to install. Use `typing.Optional` and `typing.List`; no `X | Y` unions at runtime and no `match`.
- **Hooks never fail and never print.** Anything that runs inside a Claude Code hook must exit 0 and stay silent; problems go to the log. Claude Code adds SessionStart output to the model's context, so any output becomes prompt text.
- **Never launch an agent without a terminal.** Commands that start an agent check that stdin and stdout are terminals; otherwise they print the command and return `NEEDS_TTY`.
- **Exit codes and JSON are a public interface.** Use the names in `src/agent_resurrect/exitcodes.py`, never bare integers. Build every `--json` document with `jsonout.document(...)` and print it with `jsonout.emit`: exactly one object on stdout, diagnostics on stderr. Within a schema version, only add fields; never rename or remove one. Update the schema in `docs/json/` in the same pull request; `tests/test_json_contract.py` checks every command against it.
- **Every setting can be undone exactly.** A new setting implements `check`, `apply`, `revert` and `applicable` (see `src/agent_resurrect/settings/base.py`), and `apply` returns the original value so uninstall can restore it.
- **Tests never touch the real system.** Use `tests/helpers.py` (`temp_home`) and the fake `defaults`, `launchctl` and `claude` in `tests/fakes/`. No test may read or write your real HOME, preferences, LaunchAgents or Claude Code plugins, or start a real `claude`.
- **Untrusted text is sanitised before printing.** Titles, paths and ids come from transcripts and environment variables; print them through `display.printable`.
- **Docs match the code.** `tests/test_docs.py` checks the install prompt (identical in the README, `docs/install-with-claude.md` and the design spec) and the shell block. If you change behaviour, change the README in the same pull request.
- **House style.** No em dashes anywhere in tracked files, and no all-caps headings (both checked by a test). Commands in docs are copy-paste ready.

## Adding an agent or a terminal

- Agents implement `AgentAdapter` in `src/agent_resurrect/agents/base.py`; see `agents/claude.py`. Decide how sessions are recorded: the agent's own lifecycle hooks if it has them, otherwise the shell recorder described in section 6.4 of the [design spec](docs/specs/2026-10-12-agent-resurrect-design.md). A hook-capable agent also needs a `Setting` that writes its hook configuration, with an exact revert.
- Terminals implement `TerminalAdapter` in `src/agent_resurrect/terminals/base.py`; see `terminals/iterm.py`. A terminal is only a good fit if it gives each pane an identifier that survives a restart. Both shell snippets must compute the same pane key, and `tests/test_shell_snippets.py` checks that.
- Register it in `all_agents()` (`src/agent_resurrect/agents/__init__.py`) or `all_terminals()` (`src/agent_resurrect/terminals/__init__.py`), and add tests next to the existing ones (`tests/test_registries.py` covers the registries).

## Pull requests

- One change per pull request, with a test that fails without it.
- Add a line under `## [Unreleased]` in `CHANGELOG.md`.
- Describe how you tested it on a real Mac if the change affects install, restore or the shell snippets.

## Versioning

agent-resurrect follows [Semantic Versioning](https://semver.org/). The public interface is the command line (commands, flags, exit codes and `--json` documents), the config keys and the installer's behaviour. Before 1.0, a minor release may change these; the CHANGELOG always says how.

## Releasing (maintainers)

1. Bump the version in `src/agent_resurrect/__init__.py`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` and `install.sh` (`DEFAULT_REF`). `tests/test_version.py` checks that they agree with the newest CHANGELOG entry.
2. Move the `Unreleased` entries in `CHANGELOG.md` under the new version and date.
3. Tag and push: `git tag v0.2.1 && git push origin v0.2.1`. The release workflow checks the versions against the tag, runs the tests, and publishes the GitHub Release with the notes from the CHANGELOG, an `install.sh` pinned to the tagged commit, and `SHA256SUMS`.
4. Update Homebrew: in [imgovind/homebrew-tap](https://github.com/imgovind/homebrew-tap), set `url` in `Formula/agent-resurrect.rb` to `https://github.com/imgovind/agent-resurrect/archive/refs/tags/vX.Y.Z.tar.gz` and `sha256` to the output of `curl -fsSL <that url> | shasum -a 256`, then push. The tap's CI installs, tests and audits the formula. Check with `brew upgrade agent-resurrect && agent-resurrect version`.
