"""Terminal adapter registry."""
from typing import List, Mapping, Optional

from .base import TerminalAdapter


def all_terminals() -> List[TerminalAdapter]:
    from .iterm import ITermAdapter  # imported on use: the registry itself stays cheap to import
    return [ITermAdapter()]


def detect_terminal(env: Mapping[str, str]) -> Optional[TerminalAdapter]:
    for terminal in all_terminals():
        if terminal.detect(env):
            return terminal
    return None
