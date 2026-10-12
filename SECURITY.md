# Security policy

## Supported versions

Only the latest release receives security fixes. Upgrade by running the install line again.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: open the [Security tab](https://github.com/imgovind/agent-resurrect/security) and choose **Report a vulnerability**. Do not open a public issue.

Include what you found, how to reproduce it, and the output of `agent-resurrect --version`. I aim to acknowledge reports within 7 days and to agree on a disclosure date with you. agent-resurrect is maintained by one person, so please allow some time for a fix.

## What is in scope

agent-resurrect runs code in places where a bug could matter, so these are especially welcome:

- The installer (`install.sh`), which is meant to be run with `curl | bash`, and the release assets it is published with (`install.sh` pinned to a commit, and `SHA256SUMS`).
- The shell snippet that runs at the first prompt of every interactive shell.
- The Claude Code plugin hooks, which run in every Claude Code session, and the plugin's `setup` skill.
- Anything that lets text from a transcript, a session title, a path or an environment variable reach the terminal as an escape sequence, reach a `--json` document unescaped, or reach the filesystem outside agent-resurrect's own folders.
- Anything that makes a session resume with permission checks turned off when it was not running that way and `permission_flag` is not `always`.
- Anything that starts an agent without a terminal, or lets one pane's shell resume or alter another pane's session automatically.
- Anything that makes uninstall leave a setting changed while reporting success.

Out of scope: vulnerabilities in Claude Code, iTerm2 or macOS themselves (please report those to their maintainers), and the documented effect of `permission_flag = always`.
