"""Tests for the Claude Code adapter: hook events to pane records, session lookup, and the resume command."""
import contextlib
import io
import json
import os
import unittest

from agent_resurrect import paths, store
from agent_resurrect.agents.base import HookContext
from agent_resurrect.agents.claude import ClaudeAdapter, is_claude_process
from agent_resurrect.config import Config
from agent_resurrect.model import SessionInfo
from tests.helpers import FakeProcs, FakeTerminal, make_record, proc, temp_home
from tests.test_finder import TEXT, assistant, user, write_session

AGENT_STARTED = "Mon Oct 12 09:15:00 2026"
SKIP = "--dangerously-skip-permissions"
START = {"session_id": "s1", "cwd": "/Users/me/proj", "transcript_path": "/x/s1.jsonl",
         "permission_mode": "bypassPermissions", "source": "startup"}


def tree(*extra, agent_args="claude", agent_stat="S+", tty="ttys029"):
    # pid 300 = hook (python3), 200 = sh -c, 100 = claude, 50 = -zsh (pane shell); chain[0] is the hook.
    return FakeProcs(proc(300, 200, "/usr/bin/python3", "python3 -I x hook claude SessionStart"),
                     proc(200, 100, "/bin/sh", "sh -c ..."),
                     proc(100, 50, "claude", agent_args, stat=agent_stat, tty=tty, started=AGENT_STARTED),
                     proc(50, 1, "-zsh"), *extra)


def ctx(procs):
    return HookContext(env={"FAKE_PANE": "p1"}, terminal=FakeTerminal(), pane_key="fake-p1",
                       pane_label="p1", procs=procs, pid=300)


class ExplodingProcs(FakeProcs):
    def ancestors(self, pid):
        raise RuntimeError("ps exploded")


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self._home = temp_home()
        self.home = self._home.__enter__()
        self.addCleanup(self._home.__exit__, None, None, None)
        self.agent = ClaudeAdapter()

    def start(self, procs=None, **overrides):
        self.agent.on_hook("SessionStart", dict(START, **overrides), ctx(procs or tree()))

    def end(self, reason, session_id="s1", procs=None):
        payload = {"session_id": session_id, "cwd": "/Users/me/proj", "transcript_path": "/x/s1.jsonl"}
        if reason is not None:
            payload["reason"] = reason
        self.agent.on_hook("SessionEnd", payload, ctx(procs or tree()))

    def log_text(self):
        return paths.log_path().read_text() if paths.log_path().exists() else ""


class ClaudeHookTest(HookTestCase):
    def test_session_start_records_pane(self):
        self.start()
        record = store.load("fake-p1")
        self.assertEqual((record.session_id, record.agent_pid, record.agent_started, record.status, record.terminal),
                         ("s1", 100, AGENT_STARTED, "live", "fake"))
        self.assertEqual((record.agent, record.pane_key, record.pane_label, record.cwd),
                         ("claude", "fake-p1", "p1", "/Users/me/proj"))
        self.assertEqual((record.transcript_path, record.permission_mode, record.source),
                         ("/x/s1.jsonl", "bypassPermissions", "startup"))
        self.assertTrue(record.recorded_at and record.updated_at)

    def test_nested_claude_is_not_recorded(self):
        self.start(procs=tree(proc(50, 40, "claude", stat="S+", tty="ttys029"), proc(40, 1, "-zsh")))
        self.assertIsNone(store.load("fake-p1"))
        self.assertIn("nested", self.log_text())

    def test_print_mode_background_and_no_tty_are_not_recorded(self):
        cases = [dict(agent_args="claude -p hi"), dict(agent_args="claude --print hi"),
                 dict(agent_args="claude --print=json"), dict(agent_stat="S"), dict(tty="??")]
        for case in cases:
            with self.subTest(**case):
                self.start(procs=tree(**case))
                self.assertIsNone(store.load("fake-p1"))

    def test_flags_that_only_look_like_print_are_recorded(self):
        self.start(procs=tree(agent_args="claude --permission-mode plan --append-system-prompt -pretty"))
        self.assertEqual(store.load("fake-p1").session_id, "s1")

    def test_node_installed_claude_is_recognised(self):
        node = proc(100, 50, "node", "node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js",
                    stat="S+", tty="ttys029")
        self.start(procs=tree(node))
        self.assertEqual(store.load("fake-p1").agent_pid, 100)

    def test_no_claude_ancestor_records_nothing(self):
        procs = FakeProcs(proc(300, 50, "/usr/bin/python3", "python3 -I x hook claude SessionStart"),
                          proc(50, 1, "-zsh"))
        self.start(procs=procs)
        self.assertIsNone(store.load("fake-p1"))
        self.assertIn("no claude process", self.log_text())

    def test_clear_replaces_session_id(self):
        self.start()
        self.start(session_id="s2", source="clear", transcript_path="/x/s2.jsonl")
        record = store.load("fake-p1")
        self.assertEqual((record.session_id, record.source, record.transcript_path), ("s2", "clear", "/x/s2.jsonl"))

    def test_recorded_at_is_kept_only_for_the_same_session(self):
        self.start()
        record = store.load("fake-p1")
        record.recorded_at = "2026-01-01T00:00:00Z"
        store.save(record)
        self.start(source="resume")
        self.assertEqual(store.load("fake-p1").recorded_at, "2026-01-01T00:00:00Z")
        self.start(session_id="s2", source="clear")
        self.assertNotEqual(store.load("fake-p1").recorded_at, "2026-01-01T00:00:00Z")

    def test_restarted_session_is_live_again(self):
        self.start()
        store.set_status("fake-p1", "consumed")
        self.start(source="resume")
        self.assertEqual(store.load("fake-p1").status, "live")

    def test_deliberate_exit_deletes_and_other_keeps(self):
        for reason in ("prompt_input_exit", "logout"):
            with self.subTest(reason=reason):
                self.start()
                self.end(reason)
                self.assertIsNone(store.load("fake-p1"))
        for reason in ("other", "clear", "resume", None, "something-new"):
            with self.subTest(reason=reason):
                self.start()
                self.end(reason)
                self.assertEqual(store.load("fake-p1").session_id, "s1")

    def test_session_end_for_another_session_is_ignored(self):
        self.start()
        self.end("prompt_input_exit", session_id="s9")
        self.assertEqual(store.load("fake-p1").session_id, "s1")

    def test_session_end_from_a_nested_claude_is_ignored(self):
        self.start()
        nested = tree(proc(50, 40, "claude", stat="S+", tty="ttys029"), proc(40, 1, "-zsh"))
        self.end("prompt_input_exit", procs=nested)
        self.assertEqual(store.load("fake-p1").session_id, "s1")

    def test_session_end_without_a_record_is_harmless(self):
        self.end("prompt_input_exit")
        self.assertIsNone(store.load("fake-p1"))

    def test_bad_payloads_never_raise(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.agent.on_hook("SessionStart", {}, ctx(tree()))
            self.agent.on_hook("SessionStart", "x", ctx(tree()))
            self.agent.on_hook("SessionStart", None, ctx(tree()))
            self.agent.on_hook("SessionStart", {"session_id": 5}, ctx(tree()))
            self.agent.on_hook("SessionStart", dict(START, cwd=["not", "a", "path"]), ctx(tree()))
            self.agent.on_hook("Bogus", START, ctx(tree()))
            self.agent.on_hook("SessionStart", START, ctx(ExplodingProcs()))
            self.agent.on_hook("SessionEnd", dict(START, reason="logout"), ctx(ExplodingProcs()))
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        record = store.load("fake-p1")
        # Only the payload with a non-string cwd was recordable; its cwd is dropped, not stored as a list.
        self.assertEqual((record.session_id, record.cwd), ("s1", ""))
        store.delete("fake-p1")
        self.agent.on_hook("Bogus", START, ctx(tree()))
        self.assertIsNone(store.load("fake-p1"))
        self.assertIn("ps exploded", self.log_text())


class IsClaudeProcessTest(unittest.TestCase):
    def test_recognises_install_methods_and_rejects_the_hook(self):
        yes = [proc(1, 0, "claude", "claude --resume x"),
               proc(1, 0, "/Users/me/.local/bin/claude", "/Users/me/.local/bin/claude"),
               proc(1, 0, "node", "node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js"),
               proc(1, 0, "bun", "bun /opt/claude-code/cli.js"),
               proc(1, 0, "2.1.223", "/Users/me/.local/bin/claude --dangerously-skip-permissions")]
        no = [proc(1, 0, "/usr/bin/python3", "python3 -I x hook claude SessionStart"),
              proc(1, 0, "node", "node server.js"), proc(1, 0, "-zsh", "-zsh"),
              proc(1, 0, "vim", "vim claude.md"), proc(1, 0, "Claude", "/Applications/Claude.app/Contents/MacOS/Claude"),
              proc(1, 0, "x", "")]
        for p in yes:
            self.assertTrue(is_claude_process(p), p)
        for p in no:
            self.assertFalse(is_claude_process(p), p)


class ClaudeSessionTest(unittest.TestCase):
    def setUp(self):
        self._home = temp_home()
        home = self._home.__enter__()
        self.addCleanup(self._home.__exit__, None, None, None)
        os.environ["CLAUDE_CONFIG_DIR"] = str(home / "claude-config")
        self.projects = home / "claude-config" / "projects"
        self.agent = ClaudeAdapter()

    def record(self, path=None, **overrides):
        values = dict(agent="claude", session_id="s1", transcript_path=str(path) if path else None)
        values.update(overrides)
        return make_record(**values)

    def argv(self, record, flag):
        return self.agent.resume_argv(record, Config(permission_flag=flag))

    def test_resume_argv_permission_flag(self):
        path = write_session(self.projects, "-Users-me-proj", "s1", [
            user("go", mode="default"), user("again", mode="bypassPermissions"), assistant("done")])
        record = self.record(path, permission_mode="default")  # the transcript is newer than the hook payload
        self.assertEqual(self.argv(record, "mirror"), ["claude", "--resume", "s1", SKIP])
        self.assertEqual(self.argv(record, "never"), ["claude", "--resume", "s1"])
        self.assertEqual(self.argv(record, "always"), ["claude", "--resume", "s1", SKIP])
        path = write_session(self.projects, "-Users-me-proj", "s2", [
            user("go", sid="s2", mode="bypassPermissions"), user("calm down", sid="s2", mode="default")])
        record = self.record(path, session_id="s2", permission_mode="bypassPermissions")
        self.assertEqual(self.argv(record, "mirror"), ["claude", "--resume", "s2"])
        self.assertEqual(self.argv(record, "always"), ["claude", "--resume", "s2", SKIP])

    def test_resume_argv_falls_back_to_the_recorded_mode(self):
        gone = self.record(self.projects / "-Users-me-proj" / "s1.jsonl", permission_mode="bypassPermissions")
        self.assertEqual(self.argv(gone, "mirror"), ["claude", "--resume", "s1", SKIP])
        path = write_session(self.projects, "-Users-me-proj", "s2", [user("no mode recorded", sid="s2")])
        self.assertEqual(self.argv(self.record(path, session_id="s2", permission_mode="bypassPermissions"), "mirror"),
                         ["claude", "--resume", "s2", SKIP])
        self.assertEqual(self.argv(self.record(path, session_id="s2", permission_mode="plan"), "mirror"),
                         ["claude", "--resume", "s2"])

    def test_session_exists(self):
        path = write_session(self.projects, "-Users-me-proj", "s1", [user("hello there")])
        self.assertTrue(self.agent.session_exists(self.record(path)))
        moved = self.record(self.projects / "-Users-me-old" / "s1.jsonl")  # stale path, found by id
        self.assertTrue(self.agent.session_exists(moved))
        self.assertTrue(self.agent.session_exists(self.record()))
        self.assertFalse(self.agent.session_exists(self.record(session_id="s9")))
        path.unlink()
        self.assertFalse(self.agent.session_exists(self.record(path)))

    def test_describe(self):
        path = write_session(self.projects, "-Users-me-proj", "s1", [
            user("hello there"), {"type": "ai-title", "aiTitle": "Fix login"}])
        info = self.agent.describe(self.record(path))
        self.assertEqual((info.title, info.first_prompt, info.started),
                         ("Fix login", "hello there", "2026-10-10T10:00:00Z"))
        self.assertEqual(self.agent.describe(self.record(session_id="s9")), SessionInfo())

    def test_session_record_rebuilds_from_transcript(self):
        path = write_session(self.projects, "-Users-me-proj", "s1", [
            user("hello there", mode="default"), user("go faster", mode="bypassPermissions")])
        record = self.agent.session_record("s1")
        self.assertEqual((record.terminal, record.pane_key, record.agent, record.session_id),
                         ("", "", "claude", "s1"))
        self.assertEqual((record.cwd, record.transcript_path, record.permission_mode),
                         ("/Users/me/proj", str(path), "bypassPermissions"))
        self.assertIsNone(self.agent.session_record("nope"))
        self.assertIsNone(self.agent.session_record("../../etc/passwd"))

    def test_find_by_text_searches_claude_config_dir_and_excludes_current_session(self):
        write_session(self.projects, "-Users-me-proj", "s1", [user(TEXT)])
        write_session(self.projects, "-Users-me-proj", "s2", [user(TEXT, sid="s2")])
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s1"
        self.assertEqual([c.session_id for c in self.agent.find_by_text(TEXT)], ["s2"])
        self.assertEqual([c.session_id for c in self.agent.find_by_text(TEXT, exclude="s2")], ["s1"])
        del os.environ["CLAUDE_CODE_SESSION_ID"]
        self.assertEqual(len(self.agent.find_by_text(TEXT, limit=1)), 1)

    def test_record_json_matches_the_spec_shape(self):
        # The pane record on disk is the contract with the restorer; keep its keys stable.
        ctx_ = ctx(tree())
        self.agent.on_hook("SessionStart", START, ctx_)
        data = json.loads(store.record_path("fake-p1").read_text())
        self.assertEqual(sorted(data), sorted([
            "schema", "terminal", "pane_key", "pane_label", "agent", "session_id", "cwd", "transcript_path",
            "permission_mode", "source", "agent_pid", "agent_started", "status", "recorded_at", "updated_at"]))


if __name__ == "__main__":
    unittest.main()
