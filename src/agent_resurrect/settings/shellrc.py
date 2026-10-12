"""The marked block in the user's shell rc file that loads the first-prompt restore snippet.

The rc file is edited in place through its path and as bytes: a symlink (for example into a dotfiles repo)
stays a symlink, its target is edited, and every byte outside the block is left exactly as it was.
"""
import os
import pwd
from pathlib import Path
from typing import List, Optional, Tuple

from .. import paths
from .base import Check, Setting

BEGIN = "# >>> agent-resurrect >>>"
END = "# <<< agent-resurrect <<<"
_BEGIN = BEGIN.encode()
_END = END.encode()
SHELLS = ("zsh", "bash")


def _double_quoted(text: str) -> str:
    """Escape text for use inside double quotes in sh, bash and zsh."""
    return "".join("\\" + ch if ch in '\\"$`' else ch for ch in text)


def shell_path(path: Path) -> str:
    """The path as written inside double quotes in an rc file, with a $HOME prefix when it is under HOME."""
    home = str(paths.home()).rstrip("/")
    text = str(path)
    if home and text.startswith(home + "/"):
        return "$HOME" + _double_quoted(text[len(home):])
    return _double_quoted(text)


def display_path(path: Path) -> str:
    home = str(paths.home()).rstrip("/")
    text = str(path)
    return "~" + text[len(home):] if home and text.startswith(home + "/") else text


def login_shell() -> str:
    """The name of the user's login shell ("zsh", "bash", "fish", ...): from $SHELL, else from the account
    record. "" when neither is known."""
    value = os.environ.get("SHELL") or ""
    if not value:
        try:
            value = pwd.getpwuid(os.getuid()).pw_shell or ""
        except (KeyError, OSError):
            value = ""
    return os.path.basename(value.rstrip("/"))


def find_blocks(data: bytes) -> List[Tuple[int, int]]:
    """Byte spans of each BEGIN..END block, END line included. Each END pairs with the nearest BEGIN before
    it, so a stray BEGIN without an END never swallows the user's lines."""
    spans = []
    start = None
    pos = 0
    for line in data.splitlines(keepends=True):
        text = line.rstrip(b"\r\n").rstrip()
        if text == _BEGIN:
            start = pos
        elif text == _END and start is not None:
            spans.append((start, pos + len(line)))
            start = None
        pos += len(line)
    return spans


def _splice(data: bytes, spans: List[Tuple[int, int]], replacement: Optional[bytes]) -> bytes:
    """Remove every span; put `replacement` where the first one was."""
    pieces = []
    previous = 0
    for index, (start, end) in enumerate(spans):
        pieces.append(data[previous:start])
        if index == 0 and replacement is not None:
            pieces.append(replacement)
        previous = end
    pieces.append(data[previous:])
    return b"".join(pieces)


def _rewrite(path: Path, data: bytes) -> None:
    """Overwrite the file's content through its path (following a symlink), never replacing the file."""
    with open(path, "r+b") as fh:
        fh.write(data)
        fh.truncate()
        fh.flush()
        os.fsync(fh.fileno())


class ShellRcBlock(Setting):
    """The block in ~/.zshrc (honouring ZDOTDIR), or in ~/.bash_profile, else ~/.bashrc, for bash.
    The original stored for revert() is {"existed": bool, "path": str, "added_newline": bool}.

    It applies only where the shell will read it: bash needs one of its files to exist already, and a missing
    .zshrc is created only when zsh is the login shell (or the login shell is unknown: zsh is macOS's default).
    A fish or nushell user would otherwise get a .zshrc that nothing ever reads."""

    fix_hint = "run: ~/.local/bin/agent-resurrect install"

    def __init__(self, shell: str):
        if shell not in SHELLS:
            raise ValueError(f"unsupported shell: {shell}")
        self.shell = shell
        self.id = f"shell.{shell}"

    @property
    def description(self) -> str:
        target = self.target()
        where = display_path(target) if target else "~/.bash_profile or ~/.bashrc, if either exists,"
        return f"Add a block to {where} that resumes a pane's agent session on the first prompt"

    def target(self) -> Optional[Path]:
        home = paths.home()
        if self.shell == "zsh":
            return Path(os.environ.get("ZDOTDIR") or home) / ".zshrc"
        for name in (".bash_profile", ".bashrc"):
            if (home / name).exists():
                return home / name
        return None

    def applicable(self) -> bool:
        target = self.target()
        if target is None:
            return False
        if self.shell == "zsh" and not target.exists():
            return login_shell() in ("zsh", "")
        return True

    @property
    def skip_detail(self) -> str:
        if self.shell == "bash":
            return "bash not configured (no ~/.bash_profile or ~/.bashrc)"
        return f"zsh is not your login shell ({login_shell()}) and there is no {display_path(self.target())}"

    def block(self) -> str:
        snippet = shell_path(paths.data_dir() / "shell" / f"agent-resurrect.{self.shell}")
        return "\n".join([
            BEGIN,
            "# Managed by agent-resurrect. Remove with: agent-resurrect uninstall",
            # Appended, so ~/.local/bin never shadows the user's own pyenv, Homebrew or uv tools.
            'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$PATH:$HOME/.local/bin" ;; esac',
            f'[ -f "{snippet}" ] && . "{snippet}"',
            END,
        ]) + "\n"

    def check(self) -> Check:
        target = self.target()
        if not self.applicable():
            # Nothing for this shell to load; doctor reports an unsupported login shell on its own.
            if self.shell == "bash":
                return Check(True, self.skip_detail)
            return Check(True, f"zsh not configured ({display_path(target)} does not exist and the login shell "
                               f"is {login_shell()})")
        data = target.read_bytes() if target.exists() else b""
        spans = find_blocks(data)
        where = display_path(target)
        if len(spans) == 1 and data[spans[0][0]:spans[0][1]] == self.block().encode():
            return Check(True, f"{where} loads agent-resurrect")
        if spans:
            return Check(False, f"{where} has an outdated agent-resurrect block")
        return Check(False, f"{where} does not load agent-resurrect")

    def apply(self) -> Optional[dict]:
        target = self.target()
        if target is None or not self.applicable():
            return None
        existed = target.exists()
        data = target.read_bytes() if existed else b""
        block = self.block().encode()
        spans = find_blocks(data)
        if spans:
            updated = _splice(data, spans, block)
            if updated != data:
                _rewrite(target, updated)
            return None
        added_newline = bool(data) and not data.endswith(b"\n")
        if not existed:
            target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "ab") as fh:
            fh.write((b"\n" if added_newline else b"") + block)
        return {"existed": existed, "path": str(target), "added_newline": added_newline}

    def revert(self, original: dict) -> None:
        recorded = original.get("path")
        target = Path(recorded) if recorded else self.target()
        if target is None or not target.exists():
            return
        data = target.read_bytes()
        spans = find_blocks(data)
        updated = _splice(data, spans, None)
        # Drop the newline the installer added before the block, but only while the block was still the
        # last thing in the file; if the user added lines after it, that newline now ends a line they kept.
        if spans and original.get("added_newline") and spans[-1][1] == len(data) and updated.endswith(b"\n"):
            updated = updated[:-1]
        if not original.get("existed") and not updated:
            target.unlink()
        elif updated != data:
            _rewrite(target, updated)
