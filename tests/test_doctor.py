"""Tests for `agent-resurrect doctor`: one line per check, a fix under every problem, exit 1 on any failure,
and the `--json` document agents act on (stable check ids, fix kinds, the `ready` verdict)."""
import io
import json
import os
import shutil
import stat
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agent_resurrect import __version__, doctor, exitcodes, paths, store
from agent_resurrect.model import ISO_FORMAT, Record
from agent_resurrect.settings.base import Check, Setting
from agent_resurrect.settings.shellrc import ShellRcBlock
from tests.helpers import FakeAgent, FakeProcs, FakeTerminal, make_record, proc, temp_home

LINE = r"^(OK    |WARN  |FAIL  |      fix: )\S"
BLOCK = ShellRcBlock("zsh")  # only its block() text is used; it reads HOME when called
REAL_PROBE = getattr(doctor, "probe_zsh_dir", None)  # setUp replaces it with a fake


class FakeSetting(Setting):
    def __init__(self, id_, ok=True, detail="fine", fix_hint="", error=None, applicable=True, target=None):
        self.id, self.fix_hint = id_, fix_hint
        self._check, self._error, self._applicable, self._target = Check(ok, detail), error, applicable, target

    def applicable(self):
        if isinstance(self._applicable, Exception):
            raise self._applicable
        return self._applicable

    def check(self):
        if self._error:
            raise self._error
        return self._check

    def apply(self):
        return None

    def revert(self, original):
        pass

    def target(self):
        return self._target


def stamp(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(ISO_FORMAT)


def put(record: Record, minutes_ago: float) -> None:
    record.recorded_at = record.updated_at = stamp(minutes_ago)
    path = store.record_path(record.pane_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_dict()))


class DoctorTestCase(unittest.TestCase):
    def setUp(self):
        home = temp_home()
        self.home = home.__enter__()
        self.addCleanup(home.__exit__, None, None, None)
        os.environ["FAKE_PANE"] = "p1"
        os.environ["SHELL"] = "/bin/zsh"
        os.environ["PATH"] = "/usr/bin:/bin"  # never the developer's own ~/.local/bin
        for name in ("TMUX", "STY", "ZELLIJ", "ZDOTDIR"):
            os.environ.pop(name, None)
        self.settings = []
        self.agent = FakeAgent()
        self.zsh_dir = None  # what the zsh probe answers; None means "could not tell"
        self.probed = []
        for patch in (
            mock.patch.object(doctor, "all_settings", lambda: list(self.settings)),
            mock.patch.object(doctor, "detect_terminal",
                              lambda env: FakeTerminal() if FakeTerminal().detect(env) else None),
            mock.patch.object(doctor, "get_agent", lambda name: self.agent if name == "fake" else None),
            mock.patch.object(doctor, "probe_zsh_dir", self.fake_probe),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def fake_probe(self, zsh="zsh", run=None):
        self.probed.append(zsh)
        return self.zsh_dir

    @property
    def launcher(self) -> Path:
        return paths.bin_dir() / "agent-resurrect"

    def make_launcher(self) -> None:
        self.launcher.parent.mkdir(parents=True, exist_ok=True)
        self.launcher.write_text("#!/bin/sh\n")
        self.launcher.chmod(self.launcher.stat().st_mode | stat.S_IXUSR)

    def run_doctor(self, procs=None, boot_time=None, **kwargs):
        out = io.StringIO()
        code = doctor.run(out, procs=procs or FakeProcs(),
                          boot_time=time.time() - 3600 if boot_time is None else boot_time, **kwargs)
        return code, out.getvalue()

    def run_json(self, **kwargs):
        code, out = self.run_doctor(as_json=True, **kwargs)
        self.assertTrue(out.endswith("\n"))
        self.assertEqual(out.count("\n"), 1, out)  # exactly one object, nothing else on stdout
        out.encode("ascii")
        return code, json.loads(out)

    def check(self, doc, check_id):
        found = [c for c in doc["checks"] if c["id"] == check_id]
        self.assertEqual(len(found), 1, f"{check_id} in {[c['id'] for c in doc['checks']]}")
        return found[0]

    def ids(self, doc):
        return [c["id"] for c in doc["checks"]]


class TextOutputTest(DoctorTestCase):
    def test_all_ok_returns_0(self):
        self.settings = [FakeSetting("test.ok", detail="all good")]
        code, out = self.run_doctor()
        self.assertEqual(code, exitcodes.OK, out)
        self.assertIn("OK    test.ok: all good", out)
        self.assertIn("OK    fake pane p1", out)
        self.assertIn("OK    python 3.", out)
        self.assertIn("OK    login shell zsh (/bin/zsh)", out)
        self.assertNotIn("FAIL", out)
        for line in out.splitlines():
            self.assertRegex(line, LINE)

    def test_failing_setting_returns_1_and_prints_fix(self):
        self.settings = [FakeSetting("test.ok"),
                         FakeSetting("test.bad", ok=False, detail="it is off", fix_hint="run: turn-it-on")]
        code, out = self.run_doctor()
        self.assertEqual(code, exitcodes.ERROR)
        lines = out.splitlines()
        index = lines.index("FAIL  test.bad: it is off")
        self.assertEqual(lines[index + 1], "      fix: run: turn-it-on")
        self.assertIn("OK    test.ok: fine", lines)

    def test_setting_whose_check_raises_is_a_failure(self):
        self.settings = [FakeSetting("test.broken", fix_hint="reinstall", error=OSError("no defaults binary")),
                         FakeSetting("test.after")]
        code, out = self.run_doctor()
        self.assertEqual(code, exitcodes.ERROR)
        self.assertIn("FAIL  test.broken: no defaults binary", out)
        self.assertIn("      fix: reinstall", out)
        self.assertIn("OK    test.after", out)  # one broken check does not stop the others

    def test_failing_setting_without_a_hint_names_the_launcher(self):
        self.settings = [FakeSetting("test.bad", ok=False, detail="off")]
        code, out = self.run_doctor()
        self.assertIn("FAIL  test.bad: off\n      fix: run: ~/.local/bin/agent-resurrect install\n", out)

    def test_outside_a_supported_terminal_warns_with_a_fix(self):
        del os.environ["FAKE_PANE"]
        code, out = self.run_doctor()
        self.assertEqual(code, exitcodes.OK)
        lines = out.splitlines()
        index = lines.index("WARN  not running inside a supported terminal (iTerm2)")
        self.assertTrue(lines[index + 1].startswith("      fix: "), lines[index + 1])

    def test_every_line_has_the_documented_shape(self):
        del os.environ["FAKE_PANE"]
        os.environ["TMUX"] = "/tmp/tmux-501/default,1,0"
        os.environ["SHELL"] = "/opt/homebrew/bin/fish"
        self.settings = [FakeSetting("iterm.restore_policy", ok=False, applicable=False, fix_hint="install iTerm2"),
                         FakeSetting("test.bad", ok=False)]
        code, out = self.run_doctor()
        self.assertEqual(code, exitcodes.ERROR)
        for line in out.splitlines():
            self.assertRegex(line, LINE)

    def test_output_neutralises_control_characters(self):
        hostile = "\x1b]52;c;ZXZpbA==\x07\x9b2J"
        os.environ["FAKE_PANE"] = "p1" + hostile  # the pane label comes from the environment
        self.settings = [FakeSetting("test.bad", ok=False, detail="off" + hostile, fix_hint="run" + hostile),
                         FakeSetting("test.raises", error=OSError("bad" + hostile))]
        code, out = self.run_doctor()
        self.assertEqual(code, exitcodes.ERROR)
        self.assertNotRegex(out.replace("\n", ""), r"[\x00-\x1f\x7f-\x9f]")
        self.assertIn("OK    fake pane p1?]52;c;ZXZpbA==??2J", out)
        self.assertIn("      fix: run?]52;c;ZXZpbA==??2J", out)
        self.assertEqual(doctor.printable("a\x1bb\tc\u200dd"), "a?b?c\u200dd")  # zero-width joiner (emoji) kept

    def test_text_mode_prints_no_json(self):
        code, out = self.run_doctor()
        self.assertNotIn("{", out)


class BootAndRecordsTest(DoctorTestCase):
    def test_boot_time_parsing_uses_sec_not_usec(self):
        text = "{ sec = 1791785041, usec = 851234 } Mon Oct 12 11:34:01 2026"
        self.assertEqual(doctor.parse_boot_time(text), 1791785041)
        self.assertIsNone(doctor.parse_boot_time("{ usec = 851234 }"))
        self.assertIsNone(doctor.parse_boot_time(""))
        calls = []
        self.assertEqual(doctor.read_boot_time(run=lambda argv: calls.append(argv) or text), 1791785041)
        self.assertEqual(calls, [["sysctl", "-n", "kern.boottime"]])
        self.assertIsNone(doctor.read_boot_time(run=lambda argv: ""))

    @unittest.skipUnless(os.path.exists("/usr/sbin/sysctl"), "macOS only")
    def test_boot_time_is_read_without_sbin_on_path(self):
        # setUp's PATH is /usr/bin:/bin, as in a minimal agent or launchd environment; sysctl is in /usr/sbin.
        self.assertIsNone(shutil.which("sysctl"))
        boot = doctor.read_boot_time()
        self.assertIsNotNone(boot)
        self.assertLess(boot, time.time())

    def test_unknown_boot_time_warns(self):
        out = io.StringIO()
        with mock.patch.object(doctor, "read_boot_time", return_value=None):
            code = doctor.run(out, procs=FakeProcs())
        self.assertEqual(code, exitcodes.OK)
        self.assertIn("WARN  could not read the boot time", out.getvalue())

    def test_unknown_boot_time_json_has_null_counts(self):
        put(make_record(), 120)
        with mock.patch.object(doctor, "read_boot_time", return_value=None):
            out = io.StringIO()
            code = doctor.run(out, procs=FakeProcs(), as_json=True)
        doc = json.loads(out.getvalue())
        self.assertEqual(code, exitcodes.OK)
        self.assertIsNone(doc["context"]["boot_time"])
        self.assertIsNone(doc["since_boot"])
        self.assertIsNone(doc["records"]["died_since_boot"])
        self.assertEqual(doc["records"]["total"], 1)
        since = self.check(doc, "restores.since_boot")
        self.assertEqual(since["status"], "warn")
        self.assertIsNone(since["fix"])  # nothing to do: restores still work
        self.assertTrue(doc["ready"])

    def test_orphans_warn(self):
        put(make_record(pane_key="fake-p1", session_id="s-orphan"), 120)                    # died before boot
        put(make_record(pane_key="fake-p2", session_id="s-running", agent_pid=777), 120)   # still running
        put(make_record(pane_key="fake-p3", session_id="s-done", status="consumed"), 120)  # already restored
        put(make_record(pane_key="fake-p4", session_id="s-after"), 10)                     # after boot
        code, out = self.run_doctor(procs=FakeProcs(proc(777, 1, "fake-agent")), boot_time=time.time() - 3600)
        self.assertEqual(code, exitcodes.OK)
        self.assertIn("OK    4 recorded sessions (3 live, 2 restorable)", out)
        self.assertIn("WARN  1 session died in a pane and did not come back\n"
                      "      fix: list it with: ~/.local/bin/agent-resurrect ls", out)

    def test_records_and_since_boot_counts_in_json(self):
        put(make_record(pane_key="fake-p1", session_id="s-orphan"), 120)
        put(make_record(pane_key="fake-p2", session_id="s-running", agent_pid=777), 120)
        put(make_record(pane_key="fake-p3", session_id="s-done", status="consumed"), 120)
        put(make_record(pane_key="fake-p4", session_id="s-after"), 10)
        path = paths.restore_events_path()
        events = [("resumed", 120), ("resumed", 10), ("resumed", 9), ("skipped", 5), ("lost-race", 4)]
        path.write_text("".join(json.dumps({"ts": stamp(m), "pane_key": "fake-p1", "result": r}) + "\n"
                                for r, m in events))
        code, doc = self.run_json(procs=FakeProcs(proc(777, 1, "fake-agent")))
        self.assertEqual(code, exitcodes.OK)
        self.assertEqual(doc["records"], {"total": 4, "live": 3, "restorable": 2, "died_since_boot": 1})
        self.assertEqual(doc["since_boot"], {"resumed": 2, "skipped": 1, "launch-failed": 0, "offered": 0,
                                             "not-a-tty": 0, "lost-race": 1})
        died = self.check(doc, "records.died_since_boot")
        self.assertEqual((died["status"], died["fix"]["kind"], died["fix"]["agent_may_run"]),
                         ("warn", "user-action", False))
        self.assertTrue(doc["ready"])  # sessions lost before this install do not make it unready

    def test_no_orphans_no_warning(self):
        put(make_record(), 10)
        self.agent = FakeAgent(exists=False)
        code, out = self.run_doctor()
        self.assertIn("OK    1 recorded session (1 live, 0 restorable)", out)
        self.assertNotIn("did not come back", out)
        code, doc = self.run_json()
        self.assertNotIn("records.died_since_boot", self.ids(doc))
        self.assertEqual(doc["records"]["died_since_boot"], 0)

    def test_restore_events_since_boot(self):
        path = paths.restore_events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        events = [("resumed", 120), ("resumed", 10), ("resumed", 9), ("skipped", 5)]
        path.write_text("".join(json.dumps({"ts": stamp(m), "pane_key": "fake-p1", "result": r, "detail": ""}) + "\n"
                                for r, m in events) + "not json\n")
        code, out = self.run_doctor(boot_time=time.time() - 3600)
        self.assertEqual(code, exitcodes.OK)
        self.assertRegex(out, r"OK    since boot \(.+\): 2 resumed, 1 skipped\n")

        with open(path, "a") as fh:
            fh.write(json.dumps({"ts": stamp(1), "pane_key": "fake-p1", "result": "launch-failed"}) + "\n")
        code, out = self.run_doctor(boot_time=time.time() - 3600)
        self.assertEqual(code, exitcodes.OK)
        self.assertRegex(out, r"WARN  since boot \(.+\): 1 launch-failed, 2 resumed, 1 skipped\n      fix: ")
        code, doc = self.run_json()
        since = self.check(doc, "restores.since_boot")
        self.assertEqual(since["fix"]["kind"], "user-action")
        self.assertIn(str(paths.log_path()), since["fix"]["text"])
        self.assertEqual(doc["since_boot"]["launch-failed"], 1)

    def test_nothing_since_boot(self):
        code, out = self.run_doctor()
        self.assertRegex(out, r"OK    nothing restored since boot \(.+\)")

    def test_old_records_are_collected_unless_their_agent_runs(self):
        put(make_record(pane_key="fake-p9"), 20 * 24 * 60)
        put(make_record(pane_key="fake-p8", agent_pid=888), 20 * 24 * 60)
        self.run_doctor(procs=FakeProcs(proc(888, 1, "fake-agent")))
        self.assertIsNone(store.load("fake-p9"))
        self.assertIsNotNone(store.load("fake-p8"))  # a session can run for weeks


class JsonDocumentTest(DoctorTestCase):
    def test_document_envelope_and_shape(self):
        self.settings = [FakeSetting("test.ok", detail="all good")]
        self.make_launcher()
        code, doc = self.run_json()
        self.assertEqual(code, exitcodes.OK)
        self.assertEqual(doc["schema"], "agent-resurrect.doctor/1")
        self.assertEqual(doc["version"], __version__)
        self.assertIs(doc["ok"], True)
        self.assertIsNone(doc["error"])
        self.assertIs(doc["ready"], True)
        statuses = [c["status"] for c in doc["checks"]]
        self.assertEqual(doc["summary"], {"ok": statuses.count("ok"), "warn": statuses.count("warn"),
                                          "fail": statuses.count("fail")})
        for check in doc["checks"]:
            self.assertEqual(set(check), {"id", "status", "detail", "fix"})
            self.assertIn(check["status"], ("ok", "warn", "fail"))
            self.assertIsInstance(check["detail"], str)
        self.assertEqual(len(self.ids(doc)), len(set(self.ids(doc))))
        self.assertEqual(self.check(doc, "test.ok")["detail"], "all good")  # no id prefix in JSON
        self.assertRegex(self.check(doc, "python")["detail"], r"^python 3\.\d+\.\d+ \(.+\)$")

        context = doc["context"]
        self.assertEqual(context["terminal"], "fake")
        self.assertEqual(context["pane_key"], "fake-p1")
        self.assertEqual(context["launcher"], str(self.launcher))
        self.assertEqual(context["log_path"], str(paths.log_path()))
        self.assertRegex(context["boot_time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(context["login_shell"], "/bin/zsh")
        self.assertIsNone(context["multiplexer"])
        self.assertEqual(doc["records"], {"total": 0, "live": 0, "restorable": 0, "died_since_boot": 0})
        self.assertEqual(doc["since_boot"], {"resumed": 0, "skipped": 0, "launch-failed": 0, "offered": 0,
                                             "not-a-tty": 0})

    def test_stable_check_ids_in_order(self):
        self.settings = [FakeSetting(i) for i in ("iterm.keep_windows", "iterm.restore_policy",
                                                  "iterm.launch_at_login", "shell.zsh", "shell.bash",
                                                  "claude.plugin")]
        code, doc = self.run_json()
        self.assertEqual(self.ids(doc), ["python", "launcher.on_path", "shell.login", "iterm.keep_windows",
                                         "iterm.restore_policy", "iterm.launch_at_login", "shell.zsh",
                                         "shell.bash", "claude.plugin", "terminal.pane", "records",
                                         "restores.since_boot"])

    def test_failure_document(self):
        self.make_launcher()
        self.settings = [FakeSetting("claude.plugin", ok=False, detail="Claude Code plugin agent-resurrect "
                                     "is not installed", fix_hint="run: ~/.local/bin/agent-resurrect install")]
        code, doc = self.run_json()
        self.assertEqual(code, exitcodes.ERROR)
        self.assertIs(doc["ok"], False)
        self.assertEqual(doc["error"]["code"], exitcodes.name(code))
        self.assertIn("1 check failed", doc["error"]["message"])
        self.assertIs(doc["ready"], False)
        plugin = self.check(doc, "claude.plugin")
        self.assertEqual(plugin["status"], "fail")
        self.assertEqual(plugin["detail"], "Claude Code plugin agent-resurrect is not installed")
        self.assertEqual(plugin["fix"], {"kind": "command", "agent_may_run": True,
                                         "text": "run: ~/.local/bin/agent-resurrect install",
                                         "argv": [str(self.launcher), "install", "--yes"]})

    def test_fix_invariants(self):
        del os.environ["FAKE_PANE"]
        os.environ["ZELLIJ"] = "0"
        os.environ["SHELL"] = "/usr/local/bin/nu"
        self.settings = [FakeSetting("iterm.launch_at_login", ok=False, applicable=False),
                         FakeSetting("macos.keep_windows", ok=False),
                         FakeSetting("future.setting", ok=False)]
        code, doc = self.run_json(boot_time=time.time() - 3600)
        for check in doc["checks"]:
            fix = check["fix"]
            if check["status"] == "ok":
                self.assertIsNone(fix, check)
                continue
            if check["status"] == "fail":
                self.assertIsNotNone(fix, check)
            if fix is None:
                continue
            self.assertEqual(set(fix), {"kind", "agent_may_run", "text", "argv"})
            self.assertIn(fix["kind"], doctor.FIX_KINDS)
            self.assertTrue(fix["text"])
            if fix["kind"] == "command":
                self.assertIsInstance(fix["argv"], list)
                self.assertTrue(os.path.isabs(fix["argv"][0]))
            else:
                self.assertIsNone(fix["argv"], check)
                self.assertIs(fix["agent_may_run"], False, check)

    def test_json_keeps_raw_text_escaped_as_ascii(self):
        hostile = "\x1b]52;c;ZXZpbA==\x07 caf\u00e9"
        self.settings = [FakeSetting("test.bad", ok=False, detail="off" + hostile)]
        code, doc = self.run_json()
        self.assertEqual(self.check(doc, "test.bad")["detail"], "off" + hostile)

    def test_error_message_counts_warnings_under_strict(self):
        del os.environ["FAKE_PANE"]
        code, doc = self.run_json(strict=True)
        self.assertEqual(code, exitcodes.ERROR)
        self.assertEqual(doc["error"]["code"], "error")
        self.assertIn("--strict", doc["error"]["message"])


class StrictAndReadyTest(DoctorTestCase):
    def test_strict_makes_a_warning_an_error(self):
        del os.environ["FAKE_PANE"]
        self.assertEqual(self.run_doctor()[0], exitcodes.OK)
        self.assertEqual(self.run_doctor(strict=True)[0], exitcodes.ERROR)

    def test_strict_with_nothing_to_warn_is_ok(self):
        os.environ["PATH"] = f"{paths.bin_dir()}:/usr/bin:/bin"
        self.make_launcher()
        self.settings = [FakeSetting("test.ok")]
        code, out = self.run_doctor(strict=True)
        self.assertEqual(code, exitcodes.OK, out)

    def test_ready_ignores_path_and_terminal_warnings(self):
        del os.environ["FAKE_PANE"]
        os.environ["TMUX"] = "/tmp/tmux-501/default,1,0"
        self.settings = [FakeSetting("shell.zsh"), FakeSetting("claude.plugin")]
        code, doc = self.run_json()
        self.assertEqual(code, exitcodes.OK)
        self.assertEqual(self.check(doc, "launcher.on_path")["status"], "warn")
        self.assertEqual(self.check(doc, "terminal.pane")["status"], "warn")
        self.assertEqual(self.check(doc, "terminal.multiplexer")["status"], "warn")
        self.assertIs(doc["ready"], True)

    def test_a_failing_setting_makes_it_unready(self):
        self.settings = [FakeSetting("shell.zsh", ok=False), FakeSetting("claude.plugin")]
        self.assertIs(self.run_json()[1]["ready"], False)

    def test_a_disabled_plugin_makes_it_unready(self):
        self.settings = [FakeSetting("claude.plugin", ok=False, detail="installed but disabled")]
        self.assertIs(self.run_json()[1]["ready"], False)


class FixKindTest(DoctorTestCase):
    def fix_for(self, setting):
        self.settings = [setting]
        code, doc = self.run_json()
        return self.check(doc, setting.id)

    def test_mapping_by_id_prefix(self):
        self.make_launcher()
        install = [str(self.launcher), "install", "--yes"]
        cases = [
            # (id, applicable, expected status, expected kind)
            ("macos.keep_windows", True, "fail", "command"),
            ("iterm.keep_windows", True, "fail", "command"),
            ("iterm.keep_windows", False, "fail", "install-app"),
            ("iterm.restore_policy", False, "fail", "install-app"),
            ("iterm.launch_at_login", False, "fail", "install-app"),
            ("claude.plugin", True, "fail", "command"),
            ("claude.plugin", False, "fail", "install-app"),
            ("shell.zsh", True, "fail", "command"),
            ("shell.bash", True, "fail", "command"),
            ("future.thing", True, "fail", "command"),   # unknown ids still get a fix
            ("future.thing", False, "fail", "command"),
        ]
        for setting_id, applicable, status, kind in cases:
            with self.subTest(setting_id=setting_id, applicable=applicable):
                check = self.fix_for(FakeSetting(setting_id, ok=False, applicable=applicable, fix_hint="hint"))
                self.assertEqual((check["status"], check["fix"]["kind"]), (status, kind))
                self.assertEqual(check["fix"]["text"], "hint")
                if kind == "command":
                    self.assertEqual(check["fix"]["argv"], install)
                    self.assertIs(check["fix"]["agent_may_run"], True)
                else:
                    self.assertIsNone(check["fix"]["argv"])
                    self.assertIs(check["fix"]["agent_may_run"], False)

    def test_a_shell_setting_that_does_not_apply_is_not_a_failure(self):
        check = self.fix_for(FakeSetting("shell.zsh", ok=False, applicable=False,
                                         detail="~/.zshrc does not load agent-resurrect"))
        self.assertEqual(check["status"], "ok")
        self.assertIsNone(check["fix"])
        self.assertIn("does not apply", check["detail"])

    def test_applicable_that_raises_counts_as_applicable(self):
        self.make_launcher()
        check = self.fix_for(FakeSetting("iterm.restore_policy", ok=False, applicable=OSError("boom")))
        self.assertEqual(check["fix"]["kind"], "command")

    def test_a_check_that_raises_still_gets_a_kind(self):
        check = self.fix_for(FakeSetting("claude.plugin", error=RuntimeError("claude hung"), applicable=False))
        self.assertEqual((check["status"], check["detail"], check["fix"]["kind"]),
                         ("fail", "claude hung", "install-app"))

    def test_agents_may_run_the_launcher_only_when_it_exists(self):
        check = self.fix_for(FakeSetting("claude.plugin", ok=False))
        self.assertEqual(check["fix"]["kind"], "command")
        self.assertIs(check["fix"]["agent_may_run"], False)
        self.make_launcher()
        self.assertIs(self.fix_for(FakeSetting("claude.plugin", ok=False))["fix"]["agent_may_run"], True)

    def test_launcher_not_on_path_fixes_itself_in_a_new_shell(self):
        self.make_launcher()
        check = self.run_json()[1]
        on_path = self.check(check, "launcher.on_path")
        self.assertEqual((on_path["status"], on_path["fix"]["kind"]), ("warn", "new-shell"))
        self.assertEqual(on_path["detail"], f"{self.launcher} is not on PATH")

    def test_missing_launcher_needs_the_installer(self):
        on_path = self.check(self.run_json()[1], "launcher.on_path")
        self.assertEqual((on_path["status"], on_path["fix"]["kind"]), ("warn", "user-action"))
        self.assertIn("install.sh", on_path["fix"]["text"])

    def test_launcher_on_path_is_ok(self):
        self.make_launcher()
        os.environ["PATH"] = f"{paths.bin_dir()}:/usr/bin:/bin"
        on_path = self.check(self.run_json()[1], "launcher.on_path")
        self.assertEqual((on_path["status"], on_path["fix"]), ("ok", None))


class LoginShellTest(DoctorTestCase):
    def test_zsh_and_bash_are_supported(self):
        for shell in ("/bin/zsh", "/bin/bash", "/opt/homebrew/bin/bash", "-zsh"):
            with self.subTest(shell=shell):
                os.environ["SHELL"] = shell
                code, doc = self.run_json()
                self.assertEqual(self.check(doc, "shell.login")["status"], "ok")
                self.assertEqual(code, exitcodes.OK)

    def test_other_login_shells_fail_with_a_user_action(self):
        for shell in ("/opt/homebrew/bin/fish", "/usr/local/bin/nu", "/bin/tcsh"):
            with self.subTest(shell=shell):
                os.environ["SHELL"] = shell
                code, doc = self.run_json()
                login = self.check(doc, "shell.login")
                self.assertEqual(code, exitcodes.ERROR)
                self.assertEqual(login["status"], "fail")
                self.assertIn(shell, login["detail"])
                self.assertEqual(login["fix"]["kind"], "user-action")
                self.assertIs(login["fix"]["agent_may_run"], False)
                self.assertIsNone(login["fix"]["argv"])
                self.assertIs(doc["ready"], False)

    def test_falls_back_to_the_user_database(self):
        del os.environ["SHELL"]
        entry = mock.Mock(pw_shell="/opt/homebrew/bin/fish")
        with mock.patch.object(doctor.pwd, "getpwuid", return_value=entry):
            code, doc = self.run_json()
        self.assertEqual(self.check(doc, "shell.login")["status"], "fail")
        self.assertEqual(doc["context"]["login_shell"], "/opt/homebrew/bin/fish")

    def test_unknown_login_shell_warns(self):
        del os.environ["SHELL"]
        with mock.patch.object(doctor.pwd, "getpwuid", side_effect=KeyError("no such user")):
            code, doc = self.run_json()
        login = self.check(doc, "shell.login")
        self.assertEqual(login["status"], "warn")
        self.assertIsNone(doc["context"]["login_shell"])


class MultiplexerTest(DoctorTestCase):
    def test_each_multiplexer_warns(self):
        for var, value, name in (("TMUX", "/private/tmp/tmux-501/default,4242,0", "tmux"),
                                 ("STY", "4242.ttys001.mac", "screen"),
                                 ("ZELLIJ", "0", "zellij")):
            with self.subTest(var=var):
                os.environ[var] = value
                try:
                    code, doc = self.run_json()
                    code_text, out = self.run_doctor()
                finally:
                    del os.environ[var]
                check = self.check(doc, "terminal.multiplexer")
                self.assertEqual(code, exitcodes.OK)
                self.assertEqual(check["status"], "warn")
                self.assertIn(name, check["detail"])
                self.assertEqual(check["fix"]["kind"], "user-action")
                self.assertEqual(doc["context"]["multiplexer"], name)
                self.assertIn(f"WARN  running inside {name}", out)

    def test_empty_variable_is_not_a_multiplexer(self):
        os.environ["TMUX"] = ""
        code, doc = self.run_json()
        self.assertNotIn("terminal.multiplexer", self.ids(doc))
        self.assertIsNone(doc["context"]["multiplexer"])


class ZdotdirTest(DoctorTestCase):
    def zsh_setting(self, target):
        return FakeSetting("shell.zsh", target=target)

    def test_mismatch_warns_and_blocks_ready(self):
        self.settings = [self.zsh_setting(self.home / ".zshrc")]
        self.zsh_dir = self.home / ".config" / "zsh"
        code, doc = self.run_json()
        check = self.check(doc, "shell.zdotdir")
        self.assertEqual(code, exitcodes.OK)
        self.assertEqual(check["status"], "warn")
        self.assertIn("~/.config/zsh/.zshrc", check["detail"])
        self.assertIn("~/.zshrc", check["detail"])
        self.assertEqual(check["fix"]["kind"], "user-action")
        self.assertIn("export ZDOTDIR", check["fix"]["text"])
        self.assertIs(doc["ready"], False)
        self.assertEqual(self.probed, ["/bin/zsh"])
        self.assertEqual(self.run_doctor(strict=True)[0], exitcodes.ERROR)

    def test_agreement_is_silent(self):
        os.environ["ZDOTDIR"] = str(self.home / ".config" / "zsh")
        self.settings = [self.zsh_setting(self.home / ".config" / "zsh" / ".zshrc")]
        self.zsh_dir = self.home / ".config" / "zsh"
        code, doc = self.run_json()
        self.assertNotIn("shell.zdotdir", self.ids(doc))
        self.assertTrue(doc["ready"])

    def test_probe_failure_is_silent(self):
        self.settings = [self.zsh_setting(self.home / ".zshrc")]
        code, doc = self.run_json()
        self.assertNotIn("shell.zdotdir", self.ids(doc))

    def test_not_probed_when_the_login_shell_is_bash(self):
        os.environ["SHELL"] = "/bin/bash"
        self.settings = [self.zsh_setting(self.home / ".zshrc")]
        self.zsh_dir = self.home / ".config" / "zsh"
        code, doc = self.run_json()
        self.assertEqual(self.probed, [])
        self.assertNotIn("shell.zdotdir", self.ids(doc))

    def test_probe_parses_the_marker_and_drops_an_inherited_zdotdir(self):
        os.environ["ZDOTDIR"] = "/somewhere/vscode-zsh"
        calls = []

        def run(argv, env):
            calls.append((argv, env))
            return "noise from .zshenv\nagent-resurrect-zdotdir=/Users/me/.config/zsh\n"

        result = REAL_PROBE("/bin/zsh", run=run)
        self.assertEqual(result, Path("/Users/me/.config/zsh"))
        argv, env = calls[0]
        self.assertEqual(argv[:2], ["/bin/zsh", "-c"])
        self.assertNotIn("ZDOTDIR", env)
        self.assertEqual(env["HOME"], str(self.home))
        self.assertIsNone(REAL_PROBE("/bin/zsh", run=lambda argv, env: ""))
        self.assertIsNone(REAL_PROBE("/bin/zsh", run=lambda argv, env: "agent-resurrect-zdotdir=rel\n"))

    @unittest.skipUnless(shutil.which("zsh"), "zsh is not installed")
    def test_probe_sees_an_unexported_zdotdir_in_zshenv(self):
        # A real zsh, in the temporary HOME: `zsh -c` reads /etc/zshenv and ~/.zshenv, nothing else.
        (self.home / ".zshenv").write_text('ZDOTDIR="$HOME/.config/zsh"\necho chatty\n')
        os.environ["ZDOTDIR"] = "/somewhere/else"
        zsh = shutil.which("zsh", path="/bin:/usr/bin") or shutil.which("zsh")
        self.assertEqual(REAL_PROBE(zsh), self.home / ".config" / "zsh")
        (self.home / ".zshenv").write_text("")
        self.assertEqual(REAL_PROBE(zsh), self.home)


class ExecBeforeBlockTest(DoctorTestCase):
    def rc(self, before: str, after: str = "", name: str = ".zshrc") -> Path:
        path = self.home / name
        path.write_text(before + BLOCK.block() + after)
        return path

    def scan(self, before: str, after: str = "", shell: str = "zsh"):
        self.settings = [FakeSetting(f"shell.{shell}", target=self.rc(before, after,
                                                                       ".zshrc" if shell == "zsh" else ".bash_profile"))]
        code, doc = self.run_json()
        found = [c for c in doc["checks"] if c["id"] == "shell.exec_before_block"]
        return found[0] if found else None, doc

    def test_takeovers_before_the_block_warn(self):
        cases = [
            ("exec tmux\n", 1),
            ("export A=1\n[[ -z $TMUX ]] && exec tmux new -A -s main\n", 2),
            ('if [ -z "$TMUX" ]; then\n  exec tmux attach\nfi\n', 2),
            ("tmux attach\n", 1),
            ("tmux new-session -A -s main\n", 1),
            ("tmux a\n", 1),
            ("exec fish\n", 1),
            ("exec -l /opt/homebrew/bin/nu\n", 1),
            ("zellij attach -c\n", 1),
            ("zellij\n", 1),
            ('eval "$(zellij setup --generate-auto-start zsh)"\n', 1),  # zellij's documented auto-start
        ]
        for before, line in cases:
            with self.subTest(before=before):
                check, doc = self.scan(before)
                self.assertIsNotNone(check, before)
                self.assertEqual(check["status"], "warn")
                self.assertIn(f"~/.zshrc line {line}", check["detail"])
                self.assertEqual(check["fix"]["kind"], "user-action")
                self.assertIn(f"above line {line}", check["fix"]["text"])
                self.assertIs(doc["ready"], False)

    def test_harmless_lines_do_not_warn(self):
        cases = [
            "exec 2>/dev/null\n",
            "exec >/tmp/log 2>&1\n",
            "exec zsh\n",                       # the same shell reads this file again and reaches the block
            "# exec tmux\n",
            "echo hi # exec tmux\n",
            "alias t='tmux attach'\n",
            'alias t="exec tmux new -A"\n',
            "find . -exec rm {} \\;\n",
            "tm() {\n  exec tmux new -A -s main\n}\n",
            "function tm {\n  tmux attach\n}\n",
            "export TMUX_TMPDIR=/tmp\n",
            "tmux_title() { :; }\n",
            "tm() { exec tmux new -A -s main; }\n",   # a one-line function definition runs nothing
            "function tm { tmux attach; }\n",
            "tm()\n{\n  exec tmux\n}\n",
            "exec $SHELL -l\n",                      # the login shell (zsh) reads this file again
            "source <(zellij setup --generate-completion zsh)\n",
            "tmux new-window\n",
        ]
        for before in cases:
            with self.subTest(before=before):
                check, doc = self.scan(before)
                self.assertIsNone(check, before)

    def test_takeover_after_the_block_is_fine(self):
        check, doc = self.scan("export A=1\n", after="exec tmux\n")
        self.assertIsNone(check)

    def test_bash_rc_is_scanned_too(self):
        check, doc = self.scan("exec tmux\n", shell="bash")
        self.assertIn("~/.bash_profile line 1", check["detail"])

    def test_file_without_a_block_is_not_scanned(self):
        path = self.home / ".zshrc"
        path.write_text("exec tmux\n")
        self.settings = [FakeSetting("shell.zsh", ok=False, target=path)]
        code, doc = self.run_json()
        self.assertNotIn("shell.exec_before_block", self.ids(doc))

    def test_missing_or_unreadable_target_is_ignored(self):
        self.settings = [FakeSetting("shell.zsh", target=self.home / "absent"),
                         FakeSetting("shell.bash", target=None)]
        code, doc = self.run_json()
        self.assertNotIn("shell.exec_before_block", self.ids(doc))

    def test_text_line(self):
        self.settings = [FakeSetting("shell.zsh", target=self.rc("exec tmux\n"))]
        code, out = self.run_doctor()
        self.assertIn("WARN  ~/.zshrc line 1 runs `exec tmux` before the agent-resurrect block", out)


if __name__ == "__main__":
    unittest.main()
