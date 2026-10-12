"""Installer ordering, manifest rules and uninstall, using in-memory settings defined here."""
import json
import os
import unittest
from typing import List, Optional

from agent_resurrect import installer, paths
from agent_resurrect.settings.base import Check, Setting
from tests.helpers import temp_home


class MemSetting(Setting):
    """A setting whose live value is `state`; apply moves it to `want` and returns {"v": previous}."""

    def __init__(self, id_: str, state: str, want: str = "on", journal: Optional[List[tuple]] = None,
                 fail_apply: Optional[str] = None, fail_revert: Optional[str] = None):
        self.id = id_
        self.description = f"set {id_}"
        self.fix_hint = f"fix {id_}"
        self.state = state
        self.want = want
        self.journal = journal if journal is not None else []
        self.fail_apply = fail_apply
        self.fail_revert = fail_revert

    def check(self) -> Check:
        return Check(self.state == self.want, f"{self.id} is {self.state}")

    def apply(self) -> Optional[dict]:
        self.journal.append(("apply", self.id))
        if self.fail_apply:
            raise RuntimeError(self.fail_apply)
        if self.state == self.want:
            return None
        original = {"v": self.state}
        self.state = self.want
        return original

    def revert(self, original: dict) -> None:
        self.journal.append(("revert", self.id, original))
        if self.fail_revert:
            raise RuntimeError(self.fail_revert)
        self.state = original["v"]


class Unavailable(MemSetting):
    """Like a setting whose tool is missing: apply changes nothing and check keeps failing."""

    def apply(self) -> Optional[dict]:
        self.journal.append(("apply", self.id))
        return None


def statuses(results):
    return [(r.id, r.status) for r in results]


class InstallerTest(unittest.TestCase):
    def test_install_records_changed_and_unchanged(self):
        with temp_home():
            results = installer.install([MemSetting("a", "orig"), MemSetting("b", "on")])
            self.assertEqual(statuses(results), [("a", "changed"), ("b", "unchanged")])
            manifest = installer.load_manifest()
            self.assertEqual(manifest["version"], 1)
            self.assertTrue(manifest["installed_at"])
            self.assertEqual(manifest["settings"], {"a": {"status": "changed", "original": {"v": "orig"}},
                                                    "b": {"status": "unchanged", "original": None}})
            on_disk = json.loads(paths.manifest_path().read_text())
            self.assertEqual(on_disk, manifest)
            self.assertEqual(sorted(p.name for p in paths.state_dir().iterdir()), ["manifest.json"])

    def test_load_manifest_defaults_when_missing_or_corrupt(self):
        with temp_home():
            self.assertEqual(installer.load_manifest()["settings"], {})
            paths.state_dir().mkdir(parents=True)
            paths.manifest_path().write_text("{not json")
            self.assertEqual(installer.load_manifest()["settings"], {})
            installer.install([MemSetting("a", "orig")])
            # The unreadable manifest is kept aside, never silently overwritten.
            self.assertEqual((paths.state_dir() / "manifest.json.corrupt").read_text(), "{not json")
            self.assertIn("a", installer.load_manifest()["settings"])

    def test_failure_is_reported_and_install_continues(self):
        with temp_home():
            results = installer.install([MemSetting("a", "x", fail_apply="boom"), MemSetting("b", "x")])
            self.assertEqual(statuses(results), [("a", "failed"), ("b", "changed")])
            self.assertIn("boom", results[0].detail)
            self.assertIn("boom", paths.log_path().read_text())
            self.assertEqual(set(installer.load_manifest()["settings"]), {"b"})

    def test_unavailable_setting_is_skipped_and_not_recorded(self):
        with temp_home():
            results = installer.install([Unavailable("a", "off")])
            self.assertEqual(statuses(results), [("a", "skipped")])
            self.assertEqual(results[0].detail, "a is off")
            self.assertEqual(installer.load_manifest()["settings"], {})

    def test_reinstall_keeps_first_originals(self):
        with temp_home():
            journal = []
            setting = MemSetting("a", "orig", journal=journal)
            installer.install([setting])
            first = installer.load_manifest()
            self.assertEqual(statuses(installer.install([setting])), [("a", "unchanged")])  # apply returns None
            self.assertEqual(installer.load_manifest()["settings"]["a"],
                             {"status": "changed", "original": {"v": "orig"}})
            setting.state = "drifted"  # something changed it back; the next install returns a new original
            self.assertEqual(statuses(installer.install([setting])), [("a", "changed")])
            self.assertEqual(installer.load_manifest()["settings"]["a"]["original"], {"v": "orig"})
            self.assertEqual(installer.load_manifest()["installed_at"], first["installed_at"])

            results = installer.uninstall([setting])
            self.assertEqual(journal[-1], ("revert", "a", {"v": "orig"}))
            self.assertEqual(setting.state, "orig")
            self.assertIn(("a", "reverted"), statuses(results))

    def test_uninstall_reverses_order_and_removes_files(self):
        with temp_home():
            journal = []
            a = MemSetting("a", "x", journal=journal)
            b = MemSetting("b", "y", journal=journal)
            c = MemSetting("c", "on", journal=journal)
            installer.install([a, b, c])
            data = paths.data_dir()
            (data / "src").mkdir(parents=True)
            bin_dir = paths.bin_dir()
            bin_dir.mkdir(parents=True)
            (bin_dir / "agent-resurrect").write_text("#!/bin/sh\n")
            (bin_dir / "resurrect").symlink_to("agent-resurrect")
            (bin_dir / "other-tool").write_text("keep")
            paths.config_dir().mkdir(parents=True)
            paths.config_path().write_text("[agent-resurrect]\n")
            paths.panes_dir().mkdir(parents=True)
            (paths.panes_dir() / "iterm-x.json").write_text("{}")

            journal.clear()
            results = installer.uninstall([a, b, c])
            self.assertEqual([entry[:2] for entry in journal], [("revert", "b"), ("revert", "a")])
            self.assertEqual((a.state, b.state, c.state), ("x", "y", "on"))
            self.assertEqual(statuses(results)[:3], [("c", "unchanged"), ("b", "reverted"), ("a", "reverted")])
            self.assertFalse(data.exists())
            self.assertFalse(os.path.lexists(bin_dir / "agent-resurrect"))
            self.assertFalse(os.path.lexists(bin_dir / "resurrect"))
            self.assertTrue((bin_dir / "other-tool").exists())
            self.assertFalse(paths.manifest_path().exists())
            self.assertTrue((paths.panes_dir() / "iterm-x.json").exists())
            self.assertTrue(paths.config_path().exists())

            installer.install([a])
            installer.uninstall([a], purge=True)
            self.assertFalse(paths.state_dir().exists())
            self.assertFalse(paths.config_dir().exists())

    def test_uninstall_keeps_a_resurrect_that_is_not_ours(self):
        with temp_home():
            bin_dir = paths.bin_dir()
            bin_dir.mkdir(parents=True)
            (bin_dir / "resurrect").write_text("someone else's tool")
            installer.uninstall([])
            self.assertEqual((bin_dir / "resurrect").read_text(), "someone else's tool")
            (bin_dir / "resurrect").unlink()
            (bin_dir / "resurrect").symlink_to("/usr/bin/true")
            installer.uninstall([])
            self.assertTrue((bin_dir / "resurrect").is_symlink())

    def test_failed_revert_keeps_its_manifest_entry(self):
        with temp_home():
            a = MemSetting("a", "x")
            b = MemSetting("b", "y", fail_revert="stuck")
            installer.install([a, b])
            results = installer.uninstall([a, b], purge=True)
            self.assertEqual(statuses(results)[:2], [("b", "failed"), ("a", "reverted")])
            self.assertIn("stuck", results[0].detail)
            manifest = installer.load_manifest()
            self.assertEqual(set(manifest["settings"]), {"b"})
            self.assertTrue(paths.manifest_path().exists())  # kept even with purge, so a retry can revert b
            b.fail_revert = None
            installer.uninstall([a, b])
            self.assertEqual(b.state, "y")
            self.assertFalse(paths.manifest_path().exists())


if __name__ == "__main__":
    unittest.main()
