"""macOS settings the restore depends on."""
from .defaults import GLOBAL_DOMAIN, BoolDefaultsSetting, BoolKey


class KeepWindowsOnQuit(BoolDefaultsSetting):
    """macOS "Close windows when quitting an application" off, i.e. NSQuitAlwaysKeepsWindows = true.

    iTerm2 only saves its window state when this is on: iTermRestorableStateController.stateRestorationEnabled
    returns boolForKey:@"NSQuitAlwaysKeepsWindows" || [NSApp shouldRestoreStateOnNextLaunch], and
    applicationWillTerminate erases the saved state when it is false
    (gnachman/iTerm2 sources/StateRestoration/iTermRestorableStateController.m, master 3d5cb58, 2026-10-12).
    An absent key reads as false.
    """

    id = "macos.keep_windows"
    description = 'Keep app windows when quitting (turns off macOS "Close windows when quitting an application")'
    fix_hint = ('run: ~/.local/bin/agent-resurrect install, or turn off "Close windows when quitting an application" '
                "in System Settings > Desktop & Dock")
    ok_detail = "macOS keeps windows when quitting an application"
    keys = [BoolKey(GLOBAL_DOMAIN, "NSQuitAlwaysKeepsWindows", want=True, default=False)]
