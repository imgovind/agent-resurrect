# Changelog

All notable changes to agent-resurrect are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-12

First release. Supports Claude Code in iTerm2 on macOS.

### Added

- Automatic resume after a restart: each iTerm2 pane that was running a Claude Code session resumes that same session in the same pane and folder, at the shell's first prompt.
- Four restore behaviours through `auto_resume`: `countdown` (default, 3 seconds, any key skips), `immediate`, `ask` and `off`.
- Permission mode mirroring through `permission_flag` (`mirror`, `always`, `never`): a session that ran with `--dangerously-skip-permissions` comes back with it by default.
- Commands: `agent-resurrect` (resume this pane now), `ls`, `resume <n or session-id>`, `find [text] [--clipboard]`, `doctor`, `config [key [value]]`, `install` and `uninstall [--purge]`, with `resurrect` as a short alias.
- Text search over Claude Code transcripts (`find`) for sessions that have no record, such as those started before install or outside iTerm2.
- A Claude Code plugin with SessionStart and SessionEnd hooks that record which pane each interactive session runs in, and a `resume-by-text` skill.
- zsh and bash first-prompt snippets that start no process when the pane has nothing to restore.
- A one-line installer, plus a copy-paste prompt for installing with Claude Code.
- Installer settings, each with an exact revert: macOS keeps windows on quit, iTerm2 restores windows at launch, iTerm2 opens at login (LaunchAgent), the shell rc block, and the Claude Code plugin. Original values are saved in a manifest before anything changes, and `agent-resurrect uninstall` restores every one.
- Adapter interfaces for agents and terminals, so other agents and terminals can be added later.

[0.1.0]: https://github.com/imgovind/agent-resurrect/releases/tag/v0.1.0
