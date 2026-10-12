"""Tests for the command line. Agents, terminals and processes are faked; nothing is ever launched."""
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest import mock

import agent_resurrect
from agent_resurrect import cli, exitcodes, paths, restore, store
from agent_resurrect.config import Config
from agent_resurrect.model import ISO_FORMAT, STATUS_LIVE, Candidate, Record, SessionInfo
from agent_resurrect.settings.base import Check, Setting
from tests.helpers import FakeAgent, FakeProcs, FakeTerminal, make_record, proc, temp_home

PACKAGE_DIR = Path(agent_resurrect.__file__).resolve().parent
REPO = PACKAGE_DIR.parent.parent
# The Python entry point; bin/agent-resurrect is the POSIX sh wrapper that runs it.
ENTRY = REPO / "libexec" / "agent-resurrect.py"

OK, ERROR, USAGE, NOT_FOUND = exitcodes.OK, exitcodes.ERROR, exitcodes.USAGE, exitcodes.NOT_FOUND
NEEDS_TTY, BUSY, GONE = exitcodes.NEEDS_TTY, exitcodes.BUSY, exitcodes.GONE


# ---------------------------------------------------------------------------------------------- fixtures

@dataclass
class Step:
    id: str
    status: str
    detail: str = ""


class FakeStream(io.StringIO):
    """A text stream that says whether it is a terminal."""

    def __init__(self, text: str = "", tty: bool = False):
        super().__init__(text)
        self._tty = tty

    def isatty(self):
        return self._tty


class DescribedSetting(Setting):
    def __init__(self, id_="test.setting", description="Turn the test setting on."):
        self.id, self.description = id_, description

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


def snapshot_state() -> dict:
    """Every file under the state directory, byte for byte."""
    root = paths.state_dir()
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()} \
        if root.is_dir() else {}


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
        patches = [
            mock.patch.object(cli, "detect_terminal",
                              lambda env: FakeTerminal() if FakeTerminal().detect(env) else None),
            mock.patch.object(cli, "get_agent", lambda name: self.agent if name == self.agent.name else None),
            mock.patch.object(cli, "all_agents", lambda: [self.agent]),
            mock.patch.object(cli, "Procs", lambda: self.procs),
            mock.patch.object(restore, "launch", self.launched.append),
            # Safety net: even a wrongly wired launcher must never exec or change this process's directory.
            mock.patch("os.chdir", side_effect=AssertionError("tests must never chdir")),
            *[mock.patch(f"os.{name}", side_effect=AssertionError("tests must never exec"))
              for name in ("execv", "execve", "execvp", "execvpe")],
        ]
        for patch in patches:
            started = patch.start()
            self.addCleanup(patch.stop)
            if isinstance(started, mock.Mock) and patch.attribute != "launch":
                self.addCleanup(lambda m=started, name=patch.attribute: self.assertFalse(m.called, f"os.{name} called"))

    def run_cli(self, *argv: str, stdin: str = "", tty: bool = False, stdout_tty: Optional[bool] = None):
        """Run main(argv). `tty` makes stdin and stdout terminals; `stdout_tty` overrides stdout's."""
        out = FakeStream(tty=tty if stdout_tty is None else stdout_tty)
        err = io.StringIO()
        with mock.patch("sys.stdin", FakeStream(stdin, tty)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------------------------- basics

class CliBasicsTest(CliCase):
    def test_version(self):
        for argv in (("--version",), ("version",)):
            code, out, err = self.run_cli(*argv)
            self.assertEqual((code, out.strip()), (OK, f"agent-resurrect {agent_resurrect.__version__}"), argv)

    def test_unknown_command_is_a_usage_error(self):
        code, out, err = self.run_cli("bogus")
        self.assertEqual(code, USAGE)
        self.assertEqual(out, "")
        self.assertIn("usage", err)

    def test_bad_flag_is_a_usage_error(self):
        code, out, err = self.run_cli("ls", "--bogus")
        self.assertEqual(code, USAGE)
        self.assertIn("unrecognized arguments: --bogus", err)

    def test_help_lists_every_command(self):
        code, out, _ = self.run_cli("--help")
        self.assertEqual(code, OK)
        for command in ("here", "ls", "resume", "find", "simulate", "adopt", "doctor", "install", "uninstall",
                        "config", "version"):
            self.assertIn(command, out)
        self.assertIn("Exit codes", out)

    def test_auto_delegates_to_restore(self):
        seen = []
        with mock.patch.object(restore, "auto", lambda env, **kw: seen.append(env) or 7):
            code, out, err = self.run_cli("auto")
        self.assertEqual(code, 7)
        self.assertEqual(seen[0].get("FAKE_PANE"), "p1")

    def test_auto_failure_is_one_line_and_logged(self):
        with mock.patch.object(restore, "auto", side_effect=RuntimeError("kaput")):
            code, out, err = self.run_cli("auto")
        self.assertEqual(code, ERROR)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("kaput", err)
        self.assertIn("kaput", paths.log_path().read_text())

    def test_ctrl_c_returns_130(self):
        with mock.patch.object(restore, "auto", side_effect=KeyboardInterrupt):
            code, _, err = self.run_cli("auto")
        self.assertEqual(code, exitcodes.INTERRUPTED)
        self.assertNotIn("Traceback", err)

    def test_unexpected_error_is_one_line_and_logged(self):
        with mock.patch.dict(cli.COMMANDS, {"ls": mock.Mock(side_effect=RuntimeError("disk on fire"))}):
            code, out, err = self.run_cli("ls")
        self.assertEqual(code, ERROR)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn("disk on fire", err)
        self.assertNotIn("Traceback", err)
        self.assertIn("disk on fire", paths.log_path().read_text())

    def test_bare_command_takes_here_flags(self):
        put(make_record(), 5)
        code, out, _ = self.run_cli("--print")
        self.assertEqual((code, out), (OK, "cd /tmp && fake-agent --resume s-1\n"))


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
        self.assert_silent_zero("hook", "fake", "SessionStart", "--json", stdin=json.dumps(self.START))
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


# ---------------------------------------------------------------------------------------------- here, resume

class CliHereTest(CliCase):
    def test_here_without_record_lists_and_is_not_found(self):
        put(make_record(pane_key="fake-p2", session_id="s-2", pane_label="p2"), 5)
        code, out, err = self.run_cli(tty=True)
        self.assertEqual(code, NOT_FOUND)
        self.assertTrue(out.startswith("Nothing recorded for this pane."))
        self.assertIn('"title of s-2"', out)
        self.assertEqual(self.launched, [])

    def test_here_outside_a_terminal_lists(self):
        del os.environ["FAKE_PANE"]
        code, out, _ = self.run_cli("here")
        self.assertEqual(code, NOT_FOUND)
        self.assertIn("Nothing recorded for this pane.", out)
        self.assertIn("No sessions to resurrect.", out)

    def test_here_resumes_on_a_terminal(self):
        put(make_record(), 5)
        code, out, err = self.run_cli(tty=True)
        self.assertEqual(code, OK, err)
        (plan,) = self.launched
        self.assertEqual(plan.argv, ["fake-agent", "--resume", "s-1"])
        self.assertEqual(plan.cwd, "/tmp")
        self.assertEqual(store.load("fake-p1").status, "consumed")

    def test_here_resumes_consumed_and_skipped_records_too(self):
        for status in ("consumed", "skipped"):
            put(make_record(status=status), 5)
            self.assertEqual(self.run_cli("here", tty=True)[0], OK)
        self.assertEqual(len(self.launched), 2)

    def test_here_refuses_a_running_agent(self):
        put(make_record(), 5)
        self.procs = FakeProcs(proc(4242, 1, "fake-agent"))
        code, out, err = self.run_cli(tty=True)
        self.assertEqual(code, BUSY)
        self.assertIn("still running (pid 4242)", err)
        self.assertEqual(self.launched, [])

    def test_here_reports_a_gone_session(self):
        put(make_record(), 5)
        self.agent = FakeAgent(exists=False)
        code, out, err = self.run_cli(tty=True)
        self.assertEqual(code, GONE)
        self.assertIn("s-1", err)
        self.assertIn("no longer exists", err)
        self.assertEqual(self.launched, [])
        self.assertIsNotNone(store.load("fake-p1"))

    def test_here_unknown_agent(self):
        put(make_record(agent="mystery"), 5)
        code, _, err = self.run_cli(tty=True)
        self.assertEqual(code, ERROR)
        self.assertIn("mystery", err)
        self.assertEqual(self.launched, [])


class CliNoTerminalTest(CliCase):
    """P0-1: resume and here never launch an agent unless stdin and stdout are both terminals."""

    def test_resume_and_here_without_a_terminal_print_the_command_and_change_nothing(self):
        put(make_record(), 5)
        before = snapshot_state()
        for streams in ({"tty": False}, {"tty": True, "stdout_tty": False}, {"tty": False, "stdout_tty": True}):
            for argv in (("resume", "s-1"), ("resume", "1"), ("here",), ()):
                code, out, err = self.run_cli(*argv, **streams)
                self.assertEqual(code, NEEDS_TTY, (argv, streams, err))
                self.assertEqual(out, "cd /tmp && fake-agent --resume s-1\n", (argv, streams))
                self.assertIn("without a terminal", err)
                self.assertIn("--print", err)
        self.assertEqual(snapshot_state(), before)  # the record is byte-identical; no restore event
        self.assertFalse(paths.restore_events_path().exists())
        self.assertEqual(self.launched, [])

    def test_print_prints_the_command_and_changes_nothing(self):
        put(make_record(), 5)
        before = snapshot_state()
        for tty in (False, True):
            for argv in (("resume", "s-1", "--print"), ("here", "--print"), ("--print",)):
                code, out, err = self.run_cli(*argv, tty=tty)
                self.assertEqual((code, out, err), (OK, "cd /tmp && fake-agent --resume s-1\n", ""), argv)
        self.assertEqual(snapshot_state(), before)
        self.assertEqual(self.launched, [])

    def test_print_puts_warnings_on_stderr(self):
        put(make_record(cwd=str(self.home / "gone")), 5)
        code, out, err = self.run_cli("resume", "s-1", "--print")
        self.assertEqual((code, out), (OK, "fake-agent --resume s-1\n"))
        self.assertIn("no longer exists", err)

    def test_refusals_come_before_the_terminal_check(self):
        put(make_record(), 5)
        self.procs = FakeProcs(proc(4242, 1, "fake-agent"))
        self.assertEqual(self.run_cli("resume", "s-1")[0], BUSY)
        self.procs = FakeProcs()
        self.agent = FakeAgent(exists=False)
        self.assertEqual(self.run_cli("resume", "s-1")[0], GONE)


class CliResumeTest(CliCase):
    def test_resume_by_number_and_by_session_id(self):
        put(make_record(pane_key="fake-p1", session_id="s-old"), 30)
        put(make_record(pane_key="fake-p2", session_id="s-new"), 5)

        code, _, err = self.run_cli("resume", "1", tty=True)
        self.assertEqual(code, OK, err)
        self.assertEqual(self.launched[-1].argv, ["fake-agent", "--resume", "s-new"])

        code, _, err = self.run_cli("resume", "s-old", tty=True)
        self.assertEqual(code, OK, err)
        self.assertEqual(self.launched[-1].argv, ["fake-agent", "--resume", "s-old"])

        for bad in ("3", "0", "nope"):
            code, _, err = self.run_cli("resume", bad, tty=True)
            self.assertEqual(code, NOT_FOUND, bad)
            self.assertIn(bad, err)
        self.assertEqual(len(self.launched), 2)

    def test_resume_empty_target_is_a_usage_error(self):
        code, _, err = self.run_cli("resume", "  ", tty=True)
        self.assertEqual(code, USAGE)
        self.assertEqual(self.launched, [])

    def test_resume_unrecorded_session_from_agent_store(self):
        home = str(self.home)

        class StoreAgent(FakeAgent):
            def session_record(self, session_id):
                if session_id != "s-elsewhere":
                    return None
                return Record(terminal="", pane_key="", agent="fake", session_id=session_id, cwd=home)

        self.agent = StoreAgent()
        code, _, err = self.run_cli("resume", "s-elsewhere", tty=True)
        self.assertEqual(code, OK, err)
        (plan,) = self.launched
        self.assertEqual(plan.argv, ["fake-agent", "--resume", "s-elsewhere"])
        self.assertEqual(plan.cwd, home)

    def test_resume_refuses_a_running_agent(self):
        put(make_record(pane_key="fake-p3", session_id="s-run", agent_pid=777), 1)
        self.procs = FakeProcs(proc(777, 1, "fake-agent"))
        code, out, err = self.run_cli("resume", "s-run", tty=True)
        self.assertEqual(code, BUSY)
        self.assertIn("still running (pid 777)", err)
        self.assertEqual(self.launched, [])

    def test_failed_launch_is_an_error(self):
        put(make_record(), 5)
        with mock.patch.object(restore, "launch", side_effect=OSError("No such file or directory")):
            code, out, err = self.run_cli("resume", "s-1", tty=True)
        self.assertEqual(code, ERROR)
        self.assertIn("No such file or directory", out + err)
        self.assertNotEqual(store.load("fake-p1").status, STATUS_LIVE)  # a new shell must not retry forever


# ---------------------------------------------------------------------------------------------- ls

class CliListTest(CliCase):
    def test_ls_orders_newest_first_and_hides_running(self):
        put(make_record(pane_key="fake-p1", session_id="s-old", pane_label="p1"), 30)
        put(make_record(pane_key="fake-p2", session_id="s-new", pane_label="p2", status="skipped"), 5)
        put(make_record(pane_key="fake-p3", session_id="s-running", pane_label="p3", agent_pid=777), 1)
        self.procs = FakeProcs(proc(777, 1, "fake-agent"))

        code, out, _ = self.run_cli("ls")
        self.assertEqual(code, OK)
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

    def test_ls_ids_adds_the_session_id_column(self):
        put(make_record(pane_key="fake-p1", session_id="11111111-2222-3333-4444-555555555555"), 5)
        code, out, _ = self.run_cli("ls")
        self.assertRegex(out, r"^1  fake  ")
        code, out, _ = self.run_cli("ls", "--ids")
        self.assertEqual(code, OK)
        self.assertRegex(out, r"^1  11111111-2222-3333-4444-555555555555  fake  ")

    def test_ls_collects_garbage_and_reports_empty(self):
        put(make_record(pane_key="fake-p9", session_id="s-ancient", agent_pid=None), 20 * 24 * 60)
        code, out, _ = self.run_cli("ls")
        self.assertEqual(code, OK)
        self.assertEqual(out.strip(), "No sessions to resurrect.")
        self.assertIsNone(store.load("fake-p9"))

    def test_ls_keeps_old_records_of_running_sessions(self):
        put(make_record(pane_key="fake-p9", session_id="s-weeks", agent_pid=777), 20 * 24 * 60)
        self.procs = FakeProcs(proc(777, 1, "fake-agent"))
        self.run_cli("ls")
        self.assertIsNotNone(store.load("fake-p9"))

    def test_ls_shows_home_as_tilde_and_survives_unknown_agents(self):
        project = self.home / "proj"
        project.mkdir()
        put(make_record(agent="mystery", cwd=str(project)), 5)
        code, out, _ = self.run_cli("ls")
        self.assertEqual(code, OK)
        self.assertIn("mystery", out)
        self.assertIn("~/proj", out)
        self.assertIn('"s-1"', out)  # no adapter to describe it, so the session id is the title


# ---------------------------------------------------------------------------------------------- safety

HOSTILE = "\x1b]52;c;ZXZpbA==\x07\x1b[2J\x9b31m"  # OSC 52 clipboard write, clear screen, 8-bit CSI


def assert_no_control_chars(test, text):
    test.assertNotRegex(text.replace("\n", ""), r"[\x00-\x1f\x7f-\x9f]")


class CliTerminalSafetyTest(CliCase):
    """Text from transcripts, records and the environment must never reach the terminal as escape sequences."""

    def test_ls_and_resume_messages_neutralise_control_characters(self):
        put(make_record(session_id="s" + HOSTILE, cwd="/tmp/x" + HOSTILE, pane_label="p" + HOSTILE,
                        agent="fake"), 5)
        for argv in (("ls",), ("ls", "--ids")):
            code, out, _ = self.run_cli(*argv)
            self.assertEqual(code, OK)
            assert_no_control_chars(self, out)
            self.assertIn("?]52;c;ZXZpbA==??[2J?31m", out)  # visible, one "?" per control character

        for argv in (("resume", "1"), ("resume", "1", "--print"), ("here",), ("simulate",)):
            code, out, err = self.run_cli(*argv)
            assert_no_control_chars(self, out + err)

        self.agent = FakeAgent(exists=False)  # "Session <id> no longer exists" echoes the session id
        code, out, err = self.run_cli("resume", "1")
        self.assertEqual(code, GONE)
        self.assertIn("no longer exists", err)
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
        self.assertEqual(code, OK)
        assert_no_control_chars(self, out)

    def test_adopt_neutralises_control_characters(self):
        recorded = make_record(session_id="s" + HOSTILE, pane_label="p" + HOSTILE)

        class AdoptAgent(FakeAgent):
            def adopt(self, ctx):
                return recorded

        self.agent = AdoptAgent()
        code, out, err = self.run_cli("adopt")
        self.assertEqual(code, OK)
        assert_no_control_chars(self, out + err)

    def test_install_steps_and_errors_neutralise_control_characters(self):
        fake = types.SimpleNamespace(install=lambda settings=None, dry_run=False: [
            Step("x" + HOSTILE, "failed", "boom" + HOSTILE)])
        with mock.patch.object(cli, "installer", fake):
            code, out, _ = self.run_cli("install", "--yes")
        self.assertEqual(code, ERROR)
        assert_no_control_chars(self, out)
        code, _, err = self.run_cli("resume", "nope" + HOSTILE)
        self.assertEqual(code, NOT_FOUND)
        assert_no_control_chars(self, err)


# ---------------------------------------------------------------------------------------------- find

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
        self.assertEqual(code, OK, err)
        self.assertEqual(searched, [(paste, 5)])
        self.assertIn("BEST MATCH", out)
        self.assertIn("3/4 probes; text appears in: Claude x2, you x1", out)
        self.assertIn("s-9", out)
        self.assertIn("Fix login", out)
        self.assertIn("hello there", out)
        self.assertIn("Resume command:", out)
        self.assertIn(f"cd {shlex.quote(home)} && fake-agent --resume s-9", out)
        self.assertIn("Other candidates:", out)
        self.assertIn("\n  s-8  2/4", out)
        self.assertIn("agent-resurrect resume <session id>", out)
        self.assertEqual(self.launched, [])

    def test_find_candidates_are_not_numbered(self):
        # P0-2: "resume 2" means the second ls entry, so find must not print numbers an agent could reuse.
        self.use_results(candidate("s-9", None), candidate("s-8", None, hits=2), candidate("s-7", None, hits=1))
        code, out, _ = self.run_cli("find", "some words here")
        self.assertEqual(code, OK)
        self.assertNotRegex(out, re.compile(r"^\s*\d+[.)]\s", re.MULTILINE))
        for session_id in ("s-8", "s-7"):
            self.assertRegex(out, re.compile(rf"^  {session_id}  ", re.MULTILINE))

    def test_find_joins_arguments_and_honours_limit(self):
        searched = self.use_results(candidate("s-9", None))
        code, out, _ = self.run_cli("find", "--limit", "2", "deploy", "the", "worker")
        self.assertEqual(code, OK)
        self.assertEqual(searched, [("deploy the worker", 2)])
        self.assertIn("fake-agent --resume s-9", out)

    def test_find_clipboard(self):
        searched = self.use_results(candidate("s-9", None))
        with mock.patch.object(cli, "_read_clipboard", return_value="text from the clipboard"):
            code, _, _ = self.run_cli("find", "--clipboard")
        self.assertEqual(code, OK)
        self.assertEqual(searched[0][0], "text from the clipboard")

    def test_find_clipboard_failure_is_an_error(self):
        searched = self.use_results(candidate("s-9", None))
        with mock.patch.object(cli, "_read_clipboard", return_value=None):
            code, _, err = self.run_cli("find", "--clipboard")
        self.assertEqual(code, ERROR)
        self.assertIn("clipboard", err)
        self.assertEqual(searched, [])

    def test_find_warns_about_a_missing_directory_and_weak_match(self):
        self.use_results(candidate("s-9", str(self.home / "gone"), hits=1))
        code, out, _ = self.run_cli("find", "some words here")
        self.assertEqual(code, OK)
        self.assertIn("WARNING", out)
        self.assertIn("NOTE", out)
        self.assertIn("\n  fake-agent --resume s-9\n", out)  # no cd into a directory that is gone

    def test_find_empty_text_is_a_usage_error(self):
        searched = self.use_results(candidate("s-9", None))
        for kwargs in ({"stdin": "  \n"}, {"tty": True}):
            code, out, err = self.run_cli("find", **kwargs)
            self.assertEqual(code, USAGE, kwargs)
            self.assertTrue(err.strip())
        self.assertEqual(searched, [])

    def test_find_too_short_is_a_usage_error(self):
        self.use_results(error=ValueError("text is too short to search for"))
        code, _, err = self.run_cli("find", "ok")
        self.assertEqual(code, USAGE)
        self.assertIn("too short", err)

    def test_find_without_match_is_not_found(self):
        self.use_results()
        code, out, _ = self.run_cli("find", "nothing has this text")
        self.assertEqual(code, NOT_FOUND)
        self.assertIn("No session contains that text.", out)
        self.assertEqual(self.launched, [])


# ---------------------------------------------------------------------------------------------- simulate

class GoneAgent(FakeAgent):
    """session_exists is False only for session "s-gone"."""

    def session_exists(self, record):
        return record.session_id != "s-gone"


class CliSimulateTest(CliCase):
    KEYS = ("fake-p1", "fake-p2", "fake-p3", "fake-p4", "fake-p5", "fake-p6")

    def setUp(self):
        super().setUp()
        self.agent = GoneAgent()
        put(make_record(pane_key="fake-p1", session_id="s-ready", pane_label="p1", agent_pid=None), 5)
        put(make_record(pane_key="fake-p2", session_id="s-running", pane_label="p2", agent_pid=777), 6)
        put(make_record(pane_key="fake-p3", session_id="s-used", pane_label="p3", status="consumed"), 7)
        put(make_record(pane_key="fake-p4", session_id="s-old", pane_label="p4"), 20 * 24 * 60)
        put(make_record(pane_key="fake-p5", session_id="s-gone", pane_label="p5", agent_pid=None), 8)
        put(make_record(pane_key="fake-p6", session_id="s-who", pane_label="p6", agent="mystery"), 9)
        self.procs = FakeProcs(proc(777, 1, "fake-agent"))

    def decisions(self, *flags):
        code, out, err = self.run_cli("simulate", "--json", *flags)
        self.assertEqual(code, OK, err)
        return {p["pane"]["key"]: (p["decision"], p["reason"]) for p in json.loads(out)["panes"]}

    def test_decisions_follow_the_restore_rules(self):
        self.assertEqual(self.decisions(), {
            "fake-p1": ("would-resume", "ok"),
            "fake-p2": ("would-skip", "agent-running"),
            "fake-p3": ("would-skip", "not-live"),
            "fake-p4": ("would-skip", "too-old"),
            "fake-p5": ("would-skip", "session-gone"),
            "fake-p6": ("would-skip", "unknown-agent"),
        })
        self.assertEqual(self.decisions("--assume-restart")["fake-p2"], ("would-resume", "ok"))

    def test_one_pane(self):
        self.assertEqual(self.decisions("--pane-key", "fake-p2"), {"fake-p2": ("would-skip", "agent-running")})
        self.assertEqual(self.decisions("--pane-key", "fake-p9"), {"fake-p9": ("no-record", None)})
        code, _, err = self.run_cli("simulate", "--pane-key", "../escape")
        self.assertEqual(code, USAGE)
        self.assertIn("pane key", err)

    def test_simulate_changes_nothing_and_launches_nothing(self):
        before = snapshot_state()
        for argv in (("simulate",), ("simulate", "--assume-restart", "--json"), ("simulate", "--pane-key", "fake-p1")):
            for tty in (False, True):
                self.assertEqual(self.run_cli(*argv, tty=tty)[0], OK)
        self.assertEqual(snapshot_state(), before)  # no gc, no status change, no restore event
        self.assertEqual(self.launched, [])

    def test_text_output(self):
        code, out, _ = self.run_cli("simulate", "--assume-restart")
        self.assertEqual(code, OK)
        self.assertIn("countdown", out)
        self.assertRegex(out, r"would-resume +p1 +\"title of s-ready\" +ok\n +cd /tmp && fake-agent --resume s-ready\n")
        self.assertRegex(out, r"would-resume +p2 ")
        self.assertRegex(out, r"would-skip +p4 +\"title of s-old\" +too-old\n")

    def test_no_records(self):
        for key in self.KEYS:
            store.delete(key)
        code, out, _ = self.run_cli("simulate")
        self.assertEqual((code, out), (OK, "No panes recorded.\n"))

    def test_assume_restart_agrees_with_auto(self):
        """For every fixture, simulate --assume-restart says would-resume exactly when the real restore.auto,
        run on a terminal after a restart (no agent processes), launches the agent."""
        decisions = self.decisions("--assume-restart")
        for key in self.KEYS:  # auto deletes gone records and collects old ones, so simulate runs first
            launched = []
            with mock.patch("sys.stdin", FakeStream(tty=True)):  # resume_record checks the real streams
                restore.auto({"FAKE_PANE": key[len("fake-"):]}, terminal=FakeTerminal(),
                             agent_lookup=lambda name: self.agent if name == self.agent.name else None,
                             procs=FakeProcs(), config=Config(auto_resume="immediate"),
                             stdin=FakeStream(tty=True), stdout=FakeStream(tty=True), launcher=launched.append,
                             isatty=lambda stream: True)
            self.assertEqual(bool(launched), decisions[key][0] == "would-resume", key)


# ---------------------------------------------------------------------------------------------- adopt

class AdoptAgent(FakeAgent):
    def __init__(self, result=None, error=None):
        super().__init__()
        self.result, self.error, self.calls = result, error, []

    def adopt(self, ctx):
        self.calls.append(ctx)
        if self.error:
            raise self.error
        return self.result


class CliAdoptTest(CliCase):
    def test_adopt_records_the_session_this_runs_in(self):
        self.agent = AdoptAgent(make_record(session_id="s-adopted", pane_label="p1"))
        code, out, err = self.run_cli("adopt")
        self.assertEqual(code, OK, err)
        self.assertIn("title of s-adopted", out)
        self.assertIn("p1", out)
        (ctx,) = self.agent.calls
        self.assertEqual((ctx.pane_key, ctx.pane_label, ctx.pid, ctx.terminal.name),
                         ("fake-p1", "p1", os.getpid(), "fake"))
        self.assertIs(ctx.procs, self.procs)
        self.assertEqual(ctx.env.get("FAKE_PANE"), "p1")

    def test_adopt_outside_a_supported_pane_is_not_found(self):
        self.agent = AdoptAgent(make_record())
        del os.environ["FAKE_PANE"]
        code, out, err = self.run_cli("adopt")
        self.assertEqual(code, NOT_FOUND)
        self.assertIn("pane", err)
        self.assertEqual(self.agent.calls, [])

    def test_adopt_outside_an_agent_session_is_not_found(self):
        self.agent = AdoptAgent(None)
        code, out, err = self.run_cli("adopt")
        self.assertEqual(code, NOT_FOUND)
        self.assertIn("session", err)

    def test_adopt_failure_is_an_error(self):
        # T2: adopt raises OSError only when the record cannot be saved; anything else is unexpected.
        for error, says in ((OSError("No space left on device"), "could not record the fake session"),
                            (RuntimeError("ps exploded"), "adopt failed")):
            self.agent = AdoptAgent(error=error)
            code, out, err = self.run_cli("adopt")
            self.assertEqual(code, ERROR)
            self.assertIn(says, err)
            self.assertIn(str(error), err)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertIn(str(error), paths.log_path().read_text())


# ---------------------------------------------------------------------------------------------- config

class CliConfigTest(CliCase):
    def test_config_get_set_and_invalid(self):
        code, out, _ = self.run_cli("config")
        self.assertEqual(code, OK)
        self.assertEqual(out.splitlines(), ["auto_resume = countdown", "countdown_seconds = 3",
                                            "permission_flag = mirror", "max_age_days = 14"])
        self.assertEqual(self.run_cli("config", "auto_resume", "immediate")[0], OK)
        code, out, _ = self.run_cli("config", "auto_resume")
        self.assertEqual((code, out.strip()), (OK, "immediate"))
        self.assertIn("auto_resume = immediate", paths.config_path().read_text())

        for argv in (("auto_resume", "sometimes"), ("countdown_seconds", "-1"), ("bogus",), ("bogus", "1")):
            code, _, err = self.run_cli("config", *argv)
            self.assertEqual(code, USAGE, argv)
            self.assertTrue(err.startswith("agent-resurrect: "), err)
        self.assertEqual(self.run_cli("config", "auto_resume")[1].strip(), "immediate")


# ---------------------------------------------------------------------------------------------- install

def setting_ids(settings):
    return None if settings is None else [setting.id for setting in settings]


class InstallCase(CliCase):
    def fake_installer(self, install_results=None, uninstall_results=None):
        calls = []
        results = install_results or [Step("iterm.keep_windows", "changed"),
                                      Step("iterm.restore_policy", "unchanged", "already set")]

        def install(settings=None, dry_run=False):
            calls.append(("install", setting_ids(settings), dry_run))
            return [Step(r.id, "would-change" if dry_run and r.status == "changed" else r.status, r.detail)
                    for r in results]

        def uninstall(settings=None, purge=False, dry_run=False):
            calls.append(("uninstall", purge, dry_run))
            return uninstall_results or [Step("iterm.keep_windows", "would-revert" if dry_run else "reverted")]

        patch = mock.patch.object(cli, "installer", types.SimpleNamespace(install=install, uninstall=uninstall))
        patch.start()
        self.addCleanup(patch.stop)
        return calls


class CliInstallTest(InstallCase):
    def test_install_and_uninstall_delegate(self):
        calls = self.fake_installer()
        with mock.patch.object(cli, "_confirm", side_effect=AssertionError("prompted")):
            code, out, _ = self.run_cli("install", "--yes", tty=True)
            self.assertEqual(code, OK)
            self.assertIn("changed    iterm.keep_windows", out)
            self.assertIn("unchanged  iterm.restore_policy  already set", out)
            self.assertIn("adopt", out)  # next steps: running sessions can be adopted
            code, out, _ = self.run_cli("uninstall", "--yes", "--purge", tty=True)
            self.assertEqual(code, OK)
            self.assertIn("reverted   iterm.keep_windows", out)
        self.assertEqual(calls, [("install", None, False), ("uninstall", True, False)])

    def test_install_no_plugin_leaves_out_the_plugin_setting(self):
        calls = self.fake_installer()
        settings = [DescribedSetting("shell.zsh"), DescribedSetting("claude.plugin", "Install the plugin.")]
        with mock.patch.object(cli, "all_settings", lambda: list(settings)):
            self.assertEqual(self.run_cli("install", "--yes", "--no-plugin")[0], OK)
            with mock.patch.object(cli, "_confirm", return_value=False) as confirm:
                code, out, _ = self.run_cli("install", "--no-plugin", tty=True)
        self.assertEqual(calls, [("install", ["shell.zsh"], False)])
        self.assertEqual(code, ERROR)  # declined
        self.assertNotIn("Install the plugin.", out)
        confirm.assert_called_once()

    def test_dry_runs_never_ask_and_say_so(self):
        calls = self.fake_installer()
        with mock.patch.object(cli, "_confirm", side_effect=AssertionError("prompted")):
            for tty in (False, True):
                code, out, _ = self.run_cli("install", "--dry-run", tty=tty)
                self.assertEqual(code, OK)
                self.assertIn("Dry run", out)
                self.assertIn("would-change  iterm.keep_windows", out)
                code, out, _ = self.run_cli("uninstall", "--dry-run", tty=tty)
                self.assertEqual(code, OK)
                self.assertIn("would-revert  iterm.keep_windows", out)
        self.assertEqual(calls, [("install", None, True), ("uninstall", False, True)] * 2)

    def test_install_asks_on_a_terminal(self):
        calls = self.fake_installer()
        with mock.patch.object(cli, "all_settings", lambda: [DescribedSetting()]), \
                mock.patch.object(cli, "_confirm", return_value=False) as confirm:
            code, out, _ = self.run_cli("install", tty=True)
        self.assertEqual(code, ERROR)
        self.assertIn("Turn the test setting on.", out)
        self.assertIn("Proceed? [Y/n]", confirm.call_args[0][0])
        self.assertEqual(calls, [])

    def test_install_without_a_terminal_needs_yes(self):
        calls = self.fake_installer()
        with mock.patch.object(cli, "_confirm", side_effect=AssertionError("prompted")):
            for streams in ({"tty": False}, {"tty": True, "stdout_tty": False}):
                code, _, err = self.run_cli("install", **streams)
                self.assertEqual(code, NEEDS_TTY, streams)
                self.assertIn("--yes", err)
        self.assertEqual(calls, [])

    def test_install_failure_is_an_error(self):
        self.fake_installer([Step("claude.plugin", "failed", "claude exited 1")])
        code, out, _ = self.run_cli("install", "--yes")
        self.assertEqual(code, ERROR)
        self.assertIn("failed     claude.plugin  claude exited 1", out)
        self.assertNotIn("adopt", out)

    def test_uninstall_needs_confirmation(self):
        calls = self.fake_installer()
        code, _, err = self.run_cli("uninstall")  # not a terminal and no --yes
        self.assertEqual(code, NEEDS_TTY)
        self.assertIn("--yes", err)
        with mock.patch.object(cli, "_confirm", return_value=False):
            self.assertEqual(self.run_cli("uninstall", tty=True)[0], ERROR)
        with mock.patch.object(cli, "_confirm", return_value=True):
            self.assertEqual(self.run_cli("uninstall", tty=True)[0], OK)
        self.assertEqual(calls, [("uninstall", False, False)])

    def test_uninstall_refusal_is_an_error(self):
        # H3: an unreadable manifest makes uninstall revert nothing and report a failed step.
        self.fake_installer(uninstall_results=[Step("manifest", "failed", "manifest is unreadable")])
        code, out, _ = self.run_cli("uninstall", "--yes")
        self.assertEqual(code, ERROR)
        self.assertIn("manifest is unreadable", out)

    def test_confirm_defaults_and_eof(self):
        with mock.patch("builtins.input", return_value=""), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(cli._confirm("Proceed? [Y/n] "))
        with mock.patch("builtins.input", return_value="n"):
            self.assertFalse(cli._confirm("Proceed? [Y/n] "))
        with mock.patch("builtins.input", side_effect=EOFError), contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(cli._confirm("Proceed? [Y/n] "))


class CliDoctorTest(CliCase):
    def test_doctor_delegates_flags_and_exit_status(self):
        calls = []
        fake = types.SimpleNamespace(run=lambda **kwargs: calls.append(kwargs) or 7)
        with mock.patch.object(cli, "doctor", fake):
            self.assertEqual(self.run_cli("doctor")[0], 7)
            self.assertEqual(self.run_cli("doctor", "--json", "--strict")[0], 7)
        self.assertEqual(calls, [{}, {"as_json": True, "strict": True}])


# ---------------------------------------------------------------------------------------------- entry point

class CliEntryPointTest(unittest.TestCase):
    def test_entry_point_keeps_the_main_guard(self):
        # Text search uses multiprocessing with spawn, which re-imports this file in every worker.
        text = ENTRY.read_text()
        self.assertIn('if __name__ == "__main__":', text)
        self.assertLess(text.index('if __name__ == "__main__":'), text.index("from agent_resurrect.cli import main"))
        self.assertTrue(os.access(ENTRY, os.X_OK))

    def run_entry(self, *argv, stdin=b"", env=None):
        with tempfile.TemporaryDirectory() as home:
            env = env or {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
            return subprocess.run([sys.executable, "-I", str(ENTRY), *argv], input=stdin, env=env,
                                  capture_output=True, timeout=60)

    def test_entry_point_runs_isolated(self):
        result = self.run_entry("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode().strip(), f"agent-resurrect {agent_resurrect.__version__}")

    def test_entry_point_hook_is_silent(self):
        result = self.run_entry("hook", "claude", "SessionStart", stdin=b"{garbage")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_entry_point_json_is_one_ascii_object(self):
        result = self.run_entry("version", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.isascii())
        self.assertEqual(result.stdout.count(b"\n"), 1)
        self.assertEqual(json.loads(result.stdout)["schema"], "agent-resurrect.version/1")

    def test_resume_from_a_non_terminal_shell_launches_nothing_and_changes_nothing(self):
        """P0-1 end to end: the real Claude adapter, stdin from /dev/null, a `claude` on PATH that would
        leave a marker if it ever ran."""
        session_id, guid = "11111111-2222-3333-4444-555555555555", "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            home, fakebin, marker = tmp / "home", tmp / "bin", tmp / "claude-ran"
            project = home / "proj"
            project.mkdir(parents=True)
            transcript = home / ".claude" / "projects" / "-proj" / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(json.dumps({"type": "user", "sessionId": session_id, "cwd": str(project),
                                              "timestamp": "2026-10-12T10:00:00.000Z",
                                              "message": {"role": "user", "content": "Fix the login redirect"}}) + "\n")
            now = datetime.now(timezone.utc).strftime(ISO_FORMAT)
            record = Record(terminal="iterm", pane_key=f"iterm-{guid}", agent="claude", session_id=session_id,
                            cwd=str(project), pane_label="w0t0p0", transcript_path=str(transcript),
                            recorded_at=now, updated_at=now)
            panes = home / ".local" / "state" / "agent-resurrect" / "panes"
            panes.mkdir(parents=True)
            record_file = panes / f"iterm-{guid}.json"
            record_file.write_text(json.dumps(record.to_dict(), indent=2))
            before = record_file.read_bytes()
            fakebin.mkdir()
            (fakebin / "claude").write_text(f'#!/bin/sh\necho "$@" > {shlex.quote(str(marker))}\n')
            (fakebin / "claude").chmod(0o755)
            env = {"HOME": str(home), "PATH": f"{fakebin}:/usr/bin:/bin", "TERM_PROGRAM": "iTerm.app",
                   "ITERM_SESSION_ID": f"w0t0p0:{guid}", "LANG": "C"}
            for argv in (["resume", session_id], ["resume", "1"], ["here"], []):
                result = subprocess.run([sys.executable, "-I", str(ENTRY), *argv], stdin=subprocess.DEVNULL,
                                        capture_output=True, env=env, timeout=60)
                self.assertEqual(result.returncode, NEEDS_TTY, (argv, result.stderr))
                self.assertIn(f"--resume {session_id}", result.stdout.decode(), argv)
            self.assertEqual(record_file.read_bytes(), before)
            self.assertFalse((home / ".local" / "state" / "agent-resurrect" / "restores.jsonl").exists())
            self.assertFalse(marker.exists(), "claude was launched")


class DryRunWritesNothingTest(unittest.TestCase):
    """`install --dry-run` and `uninstall --dry-run`, through the plugin's bin/agent-resurrect with the real
    settings (tests/fakes first on PATH), leave every byte under HOME as it was: no log, no manifest, no
    bytecode cache. Checked on a fresh HOME and again after a real install."""

    WRAPPER = REPO / "bin" / "agent-resurrect"
    DRY_RUNS = (["install", "--dry-run"], ["install", "--dry-run", "--json"], ["install", "--dry-run", "--no-plugin"],
                ["uninstall", "--dry-run"], ["uninstall", "--dry-run", "--json"],
                ["uninstall", "--dry-run", "--purge"])

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.home, state = root / "home", root / "state"
        for directory in (self.home, state, root / "iTerm.app"):
            directory.mkdir()
        (self.home / ".zshrc").write_text("# mine\n")
        path = os.pathsep.join([str(REPO / "tests" / "fakes"), "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
        for tool in ("claude", "defaults", "launchctl"):
            self.assertEqual(shutil.which(tool, path=path), str(REPO / "tests" / "fakes" / tool))
        # PYTHONDONTWRITEBYTECODE reaches only the Python fakes: agent-resurrect runs with -I, which ignores it.
        self.env = {"HOME": str(self.home), "PATH": path, "SHELL": "/bin/zsh", "LANG": "C", "TMPDIR": str(root),
                    "PYTHONDONTWRITEBYTECODE": "1", "AGENT_RESURRECT_ITERM_APP": str(root / "iTerm.app"),
                    "FAKE_DEFAULTS_DB": str(state / "defaults.json"), "FAKE_CLAUDE_DB": str(state / "claude.json"),
                    "FAKE_LAUNCHCTL_DB": str(state / "launchctl.json")}

    def run_wrapper(self, *argv):
        return subprocess.run([str(self.WRAPPER), *argv], env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=120)

    def snapshot(self):
        return {str(p.relative_to(self.home)): (p.is_dir(), None if p.is_dir() else p.read_bytes())
                for p in sorted(self.home.rglob("*"))}

    def assert_dry_runs_change_nothing(self):
        before = self.snapshot()
        for argv in self.DRY_RUNS:
            result = self.run_wrapper(*argv)
            self.assertEqual(result.returncode, OK, (argv, result.stdout, result.stderr))
            if "--json" in argv:
                self.assertTrue(json.loads(result.stdout)["dry_run"])
        self.assertEqual(self.snapshot(), before)

    def test_dry_runs_write_nothing_under_home(self):
        self.assert_dry_runs_change_nothing()
        installed = self.run_wrapper("install", "--yes", "--no-plugin")
        self.assertEqual(installed.returncode, OK, installed.stdout + installed.stderr)
        self.assertTrue((self.home / ".local" / "state" / "agent-resurrect" / "manifest.json").exists())
        self.assert_dry_runs_change_nothing()


if __name__ == "__main__":
    unittest.main()
