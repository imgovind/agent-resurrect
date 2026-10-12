"""Tests for the zsh and bash first-prompt snippets, run in real shells.

Every case uses a temporary HOME and XDG_STATE_HOME and a fake AGENT_RESURRECT_BIN that only appends a
line to a marker file, so nothing outside the temporary directory is read or written.
"""
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import List

from agent_resurrect.terminals.iterm import ITermAdapter

REPO = Path(__file__).resolve().parent.parent
ZSH_SNIPPET = REPO / "shell" / "agent-resurrect.zsh"
BASH_SNIPPET = REPO / "shell" / "agent-resurrect.bash"
GUID = "10FE02F5-C525-405E-9E86-C8255ED17032"
SESSION = f"w0t3p1:{GUID}"
HOOK = "_agent_resurrect_first_prompt"
TMUX = "/private/tmp/tmux-501/default,4242,0"

# Shells that inherit an iTerm2 pane's ITERM_SESSION_ID without being that pane's own shell (review C1).
# Each maps to the variables it adds to (or, with None, removes from) a direct iTerm2 pane's environment.
INHERITED = {
    "tmux": {"TERM_PROGRAM": "tmux", "TMUX": TMUX},
    "tmux, TERM_PROGRAM passed through": {"TMUX": TMUX},
    "screen": {"STY": "4242.ttys003.host"},
    "zellij": {"ZELLIJ": "0"},
    "vscode": {"TERM_PROGRAM": "vscode"},
    "no TERM_PROGRAM": {"TERM_PROGRAM": None},
}


def _available(*candidates: str) -> List[str]:
    """Distinct shells among the candidates (absolute paths or names on PATH)."""
    found, seen = [], set()
    for candidate in candidates:
        path = candidate if os.path.isabs(candidate) else shutil.which(candidate)
        if not path or not os.access(path, os.X_OK):
            continue
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            found.append(path)
    return found


ZSHES = _available("/bin/zsh", "zsh")
BASHES = _available("/bin/bash", "bash")  # /bin/bash is 3.2 on macOS


class ShellSnippetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ar-shell-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.state = self.tmp / "state"
        self.marker = self.tmp / "marker"
        self.fake_bin = self.tmp / "fake-agent-resurrect"
        self.fake_bin.write_text(f'#!/bin/sh\necho "ran $*" >> {shlex.quote(str(self.marker))}\n')
        self.fake_bin.chmod(0o755)

    # helpers

    def env(self, iterm=SESSION, **extra):
        """A shell iTerm2 started directly in a pane; `extra` adds variables, and a value of None removes one."""
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "TERM": "dumb", "TERM_PROGRAM": "iTerm.app",
               "XDG_STATE_HOME": str(self.state), "AGENT_RESURRECT_BIN": str(self.fake_bin)}
        if iterm is not None:
            env["ITERM_SESSION_ID"] = iterm
        env.update(extra)
        return {name: value for name, value in env.items() if value is not None}

    def add_record(self, session=SESSION):
        key = ITermAdapter().pane_key({"TERM_PROGRAM": "iTerm.app", "ITERM_SESSION_ID": session})
        panes = self.state / "agent-resurrect" / "panes"
        panes.mkdir(parents=True, exist_ok=True)
        (panes / f"{key}.json").write_text("{}")

    def runs(self):
        return self.marker.read_text().splitlines() if self.marker.exists() else []

    def reset_marker(self):
        if self.marker.exists():
            self.marker.unlink()

    def reset(self):
        """Fresh state for the next subtest: no marker, no records."""
        self.reset_marker()
        shutil.rmtree(self.state, ignore_errors=True)

    def run_shell(self, argv, env):
        result = subprocess.run(argv, env=env, cwd=str(self.tmp), stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
        self.assertNotIn("syntax error", result.stderr)
        self.assertNotIn("command not found", result.stderr)
        return result

    def zsh(self, shell, script, env, interactive=True):
        return self.run_shell([shell, "-f"] + (["-i"] if interactive else []) + ["-c", script], env)

    def bash(self, shell, script, env, interactive=True):
        return self.run_shell([shell, "--norc", "--noprofile"] + (["-i"] if interactive else []) + ["-c", script],
                              env)

    @staticmethod
    def zsh_prompts(n=2, snippet=ZSH_SNIPPET):
        """Source the snippet, then simulate n prompts by running every precmd hook n times."""
        prompt = "for f in ${precmd_functions[@]-}; do $f; done; "
        return f"source {shlex.quote(str(snippet))}; " + prompt * n + 'print -r -- "hooks=${(j:,:)precmd_functions-}"'

    @staticmethod
    def bash_prompts(n=2, before="", snippet=BASH_SNIPPET):
        prompt = 'eval "$PROMPT_COMMAND"; '
        return (f"{before}source {shlex.quote(str(snippet))}; " + prompt * n
                + 'printf "pc=%s\\n" "${PROMPT_COMMAND:-}"')

    def last_line(self, result, prefix):
        lines = [line for line in result.stdout.splitlines() if line.startswith(prefix)]
        self.assertTrue(lines, result.stdout + result.stderr)
        return lines[-1][len(prefix):]

    def require(self, shells, name):
        if not shells:
            self.skipTest(f"{name} is not installed")

    # zsh

    def test_zsh_runs_auto_once_when_a_record_exists(self):
        self.require(ZSHES, "zsh")
        self.add_record()
        for shell in ZSHES:
            with self.subTest(shell=shell):
                self.reset_marker()
                result = self.zsh(shell, self.zsh_prompts(), self.env())
                self.assertEqual(self.runs(), ["ran auto"], result.stderr)
                self.assertNotIn(HOOK, self.last_line(result, "hooks="))

    def test_zsh_spawns_nothing_without_a_record_or_pane(self):
        self.require(ZSHES, "zsh")
        for shell in ZSHES:
            with self.subTest(shell=shell, case="no record"):
                self.reset()
                result = self.zsh(shell, self.zsh_prompts(), self.env())
                self.assertEqual(self.runs(), [])
                self.assertNotIn(HOOK, self.last_line(result, "hooks="))
            with self.subTest(shell=shell, case="no ITERM_SESSION_ID"):
                self.reset()
                self.add_record()
                result = self.zsh(shell, self.zsh_prompts(), self.env(iterm=None))
                self.assertEqual(self.runs(), [])
                self.assertNotIn(HOOK, self.last_line(result, "hooks="))

    def test_zsh_spawns_nothing_inside_a_multiplexer_or_another_program(self):
        self.require(ZSHES, "zsh")
        self.add_record()
        for shell in ZSHES:
            for name, extra in INHERITED.items():
                with self.subTest(shell=shell, case=name):
                    self.reset_marker()
                    result = self.zsh(shell, self.zsh_prompts(), self.env(**extra))
                    self.assertEqual(self.runs(), [], result.stderr)
                    self.assertNotIn(HOOK, self.last_line(result, "hooks="))

    def test_zsh_non_interactive_shell_registers_nothing(self):
        self.require(ZSHES, "zsh")
        self.add_record()
        for shell in ZSHES:
            with self.subTest(shell=shell):
                result = self.zsh(shell, self.zsh_prompts(), self.env(), interactive=False)
                self.assertEqual(self.last_line(result, "hooks="), "")
                self.assertEqual(self.runs(), [])

    def test_zsh_resourcing_does_not_rearm(self):
        self.require(ZSHES, "zsh")
        self.add_record()
        source = f"source {shlex.quote(str(ZSH_SNIPPET))}; "
        prompt = "for f in ${precmd_functions[@]-}; do $f; done; "
        script = source + source + prompt + source + prompt + prompt + 'print -r -- "hooks=${(j:,:)precmd_functions-}"'
        for shell in ZSHES:
            with self.subTest(shell=shell):
                self.reset_marker()
                result = self.zsh(shell, script, self.env())
                self.assertEqual(self.runs(), ["ran auto"], result.stderr)
                self.assertNotIn(HOOK, self.last_line(result, "hooks="))

    def test_zsh_snippet_is_safe_under_nounset(self):
        self.require(ZSHES, "zsh")
        env = self.env(iterm=None)
        del env["XDG_STATE_HOME"]
        for shell in ZSHES:
            with self.subTest(shell=shell):
                result = self.zsh(shell, "setopt nounset; " + self.zsh_prompts(), env)
                self.assertNotIn("parameter not set", result.stderr)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_zsh_syntax(self):
        self.require(ZSHES, "zsh")
        for shell in ZSHES:
            with self.subTest(shell=shell):
                result = subprocess.run([shell, "-n", str(ZSH_SNIPPET)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    # bash

    def test_bash_runs_auto_once_when_a_record_exists(self):
        self.require(BASHES, "bash")
        self.add_record()
        for shell in BASHES:
            with self.subTest(shell=shell):
                self.reset_marker()
                result = self.bash(shell, self.bash_prompts(), self.env())
                self.assertEqual(self.runs(), ["ran auto"], result.stderr)
                self.assertNotIn(HOOK, self.last_line(result, "pc="))

    def test_bash_spawns_nothing_without_a_record_or_pane(self):
        self.require(BASHES, "bash")
        for shell in BASHES:
            with self.subTest(shell=shell, case="no record"):
                self.reset()
                result = self.bash(shell, self.bash_prompts(), self.env())
                self.assertEqual(self.runs(), [])
                self.assertEqual(self.last_line(result, "pc="), "")
            with self.subTest(shell=shell, case="no ITERM_SESSION_ID"):
                self.reset()
                self.add_record()
                result = self.bash(shell, self.bash_prompts(), self.env(iterm=None))
                self.assertEqual(self.runs(), [])
                self.assertEqual(self.last_line(result, "pc="), "")

    def test_bash_spawns_nothing_inside_a_multiplexer_or_another_program(self):
        self.require(BASHES, "bash")
        self.add_record()
        for shell in BASHES:
            for name, extra in INHERITED.items():
                with self.subTest(shell=shell, case=name):
                    self.reset_marker()
                    result = self.bash(shell, self.bash_prompts(), self.env(**extra))
                    self.assertEqual(self.runs(), [], result.stderr)
                    self.assertEqual(self.last_line(result, "pc="), "")

    def test_bash_non_interactive_shell_registers_nothing(self):
        self.require(BASHES, "bash")
        self.add_record()
        for shell in BASHES:
            with self.subTest(shell=shell):
                result = self.bash(shell, self.bash_prompts(), self.env(), interactive=False)
                self.assertEqual(self.last_line(result, "pc="), "")
                self.assertEqual(self.runs(), [])

    def test_bash_keeps_an_existing_prompt_command(self):
        self.require(BASHES, "bash")
        self.add_record()
        for shell in BASHES:
            with self.subTest(shell=shell):
                self.reset_marker()
                result = self.bash(shell, self.bash_prompts(before='PROMPT_COMMAND="echo other"; '), self.env())
                self.assertEqual(self.runs(), ["ran auto"])
                self.assertEqual(result.stdout.splitlines().count("other"), 2, result.stdout)
                self.assertEqual(self.last_line(result, "pc="), "echo other")

    def test_bash_cleans_up_after_double_sourcing_and_later_edits(self):
        """Sourcing twice before the first prompt, or another tool prepending to PROMPT_COMMAND after the
        snippet, must not leave a broken PROMPT_COMMAND or our hook behind."""
        self.require(BASHES, "bash")
        self.add_record()
        source = f"source {shlex.quote(str(BASH_SNIPPET))}; "
        cases = [
            ("double source", 'PROMPT_COMMAND="echo other"; ' + source + source, "echo other"),
            ("prepended later", source + 'PROMPT_COMMAND="echo other; $PROMPT_COMMAND"; ', "echo other; :"),
            ("appended later", 'PROMPT_COMMAND="echo other"; ' + source + 'PROMPT_COMMAND="$PROMPT_COMMAND; echo more"; ',
             "echo other; echo more"),
        ]
        prompt = 'eval "$PROMPT_COMMAND"; '
        for shell in BASHES:
            for name, before, expected in cases:
                with self.subTest(shell=shell, case=name):
                    self.reset_marker()
                    script = before + prompt + prompt + prompt + 'printf "pc=%s\\n" "$PROMPT_COMMAND"'
                    result = self.bash(shell, script, self.env())
                    self.assertEqual(self.runs(), ["ran auto"], result.stderr)
                    self.assertEqual(self.last_line(result, "pc="), expected)
                    self.assertEqual(result.stdout.splitlines().count("other"), 3, result.stdout + result.stderr)

    def test_bash_resourcing_does_not_rearm(self):
        self.require(BASHES, "bash")
        self.add_record()
        source = f"source {shlex.quote(str(BASH_SNIPPET))}; "
        prompt = 'eval "$PROMPT_COMMAND"; '
        script = source + prompt + source + prompt + prompt + 'printf "pc=%s\\n" "${PROMPT_COMMAND:-}"'
        for shell in BASHES:
            with self.subTest(shell=shell):
                self.reset_marker()
                result = self.bash(shell, script, self.env())
                self.assertEqual(self.runs(), ["ran auto"], result.stderr)
                self.assertEqual(self.last_line(result, "pc="), "")

    def test_bash_snippet_is_safe_under_nounset(self):
        self.require(BASHES, "bash")
        env = self.env(iterm=None)
        del env["XDG_STATE_HOME"]
        for shell in BASHES:
            with self.subTest(shell=shell):
                result = self.bash(shell, "set -u; " + self.bash_prompts(), env)
                self.assertNotIn("unbound variable", result.stderr)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_bash_syntax(self):
        self.require(BASHES, "bash")
        for shell in BASHES:
            with self.subTest(shell=shell):
                result = subprocess.run([shell, "-n", str(BASH_SNIPPET)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_bash_shellcheck(self):
        shellcheck = shutil.which("shellcheck")
        if not shellcheck:
            self.skipTest("shellcheck is not installed")
        result = subprocess.run([shellcheck, str(BASH_SNIPPET)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # contract with ITermAdapter.pane_key

    def key_line(self, snippet: Path) -> str:
        lines = [line.strip() for line in snippet.read_text().splitlines() if 'key="iterm-' in line]
        self.assertEqual(len(lines), 1, f"exactly one pane-key line expected in {snippet.name}")
        return lines[0]

    def test_pane_key_contract(self):
        self.require(ZSHES + BASHES, "zsh or bash")
        values = [SESSION, f"w0t0p0:{GUID.lower()}", f"w12t3p4:{GUID}", f"w0t0p0:{GUID}:extra", "w0t0p0",
                  "", ":", "w0t0p0:", "w0t0p0:../../etc", "w0t0p0:not-a-guid", "w0t0p0:*", "a:b:c"]
        runners = [(shell, self.zsh, ZSH_SNIPPET) for shell in ZSHES] + \
                  [(shell, self.bash, BASH_SNIPPET) for shell in BASHES]
        compared = 0
        for shell, run, snippet in runners:
            script = 'key=""; ' + self.key_line(snippet) + '; printf "key=%s\\n" "$key"'
            for value in values:
                expected = ITermAdapter().pane_key(self.env(iterm=value))
                if expected is None:
                    continue
                with self.subTest(shell=shell, value=value):
                    result = run(shell, script, self.env(iterm=value), interactive=False)
                    self.assertEqual(self.last_line(result, "key="), expected)
                    compared += 1
        self.assertGreaterEqual(compared, 3 * len(runners))


    def test_pane_recognition_contract(self):
        """The whole snippet runs `auto` exactly when ITermAdapter recognises the pane: same key, same
        multiplexer guard. A record exists for the GUID in every case, so only recognition decides."""
        self.require(ZSHES + BASHES, "zsh or bash")
        self.add_record()
        combos = {"direct pane": {}, "empty multiplexer variables": {"TMUX": "", "STY": "", "ZELLIJ": ""}}
        combos.update(INHERITED)
        combos.update({"tmux and screen": {"TMUX": TMUX, "STY": "1.ttys001.h"},
                       "zellij inside vscode": {"TERM_PROGRAM": "vscode", "ZELLIJ": "0"}})
        runners = [(shell, self.zsh, self.zsh_prompts()) for shell in ZSHES] + \
                  [(shell, self.bash, self.bash_prompts()) for shell in BASHES]
        for shell, run, script in runners:
            for name, extra in combos.items():
                env = self.env(**extra)
                expected = ["ran auto"] if ITermAdapter().pane_key(env) is not None else []
                with self.subTest(shell=shell, case=name):
                    self.reset_marker()
                    result = run(shell, script, env)
                    self.assertEqual(self.runs(), expected, result.stderr)
        # Both outcomes occur, so the comparison above is not vacuous.
        self.assertIsNotNone(ITermAdapter().pane_key(self.env()))
        self.assertIsNone(ITermAdapter().pane_key(self.env(**INHERITED["tmux"])))


if __name__ == "__main__":
    unittest.main()
