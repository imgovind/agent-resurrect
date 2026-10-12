"""User configuration in ~/.config/agent-resurrect/config: INI with one [agent-resurrect] section."""
import configparser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import logs, paths

SECTION = "agent-resurrect"
CHOICES = {
    "auto_resume": ("countdown", "immediate", "ask", "off"),
    "permission_flag": ("mirror", "always", "never"),
    "pane_search": ("ask", "auto", "off"),
}
DEFAULTS = {"auto_resume": "countdown", "countdown_seconds": 3, "permission_flag": "mirror", "max_age_days": 14,
            "pane_search": "ask"}


@dataclass(frozen=True)
class Config:
    auto_resume: str = "countdown"
    countdown_seconds: int = 3
    permission_flag: str = "mirror"
    max_age_days: int = 14
    # `here` with nothing recorded for the pane: ask before searching its scrollback and before resuming the
    # match (ask), search and resume a strong match without asking, as -y does (auto), or never read the
    # scrollback unless -y is passed (off).
    pane_search: str = "ask"


def _parse(key: str, value: str):
    """Return the typed value, or raise ValueError with a user-facing message."""
    default = DEFAULTS[key]
    if isinstance(default, int):
        if not value.isdigit():
            raise ValueError(f"{key} must be a non-negative integer")
        return int(value)
    if value not in CHOICES[key]:
        raise ValueError(f"{key} must be one of: {', '.join(CHOICES[key])}")
    return value


def load_config(path: Optional[Path] = None) -> Config:
    """Read the config file. A missing or invalid value falls back to its default and is logged."""
    path = path or paths.config_path()
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except configparser.Error as exc:
        logs.log(f"config: unreadable {path}: {exc}")
        return Config()
    section = parser[SECTION] if parser.has_section(SECTION) else {}
    values = {}
    for key, default in DEFAULTS.items():
        raw = section.get(key)
        if raw is None:
            values[key] = default
            continue
        try:
            values[key] = _parse(key, raw.strip())
        except ValueError as exc:
            logs.log(f"config: {exc}; using {default!r}")
            values[key] = default
    return Config(**values)


def set_value(key: str, value: str, path: Optional[Path] = None) -> None:
    """Validate and persist one key. Raises KeyError for an unknown key, ValueError for a bad value."""
    if key not in DEFAULTS:
        raise KeyError(key)
    _parse(key, value)
    path = path or paths.config_path()
    parser = configparser.ConfigParser()
    parser.read(path)
    if not parser.has_section(SECTION):
        parser.add_section(SECTION)
    parser[SECTION][key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        parser.write(fh)
