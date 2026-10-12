"""The pane-scrollback fallback: `find --from-pane`, and `agent-resurrect` when nothing is recorded for the pane.

The pane's text is always faked here: no real terminal is ever read."""
import json
import subprocess
import unittest
from unittest import mock

from agent_resurrect import cli, exitcodes, paths
from agent_resurrect.terminals.iterm import PANE_TEXT_SCRIPT, ITermAdapter
from tests.helpers import FakeAgent, FakeTerminal, make_record, proc, temp_home
from tests.test_cli import CliCase, candidate, put

OK, ERROR, NOT_FOUND, BUSY = exitcodes.OK, exitcodes.ERROR, exitcodes.NOT_FOUND, exitcodes.BUSY
GUID = "10FE02F5-C525-405E-9E86-C8255ED17032"
ITERM_ENV = {"ITERM_SESSION_ID": f"w0t12p0:{GUID}", "TERM_PROGRAM": "iTerm.app"}
SECRET = "zebra-quantum-marmalade-7f3a"  # pane text that must never leave the search
SCROLLBACK = "\n".join([
    "Claude: Noted, the word to remember is pineapple.",
    f"some detail {SECRET}",
    "govind in ~ $ agent-resurrect",
    cli.NOTHING_RECORDED,
    "Search this pane's scrollback for the session that ran here? [Y/n] y",
])


class ITermPaneTextTest(unittest.TestCase):
    def read(self, result=None, raises=None, env=ITERM_ENV, max_lines=2000):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            if raises is not None:
                raise raises
            return result

        with temp_home(), mock.patch("agent_resurrect.terminals.iterm.subprocess.run", side_effect=fake_run):
            text = ITermAdapter().pane_text(env, max_lines=max_lines)
            log = paths.log_path().read_text() if paths.log_path().exists() else ""
        return text, calls, log

    def test_reads_this_pane_by_guid_passed_as_an_argument(self):
        out = "".join(f"line {i}\n" for i in range(10))
        text, calls, _ = self.read(subprocess.CompletedProcess([], 0, stdout=out, stderr=""), max_lines=3)
        self.assertEqual(text, "line 7\nline 8\nline 9")
        argv, kwargs = calls[0]
        self.assertEqual(argv, ["osascript", "-", GUID])
        self.assertEqual(kwargs["input"], PANE_TEXT_SCRIPT)
        self.assertNotIn(GUID, kwargs["input"])  # the GUID is never spliced into the script source
        self.assertTrue(kwargs.get("timeout"))

    def test_outside_a_pane_nothing_is_run(self):
        for env in ({}, dict(ITERM_ENV, TMUX="/tmp/tmux-501/default,1,0"), dict(ITERM_ENV, TERM_PROGRAM="vscode")):
            text, calls, _ = self.read(env=env)
            self.assertIsNone(text, env)
            self.assertEqual(calls, [], env)

    def test_failures_return_none_and_never_log_the_text(self):
        for result in (subprocess.CompletedProcess([], 1, stdout=SECRET, stderr="execution error"),
                       subprocess.CompletedProcess([], 0, stdout="  \n", stderr="")):
            text, _, log = self.read(result)
            self.assertIsNone(text)
            self.assertNotIn(SECRET, log)
        for exc in (OSError("no osascript"), subprocess.TimeoutExpired(["osascript"], 15)):
            text, _, log = self.read(raises=exc)
            self.assertIsNone(text)
            self.assertIn(type(exc).__name__, log)


class PaneTerminal(FakeTerminal):
    """A FakeTerminal pane whose scrollback is scripted; counts reads."""

    def __init__(self, text):
        self.text = text
        self.reads = 0

    def pane_text(self, env, max_lines=2000):
        self.reads += 1
        return self.text


class FindingAgent(FakeAgent):
    """Finds session "s-found" when the searched text mentions pineapple, and remembers what it searched."""

    def __init__(self, hits=3):
        super().__init__()
        self.hits = hits
        self.searched = []

    def find_by_text(self, text, exclude=None, limit=5):
        self.searched.append(text)
        return [candidate("s-found", "/tmp", hits=self.hits)] if "pineapple" in text else []


class PaneCase(CliCase):
    def use(self, terminal, agent):
        self.agent = agent
        for patch in (mock.patch.object(cli, "detect_terminal", lambda env: terminal),
                      mock.patch.object(cli, "all_agents", lambda: [agent]),
                      mock.patch.object(cli, "get_agent", lambda name: agent if name == agent.name else None)):
            patch.start()
            self.addCleanup(patch.stop)
        return terminal, agent

    def log_text(self):
        return paths.log_path().read_text() if paths.log_path().exists() else ""


class FindFromPaneTest(PaneCase):
    def test_searches_the_scrollback_before_the_command_and_never_launches(self):
        _, agent = self.use(PaneTerminal("Claude: the word is pineapple.\n$ agent-resurrect find --from-pane"),
                            FindingAgent())
        code, out, err = self.run_cli("find", "--from-pane", tty=True)
        self.assertEqual(code, OK, err)
        self.assertIn("BEST MATCH", out)
        self.assertIn("s-found", out)
        self.assertEqual(agent.searched, ["Claude: the word is pineapple."])
        self.assertEqual(self.launched, [])

    def test_json_marks_the_source(self):
        self.use(PaneTerminal("Claude: the word is pineapple."), FindingAgent())
        code, out, _ = self.run_cli("find", "--from-pane", "--json", tty=True)
        doc = json.loads(out)
        self.assertEqual((code, doc["ok"], doc["query"]["source"]), (OK, True, "pane"))
        self.assertEqual(doc["best"]["session"]["session_id"], "s-found")

    def test_json_shows_no_spinner_even_on_a_terminal(self):
        self.use(PaneTerminal("Claude: the word is pineapple."), FindingAgent())
        with mock.patch.object(cli, "_isatty", lambda stream: True):  # stderr looks like a terminal too
            code, out, err = self.run_cli("find", "--from-pane", "--json", tty=True)
        self.assertEqual(code, OK)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out)["query"]["source"], "pane")

    def test_unreadable_or_unsupported_pane_is_an_error(self):
        for terminal in (PaneTerminal(None), FakeTerminal()):
            with self.subTest(terminal=type(terminal).__name__):
                self.use(terminal, FindingAgent())
                code, _, err = self.run_cli("find", "--from-pane", tty=True)
                self.assertEqual(code, ERROR)
                self.assertIn("cannot read this pane's scrollback", err)


    def test_without_a_terminal_it_refuses_and_reads_nothing(self):
        terminal, _ = self.use(PaneTerminal("Claude: the word is pineapple."), FindingAgent())
        for argv in (("find", "--from-pane"), ("find", "--from-pane", "--json")):
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, exitcodes.NEEDS_TTY, argv)
        self.assertEqual(terminal.reads, 0)


class HereFallbackTest(PaneCase):
    def test_without_a_terminal_it_points_to_find_from_pane_and_reads_nothing(self):
        terminal, _ = self.use(PaneTerminal(SCROLLBACK), FindingAgent())
        code, out, _ = self.run_cli()
        self.assertEqual(code, NOT_FOUND)
        self.assertIn("find --from-pane", out)
        self.assertEqual(terminal.reads, 0)

    def test_declining_the_search_reads_nothing(self):
        terminal, _ = self.use(PaneTerminal(SCROLLBACK), FindingAgent())
        code, out, _ = self.run_cli(stdin="n\n", tty=True)
        self.assertEqual((code, terminal.reads, self.launched), (NOT_FOUND, 0, []))

    def test_strong_match_resumes_after_yes(self):
        _, agent = self.use(PaneTerminal(SCROLLBACK), FindingAgent())
        code, out, err = self.run_cli(stdin="y\ny\n", tty=True)
        self.assertEqual(code, OK, err)
        self.assertEqual(len(self.launched), 1)
        self.assertIn("s-found", self.launched[0].argv)
        # The search saw the pane up to (not including) the command that is reading it.
        self.assertEqual(agent.searched[0].splitlines()[-1], f"some detail {SECRET}")

    def test_strong_match_declined_prints_how_to_resume_later(self):
        self.use(PaneTerminal(SCROLLBACK), FindingAgent())
        code, out, _ = self.run_cli(stdin="y\nn\n", tty=True)
        self.assertEqual((code, self.launched), (OK, []))
        self.assertIn("agent-resurrect resume s-found", out)

    def test_weak_match_defaults_to_no(self):
        self.use(PaneTerminal(SCROLLBACK), FindingAgent(hits=1))
        code, out, _ = self.run_cli(stdin="y\n\n", tty=True)
        self.assertEqual((code, self.launched), (OK, []))
        self.assertIn("[y/N]", out)

    def test_a_session_running_in_another_pane_is_refused_not_opened_twice(self):
        self.use(PaneTerminal(SCROLLBACK), FindingAgent())
        put(make_record(pane_key="fake-other", session_id="s-found", agent_pid=777), 5)
        self.procs.table[777] = proc(777, 1, "claude")
        code, _, err = self.run_cli(stdin="y\ny\n", tty=True)
        self.assertEqual((code, self.launched), (BUSY, []))
        self.assertIn("still running", err)

    def test_no_match(self):
        self.use(PaneTerminal("just an ordinary line of shell output with nothing relevant in it"), FindingAgent())
        code, out, _ = self.run_cli(stdin="y\n", tty=True)
        self.assertEqual((code, self.launched), (NOT_FOUND, []))
        self.assertIn(cli.NO_MATCH, out)

    def test_pane_text_is_never_printed_or_logged(self):
        self.use(PaneTerminal(SCROLLBACK), FindingAgent())
        code, out, err = self.run_cli(stdin="y\nn\n", tty=True)
        self.assertEqual(code, OK)
        for where in (out, err, self.log_text()):
            self.assertNotIn(SECRET, where)

    def test_json_here_is_unchanged_and_never_prompts(self):
        terminal, _ = self.use(PaneTerminal(SCROLLBACK), FindingAgent())
        code, out, _ = self.run_cli("--json", tty=True)
        self.assertEqual((code, json.loads(out)["error"]["code"], terminal.reads), (NOT_FOUND, "not-found", 0))


class ProgressTest(unittest.TestCase):
    def run_progress(self, tty, enabled=True):
        from tests.test_cli import FakeStream
        err = FakeStream(tty=tty)
        with mock.patch("sys.stderr", err):
            with cli._Progress("Searching your Claude Code sessions", enabled=enabled):
                import time
                time.sleep(0.25)
        return err.getvalue()

    def test_spins_with_elapsed_time_then_clears_the_line_on_a_terminal(self):
        out = self.run_progress(tty=True)
        self.assertIn("\rSearching your Claude Code sessions ", out)
        self.assertRegex(out, r"\d+\.\ds")
        self.assertTrue(out.endswith("\r\x1b[K"))

    def test_silent_when_stderr_is_not_a_terminal_or_disabled(self):
        self.assertEqual(self.run_progress(tty=False), "")
        self.assertEqual(self.run_progress(tty=True, enabled=False), "")



if __name__ == "__main__":
    unittest.main()
