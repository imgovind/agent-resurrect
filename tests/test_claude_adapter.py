"""Tests for the Claude Code adapter: hook events to pane records, session lookup, and the resume command."""
import contextlib
import io
import json
import os
import stat
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
# How an agent-team teammate in an iTerm2 split pane looks in `ps` (Claude Code 2.1.296): the lead starts the
# native installer's versioned binary by its full path, from the new pane's login shell.
VERSIONED = "/Users/me/.local/share/claude/versions/2.1.296"
TEAMMATE_ARGS = (f"{VERSIONED} --agent-id worker@session-e90013a3 --agent-name worker --team-name session-e90013a3"
                 " --agent-color purple --parent-session-id e90013a3-5289-4a3b-9c1d-2f6a7b8c9d0e")


def tree(*extra, agent_args="claude", agent_stat="S+", tty="ttys029", agent_comm="claude"):
    # pid 300 = hook (python3), 200 = sh -c, 100 = claude, 50 = -zsh (pane shell); chain[0] is the hook.
    return FakeProcs(proc(300, 200, "/usr/bin/python3", "python3 -I x hook claude SessionStart"),
                     proc(200, 100, "/bin/sh", "sh -c ..."),
                     proc(100, 50, agent_comm, agent_args, stat=agent_stat, tty=tty, started=AGENT_STARTED),
                     proc(50, 1, "-zsh"), *extra)


def ctx(procs, **env):
    return HookContext(env=dict({"FAKE_PANE": "p1"}, **env), terminal=FakeTerminal(), pane_key="fake-p1",
                       pane_label="p1", procs=procs, pid=300)


class ExplodingProcs(FakeProcs):
    def ancestors(self, pid):
        raise RuntimeError("ps exploded")


class CountingProcs(FakeProcs):
    """A process table that counts the chain walks (each costs two `ps` runs per ancestor for real)."""

    def __init__(self, *infos):
        super().__init__(*infos)
        self.walks = 0

    def ancestors(self, pid):
        self.walks += 1
        return super().ancestors(pid)


def counting_tree(**kw):
    return CountingProcs(*tree(**kw).table.values())


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

    def test_permission_mode_is_never_taken_from_command_line_text(self):
        # Regression guard (security review, 2026-10-12): `ps` joins argv with spaces, so a prompt such as
        # `claude "why is --dangerously-skip-permissions risky?"` is indistinguishable from the real flag.
        # Trusting that text would let a resurrect ADD bypass mode the user never chose. The mode comes
        # only from the hook payload or the transcript's top-level permissionMode (written by Claude Code).
        payload_without_mode = {k: v for k, v in START.items() if k != "permission_mode"}
        for args in ("claude why is --dangerously-skip-permissions risky",
                     "claude --dangerously-skip-permissions",
                     "claude explain --permission-mode bypassPermissions"):
            with self.subTest(args=args):
                store.delete("fake-p1")
                self.agent.on_hook("SessionStart", dict(payload_without_mode), ctx(tree(agent_args=args)))
                self.assertIsNone(store.load("fake-p1").permission_mode)

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

    def test_session_end_walks_processes_only_for_a_deliberate_exit_of_the_recorded_session(self):
        # SessionEnd hooks share a 1.5 s budget that the plugin's timeout does not raise. The ~100 ms `ps`
        # walk is needed only when there is something to delete: everything else returns after one stat.
        self.start()
        for reason, session_id in (("other", "s1"), ("clear", "s1"), (None, "s1"), ("prompt_input_exit", "s9")):
            with self.subTest(reason=reason, session_id=session_id):
                procs = counting_tree()
                self.end(reason, session_id=session_id, procs=procs)
                self.assertEqual(procs.walks, 0)
                self.assertEqual(store.load("fake-p1").session_id, "s1")
        store.delete("fake-p1")
        procs = counting_tree()
        self.end("logout", procs=procs)
        self.assertEqual(procs.walks, 0)
        self.start()
        procs = counting_tree()
        self.end("logout", procs=procs)
        self.assertEqual(procs.walks, 1)
        self.assertIsNone(store.load("fake-p1"))

    def test_session_end_deletes_when_the_walk_finds_no_claude(self):
        # Only a nested claude keeps the record. A walk that cannot see the agent (ps failed) must not leave
        # a record live after a deliberate exit: the next new shell would resurrect a session the user quit.
        self.start()
        self.end("prompt_input_exit", procs=FakeProcs())
        self.assertIsNone(store.load("fake-p1"))

    def test_session_start_records_claude_config_dir(self):
        self.agent.on_hook("SessionStart", dict(START), ctx(tree(), CLAUDE_CONFIG_DIR="/Users/me/.claude-work"))
        self.assertEqual(store.load("fake-p1").resume_env, {"CLAUDE_CONFIG_DIR": "/Users/me/.claude-work"})
        for env in ({}, {"CLAUDE_CONFIG_DIR": ""}):
            with self.subTest(env=env):
                self.agent.on_hook("SessionStart", dict(START), ctx(tree(), **env))
                self.assertIsNone(store.load("fake-p1").resume_env)

    def test_unsafe_session_ids_are_not_recorded(self):
        # The id becomes an argv element of `claude --resume <id>`: a leading dash would be read as a flag.
        for session_id in ("-rf", SKIP, "../../etc/passwd", "a b", "s1\n"):
            with self.subTest(session_id=session_id):
                self.start(session_id=session_id)
                self.assertIsNone(store.load("fake-p1"))
                self.assertIn("unusable session_id", self.log_text())

    def test_agent_team_teammates_are_not_recorded(self):
        # Teammates run in their own iTerm2 panes with a tty, in the foreground and not nested, so only
        # their flags tell them apart. Resumed alone they would lose their team; the lead's session is the
        # one to resume.
        cases = [dict(agent_comm=VERSIONED, agent_args=TEAMMATE_ARGS),
                 dict(agent_args="claude --agent-id worker@team --team-name team"),
                 dict(agent_args="claude --team-name=team"),
                 dict(agent_args="claude --parent-session-id e90013a3-5289-4a3b-9c1d-2f6a7b8c9d0e")]
        for case in cases:
            with self.subTest(**case):
                self.start(procs=tree(**case))
                self.assertIsNone(store.load("fake-p1"))
                self.assertIn("agent-team teammate", self.log_text())
        self.start(procs=tree(agent_comm=VERSIONED, agent_args=VERSIONED))  # the same binary, no team flags
        self.assertEqual(store.load("fake-p1").session_id, "s1")

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
               proc(1, 0, "2.1.223", "/Users/me/.local/bin/claude --dangerously-skip-permissions"),
               proc(1, 0, VERSIONED, TEAMMATE_ARGS),  # recognised, so the teammate rule (not luck) skips it
               proc(1, 0, "/Users/Jo Smith/.local/share/claude/versions/2.1.296",
                    "/Users/Jo Smith/.local/share/claude/versions/2.1.296")]  # comm survives the space
        no = [proc(1, 0, "/usr/bin/python3", "python3 -I x hook claude SessionStart"),
              proc(1, 0, "node", "node server.js"), proc(1, 0, "-zsh", "-zsh"),
              proc(1, 0, "vim", "vim claude.md"), proc(1, 0, "Claude", "/Applications/Claude.app/Contents/MacOS/Claude"),
              proc(1, 0, "x", ""), proc(1, 0, "/opt/tool/versions/2.1", "/opt/tool/versions/2.1"),
              proc(1, 0, "/usr/bin/python3", "/usr/bin/python3 /Users/me/claude/versions/run.py")]
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

    def test_lookups_use_the_records_config_dir(self):
        # A session recorded under CLAUDE_CONFIG_DIR=~/.claude-work (e.g. an alias for a second account) is
        # looked up there, whatever the restoring shell's environment says.
        work = self.projects.parent.parent / ".claude-work"
        path = write_session(work / "projects", "-Users-me-proj", "s7", [
            user("hello work", sid="s7", mode="bypassPermissions"), {"type": "ai-title", "aiTitle": "Work"}])
        record = self.record(session_id="s7", resume_env={"CLAUDE_CONFIG_DIR": str(work)})
        self.assertTrue(self.agent.session_exists(record))
        self.assertEqual(self.agent.describe(record).title, "Work")
        self.assertEqual(self.argv(record, "mirror"), ["claude", "--resume", "s7", SKIP])
        stale = self.record(path.with_name("moved.jsonl"), session_id="s7", resume_env={"CLAUDE_CONFIG_DIR": str(work)})
        self.assertTrue(self.agent.session_exists(stale))
        # Without resume_env the environment's config dir is searched, which does not have it.
        self.assertFalse(self.agent.session_exists(self.record(session_id="s7")))
        for bad in ({"CLAUDE_CONFIG_DIR": ""}, {"CLAUDE_CONFIG_DIR": 5}, ["not", "a", "dict"]):
            with self.subTest(resume_env=bad):
                self.assertFalse(self.agent.session_exists(self.record(session_id="s7", resume_env=bad)))

    def test_session_record_carries_the_config_dir(self):
        write_session(self.projects, "-Users-me-proj", "s1", [user("hello there")])
        self.assertEqual(self.agent.session_record("s1").resume_env,
                         {"CLAUDE_CONFIG_DIR": os.environ["CLAUDE_CONFIG_DIR"]})
        del os.environ["CLAUDE_CONFIG_DIR"]
        write_session(paths.home() / ".claude" / "projects", "-Users-me-proj", "s2", [user("hi there", sid="s2")])
        record = self.agent.session_record("s2")
        self.assertEqual((record.session_id, record.resume_env), ("s2", None))

    def test_resume_argv_finds_a_claude_that_is_not_on_path(self):
        # The old "local install" lives at ~/.claude/local/claude and is reached through a shell alias,
        # which exec cannot see. PATH wins when it has claude; otherwise "claude", so the error names it.
        bin_dir = paths.home() / "bin"
        bin_dir.mkdir()
        os.environ["PATH"] = str(bin_dir)
        local = paths.home() / ".claude" / "local" / "claude"
        argv0 = lambda: self.argv(self.record(), "never")[0]  # noqa: E731
        self.assertEqual(argv0(), "claude")
        local.parent.mkdir(parents=True)
        local.write_text("#!/bin/sh\n")
        self.assertEqual(argv0(), "claude")  # not executable
        local.chmod(local.stat().st_mode | stat.S_IXUSR)
        self.assertEqual(argv0(), str(local))
        on_path = bin_dir / "claude"
        on_path.write_text("#!/bin/sh\n")
        on_path.chmod(0o755)
        self.assertEqual(argv0(), "claude")

    def test_find_by_text_searches_claude_config_dir_and_excludes_current_session(self):
        write_session(self.projects, "-Users-me-proj", "s1", [user(TEXT)])
        write_session(self.projects, "-Users-me-proj", "s2", [user(TEXT, sid="s2")])
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s1"
        self.assertEqual([c.session_id for c in self.agent.find_by_text(TEXT)], ["s2"])
        self.assertEqual([c.session_id for c in self.agent.find_by_text(TEXT, exclude="s2")], ["s1"])
        del os.environ["CLAUDE_CODE_SESSION_ID"]
        self.assertEqual(len(self.agent.find_by_text(TEXT, limit=1)), 1)

    def test_record_json_matches_the_spec_shape_for_adopt_too(self):
        write_session(self.projects, "-Users-me-proj", ADOPT_ID, [user("hello there", sid=ADOPT_ID)])
        self.agent.adopt(adopt_ctx(adopt_tree(), CLAUDE_CONFIG_DIR=os.environ["CLAUDE_CONFIG_DIR"]))
        data = json.loads(store.record_path("fake-p1").read_text())
        self.assertEqual(sorted(data), sorted(make_record().to_dict()))

    def test_record_json_matches_the_spec_shape(self):
        # The pane record on disk is the contract with the restorer; keep its keys stable.
        ctx_ = ctx(tree())
        self.agent.on_hook("SessionStart", START, ctx_)
        data = json.loads(store.record_path("fake-p1").read_text())
        self.assertEqual(sorted(data), sorted([
            "schema", "terminal", "pane_key", "pane_label", "agent", "session_id", "cwd", "transcript_path",
            "permission_mode", "source", "agent_pid", "agent_started", "resume_env", "status", "recorded_at",
            "updated_at"]))


ADOPT_ID = "83fc435f-73ea-41b2-b2e3-076ee2c3f7b0"


def adopt_tree(*extra, agent_args="claude", agent_stat="S+", tty="ttys029", agent_comm="claude"):
    """`agent-resurrect adopt` run by the Bash tool of an interactive claude in pane p1 (Claude Code 2.1.296):
    pid 300 = the CLI (python3), 200 = the tool's zsh (no tty), 100 = claude, 50 = -zsh (pane shell)."""
    return FakeProcs(proc(300, 200, "/usr/bin/python3", "/usr/bin/python3 -I /x/libexec/agent-resurrect.py adopt"),
                     proc(200, 100, "/bin/zsh", "/bin/zsh -c source /x/snapshot.sh && agent-resurrect adopt",
                          stat="Ss", tty="??"),
                     proc(100, 50, agent_comm, agent_args, stat=agent_stat, tty=tty, started=AGENT_STARTED),
                     proc(50, 1, "-zsh"), *extra)


def adopt_ctx(procs, pane_key="fake-p1", **env):
    # What Claude Code puts in every tool call's environment, plus the pane and the shell's PWD.
    base = {"FAKE_PANE": "p1", "CLAUDE_CODE_SESSION_ID": ADOPT_ID, "CLAUDE_PID": "100", "CLAUDECODE": "1",
            "CLAUDE_CODE_CHILD_SESSION": "1", "PWD": "/Users/me/proj/sub"}
    base.update(env)
    return HookContext(env={k: v for k, v in base.items() if v is not None}, terminal=FakeTerminal(),
                       pane_key=pane_key, pane_label="p1", procs=procs, pid=300)


class ClaudeAdoptTest(HookTestCase):
    """adopt records the session a tool call runs in, for sessions that began before the plugin existed."""

    def setUp(self):
        super().setUp()
        self.config_dir = str(self.home / "claude-config")
        self.projects = self.home / "claude-config" / "projects"

    def transcript(self, folder="-Users-me-proj", records=None):
        return write_session(self.projects, folder, ADOPT_ID, records or [
            user("hello there", sid=ADOPT_ID, mode="default"), assistant("hi", sid=ADOPT_ID),
            {"type": "permission-mode", "permissionMode": "acceptEdits", "sessionId": ADOPT_ID}])

    def adopt(self, procs=None, **env):
        env.setdefault("CLAUDE_CONFIG_DIR", self.config_dir)
        return self.agent.adopt(adopt_ctx(procs or adopt_tree(), **env))

    def test_adopt_records_the_running_session(self):
        path = self.transcript()
        record = self.adopt()
        self.assertEqual(record, store.load("fake-p1"))
        self.assertEqual((record.terminal, record.pane_key, record.pane_label, record.agent, record.session_id),
                         ("fake", "fake-p1", "p1", "claude", ADOPT_ID))
        self.assertEqual((record.cwd, record.transcript_path, record.permission_mode, record.source),
                         ("/Users/me/proj", str(path), "acceptEdits", "adopt"))
        self.assertEqual((record.agent_pid, record.agent_started, record.status),
                         (100, AGENT_STARTED, "live"))
        self.assertEqual(record.resume_env, {"CLAUDE_CONFIG_DIR": self.config_dir})
        self.assertTrue(record.recorded_at and record.updated_at)

    def test_adopt_without_claude_config_dir_uses_the_default(self):
        self.projects = paths.home() / ".claude" / "projects"
        self.transcript()
        record = self.adopt(CLAUDE_CONFIG_DIR=None)
        self.assertEqual((record.session_id, record.resume_env), (ADOPT_ID, None))

    def test_adopt_works_without_claude_pid(self):
        self.transcript()
        self.assertEqual(self.adopt(CLAUDE_PID=None).agent_pid, 100)

    def test_untrusted_transcript_cwd_falls_back_to_pwd(self):
        # The transcript's cwd does not encode to its folder, so it is not where `claude --resume` looks.
        self.transcript(folder="-Users-me-other")
        self.assertEqual(self.adopt().cwd, "/Users/me/proj/sub")
        store.delete("fake-p1")
        self.assertIsNone(self.adopt(PWD=None))
        self.assertIn("no working directory", self.log_text())

    def test_adopt_keeps_recorded_at_for_the_same_session_and_replaces_another(self):
        self.transcript()
        store.save(make_record(agent="claude", session_id="old-session", status="consumed"))
        record = self.adopt()
        self.assertEqual((record.session_id, record.status), (ADOPT_ID, "live"))
        record.recorded_at = "2026-01-01T00:00:00Z"
        store.save(record)
        self.assertEqual(self.adopt().recorded_at, "2026-01-01T00:00:00Z")

    def test_permission_mode_comes_only_from_the_transcript(self):
        # Same guard as SessionStart: the command line never decides the mode.
        self.transcript(records=[user("hello there", sid=ADOPT_ID)])
        for args in ("claude --dangerously-skip-permissions", "claude --permission-mode bypassPermissions"):
            with self.subTest(args=args):
                self.assertIsNone(self.adopt(procs=adopt_tree(agent_args=args)).permission_mode)

    def test_adopt_returns_none_and_logs_when_anything_is_missing(self):
        self.transcript()
        nested = adopt_tree(proc(50, 40, "claude", stat="S+", tty="ttys029"), proc(40, 1, "-zsh"))
        no_claude = FakeProcs(proc(300, 200, "/usr/bin/python3", "python3 adopt"), proc(200, 1, "-zsh"))
        cases = {
            "no session id": (dict(CLAUDE_CODE_SESSION_ID=None), adopt_tree(), "CLAUDE_CODE_SESSION_ID"),
            "empty session id": (dict(CLAUDE_CODE_SESSION_ID=""), adopt_tree(), "CLAUDE_CODE_SESSION_ID"),
            "flag-like id": (dict(CLAUDE_CODE_SESSION_ID="-rf"), adopt_tree(), "unusable session id"),
            "path-like id": (dict(CLAUDE_CODE_SESSION_ID="../x"), adopt_tree(), "unusable session id"),
            "no transcript": (dict(CLAUDE_CODE_SESSION_ID="0f0f0f0f-0000-0000-0000-000000000000"),
                              adopt_tree(), "no transcript"),
            "other config dir": (dict(CLAUDE_CONFIG_DIR=str(self.home / "elsewhere")), adopt_tree(),
                                 "no transcript"),
            "no claude": ({}, no_claude, "no claude process"),
            "nested": ({}, nested, "nested"),
            "print mode": ({}, adopt_tree(agent_args="claude -p 'run adopt'"), "print mode"),
            "no tty": ({}, adopt_tree(tty="??"), "no terminal"),
            "background": ({}, adopt_tree(agent_stat="S"), "not the foreground"),
            "teammate": ({}, adopt_tree(agent_comm=VERSIONED, agent_args=TEAMMATE_ARGS), "agent-team teammate"),
            "CLAUDE_PID of another process": (dict(CLAUDE_PID="999"), adopt_tree(), "CLAUDE_PID"),
        }
        for name, (env, procs, expected_log) in cases.items():
            with self.subTest(name):
                self.assertIsNone(self.adopt(procs=procs, **env))
                self.assertIsNone(store.load("fake-p1"))
                self.assertIn(expected_log, self.log_text().splitlines()[-1])
                self.assertIn("claude adopt", self.log_text().splitlines()[-1])

    def test_adopt_with_an_invalid_pane_key_records_nothing(self):
        self.transcript()
        self.assertIsNone(self.agent.adopt(adopt_ctx(adopt_tree(), pane_key="../escape",
                                                     CLAUDE_CONFIG_DIR=self.config_dir)))
        self.assertIn("pane key", self.log_text())

    def test_adopt_reports_a_failed_ps_walk_as_none(self):
        self.transcript()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertIsNone(self.adopt(procs=ExplodingProcs()))
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        self.assertIn("ps exploded", self.log_text())


if __name__ == "__main__":
    unittest.main()
