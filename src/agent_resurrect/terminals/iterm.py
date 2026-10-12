"""iTerm2: a pane is identified by the GUID in ITERM_SESSION_ID, which survives a restart.

ITERM_SESSION_ID looks like "w0t12p0:10FE02F5-C525-405E-9E86-C8255ED17032": a positional label
(window 0, tab 12, pane 0), then the pane's GUID. Only the GUID is stable, so the pane key is
"iterm-" + the text after the first ":". The shell snippets in shell/ compute the same key without
starting a process; tests/test_shell_snippets.py holds both sides to that contract.
"""
import re
from typing import TYPE_CHECKING, List, Mapping, Optional, Tuple

from .base import TerminalAdapter

if TYPE_CHECKING:
    from ..settings.base import Setting

GUID = re.compile(r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}")
KEY_PREFIX = "iterm-"


def _split(env: Mapping[str, str]) -> Tuple[str, str, str]:
    """(label, separator, rest) of ITERM_SESSION_ID, split at the first ":"."""
    return (env.get("ITERM_SESSION_ID") or "").partition(":")


class ITermAdapter(TerminalAdapter):
    name = "iterm"
    restores_in_place = True

    def detect(self, env: Mapping[str, str]) -> bool:
        return self.pane_key(env) is not None

    def pane_key(self, env: Mapping[str, str]) -> Optional[str]:
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
