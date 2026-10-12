"""Every place that names the version agrees, so a release cannot ship half bumped.

The release workflow checks the same files against the git tag when it publishes; this test catches a partial
bump in a pull request, before anything is tagged. Claude Code only updates an installed plugin when the version
in its manifest changes, so a missed manifest means users never receive the release.
"""
import json
import re
import unittest
from pathlib import Path
from typing import Dict, Optional

from agent_resurrect import __version__

REPO = Path(__file__).resolve().parent.parent
PLUGIN_MANIFEST = REPO / ".claude-plugin" / "plugin.json"
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"
INSTALLER = REPO / "install.sh"
CHANGELOG = REPO / "CHANGELOG.md"


def plugin_version() -> Optional[str]:
    if not PLUGIN_MANIFEST.is_file():
        return None
    return json.loads(PLUGIN_MANIFEST.read_text()).get("version")


def marketplace_version() -> Optional[str]:
    entries = [entry for entry in json.loads(MARKETPLACE.read_text()).get("plugins", [])
               if entry.get("name") == "agent-resurrect"]
    return entries[0].get("version") if len(entries) == 1 else None


def installer_version() -> Optional[str]:
    match = re.search(r'^DEFAULT_REF="v([^"]+)"$', INSTALLER.read_text(), re.M)
    return match.group(1) if match else None


def changelog_version() -> Optional[str]:
    """The newest released entry: the first `## [x.y.z]` heading, skipping `## [Unreleased]`."""
    match = re.search(r"^## \[(\d[^\]]*)\]", CHANGELOG.read_text(), re.M)
    return match.group(1) if match else None


def versions() -> Dict[str, Optional[str]]:
    return {
        "src/agent_resurrect/__init__.py": __version__,
        ".claude-plugin/plugin.json": plugin_version(),
        ".claude-plugin/marketplace.json (agent-resurrect entry)": marketplace_version(),
        "install.sh DEFAULT_REF": installer_version(),
        "CHANGELOG.md newest entry": changelog_version(),
    }


class VersionTest(unittest.TestCase):
    def test_every_place_names_the_same_version(self):
        found = versions()
        self.assertEqual(found, {place: __version__ for place in found})

    def test_changelog_entries_are_dated_and_linked(self):
        text = CHANGELOG.read_text()
        released = re.findall(r"^## \[(\d[^\]]*)\] - (\S+)$", text, re.M)
        self.assertTrue(released)
        for version, date in released:
            with self.subTest(version):
                self.assertRegex(date, r"^\d{4}-\d{2}-\d{2}$")
                self.assertRegex(text, rf"(?m)^\[{re.escape(version)}\]: https://github\.com/imgovind/agent-resurrect/")
        self.assertRegex(text, r"(?m)^## \[Unreleased\]$")


if __name__ == "__main__":
    unittest.main()
