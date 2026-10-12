"""iTerm2 settings: keep windows when quitting, restore them at launch, and open iTerm2 at login."""
import os
import plistlib
import subprocess
from pathlib import Path
from typing import Optional

from .. import paths
from ..logs import log
from .base import Check, Setting
from .defaults import BoolDefaultsSetting, BoolKey

ITERM_DOMAIN = "com.googlecode.iterm2"
LABEL = "io.github.imgovind.agent-resurrect.iterm"
_TIMEOUT = 30


def iterm_app() -> Path:
    """iTerm.app in /Applications, else in ~/Applications (a non-admin install or `brew --appdir`).
    AGENT_RESURRECT_ITERM_APP overrides both (tests)."""
    override = os.environ.get("AGENT_RESURRECT_ITERM_APP")
    if override:
        return Path(override)
    system = Path("/Applications/iTerm.app")
    if system.exists():
        return system
    user = paths.home() / "Applications" / "iTerm.app"
    return user if user.exists() else system


_INSTALL_ITERM = "install iTerm2 from https://iterm2.com, then run: ~/.local/bin/agent-resurrect install"


class _NeedsITerm:
    """Mixin for settings that only make sense with iTerm2 installed; listed before the Setting base class.
    Without iTerm2, install skips the setting and doctor's fix is to install iTerm2, never to rerun install."""

    skip_detail = "iTerm2 is not installed"
    _fix_hint = "run: ~/.local/bin/agent-resurrect install"

    def applicable(self) -> bool:
        return iterm_app().exists()

    @property
    def fix_hint(self) -> str:
        return self._fix_hint if self.applicable() else _INSTALL_ITERM

    def check(self) -> Check:
        if not self.applicable():
            return Check(False, self.skip_detail)
        return super().check()

    def apply(self) -> Optional[dict]:
        if not self.applicable():
            return None
        return super().apply()


class ITermKeepWindows(_NeedsITerm, BoolDefaultsSetting):
    """iTerm2 keeps its windows when it quits: NSQuitAlwaysKeepsWindows = true in iTerm2's own defaults domain.

    iTerm2 saves its window state only when this reads true. Verified against gnachman/iTerm2 master 3d5cb58
    (2026-10-12):
    - sources/StateRestoration/iTermRestorableStateController.m:60-69: +stateRestorationEnabled returns
      [[iTermUserDefaults userDefaults] boolForKey:@"NSQuitAlwaysKeepsWindows"] || shouldRestoreStateOnNextLaunch,
      and applicationWillTerminate erases the saved state when it is false.
    - sources/Settings/iTermUserDefaults.m:99-107: +userDefaults is [NSUserDefaults standardUserDefaults] unless
      a custom suite was set, which only the `-suite <name>` launch argument does (sources/AppKit/main.m:52-55).
    standardUserDefaults searches the application's own domain (com.googlecode.iterm2) before NSGlobalDomain
    (Apple, NSUserDefaults "Search order": argument, application, global, languages, registration), so this
    value applies to iTerm2 alone and every other app keeps the user's macOS-wide "Close windows when quitting
    an application" choice. AppKit resolves the key for an app through the same search list, which is why
    `defaults write <bundle id> NSQuitAlwaysKeepsWindows` is the usual per-app override.

    The key is written in iTerm2's domain even when the global value is already true, so the user can turn the
    macOS-wide setting back on without stopping iTerm2's restore: here an absent key counts as false.
    agent-resurrect 0.1 wrote the global key instead; see macos.LegacyKeepWindowsOnQuit.
    """

    id = "iterm.keep_windows"
    description = "iTerm2 keeps its windows when it quits (NSQuitAlwaysKeepsWindows in iTerm2's settings only)"
    ok_detail = "iTerm2 keeps its windows when it quits"
    keys = [BoolKey(ITERM_DOMAIN, "NSQuitAlwaysKeepsWindows", want=True, default=False)]


class ITermRestorePolicy(_NeedsITerm, BoolDefaultsSetting):
    """iTerm2 Settings > General > Startup > Window restoration policy = "Use System Window Restoration Setting".

    Verified against gnachman/iTerm2 master 3d5cb58 (2026-10-12):
    - sources/Settings/GeneralPreferencesViewController.m: the popup's kUseSystemWindowRestorationSettingTag
      stores OpenArrangementAtStartup = NO and OpenNoWindowsAtStartup = NO. "Open Default Window Arrangement"
      sets OpenArrangementAtStartup = YES; "Don't Open Any Windows" sets OpenNoWindowsAtStartup = YES.
    - sources/TerminalView/PseudoTerminalRestorer.m: either key being YES aborts window restoration.
    - sources/iTermController/iTermController.m willRestoreWindowsAtNextLaunch: both NO plus
      stateRestorationEnabled (NSQuitAlwaysKeepsWindows, see ITermKeepWindows).
    - sources/Settings/iTermPreferences.m: both keys default to NO, so an absent key is already correct.
    NoSyncIgnoreSystemWindowRestoration is deliberately not touched: iTerm2 rewrites it itself at launch,
    on resign-active and at terminate to mirror its useRestorableStateController advanced setting
    (sources/AppKit/iTermApplicationDelegate.m, sources/Settings/iTermUserDefaults.m).
    """

    id = "iterm.restore_policy"
    description = 'iTerm2 restores its windows at launch (Startup: "Use System Window Restoration Setting")'
    _fix_hint = ('run: ~/.local/bin/agent-resurrect install, or set iTerm2 Settings > General > Startup to '
                 '"Use System Window Restoration Setting"')
    ok_detail = "iTerm2 restores its windows at launch"
    keys = [BoolKey(ITERM_DOMAIN, "OpenArrangementAtStartup", want=False, default=False),
            BoolKey(ITERM_DOMAIN, "OpenNoWindowsAtStartup", want=False, default=False)]


def plist_path() -> Path:
    return paths.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def desired_plist() -> dict:
    return {"Label": LABEL, "ProgramArguments": ["/usr/bin/open", "-a", "iTerm"], "RunAtLoad": True,
            "LimitLoadToSessionType": "Aqua"}


def _read_plist(path: Path) -> Optional[dict]:
    try:
        with open(path, "rb") as fh:
            return plistlib.load(fh)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=_TIMEOUT,
                          stdin=subprocess.DEVNULL)


class ITermLaunchAtLogin(_NeedsITerm, Setting):
    """A LaunchAgent that runs `open -a iTerm` at login. It needs no Automation permission, and if macOS
    reopens iTerm2 as well, `open -a` is harmless. Install only writes the plist; launchd loads it at the
    next login. Uninstall unloads it (if loaded) and deletes it."""

    id = "iterm.launch_at_login"
    description = f"Open iTerm2 at login (LaunchAgent {LABEL})"

    def check(self) -> Check:
        if not self.applicable():
            return Check(False, self.skip_detail)
        if _read_plist(plist_path()) == desired_plist():
            return Check(True, "iTerm2 opens at login")
        return Check(False, f"iTerm2 does not open at login (no LaunchAgent {LABEL})")

    def apply(self) -> Optional[dict]:
        if not self.applicable():
            return None
        path = plist_path()
        if _read_plist(path) == desired_plist():
            return None
        existed = path.exists()  # only an earlier agent-resurrect writes this label
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as fh:
            plistlib.dump(desired_plist(), fh)
        os.replace(tmp, path)
        # Deliberately not `launchctl bootstrap`: RunAtLoad would run `open -a iTerm` right now and pull iTerm2
        # to the front mid-install. launchd loads ~/Library/LaunchAgents at the next login, which is when it
        # is needed. An already loaded older copy is harmless: it only ever ran once, at its own load.
        return None if existed else {"existed": False}

    def revert(self, original: dict) -> None:
        try:
            proc = _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
            if proc.returncode != 0:
                log(f"launchctl bootout {LABEL} exited {proc.returncode}: {proc.stderr.strip()}")
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"launchctl bootout {LABEL} failed: {exc!r}")
        path = plist_path()
        if path.exists() or path.is_symlink():
            path.unlink()
