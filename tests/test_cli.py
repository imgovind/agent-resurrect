"""Tests for the command line. Agents, terminals and processes are faked; nothing is ever launched."""
import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from unittest import mock

import agent_resurrect
from agent_resurrect import paths, store
from agent_resurrect.model import ISO_FORMAT, Candidate, Record, SessionInfo
from agent_resurrect.settings.base import Check, Setting
from tests.helpers import FakeAgent, FakeProcs, FakeTerminal, make_record, proc, temp_home

PACKAGE_DIR = Path(agent_resurrect.__file__).resolve().parent
REPO = PACKAGE_DIR.parent.parent
BIN = REPO / "bin" / "agent-resurrect.py"
# restore.py (Task B) and installer.py (Task C) are written in parallel with the CLI. Until they are
# merged, minimal stand-ins that follow the plan's interfaces take their place, so these same tests run
# unchanged against the real modules after integration.
REAL_SIBLINGS = all((PACKAGE_DIR / f"{name}.py").exists() for name in ("restore", "installer"))


def _build_restore(module):
    from agent_resurrect.model import parse_iso

    @dataclass
    class Decision:
        eligible: bool
        reason: str

    @dataclass
    class ResumePlan:
        argv: List[str]
        cwd: Optional[str]
        warning: Optional[str]

    def check_eligible(record, agent, procs, config, now=None, require_live=True):
        if require_live and record.status != "live":
            return Decision(False, "not-live")
        updated = parse_iso(record.updated_at)
        now = now or datetime.now(timezone.utc)
        if updated is not None and updated < now - timedelta(days=config.max_age_days):
            return Decision(False, "too-old")
        if procs.is_alive(record.agent_pid, record.agent_started):
            return Decision(False, "agent-running")
        if not agent.session_exists(record):
            return Decision(False, "session-gone")
        return Decision(True, "ok")

    def plan_resume(record, agent, config):
        argv = agent.resume_argv(record, config)
        if record.cwd and os.path.isdir(record.cwd):
            return ResumePlan(argv, record.cwd, None)
        return ResumePlan(argv, None, f"working directory {record.cwd} no longer exists; "
                                      "resuming from the current directory")

    def format_command(plan):
        command = " ".join(shlex.quote(part) for part in plan.argv)
        return f"cd {shlex.quote(plan.cwd)} && {command}" if plan.cwd else command

    def launch(plan):
        raise AssertionError("tests must replace restore.launch")

    def resume_record(record, agent, config, *, stdout=None, launcher=launch, announce=True):
        out = stdout or sys.stdout
        plan = plan_resume(record, agent, config)
        if announce:
            print(f'Resurrecting "{record.session_id}" ({record.agent}) in {record.cwd}', file=out)
        if store.valid_pane_key(record.pane_key) and store.load(record.pane_key) is not None:
            store.set_status(record.pane_key, "consumed")
        store.append_restore_event(record.pane_key, "resumed", record.session_id)
        if plan.warning:
            print(plan.warning, file=out)
        try:
            launcher(plan)
        except OSError as exc:
            print(f"agent-resurrect: could not start {plan.argv[0]}: {exc}", file=out)
            return 1
        return 0

    def auto(env, **kwargs):
        raise AssertionError("tests must replace restore.auto")

    module.__dict__.update(Decision=Decision, ResumePlan=ResumePlan, check_eligible=check_eligible,
                           plan_resume=plan_resume, format_command=format_command, launch=launch,
                           resume_record=resume_record, auto=auto)


def _build_installer(module):
    @dataclass
    class StepResult:
        id: str
        status: str
        detail: str = ""

    def install(settings=None):
        raise AssertionError("tests must replace cli.installer")

    def uninstall(settings=None, purge=False):
        raise AssertionError("tests must replace cli.installer")

    module.__dict__.update(StepResult=StepResult, install=install, uninstall=uninstall)


for _name, _build in (("restore", _build_restore), ("installer", _build_installer)):
    _full = f"agent_resurrect.{_name}"
    if not (PACKAGE_DIR / f"{_name}.py").exists() and _full not in sys.modules:
        _module = types.ModuleType(_full)
        _build(_module)
        sys.modules[_full] = _module
        setattr(agent_resurrect, _name, _module)

from agent_resurrect import cli, restore  # noqa: E402  (after the stand-ins, if any)


@dataclass
class Step:
    id: str
    status: str
    detail: str = ""


class FakeStdin(io.StringIO):
    def __init__(self, text: str = "", tty: bool = False):
        super().__init__(text)
        self._tty = tty

    def isatty(self):
        return self._tty


class DescribedSetting(Setting):
    id = "test.setting"
    description = "Turn the test setting on."

    def check(self):
        return Check(True, "on")

    def apply(self):
        return None

    def revert(self, original):
        pass


def put(record: Record, minutes_ago: float) -> Record:
    """Store a record as if last updated `minutes_ago` minutes ago (store.save would stamp now)."""
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(ISO_FORMAT)
    record.recorded_at = record.updated_at = stamp
    path = store.record_path(record.pane_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_dict()))
    return record


def candidate(session_id: str, cwd: Optional[str], hits: int = 3, title: str = "Fix login") -> Candidate:
    return Candidate(session_id=session_id, transcript_path=f"/x/{session_id}.jsonl", cwd=cwd, cwd_trusted=True,
                     hits=hits, total_probes=4, roles={"Claude": 2, "you": 1},
                     info=SessionInfo(title=title, first_prompt="hello there", started="2026-10-10T10:00:00.123Z",
                                      last_active=1791785041.0))


class CliCase(unittest.TestCase):
    """Isolated HOME, FakeTerminal pane "p1", FakeAgent, an empty FakeProcs, and a recording launcher."""

    def setUp(self):
        home = temp_home()
        self.home = home.__enter__()
        self.addCleanup(home.__exit__, None, None, None)
        os.environ["FAKE_PANE"] = "p1"
        self.agent = FakeAgent()
        self.procs = FakeProcs()
        self.launched = []
        for patch in (
            mock.patch.object(cli, "detect_terminal",
                              lambda env: FakeTerminal() if FakeTerminal().detect(env) else None),
            mock.patch.object(cli, "get_agent", lambda name: self.agent if name == self.agent.name else None),
            mock.patch.object(cli, "all_agents", lambda: [self.agent]),
            mock.patch.object(cli, "Procs", lambda: self.procs),
            mock.patch.object(restore, "launch", self.launched.append),
            # Safety net: even a wrongly wired launcher must never exec or change this process's directory.
            mock.patch("os.execvp", side_effect=AssertionError("tests must never exec")),
            mock.patch("os.chdir", side_effect=AssertionError("tests must never chdir")),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_cli(self, *argv: str, stdin: str = "", tty: bool = False):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", FakeStdin(stdin, tty)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class CliBasicsTest(CliCase):
    def test_version(self):
        code, out, err = self.run_cli("--version")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "agent-resurrect 0.1.0")

    def test_unknown_command_is_a_usage_error(self):
        code, _, err = self.run_cli("bogus")
        self.assertEqual(code, 2)
        self.assertIn("usage", err)

    def test_auto_delegates_to_restore(self):
        seen = []
        with mock.patch.object(restore, "auto", lambda env, **kw: seen.append(env) or 7):
            code, out, err = self.run_cli("auto")
        self.assertEqual(code, 7)
        self.assertEqual(seen[0].get("FAKE_PANE"), "p1")

    def test_auto_failure_is_one_line_and_logged(self):
        with mock.patch.object(restore, "auto", side_effect=RuntimeError("kaput")):
            code, out, err = self.run_cli("auto")
        self.assertEqual(code, 1)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("kaput", err)
        self.assertIn("kaput", paths.log_path().read_text())

    def test_ctrl_c_returns_130(self):
        with mock.patch.object(restore, "auto", side_effect=KeyboardInterrupt):
            code, _, err = self.run_cli("auto")
        self.assertEqual(code, 130)
        self.assertNotIn("Traceback", err)


class CliHookTest(CliCase):
    START = {"session_id": "s1", "cwd": "/Users/me/proj", "source": "startup"}

    def assert_silent_zero(self, *argv, stdin=""):
        code, out, err = self.run_cli(*argv, stdin=stdin)
        self.assertEqual((code, out, err), (0, "", ""), argv)

    def test_hook_always_exits_zero_and_prints_nothing(self):
        for stdin in ("{not json", "", "[1, 2]", "null", "\x00\xff"):
            self.assert_silent_zero("hook", "fake", "SessionStart", stdin=stdin)
        self.assert_silent_zero("hook", "nope", "SessionStart", stdin=json.dumps(self.START))
        self.assert_silent_zero("hook")
        self.assert_silent_zero("hook", "fake")
        self.assert_silent_zero("hook", "fake", "SessionStart", "--bogus-flag", stdin=json.dumps(self.START))
        self.assertEqual(self.agent.hook_calls[-1][0], "SessionStart")  # extra args are ignored, not fatal
        self.agent.hook_calls.clear()
        del os.environ["FAKE_PANE"]
        self.assert_silent_zero("hook", "fake", "SessionStart", stdin=json.dumps(self.START))
        self.assertEqual(self.agent.hook_calls, [])

    def test_hook_swallows_agent_errors_and_output(self):
        class LoudAgent(FakeAgent):
            def __init__(self, exc):
                super().__init__()
                self.exc = exc

            def on_hook(self, event, payload, ctx):
                print("noise on stdout")
                print("noise on stderr", file=sys.stderr)
                raise self.exc

        for exc in (RuntimeError("boom"), SystemExit(3), KeyboardInterrupt()):
            self.agent = LoudAgent(exc)
            self.assert_silent_zero("hook", "fake", "SessionStart", stdin=json.dumps(self.START))
        log = paths.log_path().read_text()
        self.assertIn("boom", log)
        self.assertIn("noise on stdout", log)

    def test_hook_with_terminal_stdin_does_not_block(self):
        # Run by hand in a terminal, reading stdin would wait for typing; the hook must not read it at all.
        code, out, err = self.run_cli("hook", "fake", "SessionStart", stdin=json.dumps(self.START), tty=True)
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(self.agent.hook_calls, [])

    def test_hook_passes_context_to_agent(self):
        self.assert_silent_zero("hook", "fake", "SessionStart", stdin=json.dumps(self.START))
        (event, payload, ctx), = self.agent.hook_calls
        self.assertEqual(event, "SessionStart")
        self.assertEqual(payload, self.START)
        self.assertEqual(ctx.pane_key, "fake-p1")
        self.assertEqual(ctx.pane_label, "p1")
        self.assertEqual(ctx.pid, os.getpid())
        self.assertEqual(ctx.terminal.name, "fake")
        self.assertIs(ctx.procs, self.procs)
        self.assertEqual(ctx.env.get("FAKE_PANE"), "p1")


class CliHereTest(CliCase):
    def test_here_without_record_lists(self):
        put(make_record(pane_key="fake-p2", session_id="s-2", pane_label="p2"), 5)
        code, out, err = self.run_cli()
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("Nothing recorded for this pane."))
        self.assertIn('"title of s-2"', out)
        self.assertEqual(self.launched, [])

    def test_here_outside_a_terminal_lists(self):
        del os.environ["FAKE_PANE"]
        code, out, _ = self.run_cli("here")
        self.assertEqual(code, 0)
        self.assertIn("Nothing recorded for this pane.", out)
        self.assertIn("No sessions to resurrect.", out)

    def test_here_resumes(self):
        put(make_record(), 5)
        code, out, err = self.run_cli()
        self.assertEqual(code, 0, err)
        (plan,) = self.launched
        self.assertEqual(plan.argv, ["fake-agent", "--resume", "s-1"])
        self.assertEqual(plan.cwd, "/tmp")
        self.assertEqual(store.load("fake-p1").status, "consumed")

    def test_here_ignores_consumed_and_skipped(self):
        for status in ("consumed", "skipped"):
            put(make_record(status=status), 5)
            self.assertEqual(self.run_cli("here")[0], 0)
        self.assertEqual(len(self.launched), 2)

    def test_here_refuses_a_running_agent(self):
        put(make_record(), 5)
        self.procs = FakeProcs(proc(4242, 1, "fake-agent"))
        code, out, _ = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("That session is still running (pid 4242).", out)
        self.assertEqual(self.launched, [])

    def test_here_reports_a_gone_session(self):
        put(make_record(), 5)
        self.agent = FakeAgent(exists=False)
        code, out, _ = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("s-1", out)
        self.assertEqual(self.launched, [])
        self.assertIsNotNone(store.load("fake-p1"))

    def test_here_unknown_agent(self):
        put(make_record(agent="mystery"), 5)
        code, _, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("mystery", err)
        self.assertEqual(self.launched, [])


class CliListTest(CliCase):
    def test_ls_orders_newest_first_and_hides_running(self):
        put(make_record(pane_key="fake-p1", session_id="s-old", pane_label="p1"), 30)
        put(make_record(pane_key="fake-p2", session_id="s-new", pane_label="p2", status="skipped"), 5)
        put(make_record(pane_key="fake-p3", session_id="s-running", pane_label="p3", agent_pid=777), 1)
        self.procs = FakeProcs(proc(777, 1, "fake-agent"))

        code, out, _ = self.run_cli("ls")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 2, out)
        self.assertTrue(lines[0].lstrip().startswith("1 "))
        self.assertIn('"title of s-new"', lines[0])
        self.assertIn("p2", lines[0])
        self.assertTrue(lines[0].rstrip().endswith("skipped"))
        self.assertTrue(lines[1].lstrip().startswith("2 "))
        self.assertIn('"title of s-old"', lines[1])
        self.assertNotIn("s-running", out)

        code, out, _ = self.run_cli("ls", "--all")
        lines = out.splitlines()
        self.assertEqual(len(lines), 3, out)
        self.assertIn("title of s-running", lines[0])
        self.assertTrue(lines[0].lstrip().startswith("- "))  # running rows get no number...
        self.assertTrue(lines[0].rstrip().endswith("running"))
        self.assertTrue(lines[1].lstrip().startswith("1 "))  # ...so numbers match the default listing
        self.assertIn("title of s-new", lines[1])

    def test_ls_collects_garbage_and_reports_empty(self):
        put(make_record(pane_key="fake-p9", session_id="s-ancient"), 20 * 24 * 60)
        code, out, _ = self.run_cli("ls")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "No sessions to resurrect.")
        self.assertIsNone(store.load("fake-p9"))

    def test_ls_shows_home_as_tilde_and_survives_unknown_agents(self):
        project = self.home / "proj"
        project.mkdir()
        put(make_record(agent="mystery", cwd=str(project)), 5)
        code, out, _ = self.run_cli("ls")
        self.assertEqual(code, 0)
        self.assertIn("mystery", out)
        self.assertIn("~/proj", out)
        self.assertIn('"s-1"', out)  # no adapter to describe it, so the session id is the title


HOSTILE = "\x1b]52;c;ZXZpbA==\x07\x1b[2J\x9b31m"  # OSC 52 clipboard write, clear screen, 8-bit CSI


def assert_no_control_chars(test, text):
    test.assertNotRegex(text.replace("\n", ""), r"[\x00-\x1f\x7f-\x9f]")


class CliTerminalSafetyTest(CliCase):
    """Text from transcripts, records and the environment must never reach the terminal as escape sequences."""

    def test_ls_and_resume_messages_neutralise_control_characters(self):
        put(make_record(session_id="s" + HOSTILE, cwd="/tmp/x" + HOSTILE, pane_label="p" + HOSTILE,
                        agent="fake"), 5)
        code, out, _ = self.run_cli("ls")
        self.assertEqual(code, 0)
        assert_no_control_chars(self, out)
        self.assertIn("?]52;c;ZXZpbA==??[2J?31m", out)  # visible, one "?" per control character

        self.agent = FakeAgent(exists=False)  # "Session <id> no longer exists" echoes the session id
        code, out, err = self.run_cli("resume", "1")
        self.assertEqual(code, 1)
        self.assertIn("no longer exists", out)
        assert_no_control_chars(self, out + err)

    def test_find_neutralises_control_characters(self):
        hostile = candidate("s-9" + HOSTILE, str(self.home), title="Fix" + HOSTILE)
        hostile.info.first_prompt = "hello" + HOSTILE
        hostile.roles = {"you" + HOSTILE: 1}

        class FindAgent(FakeAgent):
            def find_by_text(self, text, exclude=None, limit=5):
                return [hostile, candidate("s-8" + HOSTILE, None, title="Other" + HOSTILE)]

        self.agent = FindAgent()
        code, out, _ = self.run_cli("find", "some pasted words")
        self.assertEqual(code, 0)
        assert_no_control_chars(self, out)

    def test_install_steps_and_errors_neutralise_control_characters(self):
        fake = types.SimpleNamespace(install=lambda settings=None: [Step("x" + HOSTILE, "failed", "boom" + HOSTILE)])
        with mock.patch.object(cli, "installer", fake):
            code, out, _ = self.run_cli("install", "--yes")
        self.assertEqual(code, 1)
        assert_no_control_chars(self, out)
        code, _, err = self.run_cli("resume", "nope" + HOSTILE)
        self.assertEqual(code, 1)
        assert_no_control_chars(self, err)


class CliResumeTest(CliCase):
    def test_resume_by_number_and_by_session_id(self):
        put(make_record(pane_key="fake-p1", session_id="s-old"), 30)
        put(make_record(pane_key="fake-p2", session_id="s-new"), 5)

        code, _, err = self.run_cli("resume", "1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.launched[-1].argv, ["fake-agent", "--resume", "s-new"])

        code, _, err = self.run_cli("resume", "s-old")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.launched[-1].argv, ["fake-agent", "--resume", "s-old"])

        for bad in ("3", "0", "nope"):
            code, _, err = self.run_cli("resume", bad)
            self.assertEqual(code, 1, bad)
            self.assertIn(bad, err)
        self.assertEqual(len(self.launched), 2)

    def test_resume_unrecorded_session_from_agent_store(self):
        home = str(self.home)

        class StoreAgent(FakeAgent):
            def session_record(self, session_id):
                if session_id != "s-elsewhere":
                    return None
                return Record(terminal="", pane_key="", agent="fake", session_id=session_id, cwd=home)

        self.agent = StoreAgent()
        code, _, err = self.run_cli("resume", "s-elsewhere")
        self.assertEqual(code, 0, err)
        (plan,) = self.launched
        self.assertEqual(plan.argv, ["fake-agent", "--resume", "s-elsewhere"])
        self.assertEqual(plan.cwd, home)

    def test_resume_refuses_a_running_agent(self):
        put(make_record(pane_key="fake-p3", session_id="s-run", agent_pid=777), 1)
        self.procs = FakeProcs(proc(777, 1, "fake-agent"))
        code, out, _ = self.run_cli("resume", "s-run")
        self.assertEqual(code, 1)
        self.assertIn("That session is still running (pid 777).", out)
        self.assertEqual(self.launched, [])


class CliFindTest(CliCase):
    def use_results(self, *results, error=None):
        searched = []

        class FindAgent(FakeAgent):
            def find_by_text(self, text, exclude=None, limit=5):
                searched.append((text, limit))
                if error:
                    raise error
                return list(results)

        self.agent = FindAgent()
        return searched

    def test_find_reads_stdin_and_prints_command(self):
        home = str(self.home)
        searched = self.use_results(candidate("s-9", home), candidate("s-8", None, hits=2, title="Other one"))
        paste = 'He said "deploy the uat2 worker first" and then\nwe merged'
        code, out, err = self.run_cli("find", stdin=paste)
        self.assertEqual(code, 0, err)
        self.assertEqual(searched, [(paste, 5)])
        self.assertIn("BEST MATCH", out)
        self.assertIn("3/4 probes; text appears in: Claude x2, you x1", out)
        self.assertIn("s-9", out)
        self.assertIn("Fix login", out)
        self.assertIn("hello there", out)
        self.assertIn("Resume command:", out)
        self.assertIn(f"cd {shlex.quote(home)} && fake-agent --resume s-9", out)
        self.assertIn("Other candidates:", out)
        self.assertIn("2. s-8", out)
        self.assertEqual(self.launched, [])

    def test_find_joins_arguments_and_honours_limit(self):
        searched = self.use_results(candidate("s-9", None))
        code, out, _ = self.run_cli("find", "--limit", "2", "deploy", "the", "worker")
        self.assertEqual(code, 0)
        self.assertEqual(searched, [("deploy the worker", 2)])
        self.assertIn("fake-agent --resume s-9", out)

    def test_find_clipboard(self):
        searched = self.use_results(candidate("s-9", None))
        with mock.patch.object(cli, "_read_clipboard", return_value="text from the clipboard"):
            code, _, _ = self.run_cli("find", "--clipboard")
        self.assertEqual(code, 0)
        self.assertEqual(searched[0][0], "text from the clipboard")

    def test_find_warns_about_a_missing_directory_and_weak_match(self):
        self.use_results(candidate("s-9", str(self.home / "gone"), hits=1))
        code, out, _ = self.run_cli("find", "some words here")
        self.assertEqual(code, 0)
        self.assertIn("WARNING", out)
        self.assertIn("NOTE", out)
        self.assertIn("\n  fake-agent --resume s-9\n", out)  # no cd into a directory that is gone

    def test_find_empty_text_returns_2(self):
        searched = self.use_results(candidate("s-9", None))
        for kwargs in ({"stdin": "  \n"}, {"tty": True}):
            code, out, err = self.run_cli("find", **kwargs)
            self.assertEqual(code, 2, kwargs)
            self.assertTrue(err.strip())
        self.assertEqual(searched, [])

    def test_find_too_short_returns_2(self):
        self.use_results(error=ValueError("text is too short to search for"))
        code, _, err = self.run_cli("find", "ok")
        self.assertEqual(code, 2)
        self.assertIn("too short", err)

    def test_find_without_match(self):
        self.use_results()
        code, out, _ = self.run_cli("find", "nothing has this text")
        self.assertEqual(code, 1)
        self.assertIn("No session contains that text.", out)
        self.assertEqual(self.launched, [])


class CliConfigTest(CliCase):
    def test_config_get_set_and_invalid(self):
        code, out, _ = self.run_cli("config")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["auto_resume = countdown", "countdown_seconds = 3",
                                            "permission_flag = mirror", "max_age_days = 14"])
        self.assertEqual(self.run_cli("config", "auto_resume", "immediate")[0], 0)
        code, out, _ = self.run_cli("config", "auto_resume")
        self.assertEqual((code, out.strip()), (0, "immediate"))
        self.assertIn("auto_resume = immediate", paths.config_path().read_text())

        for argv in (("auto_resume", "sometimes"), ("countdown_seconds", "-1"), ("bogus",), ("bogus", "1")):
            code, _, err = self.run_cli("config", *argv)
            self.assertEqual(code, 2, argv)
            self.assertTrue(err.startswith("agent-resurrect: "), err)
        self.assertEqual(self.run_cli("config", "auto_resume")[1].strip(), "immediate")


class CliInstallTest(CliCase):
    def fake_installer(self, install_results=None):
        calls = []
        results = install_results or [Step("macos.keep_windows", "changed"),
                                      Step("iterm.restore_policy", "unchanged", "already set")]

        def install(settings=None):
            calls.append(("install",))
            return results

        def uninstall(settings=None, purge=False):
            calls.append(("uninstall", purge))
            return [Step("macos.keep_windows", "reverted")]

        patch = mock.patch.object(cli, "installer", types.SimpleNamespace(install=install, uninstall=uninstall))
        patch.start()
        self.addCleanup(patch.stop)
        return calls

    def test_install_and_uninstall_delegate(self):
        calls = self.fake_installer()
        with mock.patch.object(cli, "_interactive", return_value=True), \
                mock.patch.object(cli, "_confirm", side_effect=AssertionError("prompted")):
            code, out, _ = self.run_cli("install", "--yes")
            self.assertEqual(code, 0)
            self.assertIn("changed    macos.keep_windows", out)
            self.assertIn("unchanged  iterm.restore_policy  already set", out)
            code, out, _ = self.run_cli("uninstall", "--yes", "--purge")
            self.assertEqual(code, 0)
            self.assertIn("reverted   macos.keep_windows", out)
        self.assertEqual(calls, [("install",), ("uninstall", True)])

    def test_install_asks_on_a_terminal(self):
        calls = self.fake_installer()
        with mock.patch.object(cli, "all_settings", lambda: [DescribedSetting()]), \
                mock.patch.object(cli, "_interactive", return_value=True), \
                mock.patch.object(cli, "_confirm", return_value=False) as confirm:
            code, out, _ = self.run_cli("install")
        self.assertEqual(code, 1)
        self.assertIn("Turn the test setting on.", out)
        self.assertIn("Proceed? [Y/n]", confirm.call_args[0][0])
        self.assertEqual(calls, [])

    def test_install_without_a_terminal_proceeds(self):
        calls = self.fake_installer()
        with mock.patch.object(cli, "_confirm", side_effect=AssertionError("prompted")):
            self.assertEqual(self.run_cli("install")[0], 0)
        self.assertEqual(calls, [("install",)])

    def test_install_failure_returns_1(self):
        self.fake_installer([Step("claude.plugin", "failed", "claude exited 1")])
        code, out, _ = self.run_cli("install", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("failed     claude.plugin  claude exited 1", out)

    def test_uninstall_needs_confirmation(self):
        calls = self.fake_installer()
        code, _, err = self.run_cli("uninstall")  # not a terminal and no --yes
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        with mock.patch.object(cli, "_interactive", return_value=True), \
                mock.patch.object(cli, "_confirm", return_value=False):
            self.assertEqual(self.run_cli("uninstall")[0], 1)
        with mock.patch.object(cli, "_interactive", return_value=True), \
                mock.patch.object(cli, "_confirm", return_value=True):
            self.assertEqual(self.run_cli("uninstall")[0], 0)
        self.assertEqual(calls, [("uninstall", False)])

    def test_confirm_defaults_and_eof(self):
        with mock.patch("builtins.input", return_value=""), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(cli._confirm("Proceed? [Y/n] "))
        with mock.patch("builtins.input", return_value="n"):
            self.assertFalse(cli._confirm("Proceed? [Y/n] "))
        with mock.patch("builtins.input", side_effect=EOFError), contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(cli._confirm("Proceed? [Y/n] "))


class CliEntryPointTest(unittest.TestCase):
    def test_entry_point_keeps_the_main_guard(self):
        # Text search uses multiprocessing with spawn, which re-imports this file in every worker.
        text = BIN.read_text()
        self.assertIn('if __name__ == "__main__":', text)
        self.assertLess(text.index('if __name__ == "__main__":'), text.index("from agent_resurrect.cli import main"))
        self.assertTrue(os.access(BIN, os.X_OK))

    def run_bin(self, *argv, stdin=b""):
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
            return subprocess.run([sys.executable, "-I", str(BIN), *argv], input=stdin, env=env,
                                  capture_output=True, timeout=30)

    @unittest.skipUnless(REAL_SIBLINGS, "needs restore.py and installer.py (merged at integration)")
    def test_entry_point_runs_isolated(self):
        result = self.run_bin("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode().strip(), "agent-resurrect 0.1.0")

    @unittest.skipUnless(REAL_SIBLINGS, "needs restore.py and installer.py (merged at integration)")
    def test_entry_point_hook_is_silent(self):
        result = self.run_bin("hook", "claude", "SessionStart", stdin=b"{garbage")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))


if __name__ == "__main__":
    unittest.main()
