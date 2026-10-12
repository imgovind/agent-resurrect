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
"""
import json
import shutil
import subprocess
from typing import Optional, Set

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


class ClaudePlugin(Setting):
    """The original stored for revert() is {"installed": False, "marketplace_existed": bool} after a fresh
    install, or {"installed": True, "enabled": False} after enabling an installed but disabled plugin."""

    id = "claude.plugin"
    description = f"Install and enable the {PLUGIN} Claude Code plugin, which records each session's pane"

    @property
    def fix_hint(self) -> str:
        if not shutil.which("claude"):
            return "install Claude Code, then run: ~/.local/bin/agent-resurrect install"
        return "run: ~/.local/bin/agent-resurrect install"

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

    def _entry(self) -> Optional[dict]:
        """This plugin's user-scope entry in `claude plugin list --json`, or None."""
        rows = _parse_json(self._require("plugin", "list", "--json").stdout)
        if isinstance(rows, dict):  # the --available form wraps the list
            rows = rows.get("installed", [])
        for row in rows:
            if isinstance(row, dict) and row.get("id") == PLUGIN_ID and row.get("scope") == SCOPE:
                return row
        return None

    def _marketplaces(self) -> Set[str]:
        rows = _parse_json(self._require("plugin", "marketplace", "list", "--json").stdout)
        return {row.get("name") for row in rows if isinstance(row, dict)}

    def check(self) -> Check:
        if not shutil.which("claude"):
            return Check(False, "Claude Code (claude) is not installed")
        try:
            entry = self._entry()
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            return Check(False, f"could not list Claude Code plugins: {exc}")
        if entry is None:
            return Check(False, f"Claude Code plugin {PLUGIN} is not installed")
        if entry.get("enabled") is False:
            return Check(False, f"Claude Code plugin {PLUGIN} is installed but disabled")
        return Check(True, f"Claude Code plugin {PLUGIN} is installed and enabled")

    def apply(self) -> Optional[dict]:
        if not shutil.which("claude"):
            return None
        entry = self._entry()
        if entry is not None:
            self._require("plugin", "marketplace", "update", MARKETPLACE)
            self._require("plugin", "update", PLUGIN_ID, "--scope", SCOPE)
            if entry.get("enabled") is False:
                self._require("plugin", "enable", PLUGIN_ID, "--scope", SCOPE)
                return {"installed": True, "enabled": False}
            return None
        marketplace_existed = MARKETPLACE in self._marketplaces()
        self._require("plugin", "marketplace", "add", str(paths.data_dir()), "--scope", SCOPE)
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
            if proc.returncode != 0 and MARKETPLACE in self._marketplaces():
                raise RuntimeError(f"`claude plugin marketplace remove {MARKETPLACE}` exited {proc.returncode}: "
                                   f"{proc.stderr.strip() or proc.stdout.strip()}")
