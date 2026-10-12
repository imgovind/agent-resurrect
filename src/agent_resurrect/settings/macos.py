"""agent-resurrect 0.1's macOS-wide window setting, kept only so it can be undone.

0.1 set NSGlobalDomain NSQuitAlwaysKeepsWindows = true, which turns off macOS "Close windows when quitting an
application" for every app. From 0.2 the key is set for iTerm2 alone (iterm.ITermKeepWindows). The installer
never applies this setting: install (the upgrade) and uninstall only revert the "changed" manifest entry 0.1
left under this id, restoring the user's own global value, and then drop the entry.
"""
from .defaults import GLOBAL_DOMAIN, BoolDefaultsSetting, BoolKey

LEGACY_ID = "macos.keep_windows"


class LegacyKeepWindowsOnQuit(BoolDefaultsSetting):
    id = LEGACY_ID
    description = ('Give back your macOS-wide "Close windows when quitting an application" setting '
                   "(agent-resurrect 0.1 changed it for every app; it now changes it for iTerm2 only)")
    fix_hint = "run: ~/.local/bin/agent-resurrect install"
    skip_detail = "only reverted, never applied"
    ok_detail = "macOS keeps windows when quitting an application"
    keys = [BoolKey(GLOBAL_DOMAIN, "NSQuitAlwaysKeepsWindows", want=True, default=False)]

    def applicable(self) -> bool:
        return False
