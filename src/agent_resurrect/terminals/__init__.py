"""Terminal adapter registry."""
from typing import List, Mapping, Optional

from .base import TerminalAdapter


def all_terminals() -> List[TerminalAdapter]:
    return []


def detect_terminal(env: Mapping[str, str]) -> Optional[TerminalAdapter]:
    for terminal in all_terminals():
        if terminal.detect(env):
            return terminal
    return None
