"""Tests for the foundation every adapter builds on: model, store, procs, config, paths."""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from agent_resurrect import config, paths, store
from agent_resurrect.model import STATUS_CONSUMED, Record, utc_now_iso
from agent_resurrect.procs import Procs
from tests.helpers import FakeProcs, make_record, proc, temp_home


class ModelTest(unittest.TestCase):
    def test_round_trip(self):
        record = make_record()
        self.assertEqual(Record.from_dict(record.to_dict()), record)

    def test_rejects_other_schema(self):
        data = make_record().to_dict()
        data["schema"] = 2
        with self.assertRaises(ValueError):
            Record.from_dict(data)

    def test_ignores_unknown_keys_and_rejects_missing_required(self):
        data = make_record().to_dict()
        data["future_field"] = 1
        self.assertEqual(Record.from_dict(data).session_id, "s-1")
        del data["session_id"]
        with self.assertRaises(ValueError):
            Record.from_dict(data)

    def test_rejects_unknown_status(self):
        data = make_record(status="zombie").to_dict()
        with self.assertRaises(ValueError):
            Record.from_dict(data)


class StoreTest(unittest.TestCase):
    def test_save_load_delete(self):
        with temp_home():
            saved = store.save(make_record())
            self.assertTrue(saved.recorded_at and saved.updated_at)
            self.assertEqual(store.load("fake-p1").session_id, "s-1")
            self.assertTrue(store.delete("fake-p1"))
            self.assertIsNone(store.load("fake-p1"))
            self.assertFalse(store.delete("fake-p1"))

    def test_save_is_atomic_and_leaves_no_temp_files(self):
        with temp_home():
            store.save(make_record())
            store.save(make_record(session_id="s-2"))
            self.assertEqual(sorted(p.name for p in paths.panes_dir().iterdir()), ["fake-p1.json"])
            self.assertEqual(store.load("fake-p1").session_id, "s-2")

    def test_recorded_at_survives_updates(self):
        with temp_home():
            first = store.save(make_record()).recorded_at
            record = store.load("fake-p1")
            record.recorded_at = "2026-01-01T00:00:00Z"
            store.save(record)
            self.assertEqual(store.load("fake-p1").recorded_at, "2026-01-01T00:00:00Z")
            self.assertTrue(first)

    def test_pane_keys_cannot_escape_the_panes_directory(self):
        for bad in ["", "../x", "a/b", ".hidden", "a..b", "x" * 200, "has space"]:
            self.assertFalse(store.valid_pane_key(bad), bad)
            with self.assertRaises(ValueError):
                store.record_path(bad)
        self.assertTrue(store.valid_pane_key("iterm-10FE02F5-C525-405E-9E86-C8255ED17032"))

    def test_corrupt_record_is_ignored_not_deleted(self):
        with temp_home():
            paths.panes_dir().mkdir(parents=True)
            corrupt = paths.panes_dir() / "fake-p9.json"
            corrupt.write_text("{not json")
            self.assertIsNone(store.load("fake-p9"))
            self.assertEqual(store.list_records(), [])
            self.assertTrue(corrupt.exists())
            self.assertIn("unreadable record", paths.log_path().read_text())

    def test_list_is_newest_first(self):
        with temp_home():
            for key, stamp in [("fake-a", "2026-10-01T00:00:00Z"), ("fake-b", "2026-10-03T00:00:00Z"),
                               ("fake-c", "2026-10-02T00:00:00Z")]:
                store.save(make_record(pane_key=key))
                data = json.loads(store.record_path(key).read_text())
                data["updated_at"] = stamp
                store.record_path(key).write_text(json.dumps(data))
            self.assertEqual([r.pane_key for r in store.list_records()], ["fake-b", "fake-c", "fake-a"])

    def test_gc_removes_only_old_records(self):
        with temp_home():
            store.save(make_record(pane_key="fake-new"))
            store.save(make_record(pane_key="fake-old"))
            old = json.loads(store.record_path("fake-old").read_text())
            old["updated_at"] = (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
            store.record_path("fake-old").write_text(json.dumps(old))
            self.assertEqual(store.gc(14), ["fake-old"])
            self.assertEqual([r.pane_key for r in store.list_records()], ["fake-new"])

    def test_set_status(self):
        with temp_home():
            store.save(make_record())
            self.assertEqual(store.set_status("fake-p1", STATUS_CONSUMED).status, STATUS_CONSUMED)
            self.assertIsNone(store.set_status("fake-none", STATUS_CONSUMED))

    def test_restore_events(self):
        with temp_home():
            store.append_restore_event("fake-p1", "resumed", "s-1")
            with paths.restore_events_path().open("a") as fh:
                fh.write("garbage\n")
            events = store.read_restore_events()
            self.assertEqual([(e["pane_key"], e["result"]) for e in events], [("fake-p1", "resumed")])


class ProcsTest(unittest.TestCase):
    def fake_ps(self, rows):
        def run(argv):
            pid = int(argv[-1])
            if pid not in rows:
                return ""
            line, args = rows[pid]
            return args + "\n" if "args=" in argv else line + "\n"
        return run

    def test_parses_ps_output_including_single_digit_days(self):
        rows = {92528: ("92528 10141 S+   ttys029  Mon Oct  5 11:40:02 2026     claude", "claude --resume x")}
        info = Procs(run=self.fake_ps(rows)).get(92528)
        self.assertEqual((info.pid, info.ppid, info.stat, info.tty), (92528, 10141, "S+", "ttys029"))
        self.assertEqual(info.started, "Mon Oct 5 11:40:02 2026")
        self.assertEqual((info.comm, info.name, info.args), ("claude", "claude", "claude --resume x"))
        self.assertTrue(info.foreground and info.has_tty)

    def test_login_shell_and_path_names(self):
        self.assertEqual(proc(1, 0, "-zsh").name, "zsh")
        self.assertEqual(proc(1, 0, "/bin/zsh").name, "zsh")
        self.assertFalse(proc(1, 0, "x", tty="??").has_tty)

    def test_missing_or_garbled_process(self):
        procs = Procs(run=self.fake_ps({7: ("garbage", "")}))
        self.assertIsNone(procs.get(7))
        self.assertIsNone(procs.get(8))

    def test_ancestors_stop_before_launchd_and_survive_cycles(self):
        fake = FakeProcs(proc(30, 20, "python3"), proc(20, 10, "claude"), proc(10, 1, "-zsh"))
        self.assertEqual([p.pid for p in fake.ancestors(30)], [30, 20, 10])
        looped = FakeProcs(proc(5, 6, "a"), proc(6, 5, "b"))
        self.assertEqual([p.pid for p in looped.ancestors(5)], [5, 6])

    def test_is_alive_requires_matching_start_time(self):
        fake = FakeProcs(proc(42, 1, "claude", started="Mon Oct 12 11:40:02 2026"))
        self.assertTrue(fake.is_alive(42, "Mon Oct 12 11:40:02 2026"))
        self.assertFalse(fake.is_alive(42, "Sun Oct 11 09:00:00 2026"))  # pid reused after reboot
        self.assertFalse(fake.is_alive(43, None))
        self.assertFalse(fake.is_alive(None, None))
        self.assertTrue(fake.is_alive(42, None))

    def test_real_ps_sees_this_process(self):
        info = Procs().get(os.getpid())
        self.assertIsNotNone(info)
        self.assertEqual(info.pid, os.getpid())
        self.assertTrue(Procs().is_alive(os.getpid(), info.started))


class ConfigTest(unittest.TestCase):
    def test_defaults_when_missing(self):
        with temp_home():
            self.assertEqual(config.load_config(), config.Config())

    def test_valid_values_and_invalid_fallback(self):
        with temp_home():
            paths.config_dir().mkdir(parents=True)
            paths.config_path().write_text(
                "[agent-resurrect]\nauto_resume = ask\ncountdown_seconds = 7\n"
                "permission_flag = sometimes\nmax_age_days = -3\n")
            cfg = config.load_config()
            self.assertEqual((cfg.auto_resume, cfg.countdown_seconds), ("ask", 7))
            self.assertEqual((cfg.permission_flag, cfg.max_age_days), ("mirror", 14))

    def test_set_value_validates_and_persists(self):
        with temp_home():
            config.set_value("permission_flag", "always")
            self.assertEqual(config.load_config().permission_flag, "always")
            with self.assertRaises(ValueError):
                config.set_value("auto_resume", "sometimes")
            with self.assertRaises(KeyError):
                config.set_value("nope", "1")


class PathsTest(unittest.TestCase):
    def test_xdg_overrides_and_defaults(self):
        with temp_home() as home:
            self.assertEqual(paths.state_dir(), home / ".local/state/agent-resurrect")
            os.environ["XDG_STATE_HOME"] = str(home / "st")
            self.assertEqual(paths.panes_dir(), home / "st/agent-resurrect/panes")
            self.assertEqual(paths.claude_config_dir(), home / ".claude")
            self.assertTrue(utc_now_iso().endswith("Z"))


if __name__ == "__main__":
    unittest.main()
