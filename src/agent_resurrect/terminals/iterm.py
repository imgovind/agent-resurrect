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
from typing import TYPE_CHECKING, List, Mapping, Optional, Tuple

from .base import TerminalAdapter

if TYPE_CHECKING:
    from ..settings.base import Setting

GUID = re.compile(r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}")
KEY_PREFIX = "iterm-"
TERM_PROGRAM = "iTerm.app"
MULTIPLEXER_VARIABLES = ("TMUX", "STY", "ZELLIJ")  # tmux, GNU screen, Zellij


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

    def settings(self) -> List["Setting"]:
        # Imported here so that detecting a pane (on every hook call) never loads the settings code.
        from ..settings.iterm import ITermLaunchAtLogin, ITermRestorePolicy
        return [ITermRestorePolicy(), ITermLaunchAtLogin()]
