"""The agent-resurrect Claude Code plugin (its hooks record which pane each session runs in).

`claude` is run by name from PATH. Flags verified with `claude plugin ... --help` on Claude Code 2.1.296 and
https://code.claude.com/docs/en/plugins/cli-reference (2026-10-12):
- `plugin marketplace add <path> --scope user`: adding an already-present marketplace exits 0.
- `plugin install <plugin>@<marketplace> --scope user`: no prompt for a plugin like ours. `-y` only accepts a
  marketplace-declared install command or headersHelper, which this plugin has none of, so it is not passed.
- `plugin list --json`: an array of {"id": "name@marketplace", "scope", "enabled", "version", "installPath"}.
- `plugin marketplace list --json`: an array of {"name", "source", ...}.
- `plugin uninstall <id> --scope user` exits 1 when the plugin is not installed at that scope.
- `plugin marketplace remove <name>` removes it from every scope and uninstalls the plugins it provided.

The plugin can also be installed without this installer: `/plugin marketplace add imgovind/agent-resurrect`
declares a GitHub marketplace with the same name, and the plugin may be installed at project or local scope, or
from another marketplace. Any installed `agent-resurrect@*` counts as installed, and only our own install (this
plugin id, user scope, from the directory marketplace at paths.program_dir()) is ever updated, enabled or removed.
A marketplace that already has our name is used as it is, never declared a second time.
"""
import json
import os
import shutil
import subprocess
from typing import List, Optional

from .. import paths
from .base import Check, Setting

PLUGIN = "agent-resurrect"
MARKETPLACE = "agent-resurrect"
PLUGIN_ID = f"{PLUGIN}@{MARKETPLACE}"
SCOPE = "user"
_TIMEOUT = 180


def _parse_json(text: str):
    """Parse a JSON document, skipping any warning lines printed before it."""
    for start, char in enumerate(text):
        if char in "[{":
            try:
                return json.loads(text[start:])
            except ValueError:
                continue
    raise ValueError("no JSON in output")


def _same_path(a: str, b: str) -> bool:
    return os.path.realpath(os.path.expanduser(a)) == os.path.realpath(os.path.expanduser(b))


def _is_local_marketplace(row: dict) -> bool:
    """Whether a `plugin marketplace list --json` row is our directory marketplace in paths.program_dir().

    A GitHub, git or URL source is never ours, and neither is a directory elsewhere (a developer's checkout).
    A directory row that names no path at all counts as ours, so that an upgrade keeps updating the plugin
    this installer added even if Claude Code's output stops including the path."""
    source = row.get("source")
    kind = source.get("source") if isinstance(source, dict) else source
    if kind not in (None, "directory"):
        return False
    places = [row.get("path"), row.get("installLocation")]
    if isinstance(source, dict):  # {"source": "directory", "path": ...}, as in known_marketplaces.json
        places.append(source.get("path"))
    places = [place for place in places if isinstance(place, str) and place]
    program_dir = str(paths.program_dir())
    return not places or any(_same_path(place, program_dir) for place in places)


class ClaudePlugin(Setting):
    """The original stored for revert() is {"installed": False, "marketplace_existed": bool} after a fresh
    install, or {"installed": True, "enabled": False} after enabling an installed but disabled plugin."""

    id = "claude.plugin"
    description = f"Install and enable the {PLUGIN} Claude Code plugin, which records each session's pane"
    skip_detail = "Claude Code (claude) is not on PATH"

    def applicable(self) -> bool:
        return shutil.which("claude") is not None

    @property
    def fix_hint(self) -> str:
        if not shutil.which("claude"):
            return f"install Claude Code, then run: {paths.command()} install"
        return f"run: {paths.command()} install"

    def _claude(self, *args: str) -> subprocess.CompletedProcess:
        executable = shutil.which("claude")
        if executable is None:
            raise RuntimeError("claude is not on PATH")
        return subprocess.run([executable, *args], capture_output=True, text=True, timeout=_TIMEOUT,
                              stdin=subprocess.DEVNULL)

    def _require(self, *args: str) -> subprocess.CompletedProcess:
        proc = self._claude(*args)
        if proc.returncode != 0:
            lines = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
            raise RuntimeError(f"`claude {' '.join(args)}` exited {proc.returncode}: {lines[-1] if lines else ''}")
        return proc

    def _installed(self) -> List[dict]:
        """Every installed agent-resurrect plugin in `claude plugin list --json`: any marketplace, any scope."""
        rows = _parse_json(self._require("plugin", "list", "--json").stdout)
        if isinstance(rows, dict):  # the --available form wraps the list
            rows = rows.get("installed", [])
        return [row for row in rows
                if isinstance(row, dict) and str(row.get("id") or "").partition("@")[0] == PLUGIN]

    def _entry(self) -> Optional[dict]:
        """This plugin's user-scope entry from our marketplace name, or None."""
        for row in self._installed():
            if row.get("id") == PLUGIN_ID and row.get("scope") == SCOPE:
                return row
        return None

    def _marketplace(self) -> Optional[dict]:
        """The configured marketplace named MARKETPLACE, whatever its source, or None."""
        rows = _parse_json(self._require("plugin", "marketplace", "list", "--json").stdout)
        for row in rows:
            if isinstance(row, dict) and row.get("name") == MARKETPLACE:
                return row
        return None

    def _has_marketplace(self) -> bool:
        return self._marketplace() is not None

    def check(self) -> Check:
        if not shutil.which("claude"):
            return Check(False, "Claude Code (claude) is not installed")
        try:
            installed = self._installed()
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            return Check(False, f"could not list Claude Code plugins: {exc}")
        if not installed:
            return Check(False, f"Claude Code plugin {PLUGIN} is not installed")
        if all(row.get("enabled") is False for row in installed):
            return Check(False, f"Claude Code plugin {PLUGIN} is installed but disabled")
        return Check(True, f"Claude Code plugin {PLUGIN} is installed and enabled")

    def apply(self) -> Optional[dict]:
        if not shutil.which("claude"):
            return None
        installed = self._installed()
        marketplace = self._marketplace()
        if installed:
            entry = self._entry()
            if entry is None or marketplace is None or not _is_local_marketplace(marketplace):
                # Installed some other way (GitHub marketplace, another scope or marketplace): not ours to touch.
                return None
            self._require("plugin", "marketplace", "update", MARKETPLACE)
            self._require("plugin", "update", PLUGIN_ID, "--scope", SCOPE)
            if entry.get("enabled") is False:
                self._require("plugin", "enable", PLUGIN_ID, "--scope", SCOPE)
                return {"installed": True, "enabled": False}
            return None
        marketplace_existed = marketplace is not None
        if not marketplace_existed:  # one of the same name (ours, or GitHub's) is used as it is
            self._require("plugin", "marketplace", "add", str(paths.program_dir()), "--scope", SCOPE)
        try:
            self._require("plugin", "install", PLUGIN_ID, "--scope", SCOPE)
        except Exception:
            if not marketplace_existed:
                self._claude("plugin", "marketplace", "remove", MARKETPLACE)
            raise
        return {"installed": False, "marketplace_existed": marketplace_existed}

    def revert(self, original: dict) -> None:
        if not shutil.which("claude"):
            return  # Claude Code itself is gone, and its plugins with it
        if original.get("installed"):
            if original.get("enabled") is False:
                self._require("plugin", "disable", PLUGIN_ID, "--scope", SCOPE)
            return
        proc = self._claude("plugin", "uninstall", PLUGIN_ID, "--scope", SCOPE)
        if proc.returncode != 0 and self._entry() is not None:
            raise RuntimeError(f"`claude plugin uninstall {PLUGIN_ID}` exited {proc.returncode}: "
                               f"{proc.stderr.strip() or proc.stdout.strip()}")
        if not original.get("marketplace_existed"):
            proc = self._claude("plugin", "marketplace", "remove", MARKETPLACE)
            if proc.returncode != 0 and self._has_marketplace():
                raise RuntimeError(f"`claude plugin marketplace remove {MARKETPLACE}` exited {proc.returncode}: "
                                   f"{proc.stderr.strip() or proc.stdout.strip()}")
