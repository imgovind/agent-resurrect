"""Filesystem locations. Every path honours HOME and the XDG variables, so tests can redirect them."""
import os
from pathlib import Path

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
