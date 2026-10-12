"""Setting registry: everything the installer applies, in order, plus the retired settings it only reverts."""
from typing import List

from .base import Check, Setting


def all_settings() -> List[Setting]:
    from .. import terminals
    from .claude_plugin import ClaudePlugin
    from .iterm import ITermKeepWindows
    from .shellrc import ShellRcBlock

    # ITermKeepWindows takes the first place, which 0.1's macOS-wide macos.keep_windows had.
    return [ITermKeepWindows(),
            *[setting for terminal in terminals.all_terminals() for setting in terminal.settings()],
            ShellRcBlock("zsh"), ShellRcBlock("bash"), ClaudePlugin()]


def legacy_settings() -> List[Setting]:
    """Settings an earlier version applied and this one no longer does. A manifest entry for one of them is
    reverted (when "changed") and dropped, by install as part of the upgrade and by uninstall."""
    from .macos import LegacyKeepWindowsOnQuit

    return [LegacyKeepWindowsOnQuit()]


__all__ = ["Check", "Setting", "all_settings", "legacy_settings"]
