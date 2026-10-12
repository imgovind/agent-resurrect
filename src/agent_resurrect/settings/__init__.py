"""Setting registry: everything the installer applies, in order."""
from typing import List

from .base import Check, Setting


def all_settings() -> List[Setting]:
    from .. import terminals
    from .claude_plugin import ClaudePlugin
    from .macos import KeepWindowsOnQuit
    from .shellrc import ShellRcBlock

    return [KeepWindowsOnQuit(),
            *[setting for terminal in terminals.all_terminals() for setting in terminal.settings()],
            ShellRcBlock("zsh"), ShellRcBlock("bash"), ClaudePlugin()]


__all__ = ["Check", "Setting", "all_settings"]
