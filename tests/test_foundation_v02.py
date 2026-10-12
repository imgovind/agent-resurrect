"""Foundation contracts added in 0.2.0: atomic claim, gc of running sessions, private state, resume_env,
C-locale ps, exit codes and the --json envelope."""
import io
import json
import os
import re
import stat
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agent_resurrect import exitcodes, jsonout, paths, procs, store
from agent_resurrect.model import STATUS_CONSUMED, STATUS_SKIPPED, Record
from agent_resurrect.settings.base import Check, Setting
from tests.helpers import FakeAgent, make_record, temp_home

ROOT = Path(__file__).resolve().parent.parent


class ClaimTest(unittest.TestCase):
    def test_claim_marks_consumed_once(self):
        with temp_home():
            store.save(make_record())
            first = store.claim("fake-p1")
            self.assertEqual(first.status, STATUS_CONSUMED)
            self.assertEqual(store.load("fake-p1").status, STATUS_CONSUMED)
            self.assertIsNone(store.claim("fake-p1"))  # no longer live

    def test_claim_leaves_non_live_and_absent_records_untouched(self):
        with temp_home():
            self.assertIsNone(store.claim("fake-none"))
            store.save(make_record(status=STATUS_SKIPPED))
            before = store.record_path("fake-p1").read_bytes()
            self.assertIsNone(store.claim("fake-p1"))
            self.assertEqual(store.record_path("fake-p1").read_bytes(), before)

    def test_corrupt_record_is_put_back(self):
        with temp_home():
            paths.panes_dir().mkdir(parents=True)
            store.record_path("fake-p1").write_text("{broken")
            self.assertIsNone(store.claim("fake-p1"))
            self.assertEqual(store.record_path("fake-p1").read_text(), "{broken")
            self.assertEqual(sorted(p.name for p in paths.panes_dir().iterdir()), ["fake-p1.json"])

    def test_exactly_one_of_many_racing_processes_wins(self):
        with temp_home() as home:
            store.save(make_record())
            code = ("import sys; sys.path.insert(0, %r); from agent_resurrect import store; "
                    "print('WON' if store.claim('fake-p1') else 'LOST')" % str(ROOT / "src"))
            env = dict(os.environ, HOME=str(home))
            racers = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=env)
                      for _ in range(8)]
            results = [p.communicate(timeout=30)[0].strip() for p in racers]
            self.assertEqual(results.count("WON"), 1, results)
            self.assertEqual(store.load("fake-p1").status, STATUS_CONSUMED)
            self.assertEqual([p.name for p in paths.panes_dir().iterdir()], ["fake-p1.json"])  # no claim files left


class GcTest(unittest.TestCase):
    def test_gc_keeps_old_records_of_running_agents(self):
        with temp_home():
            for key in ("fake-running", "fake-dead"):
                store.save(make_record(pane_key=key))
                data = json.loads(store.record_path(key).read_text())
                data["updated_at"] = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
                store.record_path(key).write_text(json.dumps(data))
            removed = store.gc(14, is_running=lambda r: r.pane_key == "fake-running")
            self.assertEqual(removed, ["fake-dead"])
            self.assertIsNotNone(store.load("fake-running"))


class PrivateStateTest(unittest.TestCase):
    def test_state_and_panes_dirs_are_owner_only(self):
        with temp_home():
            store.save(make_record())
            for directory in (paths.state_dir(), paths.panes_dir()):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, directory)


class ResumeEnvTest(unittest.TestCase):
    def test_round_trip_and_backwards_compatible(self):
        record = make_record(resume_env={"CLAUDE_CONFIG_DIR": "/Users/me/.claude-work"})
        self.assertEqual(Record.from_dict(record.to_dict()).resume_env, {"CLAUDE_CONFIG_DIR": "/Users/me/.claude-work"})
        old = make_record().to_dict()
        del old["resume_env"]  # a 0.1.0 record
        self.assertIsNone(Record.from_dict(old).resume_env)


class PsLocaleTest(unittest.TestCase):
    def test_ps_always_runs_in_the_c_locale(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs.get("env") or {})
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.dict(os.environ, {"LC_ALL": "ja_JP.UTF-8", "LANG": "fr_FR.UTF-8"}), \
                mock.patch.object(procs.subprocess, "run", side_effect=fake_run):
            procs._run_ps(["-p", "1"])
        self.assertEqual((seen.get("LC_ALL"), seen.get("LANG")), ("C", "C"))

    def test_real_ps_start_time_is_english_under_a_foreign_locale(self):
        with mock.patch.dict(os.environ, {"LC_ALL": "ja_JP.UTF-8", "LANG": "ja_JP.UTF-8"}):
            info = procs.Procs().get(os.getpid())
        self.assertRegex(info.started, r"^[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2} \d\d:\d\d:\d\d \d{4}$")


class ContractsTest(unittest.TestCase):
    def test_exit_codes_are_stable(self):
        self.assertEqual((exitcodes.OK, exitcodes.ERROR, exitcodes.USAGE, exitcodes.NOT_FOUND, exitcodes.NEEDS_TTY,
                          exitcodes.BUSY, exitcodes.GONE, exitcodes.INTERRUPTED), (0, 1, 2, 3, 4, 5, 6, 130))
        self.assertEqual(exitcodes.name(4), "needs-tty")
        self.assertEqual(exitcodes.name(99), "error")

    def test_json_envelope(self):
        ok = jsonout.document("ls", entries=[])
        self.assertEqual((ok["schema"], ok["ok"], ok["error"], ok["entries"]), ("agent-resurrect.ls/1", True, None, []))
        bad = jsonout.document("resume", exitcodes.NEEDS_TTY, "no terminal")
        self.assertEqual(bad["error"], {"code": "needs-tty", "message": "no terminal"})
        self.assertFalse(bad["ok"])
        out = io.StringIO()
        jsonout.emit(jsonout.document("find", title="caf\u00e9 \u2603"), out)
        text = out.getvalue()
        self.assertTrue(text.endswith("\n") and text.count("\n") == 1)
        self.assertTrue(text.isascii())
        self.assertEqual(json.loads(text)["title"], "caf\u00e9 \u2603")
        self.assertEqual(jsonout.iso_from_epoch(0), "1970-01-01T00:00:00Z")
        self.assertIsNone(jsonout.iso_from_epoch(None))

    def test_new_base_defaults(self):
        self.assertIsNone(FakeAgent().adopt(ctx=None))

        class Minimal(Setting):
            def check(self):
                return Check(True, "")

            def apply(self):
                return None

            def revert(self, original):
                pass

        self.assertTrue(Minimal().applicable())


if __name__ == "__main__":
    unittest.main()
