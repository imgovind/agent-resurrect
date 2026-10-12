"""iTerm2: a pane is identified by the GUID in ITERM_SESSION_ID, which survives a restart.

ITERM_SESSION_ID looks like "w0t12p0:10FE02F5-C525-405E-9E86-C8255ED17032": a positional label
(window 0, tab 12, pane 0), then the pane's GUID. Only the GUID is stable, so the pane key is
"iterm-" + the text after the first ":". The shell snippets in shell/ compute the same key without
starting a process; tests/test_shell_snippets.py holds both sides to that contract.

Only a shell that iTerm2 started directly in a pane owns that pane. Everything started from a pane inherits
its ITERM_SESSION_ID: a tmux or screen server and every shell inside it, Zellij, an editor opened with
`code .`. Without a guard, all of those would share the pane's key, overwrite each other's records, and
after a restart resume one session several times at once. So a pane is recognised only when
TERM_PROGRAM is "iTerm.app" (tmux and editors set their own) and none of TMUX, STY (screen) or ZELLIJ is
set (screen and Zellij keep the inherited TERM_PROGRAM). The snippets apply the same test.
"""
import re
import subprocess
from typing import TYPE_CHECKING, List, Mapping, Optional, Tuple

from .. import logs
from .base import TerminalAdapter

if TYPE_CHECKING:
    from ..settings.base import Setting

GUID = re.compile(r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}")
KEY_PREFIX = "iterm-"
TERM_PROGRAM = "iTerm.app"
MULTIPLEXER_VARIABLES = ("TMUX", "STY", "ZELLIJ")  # tmux, GNU screen, Zellij

# Reads one pane's text through iTerm2's AppleScript interface. The pane's GUID arrives as an argument
# (`osascript - <guid>`), never spliced into the script source.
PANE_TEXT_SCRIPT = """on run argv
    set wanted to item 1 of argv
    tell application "iTerm2"
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    if (unique id of s) is wanted then return text of s
                end repeat
            end repeat
        end repeat
    end tell
    return ""
end run
"""
PANE_TEXT_TIMEOUT = 15  # seconds


def _split(env: Mapping[str, str]) -> Tuple[str, str, str]:
    """(label, separator, rest) of ITERM_SESSION_ID, split at the first ":"."""
    return (env.get("ITERM_SESSION_ID") or "").partition(":")


def direct_child(env: Mapping[str, str]) -> bool:
    """True if this environment is a shell iTerm2 started in a pane, not one inside a multiplexer or another
    program that only inherited the pane's variables. Empty values count as unset, as in the snippets."""
    return env.get("TERM_PROGRAM") == TERM_PROGRAM and not any(env.get(name) for name in MULTIPLEXER_VARIABLES)


class ITermAdapter(TerminalAdapter):
    name = "iterm"
    restores_in_place = True

    def detect(self, env: Mapping[str, str]) -> bool:
        return self.pane_key(env) is not None

    def pane_key(self, env: Mapping[str, str]) -> Optional[str]:
        if not direct_child(env):
            return None
        _, sep, guid = _split(env)
        # fullmatch, not match with "$": "$" also matches before a trailing newline.
        if sep and GUID.fullmatch(guid):
            return KEY_PREFIX + guid
        return None

    def pane_label(self, env: Mapping[str, str]) -> Optional[str]:
        label, sep, _ = _split(env)
        return label if sep and label else None

    def pane_text(self, env: Mapping[str, str], max_lines: int = 2000) -> Optional[str]:
        key = self.pane_key(env)
        if key is None:
            return None
        try:
            result = subprocess.run(["osascript", "-", key[len(KEY_PREFIX):]], input=PANE_TEXT_SCRIPT,
                                    capture_output=True, text=True, timeout=PANE_TEXT_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            logs.log(f"iterm: could not read the pane's text: {type(exc).__name__}")
            return None
        if result.returncode != 0 or not result.stdout.strip():
            # Only the exit status is logged: the pane's text never reaches the log.
            logs.log(f"iterm: could not read the pane's text (osascript exit {result.returncode})")
            return None
        lines = result.stdout.splitlines()
        return "\n".join(lines[-max_lines:] if max_lines > 0 else lines)

    def settings(self) -> List["Setting"]:
        # Imported here so that detecting a pane (on every hook call) never loads the settings code.
        from ..settings.iterm import ITermLaunchAtLogin, ITermRestorePolicy
        return [ITermRestorePolicy(), ITermLaunchAtLogin()]
