# Developer tasks. `make test` is what to run before a pull request; `make lint` matches the shell checks in CI.
# The launcher wrapper bin/agent-resurrect is linted when it exists.

SHELL_FILES := install.sh shell/agent-resurrect.bash $(wildcard bin/agent-resurrect)

.PHONY: test lint validate check install-dev

test:
	./run_tests.sh

lint:
	shellcheck $(SHELL_FILES)
	zsh -n shell/agent-resurrect.zsh

# Needs Claude Code. Validates the plugin and the marketplace at the repository root.
validate:
	claude plugin validate . --strict

check: test lint

# Installs this working tree on your own Mac (the same as ./install.sh from a clone). It changes real settings;
# `agent-resurrect uninstall` reverts them, and `./install.sh --dry-run` shows them first.
install-dev:
	./install.sh
