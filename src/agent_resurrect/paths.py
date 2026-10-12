"""Filesystem locations. Every path honours HOME and the XDG variables, so tests can redirect them.

Two ways to install the program: the one-line installer copies it to data_dir() and writes the launcher
~/.local/bin/agent-resurrect; a package manager (Homebrew) installs it wherever it likes and runs it through a
wrapper that sets AGENT_RESURRECT_HOME (the program tree, at a path that survives upgrades: Homebrew's
$(brew --prefix)/opt/agent-resurrect/libexec), AGENT_RESURRECT_BIN (its command) and AGENT_RESURRECT_PACKAGE
(its name). program_dir(), launcher() and command() answer for whichever copy is running.
"""
import os
from pathlib import Path
from typing import Optional

APP = "agent-resurrect"


def home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def _xdg(variable: str, default: str) -> Path:
    value = os.environ.get(variable)
    return Path(value) if value else home() / default


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / APP


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / APP


def bin_dir() -> Path:
    return home() / ".local" / "bin"


def package_home() -> Optional[Path]:
    """The program tree a package manager installed (AGENT_RESURRECT_HOME), or None for the one-line installer."""
    value = os.environ.get("AGENT_RESURRECT_HOME")
    return Path(value) if value else None


def program_dir() -> Path:
    """The program tree that the shell snippet and the Claude Code plugin are installed from."""
    return package_home() or data_dir()


def launcher() -> Path:
    """The command the shell snippet runs and doctor's fixes run: the package's, or ~/.local/bin/agent-resurrect."""
    value = os.environ.get("AGENT_RESURRECT_BIN") if package_home() else None
    return Path(value) if value else bin_dir() / APP


def package_manager() -> Optional[str]:
    """The package manager that installed the running copy (AGENT_RESURRECT_PACKAGE), or None."""
    if package_home() is None:
        return None
    return os.environ.get("AGENT_RESURRECT_PACKAGE") or "your package manager"


def command() -> str:
    """How to write the command in a hint: by name when a package manager put it on PATH, else the launcher's
    path (a new install's ~/.local/bin is on PATH only from the next shell)."""
    return APP if package_home() else f"~/.local/bin/{APP}"


def cache_dir() -> Path:
    """Bytecode cache (the launcher's `-X pycache_prefix`). Fixed under HOME: install.sh writes the same path."""
    return home() / ".cache" / APP


def panes_dir() -> Path:
    return state_dir() / "panes"


def manifest_path() -> Path:
    return state_dir() / "manifest.json"


def log_path() -> Path:
    return state_dir() / "log"


def restore_events_path() -> Path:
    return state_dir() / "restores.jsonl"


def config_path() -> Path:
    return config_dir() / "config"


def claude_config_dir() -> Path:
    value = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(value) if value else home() / ".claude"
