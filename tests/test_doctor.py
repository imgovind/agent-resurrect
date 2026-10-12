"""Tests for `agent-resurrect doctor`: one line per check, a fix under every failure, exit 1 on any failure."""
import io
import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from agent_resurrect import doctor, paths, store
from agent_resurrect.model import ISO_FORMAT, Record
from agent_resurrect.settings.base import Check, Setting
from tests.helpers import FakeAgent, FakeProcs, FakeTerminal, make_record, proc, temp_home


class FakeSetting(Setting):
    def __init__(self, id_, ok=True, detail="fine", fix_hint="", error=None):
        self.id, self.fix_hint = id_, fix_hint
        self._check, self._error = Check(ok, detail), error

    def check(self):
        if self._error:
            raise self._error
        return self._check

    def apply(self):
        return None

    def revert(self, original):
        pass


def stamp(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(ISO_FORMAT)


def put(record: Record, minutes_ago: float) -> None:
    record.recorded_at = record.updated_at = stamp(minutes_ago)
    path = store.record_path(record.pane_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_dict()))


class DoctorTest(unittest.TestCase):
    def setUp(self):
        home = temp_home()
        self.home = home.__enter__()
        self.addCleanup(home.__exit__, None, None, None)
        os.environ["FAKE_PANE"] = "p1"
        self.settings = []
        self.agent = FakeAgent()
        for patch in (
            mock.patch.object(doctor, "all_settings", lambda: list(self.settings)),
            mock.patch.object(doctor, "detect_terminal",
                              lambda env: FakeTerminal() if FakeTerminal().detect(env) else None),
            mock.patch.object(doctor, "get_agent", lambda name: self.agent if name == "fake" else None),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_doctor(self, procs=None, boot_time=None):
        out = io.StringIO()
        code = doctor.run(out, procs=procs or FakeProcs(),
                          boot_time=time.time() - 3600 if boot_time is None else boot_time)
        return code, out.getvalue()

    def test_all_ok_returns_0(self):
        self.settings = [FakeSetting("test.ok", detail="all good")]
        code, out = self.run_doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("OK    test.ok: all good", out)
        self.assertIn("OK    fake pane p1", out)
        self.assertIn("OK    python 3.", out)
        self.assertNotIn("FAIL", out)
        for line in out.splitlines():
            self.assertRegex(line, r"^(OK    |WARN  |FAIL  |      fix: )\S")

    def test_failing_setting_returns_1_and_prints_fix(self):
        self.settings = [FakeSetting("test.ok"),
                         FakeSetting("test.bad", ok=False, detail="it is off", fix_hint="run: turn-it-on")]
        code, out = self.run_doctor()
        self.assertEqual(code, 1)
        lines = out.splitlines()
        index = lines.index("FAIL  test.bad: it is off")
        self.assertEqual(lines[index + 1], "      fix: run: turn-it-on")
        self.assertIn("OK    test.ok: fine", lines)

    def test_setting_whose_check_raises_is_a_failure(self):
        self.settings = [FakeSetting("test.broken", fix_hint="reinstall", error=OSError("no defaults binary")),
                         FakeSetting("test.after")]
        code, out = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn("FAIL  test.broken: no defaults binary", out)
        self.assertIn("      fix: reinstall", out)
        self.assertIn("OK    test.after", out)  # one broken check does not stop the others

    def test_outside_a_supported_terminal_warns(self):
        del os.environ["FAKE_PANE"]
        code, out = self.run_doctor()
        self.assertEqual(code, 0)
        self.assertIn("WARN  not running inside a supported terminal (iTerm2)", out)

    def test_boot_time_parsing_uses_sec_not_usec(self):
        text = "{ sec = 1791785041, usec = 851234 } Mon Oct 12 11:34:01 2026"
        self.assertEqual(doctor.parse_boot_time(text), 1791785041)
        self.assertIsNone(doctor.parse_boot_time("{ usec = 851234 }"))
        self.assertIsNone(doctor.parse_boot_time(""))
        calls = []
        self.assertEqual(doctor.read_boot_time(run=lambda argv: calls.append(argv) or text), 1791785041)
        self.assertEqual(calls, [["sysctl", "-n", "kern.boottime"]])
        self.assertIsNone(doctor.read_boot_time(run=lambda argv: ""))

    def test_unknown_boot_time_warns(self):
        out = io.StringIO()
        with mock.patch.object(doctor, "read_boot_time", return_value=None):
            code = doctor.run(out, procs=FakeProcs())
        self.assertEqual(code, 0)
        self.assertIn("WARN  could not read the boot time", out.getvalue())

    def test_orphans_warn(self):
        put(make_record(pane_key="fake-p1", session_id="s-orphan"), 120)                    # died before boot
        put(make_record(pane_key="fake-p2", session_id="s-running", agent_pid=777), 120)   # still running
        put(make_record(pane_key="fake-p3", session_id="s-done", status="consumed"), 120)  # already restored
        put(make_record(pane_key="fake-p4", session_id="s-after"), 10)                     # after boot
        code, out = self.run_doctor(procs=FakeProcs(proc(777, 1, "fake-agent")), boot_time=time.time() - 3600)
        self.assertEqual(code, 0)
        self.assertIn("OK    4 recorded sessions (3 live, 2 restorable)", out)
        self.assertIn("WARN  1 session died in a pane that did not come back; run: agent-resurrect ls", out)

    def test_no_orphans_no_warning(self):
        put(make_record(), 10)
        self.agent = FakeAgent(exists=False)
        code, out = self.run_doctor()
        self.assertIn("OK    1 recorded session (1 live, 0 restorable)", out)
        self.assertNotIn("did not come back", out)

    def test_restore_events_since_boot(self):
        path = paths.restore_events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        events = [("resumed", 120), ("resumed", 10), ("resumed", 9), ("skipped", 5)]
        path.write_text("".join(json.dumps({"ts": stamp(m), "pane_key": "fake-p1", "result": r, "detail": ""}) + "\n"
                                for r, m in events) + "not json\n")
        code, out = self.run_doctor(boot_time=time.time() - 3600)
        self.assertEqual(code, 0)
        self.assertRegex(out, r"OK    since boot \(.+\): 2 resumed, 1 skipped\n")

        with open(path, "a") as fh:
            fh.write(json.dumps({"ts": stamp(1), "pane_key": "fake-p1", "result": "launch-failed"}) + "\n")
        code, out = self.run_doctor(boot_time=time.time() - 3600)
        self.assertEqual(code, 0)
        self.assertRegex(out, r"WARN  since boot \(.+\): 1 launch-failed, 2 resumed, 1 skipped")

    def test_nothing_since_boot(self):
        code, out = self.run_doctor()
        self.assertRegex(out, r"OK    nothing restored since boot \(.+\)")

    def test_old_records_are_collected(self):
        put(make_record(pane_key="fake-p9"), 20 * 24 * 60)
        self.run_doctor()
        self.assertIsNone(store.load("fake-p9"))


if __name__ == "__main__":
    unittest.main()
