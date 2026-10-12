"""Tests for restore: eligibility, resume plans, the exec launcher, prompts and `auto`."""
import io
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from agent_resurrect import exitcodes, paths, restore, store
from agent_resurrect.config import Config
from agent_resurrect.model import (STATUS_CONSUMED, STATUS_LIVE, STATUS_SKIPPED, ISO_FORMAT, Record,
                                   SessionInfo, utc_now_iso)
from agent_resurrect.restore import Decision, ResumePlan
from tests.helpers import FakeAgent, FakeProcs, FakeTerminal, make_record, proc, temp_home

ENV = {"FAKE_PANE": "p1"}
KEY = "fake-p1"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
OTHER_START = "Tue Oct 13 09:00:00 2026"
SKIP_HINT = 'Skipped. Run "agent-resurrect" in this pane to resume it later.'


def iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(ISO_FORMAT)


def never(*args, **kwargs):
    raise AssertionError(f"must not be called: {args!r} {kwargs!r}")


def events():
    return [e["result"] for e in store.read_restore_events()]


def tty(stream):
    return True


def available_shells():
    shells = []
    for shell in ("/bin/bash", shutil.which("bash"), "/bin/zsh", shutil.which("zsh")):
        if shell and os.access(shell, os.X_OK) and os.path.realpath(shell) not in [os.path.realpath(s) for s in shells]:
            shells.append(shell)
    return shells


def term_attrs(fd):
    """Terminal attributes without PENDIN, which the macOS tty driver sets by itself whenever ICANON is
    switched back on (to reprocess pending input), so it is not something the caller changed."""
    attrs = termios.tcgetattr(fd)
    attrs[3] &= ~getattr(termios, "PENDIN", 0)
    return attrs


class RestoreEligibilityTest(unittest.TestCase):
    def check(self, record, agent=None, procs=None, **kw):
        return restore.check_eligible(record, agent or FakeAgent(), procs or FakeProcs(), Config(), **kw)

    def test_eligibility_order_and_reasons(self):
        fresh, old = utc_now_iso(), iso_days_ago(30)
        alive = FakeProcs(proc(4242, 1, "claude"))  # same start time as make_record's agent_started
        reused = FakeProcs(proc(4242, 1, "zsh", started=OTHER_START))
        gone = FakeAgent(exists=False)
        cases = [
            # status is checked first, even when everything else is also wrong
            (make_record(status=STATUS_CONSUMED, updated_at=old), gone, alive, "not-live"),
            (make_record(status=STATUS_SKIPPED, updated_at=fresh), None, None, "not-live"),
            # age before liveness
            (make_record(updated_at=old), gone, alive, "too-old"),
            # liveness before session existence
            (make_record(updated_at=fresh), gone, alive, "agent-running"),
            (make_record(updated_at=fresh), gone, None, "session-gone"),
            (make_record(updated_at=fresh), None, None, "ok"),
            # a reused pid is a different process, so the agent counts as gone
            (make_record(updated_at=fresh), None, reused, "ok"),
        ]
        for record, agent, procs, reason in cases:
            with self.subTest(reason=reason, status=record.status):
                decision = self.check(record, agent, procs)
                self.assertEqual(decision, Decision(eligible=(reason == "ok"), reason=reason))

    def test_age_limit_uses_max_age_days_and_now(self):
        record = make_record(updated_at="2026-10-01T00:00:00Z")
        early = datetime(2026, 10, 14, tzinfo=timezone.utc)
        late = datetime(2026, 10, 16, tzinfo=timezone.utc)
        self.assertEqual(self.check(record, now=early).reason, "ok")
        self.assertEqual(self.check(record, now=late).reason, "too-old")
        roomy = restore.check_eligible(record, FakeAgent(), FakeProcs(), Config(max_age_days=30), now=late)
        self.assertEqual(roomy.reason, "ok")

    def test_unparseable_timestamp_is_not_too_old(self):
        self.assertEqual(self.check(make_record(updated_at="")).reason, "ok")
        self.assertEqual(self.check(make_record(updated_at="yesterday")).reason, "ok")

    def test_require_live_false_ignores_status(self):
        for status in (STATUS_CONSUMED, STATUS_SKIPPED, STATUS_LIVE):
            with self.subTest(status=status):
                record = make_record(status=status, updated_at=utc_now_iso())
                self.assertEqual(self.check(record, require_live=False).reason, "ok")
                alive = FakeProcs(proc(4242, 1, "claude"))
                self.assertEqual(self.check(record, procs=alive, require_live=False).reason, "agent-running")


class RestoreSimulateTest(unittest.TestCase):
    """simulate: the decision `auto` would make, without a terminal, a launch or any change."""

    FRESH = utc_now_iso()
    OLD = iso_days_ago(30)
    ALIVE = FakeProcs(proc(4242, 1, "claude"))
    GONE = FakeAgent(exists=False)

    def fixtures(self):
        """(name, record, agent, procs) covering every reason."""
        return [
            ("ok", make_record(updated_at=self.FRESH), None, None),
            ("consumed", make_record(status=STATUS_CONSUMED, updated_at=self.FRESH), None, None),
            ("skipped", make_record(status=STATUS_SKIPPED, updated_at=self.FRESH), None, self.ALIVE),
            ("old", make_record(updated_at=self.OLD), None, None),
            ("old and running", make_record(updated_at=self.OLD), None, self.ALIVE),
            ("running", make_record(updated_at=self.FRESH), None, self.ALIVE),
            ("running, transcript gone", make_record(updated_at=self.FRESH), self.GONE, self.ALIVE),
            ("transcript gone", make_record(updated_at=self.FRESH), self.GONE, None),
            ("pid reused", make_record(updated_at=self.FRESH), None, FakeProcs(proc(4242, 1, "zsh", started=OTHER_START))),
        ]

    def test_simulate_without_assume_restart_is_check_eligible(self):
        for name, record, agent, procs in self.fixtures():
            with self.subTest(name):
                agent, procs = agent or FakeAgent(), procs or FakeProcs()
                self.assertEqual(restore.simulate(record, agent, procs, Config()),
                                 restore.check_eligible(record, agent, procs, Config()))

    def test_assume_restart_skips_only_the_running_check(self):
        expected = {"ok": "ok", "consumed": "not-live", "skipped": "not-live", "old": "too-old",
                    "old and running": "too-old", "running": "ok", "running, transcript gone": "session-gone",
                    "transcript gone": "session-gone", "pid reused": "ok"}
        for name, record, agent, procs in self.fixtures():
            with self.subTest(name):
                decision = restore.simulate(record, agent or FakeAgent(), procs or FakeProcs(), Config(),
                                            assume_restart=True)
                self.assertEqual(decision, Decision(eligible=(expected[name] == "ok"), reason=expected[name]))

    def test_simulate_uses_now_and_max_age(self):
        record = make_record(updated_at="2026-10-01T00:00:00Z")
        late = datetime(2026, 10, 16, tzinfo=timezone.utc)
        self.assertEqual(restore.simulate(record, FakeAgent(), FakeProcs(), Config(), now=late).reason, "too-old")
        self.assertEqual(restore.simulate(record, FakeAgent(), FakeProcs(), Config(max_age_days=30), now=late,
                                          assume_restart=True).reason, "ok")

    def test_simulate_changes_nothing(self):
        with temp_home():
            store.save(make_record())
            before = store.record_path(KEY).read_bytes()
            for assume_restart in (False, True):
                restore.simulate(store.load(KEY), FakeAgent(), self.ALIVE, Config(), assume_restart=assume_restart)
            self.assertEqual(store.record_path(KEY).read_bytes(), before)
            self.assertFalse(paths.restore_events_path().exists())

    def test_simulate_agrees_with_auto(self):
        """For each fixture, auto launches exactly when simulate says eligible, and otherwise logs simulate's reason."""
        for name, record, agent, procs in self.fixtures():
            with self.subTest(name), temp_home():
                agent, procs = agent or FakeAgent(), procs or FakeProcs()
                path = store.record_path(KEY)
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(record.to_dict()))  # keep updated_at as given
                decision = restore.simulate(record, agent, procs, Config())
                launched = []
                rc = restore.auto(ENV, terminal=FakeTerminal(), agent_lookup=lambda n: agent, procs=procs,
                                  config=Config(auto_resume="immediate"), stdin=io.StringIO(), stdout=io.StringIO(),
                                  keypress=never, ask=never, launcher=launched.append, isatty=tty)
                self.assertEqual(rc, exitcodes.OK)
                self.assertEqual(bool(launched), decision.eligible)
                self.assertEqual(events()[-1], "resumed" if decision.eligible else decision.reason)


class RestorePlanTest(unittest.TestCase):
    def setUp(self):
        # plan_resume logs the resume_env entries it drops: the log must go to a temporary HOME, never the real one.
        home = temp_home()
        home.__enter__()
        self.addCleanup(home.__exit__, None, None, None)

    def test_plan_uses_cwd_or_warns(self):
        existing = tempfile.mkdtemp(prefix="ar-cwd-")
        self.addCleanup(shutil.rmtree, existing, True)
        plan = restore.plan_resume(make_record(cwd=existing), FakeAgent(), Config())
        self.assertEqual(plan, ResumePlan(argv=["fake-agent", "--resume", "s-1"], cwd=existing, warning=None))

        missing = os.path.join(existing, "gone")
        plan = restore.plan_resume(make_record(cwd=missing), FakeAgent(), Config())
        self.assertIsNone(plan.cwd)
        self.assertEqual(plan.warning,
                         f"working directory {missing} no longer exists; resuming from the current directory")
        self.assertEqual(plan.argv, ["fake-agent", "--resume", "s-1"])

    def test_plan_without_a_recorded_cwd_has_no_warning(self):
        plan = restore.plan_resume(make_record(cwd=""), FakeAgent(), Config())
        self.assertEqual((plan.cwd, plan.warning), (None, None))

    def test_format_command_quotes_spaces_and_quotes(self):
        plan = ResumePlan(argv=["fake-agent", "--resume", "s-1"], cwd="/tmp/a b'c", warning=None)
        self.assertEqual(restore.format_command(plan), "cd '/tmp/a b'\"'\"'c' && fake-agent --resume s-1")
        plan = ResumePlan(argv=["fake agent", "--resume", "s-1"], cwd=None, warning="w")
        self.assertEqual(restore.format_command(plan), "'fake agent' --resume s-1")

    def test_format_command_with_an_absolute_executable(self):
        # The Claude adapter falls back to ~/.claude/local/claude when claude is not on PATH.
        plan = ResumePlan(["/Users/me/.claude/local/claude", "--resume", "s-1"], "/tmp", None,
                          env={"CLAUDE_CONFIG_DIR": "/x"})
        self.assertEqual(restore.format_command(plan),
                         "cd /tmp && CLAUDE_CONFIG_DIR=/x /Users/me/.claude/local/claude --resume s-1")
        plan = ResumePlan(["/Users/a b/.claude/local/claude", "--resume", "s-1"], None, None)
        self.assertEqual(restore.format_command(plan), "'/Users/a b/.claude/local/claude' --resume s-1")

    def test_plan_carries_a_copy_of_the_recorded_environment(self):
        recorded = {"CLAUDE_CONFIG_DIR": "/Users/me/.claude-work"}
        record = make_record(cwd="", resume_env=recorded)
        plan = restore.plan_resume(record, FakeAgent(), Config())
        self.assertEqual(plan.env, {"CLAUDE_CONFIG_DIR": "/Users/me/.claude-work"})
        plan.env["OTHER"] = "x"
        self.assertEqual(record.resume_env, {"CLAUDE_CONFIG_DIR": "/Users/me/.claude-work"})
        self.assertEqual(restore.plan_resume(make_record(), FakeAgent(), Config()).env, {})
        self.assertEqual(ResumePlan(["a"], None, None).env, {})

    def test_plan_drops_environment_entries_that_cannot_be_exported(self):
        # Records are files on disk: a hand-edited or corrupt one must not inject shell text or break exec.
        record = make_record(resume_env={"GOOD_1": "v", "_ALSO": "", "1BAD": "v", "BAD-NAME": "v", "A=B": "v",
                                         "": "v", "X; rm -rf ~": "v", "NUL": "a\x00b", "NUMBER": 5, "NONE": None})
        self.assertEqual(restore.plan_resume(record, FakeAgent(), Config()).env, {"GOOD_1": "v", "_ALSO": ""})
        for bad in (["CLAUDE_CONFIG_DIR=/x"], "CLAUDE_CONFIG_DIR=/x", 7):
            with self.subTest(resume_env=bad):
                self.assertEqual(restore.plan_resume(make_record(resume_env=bad), FakeAgent(), Config()).env, {})

    def test_format_command_prefixes_the_plan_env(self):
        plan = ResumePlan(["fake-agent", "--resume", "s-1"], "/tmp", None,
                          env={"CLAUDE_CONFIG_DIR": "/Users/me/.claude work", "A_VAR": "1"})
        self.assertEqual(restore.format_command(plan),
                         "cd /tmp && A_VAR=1 CLAUDE_CONFIG_DIR='/Users/me/.claude work' fake-agent --resume s-1")
        plan = ResumePlan(["fake-agent"], None, None, env={"EMPTY": ""})
        self.assertEqual(restore.format_command(plan), "EMPTY='' fake-agent")

    def test_format_command_env_values_reach_the_program_exactly(self):
        shells = available_shells()
        if not shells:
            self.skipTest("no bash or zsh to evaluate the quoting")
        value = "a b'c\x1b[2J \"$HOME\" caf\u00e9 `x` \\ end"
        command = restore.format_command(ResumePlan(["/bin/sh", "-c", 'printf "%s" "$V"'], None, None,
                                                    env={"V": value}))
        self.assertEqual(unsafe_characters(command), [], repr(command))
        for shell in shells:
            with self.subTest(shell=shell):
                result = subprocess.run([shell, "-c", command], capture_output=True, timeout=30,
                                        env={"PATH": "/usr/bin:/bin", "LC_ALL": "en_US.UTF-8", "HOME": "/nonexistent"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, value.encode("utf-8"))


class RestoreLaunchTest(unittest.TestCase):
    def setUp(self):
        # Safety net: launch() must use execvpe, so a plain execvp call is a bug, never a real exec.
        patch = mock.patch("os.execvp", side_effect=AssertionError("launch must use os.execvpe"))
        patch.start()
        self.addCleanup(patch.stop)

    def test_launch_execs_without_spawning(self):
        calls = []
        with mock.patch("os.chdir", side_effect=lambda d: calls.append(("chdir", d))), \
                mock.patch("os.execvpe", side_effect=lambda f, a, e: calls.append(("execvpe", f, list(a)))), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("must not spawn")), \
                mock.patch("signal.signal"):
            restore.launch(ResumePlan(["claude", "--resume", "x"], "/tmp", None))
        self.assertEqual(calls, [("chdir", "/tmp"), ("execvpe", "claude", ["claude", "--resume", "x"])])

    def test_launch_without_cwd_stays_put(self):
        calls = []
        with mock.patch("os.chdir", side_effect=lambda d: calls.append(("chdir", d))), \
                mock.patch("os.execvpe", side_effect=lambda f, a, e: calls.append(("execvpe", f))), \
                mock.patch("signal.signal"):
            restore.launch(ResumePlan(["claude"], None, None))
        self.assertEqual(calls, [("execvpe", "claude")])

    def test_launch_passes_this_environment_with_the_plan_env_on_top(self):
        seen = []
        with mock.patch.dict(os.environ, {"KEEP_ME": "1", "CLAUDE_CONFIG_DIR": "/from/the/shell"}), \
                mock.patch("os.execvpe", side_effect=lambda f, a, e: seen.append(dict(e))), \
                mock.patch("signal.signal"):
            restore.launch(ResumePlan(["claude"], None, None, env={"CLAUDE_CONFIG_DIR": "/Users/me/.claude-work"}))
            self.assertEqual(os.environ["CLAUDE_CONFIG_DIR"], "/from/the/shell", "this process is not changed")
        self.assertEqual(seen[0]["KEEP_ME"], "1")
        self.assertEqual(seen[0]["CLAUDE_CONFIG_DIR"], "/Users/me/.claude-work")
        self.assertEqual(seen[0]["PATH"], os.environ["PATH"])

    def test_launch_really_execs_with_the_plan_env(self):
        # End to end through a real exec, of env (never an agent), found on PATH, in a child Python.
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from agent_resurrect.restore import ResumePlan, launch; "
                "launch(ResumePlan(['env'], None, None, env={'CLAUDE_CONFIG_DIR': '/x/a b'}))")
        result = subprocess.run([sys.executable, "-I", "-c", code, SRC], capture_output=True, text=True, timeout=30,
                                env={"PATH": "/usr/bin:/bin", "KEEP_ME": "kept", "CLAUDE_CONFIG_DIR": "/shell"})
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn("CLAUDE_CONFIG_DIR=/x/a b", lines)
        self.assertIn("KEEP_ME=kept", lines)
        self.assertNotIn("CLAUDE_CONFIG_DIR=/shell", lines)

    def test_launch_execs_an_absolute_executable_without_a_path_search(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from agent_resurrect.restore import ResumePlan, launch; "
                "launch(ResumePlan(['/usr/bin/env'], None, None, env={'MARK': 'm'}))")
        result = subprocess.run([sys.executable, "-I", "-c", code, SRC], capture_output=True, text=True, timeout=30,
                                env={"PATH": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MARK=m", result.stdout.splitlines())

    def test_launch_restores_signals_python_ignores_before_exec(self):
        # Python starts with SIGPIPE (and SIGXFSZ) ignored; an ignored disposition survives exec.
        order = []
        with mock.patch("signal.signal", side_effect=lambda s, h: order.append((s, h))), \
                mock.patch("os.execvpe", side_effect=lambda f, a, e: order.append("exec")):
            restore.launch(ResumePlan(["claude"], None, None))
        self.assertIn((signal.SIGPIPE, signal.SIG_DFL), order)
        self.assertEqual(order[-1], "exec")

    def test_launch_flushes_output_before_exec(self):
        stream = mock.Mock()
        with mock.patch("sys.stdout", stream), mock.patch("signal.signal"), \
                mock.patch("os.execvpe", side_effect=lambda f, a, e: self.assertTrue(stream.flush.called)):
            restore.launch(ResumePlan(["claude"], None, None))

    def test_launch_of_a_missing_executable_raises_oserror(self):
        empty = tempfile.mkdtemp(prefix="ar-path-")
        self.addCleanup(shutil.rmtree, empty, True)
        with mock.patch.dict(os.environ, {"PATH": empty}), mock.patch("signal.signal"):
            with self.assertRaises(OSError):
                restore.launch(ResumePlan(["agent-resurrect-no-such-binary"], None, None))


class RestoreResumeRecordTest(unittest.TestCase):
    def setUp(self):
        context = temp_home()
        self.home = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def resume(self, record, agent=None, launcher=None, isatty=tty, **kw):
        self.out = io.StringIO()
        return restore.resume_record(record, agent or FakeAgent(), Config(), stdout=self.out,
                                     launcher=launcher or never, isatty=isatty, **kw)

    def test_resume_record_announces_claims_and_launches(self):
        record = store.save(make_record())
        seen = []

        def launcher(plan):
            seen.append((plan.argv, plan.cwd, store.load(KEY).status))

        rc = self.resume(record, launcher=launcher)
        self.assertEqual(rc, exitcodes.OK)
        self.assertEqual(seen, [(["fake-agent", "--resume", "s-1"], "/tmp", STATUS_CONSUMED)])
        self.assertEqual(self.out.getvalue(), 'Resurrecting "title of s-1" (fake) in /tmp\n')
        self.assertEqual(store.read_restore_events()[-1]["result"], "resumed")
        self.assertEqual(store.read_restore_events()[-1]["detail"], "s-1")

    def test_a_live_record_is_taken_with_an_atomic_claim(self):
        record = store.save(make_record())
        with mock.patch.object(store, "claim", wraps=store.claim) as claim, \
                mock.patch.object(store, "set_status", side_effect=AssertionError("claim, not set_status")):
            self.assertEqual(self.resume(record, launcher=lambda plan: None), exitcodes.OK)
        claim.assert_called_once_with(KEY)
        self.assertEqual(store.load(KEY).status, STATUS_CONSUMED)

    def test_without_a_terminal_prints_the_command_and_changes_nothing(self):
        store.save(make_record())
        before = store.record_path(KEY).read_bytes()
        stdin = io.StringIO()
        for name, isatty in [("neither", lambda f: False), ("stdin only", lambda f: f is stdin),
                             ("stdout only", lambda f: f is self.out), ("a plain False", False)]:
            with self.subTest(name):
                self.out = io.StringIO()
                rc = restore.resume_record(store.load(KEY), FakeAgent(), Config(), stdin=stdin, stdout=self.out,
                                           launcher=never, isatty=isatty)
                self.assertEqual(rc, exitcodes.NEEDS_TTY)
                self.assertEqual(store.record_path(KEY).read_bytes(), before)
                self.assertFalse(paths.restore_events_path().exists(), "no event without a launch")
                text = self.out.getvalue()
                self.assertIn("without a terminal", text)
                self.assertIn("\n  cd /tmp && fake-agent --resume s-1\n", text)
                self.assertNotIn("Resurrecting", text)

    def test_default_isatty_checks_the_real_streams(self):
        # A caller that passes no isatty is still safe: plain streams are not terminals.
        store.save(make_record())
        before = store.record_path(KEY).read_bytes()
        with open(os.devnull) as devnull, mock.patch("sys.stdin", devnull):
            rc = restore.resume_record(store.load(KEY), FakeAgent(), Config(), stdout=io.StringIO(),
                                       launcher=never)
        self.assertEqual(rc, exitcodes.NEEDS_TTY)
        self.assertEqual(store.record_path(KEY).read_bytes(), before)
        self.assertEqual(events(), [])
        self.assertTrue(restore.has_terminal(isatty=tty))
        self.assertFalse(restore.has_terminal(stdin=io.StringIO(), stdout=io.StringIO()))
        self.assertTrue(restore.has_terminal(stdin=io.StringIO(), stdout=io.StringIO(), isatty=True))

    def test_without_a_terminal_the_missing_cwd_warning_is_kept(self):
        record = store.save(make_record(cwd=str(self.home / "deleted")))
        self.assertEqual(self.resume(record, isatty=lambda f: False), exitcodes.NEEDS_TTY)
        self.assertIn(f"working directory {self.home / 'deleted'} no longer exists", self.out.getvalue())
        self.assertIn("\n  fake-agent --resume s-1\n", self.out.getvalue())

    def test_records_not_live_are_launched_without_a_claim(self):
        for status in (STATUS_CONSUMED, STATUS_SKIPPED):
            with self.subTest(status):
                record = store.save(make_record(status=status))
                before = store.record_path(KEY).read_bytes()
                seen = []
                with mock.patch.object(store, "claim", side_effect=AssertionError("must not claim")):
                    rc = self.resume(record, launcher=lambda plan: seen.append(store.load(KEY).status))
                self.assertEqual(rc, exitcodes.OK)
                self.assertEqual(seen, [status])
                self.assertEqual(store.record_path(KEY).read_bytes(), before)
                self.assertEqual(events()[-1], "resumed")

    def test_a_lost_claim_launches_nothing(self):
        record = store.save(make_record())
        with mock.patch.object(store, "claim", return_value=None):
            self.assertEqual(self.resume(record, launcher=never), exitcodes.BUSY)
        self.assertEqual(events(), ["lost-race"])
        self.assertIn("already", self.out.getvalue())

    def test_a_claim_that_returns_another_session_is_handed_back(self):
        record = store.save(make_record(session_id="s-1"))
        real_claim = store.claim

        def replaced_then_claimed(key):  # a new session started in the pane between the load and the claim
            store.save(make_record(session_id="s-2"))
            return real_claim(key)

        with mock.patch.object(store, "claim", side_effect=replaced_then_claimed):
            self.assertEqual(self.resume(record, launcher=never), exitcodes.BUSY)
        stored = store.load(KEY)
        self.assertEqual((stored.session_id, stored.status), ("s-2", STATUS_LIVE))

    def test_launch_failure_marks_the_record_skipped(self):
        record = store.save(make_record())

        def launcher(plan):
            raise FileNotFoundError(2, "No such file or directory", "fake-agent")

        self.assertEqual(self.resume(record, launcher=launcher), exitcodes.ERROR)
        self.assertEqual(store.load(KEY).status, STATUS_SKIPPED)
        self.assertEqual(events(), ["resumed", "launch-failed"])
        text = self.out.getvalue()
        self.assertIn("agent-resurrect: could not start fake-agent: ", text)
        self.assertIn("When fake-agent is on your PATH, resume with: agent-resurrect resume s-1", text)

    def test_launch_failure_of_an_absolute_executable_names_it_and_the_command(self):
        class LocalInstall(FakeAgent):
            def resume_argv(self, record, config):
                return ["/Users/me/.claude/local/claude", "--resume", record.session_id]

        record = store.save(make_record())

        def launcher(plan):
            raise FileNotFoundError(2, "No such file or directory", plan.argv[0])

        self.assertEqual(self.resume(record, agent=LocalInstall(), launcher=launcher), exitcodes.ERROR)
        lines = self.out.getvalue().splitlines()
        self.assertTrue(any(line.startswith("agent-resurrect: could not start /Users/me/.claude/local/claude: ")
                            for line in lines), lines)
        self.assertIn("When claude is on your PATH, resume with: agent-resurrect resume s-1", lines)
        self.assertEqual(store.load(KEY).status, STATUS_SKIPPED)

    def test_launch_failure_of_an_unclaimed_record_leaves_its_status(self):
        record = store.save(make_record(status=STATUS_CONSUMED))

        def launcher(plan):
            raise PermissionError(13, "Permission denied", "fake-agent")

        self.assertEqual(self.resume(record, launcher=launcher), exitcodes.ERROR)
        self.assertEqual(store.load(KEY).status, STATUS_CONSUMED)
        self.assertEqual(events(), ["resumed", "launch-failed"])

    def test_launch_failure_hint_quotes_the_session_id(self):
        record = Record(terminal="", pane_key="", agent="fake", session_id="s 1;x", cwd="")

        def launcher(plan):
            raise FileNotFoundError(2, "No such file or directory", "fake-agent")

        self.assertEqual(self.resume(record, launcher=launcher), exitcodes.ERROR)
        self.assertIn("agent-resurrect resume 's 1;x'", self.out.getvalue())

    def test_missing_cwd_warning_is_printed(self):
        record = store.save(make_record(cwd=str(self.home / "deleted")))
        seen = []
        self.resume(record, launcher=seen.append, announce=False)
        self.assertEqual(self.out.getvalue(), f"working directory {self.home / 'deleted'} no longer exists; "
                                              "resuming from the current directory\n")
        self.assertIsNone(seen[0].cwd)

    def test_record_without_a_pane_is_launched_without_touching_the_store(self):
        record = Record(terminal="", pane_key="", agent="fake", session_id="s-9", cwd="")
        seen = []
        self.assertEqual(self.resume(record, launcher=seen.append), exitcodes.OK)
        self.assertEqual(seen[0].argv, ["fake-agent", "--resume", "s-9"])
        self.assertFalse(paths.panes_dir().exists())

    def test_a_newer_session_in_the_same_pane_is_not_consumed(self):
        stale = make_record(session_id="s-1")
        store.save(make_record(session_id="s-2"))
        self.resume(stale, launcher=lambda p: None)
        self.assertEqual(store.load(KEY).status, STATUS_LIVE)

    def test_launcher_defaults_to_the_module_launch_looked_up_at_call_time(self):
        record = store.save(make_record())
        seen = []
        with mock.patch.object(restore, "launch", seen.append):
            self.assertEqual(restore.resume_record(record, FakeAgent(), Config(), stdout=io.StringIO(), isatty=tty),
                             exitcodes.OK)
        self.assertEqual(len(seen), 1)

    def test_title_falls_back_to_first_prompt_then_session_id(self):
        class Described(FakeAgent):
            def __init__(self, info=None, error=None):
                super().__init__()
                self.info, self.error = info, error

            def describe(self, record):
                if self.error:
                    raise self.error
                return self.info

        long_prompt = "fix   the\nlogin " + "x" * 200
        cases = [
            (Described(SessionInfo(title="Fix login", first_prompt="hi")), '"Fix login"'),
            (Described(SessionInfo(first_prompt="hello there")), '"hello there"'),
            (Described(SessionInfo()), '"s-1"'),
            (Described(error=RuntimeError("boom")), '"s-1"'),
        ]
        for agent, expected in cases:
            with self.subTest(expected=expected), temp_home():
                out = io.StringIO()
                restore.resume_record(make_record(), agent, Config(), stdout=out, launcher=lambda p: None,
                                      isatty=tty)
                self.assertTrue(out.getvalue().startswith(f"Resurrecting {expected} (fake) in /tmp"), out.getvalue())
        with temp_home():
            out = io.StringIO()
            agent = Described(SessionInfo(first_prompt=long_prompt))
            restore.resume_record(make_record(), agent, Config(), stdout=out, launcher=lambda p: None, isatty=tty)
            first_line = out.getvalue().splitlines()[0]
            self.assertIn('"fix the login xxx', first_line)
            self.assertIn('..."', first_line)
            self.assertLess(len(first_line), 120)


class RestoreAutoTest(unittest.TestCase):
    """`auto` with every collaborator injected. A record is stored for pane fake-p1 unless a test says otherwise."""

    def setUp(self):
        context = temp_home()
        self.home = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.launched = []

    def store_record(self, **kw):
        return store.save(make_record(**kw))

    def launcher(self, plan):
        current = store.load(KEY)
        self.launched.append((plan, current.status if current else None))

    def auto(self, config=None, agent=None, procs=None, keypress=never, ask=never, isatty=None,
             launcher=None, terminal=None, env=ENV):
        self.out, self.stdin = io.StringIO(), io.StringIO()
        agent = agent or FakeAgent()
        return restore.auto(env, terminal=terminal or FakeTerminal(),
                            agent_lookup=lambda name: agent if name == agent.name else None,
                            procs=procs or FakeProcs(), config=config or Config(),
                            stdin=self.stdin, stdout=self.out, keypress=keypress, ask=ask,
                            launcher=launcher or self.launcher, isatty=isatty or (lambda f: True))

    def test_auto_no_record_is_silent(self):
        self.assertEqual(self.auto(), 0)
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.launched, [])
        self.assertEqual(events(), [])

    def test_auto_without_a_pane_is_silent(self):
        self.store_record()
        with mock.patch.object(restore, "detect_terminal", return_value=None):
            self.assertEqual(restore.auto({}, launcher=never, keypress=never, isatty=never), 0)
        self.assertEqual(self.auto(env={}), 0)
        self.assertEqual((self.out.getvalue(), self.launched, events()), ("", [], []))

    def test_auto_never_waits_without_a_tty(self):
        self.store_record()
        for name, isatty in [("neither", lambda f: False),
                             ("stdin only", lambda f: f is self.stdin),
                             ("stdout only", lambda f: f is self.out)]:
            with self.subTest(name):
                self.assertEqual(self.auto(isatty=isatty, keypress=never, ask=never), 0)
                self.assertEqual(self.out.getvalue(), "")
                self.assertEqual(self.launched, [])
                self.assertEqual(events()[-1], "not-a-tty")
                self.assertEqual(store.load(KEY).status, STATUS_LIVE)

    def test_auto_default_isatty_rejects_plain_streams(self):
        self.store_record()
        rc = restore.auto(ENV, terminal=FakeTerminal(), agent_lookup=lambda n: FakeAgent(), procs=FakeProcs(),
                          config=Config(), stdin=io.StringIO(), stdout=io.StringIO(),
                          keypress=never, ask=never, launcher=never)
        self.assertEqual(rc, 0)
        self.assertEqual(events(), ["not-a-tty"])

    def test_auto_countdown_resumes(self):
        self.store_record()
        waited = []

        def keypress(seconds, **kw):
            waited.append(seconds)
            return False

        self.assertEqual(self.auto(config=Config(countdown_seconds=7), keypress=keypress), 0)
        self.assertEqual(waited, [7])
        self.assertEqual(len(self.launched), 1)
        plan, status_at_launch = self.launched[0]
        self.assertEqual(plan.argv, ["fake-agent", "--resume", "s-1"])
        self.assertEqual(status_at_launch, STATUS_CONSUMED)
        self.assertEqual(events(), ["resumed"])
        text = self.out.getvalue()
        self.assertIn('Resurrecting "title of s-1" (fake) in /tmp. Press any key to skip.', text)
        self.assertEqual(text.count("Resurrecting"), 1)

    def test_auto_countdown_skip(self):
        self.store_record()
        self.assertEqual(self.auto(keypress=lambda seconds, **kw: True), 0)
        self.assertEqual(self.launched, [])
        self.assertEqual(store.load(KEY).status, STATUS_SKIPPED)
        self.assertEqual(events(), ["skipped"])
        self.assertIn(SKIP_HINT, self.out.getvalue())

    def test_auto_modes(self):
        with self.subTest("immediate"):
            self.store_record()
            self.assertEqual(self.auto(config=Config(auto_resume="immediate"), keypress=never, ask=never), 0)
            self.assertEqual(len(self.launched), 1)
            self.assertEqual(self.out.getvalue().count("Resurrecting"), 1)

        with self.subTest("ask, no"):
            self.store_record()
            self.launched.clear()
            prompts = []
            rc = self.auto(config=Config(auto_resume="ask"), keypress=never,
                           ask=lambda prompt, **kw: prompts.append(prompt) or False)
            self.assertEqual(rc, 0)
            self.assertEqual(prompts, ['Resurrect "title of s-1" (fake) in /tmp?'])
            self.assertEqual(self.launched, [])
            self.assertEqual(store.load(KEY).status, STATUS_SKIPPED)
            self.assertEqual(events()[-1], "skipped")
            self.assertIn(SKIP_HINT, self.out.getvalue())

        with self.subTest("ask, yes"):
            self.store_record()
            self.assertEqual(self.auto(config=Config(auto_resume="ask"), keypress=never,
                                       ask=lambda prompt, **kw: True), 0)
            self.assertEqual(len(self.launched), 1)
            self.assertEqual(self.launched[0][1], STATUS_CONSUMED)
            self.assertNotIn("Resurrecting", self.out.getvalue())  # the question already named the session

        with self.subTest("off"):
            self.store_record()
            self.launched.clear()
            rc = self.auto(config=Config(auto_resume="off"), keypress=never, ask=never)
            self.assertEqual(rc, 0)
            self.assertEqual(self.out.getvalue(), "agent-resurrect: resume with: cd /tmp && fake-agent --resume s-1\n")
            self.assertEqual(store.load(KEY).status, STATUS_CONSUMED)
            self.assertEqual(self.launched, [])

    def test_auto_ignores_running_agent_and_consumed_record(self):
        with self.subTest("running"):
            self.store_record()
            self.assertEqual(self.auto(procs=FakeProcs(proc(4242, 1, "claude"))), 0)
            self.assertEqual(events()[-1], "agent-running")
            self.assertEqual(store.load(KEY).status, STATUS_LIVE)
        for status in (STATUS_CONSUMED, STATUS_SKIPPED):
            with self.subTest(status):
                self.store_record(status=status)
                self.assertEqual(self.auto(), 0)
                self.assertEqual(events()[-1], "not-live")
                self.assertEqual(store.load(KEY).status, status)
        self.assertEqual(self.launched, [])
        self.assertEqual(self.out.getvalue(), "")

    def test_auto_session_gone_deletes_record(self):
        self.store_record()
        self.assertEqual(self.auto(agent=FakeAgent(exists=False)), 0)
        self.assertIsNone(store.load(KEY))
        self.assertEqual(events(), ["session-gone"])
        self.assertEqual(self.launched, [])
        lines = self.out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("s-1", lines[0])

    def test_auto_garbage_collects_old_records(self):
        paths.panes_dir().mkdir(parents=True)
        old = make_record(recorded_at=iso_days_ago(40), updated_at=iso_days_ago(30))
        store.record_path(KEY).write_text(json.dumps(old.to_dict()))
        other = make_record(pane_key="fake-p2", recorded_at=iso_days_ago(40), updated_at=iso_days_ago(30))
        store.record_path("fake-p2").write_text(json.dumps(other.to_dict()))
        self.assertEqual(self.auto(), 0)
        self.assertEqual(events(), ["too-old"])
        self.assertEqual(store.list_records(), [])
        self.assertEqual((self.launched, self.out.getvalue()), ([], ""))

    def test_auto_unknown_agent_is_skipped(self):
        self.store_record(agent="mystery")
        self.assertEqual(self.auto(), 0)
        self.assertEqual(events(), ["unknown-agent"])
        self.assertEqual(store.load(KEY).status, STATUS_LIVE)
        self.assertEqual((self.launched, self.out.getvalue()), ([], ""))

    def test_auto_launch_failure_is_not_retried_by_every_new_shell(self):
        self.store_record()

        def launcher(plan):
            raise FileNotFoundError(2, "No such file or directory", "fake-agent")

        self.assertEqual(self.auto(config=Config(auto_resume="immediate"), launcher=launcher), exitcodes.ERROR)
        self.assertEqual(store.load(KEY).status, STATUS_SKIPPED)
        self.assertEqual(events(), ["resumed", "launch-failed"])
        self.assertIn("agent-resurrect resume s-1", self.out.getvalue())
        # The next shell in the pane finds nothing to restore, instead of failing the same way again.
        self.assertEqual(self.auto(config=Config(auto_resume="immediate"), launcher=never), exitcodes.OK)
        self.assertEqual(events(), ["resumed", "launch-failed", "not-live"])
        self.assertEqual(self.out.getvalue(), "")

    def test_auto_claims_with_store_claim_after_the_countdown(self):
        self.store_record()
        order = []
        real_claim = store.claim

        def keypress(seconds, **kw):
            order.append("countdown")
            return False

        def claim(key):
            order.append("claim")
            return real_claim(key)

        with mock.patch.object(store, "claim", side_effect=claim), \
                mock.patch.object(store, "set_status", side_effect=AssertionError("claim, not set_status")):
            self.assertEqual(self.auto(keypress=keypress), exitcodes.OK)
        self.assertEqual(order, ["countdown", "claim"])
        self.assertEqual(self.launched[0][1], STATUS_CONSUMED)

    def test_auto_that_loses_the_race_during_the_countdown_is_silent(self):
        self.store_record()
        winner_out, winner_launched = io.StringIO(), []

        def keypress(seconds, **kw):
            # Another shell for the same pane (a duplicated pane, a leftover multiplexer) wins meanwhile.
            rc = restore.auto(ENV, terminal=FakeTerminal(), agent_lookup=lambda n: FakeAgent(), procs=FakeProcs(),
                              config=Config(auto_resume="immediate"), stdin=io.StringIO(), stdout=winner_out,
                              keypress=never, ask=never, launcher=winner_launched.append, isatty=tty)
            self.assertEqual(rc, exitcodes.OK)
            return False

        self.assertEqual(self.auto(keypress=keypress), exitcodes.OK)
        self.assertEqual(len(winner_launched), 1)
        self.assertEqual(self.launched, [])
        self.assertEqual(events(), ["resumed", "lost-race"])
        self.assertEqual(store.load(KEY).status, STATUS_CONSUMED)
        self.assertEqual(self.out.getvalue(),
                         'Resurrecting "title of s-1" (fake) in /tmp. Press any key to skip.\n')

    def test_auto_ask_and_off_also_claim(self):
        for mode, ask in (("ask", lambda prompt, **kw: True), ("off", never)):
            with self.subTest(mode):
                self.store_record()
                self.launched.clear()
                with mock.patch.object(store, "claim", return_value=None):
                    self.assertEqual(self.auto(config=Config(auto_resume=mode), ask=ask), exitcodes.OK)
                self.assertEqual(self.launched, [])
                self.assertEqual(events()[-1], "lost-race")
                self.assertNotIn("resume with", self.out.getvalue())

    def test_concurrent_shells_launch_exactly_once(self):
        """Real processes racing on one record, as in the review's tmux reproduction: every shell passes the
        eligibility check and the countdown before any of them claims, and exactly one launches."""
        n = 6
        work = tempfile.mkdtemp(prefix="ar-race-")
        self.addCleanup(shutil.rmtree, work, True)
        ready, launches = os.path.join(work, "ready"), os.path.join(work, "launches")
        os.mkdir(ready)
        self.store_record()
        code = """
import io, os, sys, time
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from agent_resurrect import restore
from agent_resurrect.config import Config
from tests.helpers import FakeAgent, FakeProcs, FakeTerminal
ready, n, launches = sys.argv[3], int(sys.argv[4]), sys.argv[5]

def keypress(seconds, **kw):
    open(os.path.join(ready, str(os.getpid())), "w").close()
    deadline = time.monotonic() + 30
    while len(os.listdir(ready)) < n and time.monotonic() < deadline:
        time.sleep(0.005)
    return False

def launcher(plan):
    with open(launches, "a") as fh:
        fh.write(" ".join(plan.argv) + "\\n")

agent = FakeAgent()
sys.exit(restore.auto({"FAKE_PANE": "p1"}, terminal=FakeTerminal(), agent_lookup=lambda name: agent,
                      procs=FakeProcs(), config=Config(), stdin=io.StringIO(), stdout=io.StringIO(),
                      keypress=keypress, launcher=launcher, isatty=lambda f: True))
"""
        children = [subprocess.Popen([sys.executable, "-I", "-c", code, SRC, REPO, ready, str(n), launches],
                                     env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    for _ in range(n)]
        for child in children:
            out, err = child.communicate(timeout=60)
            self.assertEqual(child.returncode, 0, err.decode())
        with open(launches) as fh:
            self.assertEqual(fh.read().splitlines(), ["fake-agent --resume s-1"])
        self.assertEqual(sorted(events()), ["lost-race"] * (n - 1) + ["resumed"])
        self.assertEqual(store.load(KEY).status, STATUS_CONSUMED)
        self.assertEqual(sorted(os.listdir(paths.panes_dir())), [f"{KEY}.json"], "no claim file is left behind")

    def test_auto_keeps_old_records_of_running_agents(self):
        # Garbage collection must not delete the record of a session that has simply run for weeks.
        paths.panes_dir().mkdir(parents=True)
        running = make_record(pane_key="fake-p2", recorded_at=iso_days_ago(40), updated_at=iso_days_ago(30))
        store.record_path("fake-p2").write_text(json.dumps(running.to_dict()))
        self.store_record()
        self.assertEqual(self.auto(procs=FakeProcs(proc(4242, 1, "claude"))), exitcodes.OK)
        self.assertIsNotNone(store.load("fake-p2"))

    def test_auto_that_cannot_plan_leaves_the_record_live(self):
        class Unplannable(FakeAgent):
            def resume_argv(self, record, config):
                raise RuntimeError("transcript unreadable")

        self.store_record()
        self.assertEqual(self.auto(agent=Unplannable(), keypress=never, ask=never,
                                   config=Config(auto_resume="immediate")), exitcodes.ERROR)
        self.assertEqual(store.load(KEY).status, STATUS_LIVE)
        self.assertEqual(self.launched, [])

    def test_auto_contains_unexpected_errors(self):
        class Broken(FakeAgent):
            def session_exists(self, record):
                raise RuntimeError("transcript store exploded")

        self.store_record()
        self.assertEqual(self.auto(agent=Broken()), 1)
        self.assertEqual(self.launched, [])
        self.assertEqual(len(self.out.getvalue().splitlines()), 1)
        self.assertIn("agent-resurrect:", self.out.getvalue())
        self.assertIn("transcript store exploded", paths.log_path().read_text())
        self.assertEqual(store.load(KEY).status, STATUS_LIVE)


def unsafe_characters(text: str):
    """Characters that must never reach the terminal raw: controls (escape sequences), C1, bidi overrides."""
    return [c for c in text if not (c.isprintable() or c in "\n\r")]


EVIL_TITLE = "evil\x1b]52;c;ZXZpbA==\x07 title \u202e"   # OSC 52 writes the clipboard; RLO flips text
EVIL_CWD = "/nonexistent/a\x1b[2Jb"                         # CSI 2J clears the screen
EVIL_SESSION = "s-\x9b31m"                                  # C1 CSI


class EvilAgent(FakeAgent):
    def describe(self, record):
        return SessionInfo(title=EVIL_TITLE)


class EvilPathAgent(EvilAgent):
    """An absolute executable path with control characters in a folder name and in the file name."""
    def resume_argv(self, record, config):
        return ["/Users/me/odd\x1b[2Jdir/.claude/local/cla\x07ude", "--resume", record.session_id]


class RestoreTerminalSafetyTest(unittest.TestCase):
    """Titles, prompts, paths and ids come from transcripts and hook payloads; printing them raw would let
    their escape sequences drive the terminal."""

    def test_printable_replaces_control_characters(self):
        self.assertEqual(restore.printable("a\x1b[2Jb\x07\x9b\u202ec\u00e9 d"), "a?[2Jb???c\u00e9 d")
        self.assertEqual(restore.printable("plain /path ok"), "plain /path ok")

    def test_untrusted_text_never_reaches_the_terminal_raw(self):
        outputs = {}
        with temp_home():
            def run_auto(name, mode, agent=None, launcher=lambda plan: None, **kw):
                store.save(make_record(session_id=EVIL_SESSION, cwd=EVIL_CWD))
                out, prompts = io.StringIO(), []
                restore.auto(ENV, terminal=FakeTerminal(), agent_lookup=lambda n: agent or EvilAgent(),
                             procs=FakeProcs(), config=Config(auto_resume=mode), stdin=io.StringIO(), stdout=out,
                             keypress=lambda s, **k: kw.get("skip", False),
                             ask=lambda q, **k: prompts.append(q) or False,
                             launcher=launcher, isatty=lambda f: True)
                outputs[name] = out.getvalue() + "".join(prompts)

            def fail(plan):
                raise FileNotFoundError(2, "No such file or directory", "agent\x1b[2J")

            class Gone(EvilAgent):
                def session_exists(self, record):
                    return False

            class Broken(EvilAgent):
                def session_exists(self, record):
                    raise RuntimeError("bad \x1b]0;pwned\x07 state")

            run_auto("countdown", "countdown")
            run_auto("countdown skip", "countdown", skip=True)
            run_auto("ask", "ask")
            run_auto("off", "off")
            run_auto("immediate", "immediate")
            run_auto("launch failure", "immediate", launcher=fail)
            run_auto("session gone", "countdown", agent=Gone())
            run_auto("internal error", "countdown", agent=Broken())
            for name, launcher, isatty in (("resume_record", lambda plan: None, tty),
                                           ("resume_record launch failure", fail, tty),
                                           ("resume_record without a terminal", never, lambda f: False)):
                out = io.StringIO()
                restore.resume_record(make_record(session_id=EVIL_SESSION, cwd=EVIL_CWD), EvilAgent(), Config(),
                                      stdout=out, launcher=launcher, isatty=isatty)
                outputs[name] = out.getvalue()
                out = io.StringIO()
                restore.resume_record(make_record(session_id=EVIL_SESSION, cwd=EVIL_CWD), EvilPathAgent(), Config(),
                                      stdout=out, launcher=launcher, isatty=isatty)
                outputs[name + ", evil executable path"] = out.getvalue()
        for name, text in outputs.items():
            with self.subTest(name):
                self.assertTrue(text, "every path prints something")
                self.assertEqual(unsafe_characters(text), [], repr(text))
        self.assertIn("fake-agent --resume $'s-\\xc2\\x9b31m'", outputs["off"])  # exact, yet printable
        self.assertIn("fake-agent --resume $'s-\\xc2\\x9b31m'", outputs["resume_record without a terminal"])
        self.assertIn("agent-resurrect resume $'s-\\xc2\\x9b31m'", outputs["resume_record launch failure"])

    def test_format_command_escapes_control_characters_exactly(self):
        parts = ["plain", "a b'c", "x\x1b[2Jy", "tab\there", "new\nline", "bell\x07 and \\ and 'q'",
                 "zero\u200bwidth", "caf\u00e9", "c1\x9bm", ""]
        for part in parts:
            quoted = restore.format_command(ResumePlan([part], None, None))
            self.assertEqual(unsafe_characters(quoted), [], repr(quoted))
        shells = []
        for shell in ("/bin/bash", shutil.which("bash"), "/bin/zsh", shutil.which("zsh")):
            if shell and os.access(shell, os.X_OK) and os.path.realpath(shell) not in [os.path.realpath(s) for s in shells]:
                shells.append(shell)
        if not shells:
            self.skipTest("no bash or zsh to evaluate the quoting")
        command = restore.format_command(ResumePlan(["printf", "%s\\0"] + parts, None, None))
        for shell in shells:
            with self.subTest(shell=shell):
                result = subprocess.run([shell, "-c", command], capture_output=True, timeout=30,
                                        env={"PATH": "/usr/bin:/bin", "LC_ALL": "en_US.UTF-8"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.split(b"\0")[:-1], [p.encode("utf-8") for p in parts])


class RestorePromptTest(unittest.TestCase):
    """The real countdown on a pseudo-terminal, and the yes/no question."""

    def open_pty(self):
        master, slave = pty.openpty()
        stdin = os.fdopen(slave, "r")
        self.addCleanup(stdin.close)
        self.addCleanup(os.close, master)
        return master, stdin

    def test_wait_for_key_returns_true_on_a_key_and_restores_the_terminal(self):
        master, stdin = self.open_pty()
        before = term_attrs(stdin.fileno())
        threading.Timer(0.3, os.write, (master, b"\x1b[A")).start()  # an arrow key: three bytes
        out = io.StringIO()
        started = time.monotonic()
        self.assertTrue(restore.wait_for_key(5, stdin=stdin, stdout=out))
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(term_attrs(stdin.fileno()), before)
        self.assertIn("\rResuming in 5s", out.getvalue())
        ready, _, _ = select.select([stdin.fileno()], [], [], 0)
        self.assertEqual(ready, [], "the whole key sequence is consumed")

    def test_wait_for_key_times_out_and_counts_down(self):
        master, stdin = self.open_pty()
        before = term_attrs(stdin.fileno())
        out = io.StringIO()
        started = time.monotonic()
        self.assertFalse(restore.wait_for_key(2, stdin=stdin, stdout=out))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1.9)
        self.assertLess(elapsed, 4)
        self.assertEqual(term_attrs(stdin.fileno()), before)
        text = out.getvalue()
        self.assertLess(text.index("in 2s"), text.index("in 1s"))
        self.assertTrue(text.endswith("\n"))

    def test_wait_for_key_zero_seconds_returns_at_once(self):
        master, stdin = self.open_pty()
        self.assertFalse(restore.wait_for_key(0, stdin=stdin, stdout=io.StringIO()))

    def test_wait_for_key_works_without_a_terminal_device(self):
        read_fd, write_fd = os.pipe()
        stdin = os.fdopen(read_fd, "r")
        self.addCleanup(stdin.close)
        os.write(write_fd, b"\n")
        os.close(write_fd)
        self.assertTrue(restore.wait_for_key(5, stdin=stdin, stdout=io.StringIO()))

    def test_ctrl_c_during_the_countdown_skips(self):
        master, stdin = self.open_pty()
        before = term_attrs(stdin.fileno())
        with mock.patch("select.select", side_effect=KeyboardInterrupt):
            self.assertTrue(restore.wait_for_key(5, stdin=stdin, stdout=io.StringIO()))
        self.assertEqual(term_attrs(stdin.fileno()), before)

    def test_ask_yes_no_defaults_to_yes(self):
        for answer, expected in [("\n", True), ("y\n", True), ("Yes\n", True), (" Y \n", True),
                                 ("n\n", False), ("no\n", False), ("nope\n", False), ("", False)]:
            with self.subTest(answer=answer):
                out = io.StringIO()
                self.assertEqual(restore.ask_yes_no("Resume?", stdin=io.StringIO(answer), stdout=out), expected)
                self.assertTrue(out.getvalue().startswith("Resume? [Y/n] "))

    def test_ask_yes_no_ctrl_c_means_no(self):
        stdin = mock.Mock()
        stdin.readline.side_effect = KeyboardInterrupt
        self.assertFalse(restore.ask_yes_no("Resume?", stdin=stdin, stdout=io.StringIO()))


if __name__ == "__main__":
    unittest.main()
