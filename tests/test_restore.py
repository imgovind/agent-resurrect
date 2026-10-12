"""Tests for restore: eligibility, resume plans, the exec launcher, prompts and `auto`."""
import io
import json
import os
import pty
import select
import shutil
import signal
import tempfile
import termios
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from agent_resurrect import paths, restore, store
from agent_resurrect.config import Config
from agent_resurrect.model import (STATUS_CONSUMED, STATUS_LIVE, STATUS_SKIPPED, ISO_FORMAT, Record,
                                   SessionInfo, utc_now_iso)
from agent_resurrect.restore import Decision, ResumePlan
from tests.helpers import FakeAgent, FakeProcs, FakeTerminal, make_record, proc, temp_home

ENV = {"FAKE_PANE": "p1"}
KEY = "fake-p1"
OTHER_START = "Tue Oct 13 09:00:00 2026"
SKIP_HINT = 'Skipped. Run "agent-resurrect" in this pane to resume it later.'


def iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(ISO_FORMAT)


def never(*args, **kwargs):
    raise AssertionError(f"must not be called: {args!r} {kwargs!r}")


def events():
    return [e["result"] for e in store.read_restore_events()]


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


class RestorePlanTest(unittest.TestCase):
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


class RestoreLaunchTest(unittest.TestCase):
    def test_launch_execs_without_spawning(self):
        calls = []
        with mock.patch("os.chdir", side_effect=lambda d: calls.append(("chdir", d))), \
                mock.patch("os.execvp", side_effect=lambda f, a: calls.append(("execvp", f, list(a)))), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("must not spawn")), \
                mock.patch("signal.signal"):
            restore.launch(ResumePlan(["claude", "--resume", "x"], "/tmp", None))
        self.assertEqual(calls, [("chdir", "/tmp"), ("execvp", "claude", ["claude", "--resume", "x"])])

    def test_launch_without_cwd_stays_put(self):
        calls = []
        with mock.patch("os.chdir", side_effect=lambda d: calls.append(("chdir", d))), \
                mock.patch("os.execvp", side_effect=lambda f, a: calls.append(("execvp", f))), \
                mock.patch("signal.signal"):
            restore.launch(ResumePlan(["claude"], None, None))
        self.assertEqual(calls, [("execvp", "claude")])

    def test_launch_restores_signals_python_ignores_before_exec(self):
        # Python starts with SIGPIPE (and SIGXFSZ) ignored; an ignored disposition survives exec.
        order = []
        with mock.patch("signal.signal", side_effect=lambda s, h: order.append((s, h))), \
                mock.patch("os.execvp", side_effect=lambda f, a: order.append("exec")):
            restore.launch(ResumePlan(["claude"], None, None))
        self.assertIn((signal.SIGPIPE, signal.SIG_DFL), order)
        self.assertEqual(order[-1], "exec")

    def test_launch_flushes_output_before_exec(self):
        stream = mock.Mock()
        with mock.patch("sys.stdout", stream), mock.patch("signal.signal"), \
                mock.patch("os.execvp", side_effect=lambda f, a: self.assertTrue(stream.flush.called)):
            restore.launch(ResumePlan(["claude"], None, None))

    def test_launch_of_a_missing_executable_raises_oserror(self):
        empty = tempfile.mkdtemp(prefix="ar-path-")
        self.addCleanup(shutil.rmtree, empty, True)
        with mock.patch.dict(os.environ, {"PATH": empty}), mock.patch("signal.signal"):
            with self.assertRaises(OSError):
                restore.launch(ResumePlan(["agent-resurrect-no-such-binary"], None, None))


class RestoreResumeRecordTest(unittest.TestCase):
    def test_resume_record_announces_consumes_and_launches(self):
        with temp_home():
            record = store.save(make_record())
            out, seen = io.StringIO(), []

            def launcher(plan):
                seen.append((plan.argv, plan.cwd, store.load(KEY).status))

            rc = restore.resume_record(record, FakeAgent(), Config(), stdout=out, launcher=launcher)
            self.assertEqual(rc, 0)
            self.assertEqual(seen, [(["fake-agent", "--resume", "s-1"], "/tmp", STATUS_CONSUMED)])
            self.assertEqual(out.getvalue(), 'Resurrecting "title of s-1" (fake) in /tmp\n')
            self.assertEqual(store.read_restore_events()[-1]["result"], "resumed")
            self.assertEqual(store.read_restore_events()[-1]["detail"], "s-1")

    def test_launch_failure_restores_live(self):
        with temp_home():
            record = store.save(make_record())
            out = io.StringIO()

            def launcher(plan):
                raise FileNotFoundError(2, "No such file or directory", "fake-agent")

            rc = restore.resume_record(record, FakeAgent(), Config(), stdout=out, launcher=launcher)
            self.assertEqual(rc, 1)
            self.assertEqual(store.load(KEY).status, STATUS_LIVE)
            self.assertEqual(events(), ["resumed", "launch-failed"])
            self.assertIn("agent-resurrect: could not start fake-agent: ", out.getvalue())

    def test_missing_cwd_warning_is_printed(self):
        with temp_home() as home:
            record = store.save(make_record(cwd=str(home / "deleted")))
            out, seen = io.StringIO(), []
            restore.resume_record(record, FakeAgent(), Config(), stdout=out, launcher=seen.append,
                                  announce=False)
            self.assertEqual(out.getvalue(), f"working directory {home / 'deleted'} no longer exists; "
                                             "resuming from the current directory\n")
            self.assertIsNone(seen[0].cwd)

    def test_record_without_a_pane_is_launched_without_touching_the_store(self):
        with temp_home():
            record = Record(terminal="", pane_key="", agent="fake", session_id="s-9", cwd="")
            seen = []
            rc = restore.resume_record(record, FakeAgent(), Config(), stdout=io.StringIO(),
                                       launcher=seen.append)
            self.assertEqual(rc, 0)
            self.assertEqual(seen[0].argv, ["fake-agent", "--resume", "s-9"])
            self.assertFalse(paths.panes_dir().exists())

    def test_a_newer_session_in_the_same_pane_is_not_consumed(self):
        with temp_home():
            stale = make_record(session_id="s-1")
            store.save(make_record(session_id="s-2"))
            restore.resume_record(stale, FakeAgent(), Config(), stdout=io.StringIO(), launcher=lambda p: None)
            self.assertEqual(store.load(KEY).status, STATUS_LIVE)

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
                restore.resume_record(make_record(), agent, Config(), stdout=out, launcher=lambda p: None)
                self.assertTrue(out.getvalue().startswith(f"Resurrecting {expected} (fake) in /tmp"), out.getvalue())
        with temp_home():
            out = io.StringIO()
            agent = Described(SessionInfo(first_prompt=long_prompt))
            restore.resume_record(make_record(), agent, Config(), stdout=out, launcher=lambda p: None)
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

    def test_auto_launch_failure_restores_live(self):
        self.store_record()

        def launcher(plan):
            raise PermissionError(13, "Permission denied", "fake-agent")

        self.assertEqual(self.auto(config=Config(auto_resume="immediate"), launcher=launcher), 1)
        self.assertEqual(store.load(KEY).status, STATUS_LIVE)
        self.assertEqual(events(), ["resumed", "launch-failed"])

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
