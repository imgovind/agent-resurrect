"""Installer ordering, manifest rules, dry runs and uninstall, using in-memory settings defined here."""
import json
import os
import stat
import unittest
from pathlib import Path
from typing import List, Optional

from agent_resurrect import installer, paths
from agent_resurrect.settings.base import Check, Setting
from tests.helpers import temp_home


class MemSetting(Setting):
    """A setting whose live value is `state`; apply moves it to `want` and returns {"v": previous}."""

    def __init__(self, id_: str, state: str, want: str = "on", journal: Optional[List[tuple]] = None,
                 fail_apply: Optional[str] = None, fail_revert: Optional[str] = None, fail_check: Optional[str] = None,
                 usable: bool = True):
        self.id = id_
        self.description = f"set {id_}"
        self.fix_hint = f"fix {id_}"
        self.state = state
        self.want = want
        self.journal = journal if journal is not None else []
        self.fail_apply = fail_apply
        self.fail_revert = fail_revert
        self.fail_check = fail_check
        self.usable = usable

    def applicable(self) -> bool:
        return self.usable

    @property
    def skip_detail(self) -> str:
        return f"{self.id} cannot be used here"

    def check(self) -> Check:
        self.journal.append(("check", self.id))
        if self.fail_check:
            raise RuntimeError(self.fail_check)
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


def tree(root: Path) -> dict:
    """Every path under root with its bytes (files), link target (symlinks) or None (dirs), and its mode."""
    snapshot = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if path.is_symlink():
            content = ("link", os.readlink(path))
        elif path.is_dir():
            content = ("dir", None)
        else:
            content = ("file", path.read_bytes())
        snapshot[str(path.relative_to(root))] = (content, stat.S_IMODE(info.st_mode))
    return snapshot


def install_files() -> None:
    """What install.sh puts in place: the data dir, the launcher, its alias and the bytecode cache."""
    (paths.data_dir() / "src").mkdir(parents=True, exist_ok=True)
    paths.bin_dir().mkdir(parents=True, exist_ok=True)
    (paths.bin_dir() / "agent-resurrect").write_text("#!/bin/sh\n")
    if not os.path.lexists(paths.bin_dir() / "resurrect"):
        (paths.bin_dir() / "resurrect").symlink_to("agent-resurrect")
    (paths.cache_dir() / "pycache").mkdir(parents=True, exist_ok=True)


def write_manifest(text: str) -> None:
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    paths.manifest_path().write_text(text)


class InstallerTest(unittest.TestCase):
    def test_install_records_changed_and_unchanged(self):
        with temp_home():
            results = installer.install([MemSetting("a", "orig"), MemSetting("b", "on")])
            self.assertEqual(statuses(results), [("a", "changed"), ("b", "unchanged")])
            self.assertEqual([r.description for r in results], ["set a", "set b"])
            manifest = installer.load_manifest()
            self.assertEqual(manifest["version"], 1)
            self.assertTrue(manifest["installed_at"])
            self.assertEqual(manifest["settings"], {"a": {"status": "changed", "original": {"v": "orig"}},
                                                    "b": {"status": "unchanged", "original": None}})
            on_disk = json.loads(paths.manifest_path().read_text())
            self.assertEqual(on_disk, manifest)
            self.assertEqual(sorted(p.name for p in paths.state_dir().iterdir()), ["manifest.json"])
            # It holds the original values of the user's settings: private to the user.
            self.assertEqual(stat.S_IMODE(paths.manifest_path().stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(paths.state_dir().stat().st_mode), 0o700)

    def test_status_values(self):
        self.assertEqual(installer.STATUSES, ("changed", "unchanged", "skipped", "failed", "reverted",
                                              "would-change", "would-revert"))
        self.assertEqual((installer.WOULD_CHANGE, installer.WOULD_REVERT), ("would-change", "would-revert"))
        self.assertEqual(installer.StepResult("x", "changed"), installer.StepResult("x", "changed", "", ""))

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

    def test_setting_that_is_not_applicable_is_skipped_without_applying(self):
        with temp_home():
            journal = []
            results = installer.install([MemSetting("a", "off", journal=journal, usable=False)])
            self.assertEqual(statuses(results), [("a", "skipped")])
            self.assertEqual(results[0].detail, "a cannot be used here")
            self.assertEqual(journal, [])
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
            bin_dir = paths.bin_dir()
            install_files()
            (bin_dir / "other-tool").write_text("keep")
            paths.config_dir().mkdir(parents=True)
            paths.config_path().write_text("[agent-resurrect]\n")
            paths.panes_dir().mkdir(parents=True)
            (paths.panes_dir() / "iterm-x.json").write_text("{}")
            cache = paths.home() / ".cache" / "agent-resurrect"
            self.assertEqual(paths.cache_dir(), cache)
            (cache / "pycache" / "usr").mkdir(parents=True)  # the launcher's -X pycache_prefix tree
            (paths.home() / ".cache" / "other-app").mkdir()

            journal.clear()
            results = installer.uninstall([a, b, c])
            self.assertEqual([entry[:2] for entry in journal], [("revert", "b"), ("revert", "a")])
            self.assertEqual((a.state, b.state, c.state), ("x", "y", "on"))
            self.assertEqual(statuses(results), [("c", "unchanged"), ("b", "reverted"), ("a", "reverted"),
                                                 ("files", "reverted")])
            self.assertEqual(results[-1].description, "Remove agent-resurrect's program files and install manifest")
            self.assertFalse(data.exists())
            self.assertFalse(os.path.lexists(bin_dir / "agent-resurrect"))
            self.assertFalse(os.path.lexists(bin_dir / "resurrect"))
            self.assertTrue((bin_dir / "other-tool").exists())
            self.assertFalse(cache.exists())  # program files: removed without --purge
            self.assertTrue((paths.home() / ".cache" / "other-app").is_dir())
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

    def test_uninstall_without_a_manifest_removes_the_files(self):
        with temp_home():
            install_files()
            results = installer.uninstall([MemSetting("a", "x")])
            self.assertEqual(statuses(results), [("a", "skipped"), ("files", "reverted")])
            self.assertFalse(paths.data_dir().exists())


class FailedRevertTest(unittest.TestCase):
    """M1: a failed revert keeps everything a retry needs: the launcher, the code, and the manifest entry."""

    def test_failed_revert_keeps_its_manifest_entry_and_the_files(self):
        with temp_home():
            a = MemSetting("a", "x")
            b = MemSetting("b", "y", fail_revert="stuck")
            installer.install([a, b])
            install_files()
            paths.config_dir().mkdir(parents=True)
            paths.config_path().write_text("[agent-resurrect]\n")
            results = installer.uninstall([a, b], purge=True)
            self.assertEqual(statuses(results), [("b", "failed"), ("a", "reverted"), ("files", "skipped")])
            self.assertIn("stuck", results[0].detail)
            self.assertIn("run: ~/.local/bin/agent-resurrect uninstall", results[-1].detail)
            manifest = installer.load_manifest()
            self.assertEqual(set(manifest["settings"]), {"b"})
            # Kept even with purge, so a retry can revert b.
            self.assertTrue(paths.manifest_path().exists())
            self.assertTrue((paths.bin_dir() / "agent-resurrect").exists())
            self.assertTrue((paths.bin_dir() / "resurrect").is_symlink())
            self.assertTrue((paths.data_dir() / "src").is_dir())
            self.assertTrue(paths.cache_dir().is_dir())
            self.assertTrue(paths.config_path().exists())

            b.fail_revert = None
            results = installer.uninstall([a, b])
            self.assertEqual(statuses(results), [("b", "reverted"), ("a", "skipped"), ("files", "reverted")])
            self.assertEqual(b.state, "y")
            self.assertFalse(paths.manifest_path().exists())
            self.assertFalse(os.path.lexists(paths.bin_dir() / "agent-resurrect"))
            self.assertFalse(paths.data_dir().exists())

    def test_changed_entry_without_a_setting_fails_and_keeps_everything(self):
        with temp_home():
            installer.install([MemSetting("a", "x"), MemSetting("future.thing", "x")])
            install_files()
            results = installer.uninstall([MemSetting("a", "on")])
            self.assertEqual(statuses(results), [("a", "reverted"), ("future.thing", "failed"), ("files", "skipped")])
            self.assertIn("upgrade agent-resurrect", results[1].detail)
            self.assertEqual(set(installer.load_manifest()["settings"]), {"future.thing"})
            self.assertTrue((paths.bin_dir() / "agent-resurrect").exists())

    def test_unchanged_entry_without_a_setting_is_dropped(self):
        with temp_home():
            installer.install([MemSetting("gone", "on")])
            results = installer.uninstall([])
            self.assertEqual(statuses(results), [("files", "reverted")])
            self.assertFalse(paths.manifest_path().exists())


class BadManifestTest(unittest.TestCase):
    """H3: a manifest that exists but cannot be used makes install and uninstall refuse, changing nothing."""

    BAD = {
        "corrupt": ("{not json", "is not valid JSON"),
        "newer": (json.dumps({"version": 2, "installed_at": None, "settings": {}}),
                  "written by a newer agent-resurrect (manifest version 2); upgrade agent-resurrect first"),
        "unknown": (json.dumps({"version": 1, "settings": []}), "has an unknown format"),
        "bad entry": (json.dumps({"version": 1, "settings": {"a": "changed"}}), "has an unknown format"),
        "not an object": ("[1]", "has an unknown format"),
        "no version": (json.dumps({"settings": {}}), "has an unknown format"),
    }

    def assert_refused(self, results, reason: str, verb: str):
        self.assertEqual(statuses(results), [("manifest", "failed")])
        self.assertIn(str(paths.manifest_path()), results[0].detail)
        self.assertIn(reason, results[0].detail)
        self.assertIn(f"nothing was {verb}", results[0].detail)

    def test_uninstall_refuses_and_deletes_nothing(self):
        for name, (text, reason) in self.BAD.items():
            with self.subTest(name), temp_home() as home:
                write_manifest(text)
                install_files()
                journal = []
                before = tree(home)
                results = installer.uninstall([MemSetting("a", "x", journal=journal)], purge=True)
                self.assert_refused(results, reason, "reverted or deleted")
                self.assertEqual(journal, [])
                after = tree(home)
                after.pop(str(paths.log_path().relative_to(home)), None)  # the reason is logged
                self.assertEqual(after, before)
                self.assertIn(reason, paths.log_path().read_text())

    def test_uninstall_refuses_an_unreadable_manifest(self):
        if os.geteuid() == 0:
            self.skipTest("root can read any file")
        with temp_home():
            write_manifest(json.dumps({"version": 1, "installed_at": None, "settings": {}}))
            install_files()
            paths.manifest_path().chmod(0)
            try:
                results = installer.uninstall([MemSetting("a", "x")])
            finally:
                paths.manifest_path().chmod(0o600)
            self.assert_refused(results, "could not be read", "reverted or deleted")
            self.assertTrue((paths.bin_dir() / "agent-resurrect").exists())

    def test_install_refuses_and_changes_nothing(self):
        for name, (text, reason) in self.BAD.items():
            with self.subTest(name), temp_home():
                write_manifest(text)
                journal = []
                setting = MemSetting("a", "x", journal=journal)
                results = installer.install([setting])
                self.assert_refused(results, reason, "changed")
                self.assertEqual(journal, [])
                self.assertEqual(setting.state, "x")
                # Never set aside or overwritten: it may hold the only copy of the user's original values.
                self.assertEqual(paths.manifest_path().read_text(), text)
                self.assertFalse((paths.state_dir() / "manifest.json.corrupt").exists())

    def test_dry_runs_report_the_bad_manifest_without_logging(self):
        with temp_home() as home:
            write_manifest("{not json")
            before = tree(home)
            self.assertEqual(statuses(installer.install([MemSetting("a", "x")], dry_run=True)),
                             [("manifest", "failed")])
            self.assertEqual(statuses(installer.uninstall([MemSetting("a", "x")], dry_run=True)),
                             [("manifest", "failed")])
            self.assertEqual(tree(home), before)

    def test_load_manifest_defaults_when_missing_or_corrupt(self):
        with temp_home():
            self.assertEqual(installer.load_manifest()["settings"], {})
            write_manifest("{not json")
            self.assertEqual(installer.load_manifest()["settings"], {})
            with self.assertRaises(installer.ManifestError):
                installer.read_manifest()


class DryRunTest(unittest.TestCase):
    def test_install_dry_run_writes_nothing(self):
        with temp_home() as home:
            journal = []
            settings = [MemSetting("a", "x", journal=journal), MemSetting("b", "on", journal=journal),
                        MemSetting("c", "x", journal=journal, usable=False),
                        MemSetting("d", "x", journal=journal, fail_check="no tool")]
            before = tree(home)
            results = installer.install(settings, dry_run=True)
            self.assertEqual(statuses(results), [("a", "would-change"), ("b", "unchanged"), ("c", "skipped"),
                                                 ("d", "failed")])
            self.assertEqual([r.detail for r in results], ["set a", "b is on", "c cannot be used here", "no tool"])
            self.assertEqual([r.description for r in results], ["set a", "set b", "set c", "set d"])
            self.assertNotIn("apply", [entry[0] for entry in journal])
            self.assertEqual([s.state for s in settings], ["x", "on", "x", "x"])
            self.assertEqual(tree(home), before)  # no manifest, no state dir, no log

    def test_install_dry_run_after_install(self):
        with temp_home() as home:
            a = MemSetting("a", "x")
            installer.install([a])
            before = tree(home)
            self.assertEqual(statuses(installer.install([a], dry_run=True)), [("a", "unchanged")])
            a.state = "drifted"
            self.assertEqual(statuses(installer.install([a], dry_run=True)), [("a", "would-change")])
            self.assertEqual(tree(home), before)

    def test_uninstall_dry_run_writes_nothing(self):
        with temp_home() as home:
            journal = []
            a = MemSetting("a", "x", journal=journal)
            b = MemSetting("b", "on", journal=journal)
            c = MemSetting("c", "x", journal=journal)
            installer.install([a, b])
            install_files()
            journal.clear()
            before = tree(home)
            results = installer.uninstall([a, b, c], dry_run=True)
            self.assertEqual(statuses(results), [("c", "skipped"), ("b", "unchanged"), ("a", "would-revert"),
                                                 ("files", "would-revert")])
            files = results[-1].detail
            for path in (paths.data_dir(), paths.bin_dir() / "agent-resurrect", paths.bin_dir() / "resurrect",
                         paths.manifest_path(), paths.cache_dir()):
                self.assertIn(str(path), files)
            self.assertNotIn(str(paths.state_dir()) + ",", files)
            self.assertEqual(journal, [])
            self.assertEqual(tree(home), before)

            purge = installer.uninstall([a, b, c], purge=True, dry_run=True)[-1]
            self.assertIn(str(paths.state_dir()), purge.detail)
            self.assertEqual(tree(home), before)

    def test_uninstall_dry_run_with_nothing_installed(self):
        with temp_home() as home:
            results = installer.uninstall([MemSetting("a", "x")], dry_run=True)
            self.assertEqual(statuses(results), [("a", "skipped"), ("files", "unchanged")])
            self.assertEqual(list(home.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
