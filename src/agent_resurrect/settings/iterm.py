"""iTerm2 settings: restore windows at launch, and open iTerm2 at login."""
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


class ITermRestorePolicy(BoolDefaultsSetting):
    """iTerm2 Settings > General > Startup > Window restoration policy = "Use System Window Restoration Setting".

    Verified against gnachman/iTerm2 master 3d5cb58 (2026-10-12):
    - sources/Settings/GeneralPreferencesViewController.m: the popup's kUseSystemWindowRestorationSettingTag
      stores OpenArrangementAtStartup = NO and OpenNoWindowsAtStartup = NO. "Open Default Window Arrangement"
      sets OpenArrangementAtStartup = YES; "Don't Open Any Windows" sets OpenNoWindowsAtStartup = YES.
    - sources/TerminalView/PseudoTerminalRestorer.m: either key being YES aborts window restoration.
    - sources/iTermController/iTermController.m willRestoreWindowsAtNextLaunch: both NO plus
      stateRestorationEnabled (NSQuitAlwaysKeepsWindows, see macos.KeepWindowsOnQuit).
    - sources/Settings/iTermPreferences.m: both keys default to NO, so an absent key is already correct.
    NoSyncIgnoreSystemWindowRestoration is deliberately not touched: iTerm2 rewrites it itself at launch,
    on resign-active and at terminate to mirror its useRestorableStateController advanced setting
    (sources/AppKit/iTermApplicationDelegate.m, sources/Settings/iTermUserDefaults.m).
    """

    id = "iterm.restore_policy"
    description = 'iTerm2 restores its windows at launch (Startup: "Use System Window Restoration Setting")'
    fix_hint = ('run: agent-resurrect install, or set iTerm2 Settings > General > Startup to '
                '"Use System Window Restoration Setting"')
    ok_detail = "iTerm2 restores its windows at launch"
    keys = [BoolKey(ITERM_DOMAIN, "OpenArrangementAtStartup", want=False, default=False),
            BoolKey(ITERM_DOMAIN, "OpenNoWindowsAtStartup", want=False, default=False)]


def iterm_app() -> Path:
    return Path(os.environ.get("AGENT_RESURRECT_ITERM_APP") or "/Applications/iTerm.app")


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


class ITermLaunchAtLogin(Setting):
    """A LaunchAgent that runs `open -a iTerm` at login. It needs no Automation permission, and if macOS
    reopens iTerm2 as well, `open -a` is harmless."""

    id = "iterm.launch_at_login"
    description = f"Open iTerm2 at login (LaunchAgent {LABEL})"

    @property
    def fix_hint(self) -> str:
        if not iterm_app().exists():
            return "install iTerm2 from https://iterm2.com, then run: agent-resurrect install"
        return "run: agent-resurrect install"

    def check(self) -> Check:
        if not iterm_app().exists():
            return Check(False, "iTerm2 is not installed")
        if _read_plist(plist_path()) == desired_plist():
            return Check(True, "iTerm2 opens at login")
        return Check(False, f"iTerm2 does not open at login (no LaunchAgent {LABEL})")

    def apply(self) -> Optional[dict]:
        if not iterm_app().exists():
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
        uid = os.getuid()
        if existed:
            _launchctl("bootout", f"gui/{uid}/{LABEL}")
        proc = _launchctl("bootstrap", f"gui/{uid}", str(path))
        if proc.returncode != 0:  # usually "already loaded"; the agent is loaded at the next login anyway
            log(f"launchctl bootstrap {LABEL} exited {proc.returncode}: {proc.stderr.strip()}")
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
