"""The repository root is the Claude Code plugin, and it carries its own code.

Claude Code facts this layout relies on (https://code.claude.com/docs/en/plugins-reference):
- Files in the plugin's bin/ are on the Bash tool's PATH while the plugin is enabled.
- ${CLAUDE_PLUGIN_ROOT} is substituted in hook commands, in skill bodies and in a skill's allowed-tools Bash
  rules. It is not exported to commands Claude runs with the Bash tool.
- A copied plugin cannot read files above its root, so src/ and libexec/ must be inside it.
- Timeouts on plugin-provided SessionEnd hooks do not raise the 1.5 s SessionEnd budget
  (https://code.claude.com/docs/en/hooks).
`claude plugin validate . --strict` checks the same files; run it by hand (see AGENTS.md).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_resurrect import __version__

REPO = Path(__file__).resolve().parent.parent
WRAPPER = REPO / "bin" / "agent-resurrect"
ENTRY = REPO / "libexec" / "agent-resurrect.py"
SKILLS = REPO / "skills"
SYSTEM_DIRS = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
EVENTS = ("SessionStart", "SessionEnd")
NO_PYTHON_MESSAGE = ("agent-resurrect: Python 3.9 or newer is required. "
                     "Install Apple's command line tools with: xcode-select --install")

STUB_ENTRY = '''"""Stand-in for libexec/agent-resurrect.py: records how the wrapper ran it."""
import json, os, sys
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "isolated": sys.flags.isolated, "executable": sys.executable,
                         "pycache_prefix": sys.pycache_prefix, "no_bytecode": sys.dont_write_bytecode,
                         "stdin": sys.stdin.read()}) + "\\n")
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
'''


def hook_command(event: str) -> str:
    return f'"${{CLAUDE_PLUGIN_ROOT}}/bin/agent-resurrect" hook claude {event} >/dev/null 2>&1 || true'


def frontmatter(path: Path):
    """(fields, body) of a SKILL.md whose frontmatter is one `key: value` line per field."""
    text = path.read_text()
    if not text.startswith("---\n"):
        raise AssertionError(f"{path} has no frontmatter")
    end = text.index("\n---\n", 4)
    fields = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(": ")
        if not sep or not re.fullmatch(r"[a-z_-]+", key):
            raise AssertionError(f"{path}: not a one-line `key: value` field: {line!r}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        elif ": " in value or " #" in value:
            raise AssertionError(f"{path}: {key} needs quotes to be valid YAML: {value!r}")
        fields[key] = value
    return fields, text[end + len("\n---\n"):]


def load_json(*parts: str):
    return json.loads(REPO.joinpath(*parts).read_text())


class LayoutTest(unittest.TestCase):
    def test_the_plugin_is_the_repository_root(self):
        for name in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", "hooks/hooks.json",
                     "skills/resume-session/SKILL.md", "skills/setup/SKILL.md", "bin/agent-resurrect",
                     "libexec/agent-resurrect.py", "install.sh"):
            self.assertTrue((REPO / name).is_file(), name)
        for gone in ("plugin", "bin/agent-resurrect.py", "skills/resume-by-text"):
            self.assertFalse((REPO / gone).exists(), gone)

    def test_bin_holds_only_the_wrapper(self):
        # Everything in bin/ becomes a command on the Bash tool's PATH.
        self.assertEqual(sorted(p.name for p in (REPO / "bin").iterdir()), ["agent-resurrect"])
        self.assertTrue(os.access(WRAPPER, os.X_OK))

    def test_entry_point_finds_src_beside_libexec(self):
        text = ENTRY.read_text()
        self.assertIn('Path(__file__).resolve().parent.parent / "src"', text)
        self.assertLess(text.index('if __name__ == "__main__":'), text.index("from agent_resurrect.cli import main"))
        self.assertTrue((ENTRY.parent.parent / "src" / "agent_resurrect" / "cli.py").is_file())

    def test_plugin_manifest(self):
        plugin = load_json(".claude-plugin", "plugin.json")
        self.assertEqual(plugin["name"], "agent-resurrect")
        self.assertEqual(plugin["version"], __version__)
        for key in ("description", "author", "homepage", "license"):
            self.assertTrue(plugin.get(key), key)
        # Components are found in their default places; a path key would only add a way to get them wrong.
        for key in ("hooks", "skills", "commands", "agents"):
            self.assertNotIn(key, plugin)

    def test_marketplace_serves_the_repository_root(self):
        marketplace = load_json(".claude-plugin", "marketplace.json")
        self.assertEqual(marketplace["name"], "agent-resurrect")  # the installer and doctor look for this name
        (entry,) = marketplace["plugins"]
        self.assertEqual(entry["name"], "agent-resurrect")
        self.assertEqual(entry["source"], "./")
        # plugin.json wins over an entry version; validate --strict fails if they differ.
        self.assertEqual(entry.get("version"), load_json(".claude-plugin", "plugin.json")["version"])
        for key in ("hooks", "skills", "commands", "agents", "strict"):
            self.assertNotIn(key, entry)


class HooksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ar-hooks-")).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hooks_run_the_plugins_own_wrapper(self):
        hooks = load_json("hooks", "hooks.json")["hooks"]
        self.assertEqual(sorted(hooks), sorted(EVENTS))
        for event in EVENTS:
            (group,) = hooks[event]
            self.assertNotIn("matcher", group)  # every source: startup, resume, clear and compact
            (hook,) = group["hooks"]
            self.assertEqual(hook["type"], "command")
            self.assertEqual(hook["command"], hook_command(event))
            self.assertNotIn("args", hook)
            self.assertNotIn("async", hook)
        self.assertEqual(hooks["SessionStart"][0]["hooks"][0].get("timeout"), 10)
        # A plugin hook's timeout does not extend the 1.5 s SessionEnd budget, so setting one only misleads.
        self.assertNotIn("timeout", hooks["SessionEnd"][0]["hooks"][0])

    def plugin_copy(self, name: str) -> Path:
        """A copy of what the plugin cache holds: wrapper, entry point and code, at a root named `name`."""
        root = self.tmp / name
        root.mkdir()
        for item in ("bin", "libexec", "src"):
            shutil.copytree(REPO / item, root / item, ignore=shutil.ignore_patterns("__pycache__"))
        return root

    def run_hook(self, event: str, root: Path, path_dirs, stdin: bytes) -> subprocess.CompletedProcess:
        """Run a hook as Claude Code does: substitute ${CLAUDE_PLUGIN_ROOT}, then `sh -c` the command."""
        command = hook_command(event).replace("${CLAUDE_PLUGIN_ROOT}", str(root))
        env = {"HOME": str(self.home), "PATH": os.pathsep.join(path_dirs), "LANG": "C",
               "CLAUDE_PLUGIN_ROOT": str(root)}
        return subprocess.run(["/bin/sh", "-c", command], input=stdin, env=env, capture_output=True, timeout=60)

    def test_hooks_are_silent_and_exit_0_without_python(self):
        tools = self.tmp / "tools"
        tools.mkdir()
        (tools / "xcode-select").write_text("#!/bin/sh\nexit 2\n")  # no command line tools, no python3 on PATH
        (tools / "xcode-select").chmod(0o755)
        root = self.plugin_copy("plugin root with spaces")
        for event in EVENTS:
            result = self.run_hook(event, root, [str(tools)] + SYSTEM_DIRS, b'{"session_id": "x"}')
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""), event)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_hooks_are_silent_and_exit_0_on_a_bad_payload(self):
        root = self.plugin_copy("plugin root with spaces")
        for event in EVENTS:
            result = self.run_hook(event, root, os.environ.get("PATH", "").split(os.pathsep), b"{garbage")
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""), event)
        self.assertEqual(list(root.rglob("__pycache__")), [])  # bytecode goes to ~/.cache, never the plugin


class WrapperTest(unittest.TestCase):
    """bin/agent-resurrect, run from a temporary copy with a stub entry point and stand-in tools."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ar-wrapper-")).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.tools = self.tmp / "tools"
        self.tools.mkdir()
        self.log = self.tmp / "stub.jsonl"
        self.root = self.tmp / "plugin root"
        (self.root / "bin").mkdir(parents=True)
        (self.root / "libexec").mkdir()
        shutil.copy2(WRAPPER, self.root / "bin" / "agent-resurrect")
        (self.root / "libexec" / "agent-resurrect.py").write_text(STUB_ENTRY)
        self.wrapper = self.root / "bin" / "agent-resurrect"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tool(self, name: str, script: str) -> None:
        path = self.tools / name
        path.write_text(script)
        path.chmod(0o755)

    def without_command_line_tools(self, python3=True) -> None:
        self.tool("xcode-select", "#!/bin/sh\nexit 2\n")
        if python3:
            (self.tools / "python3").symlink_to(sys.executable)

    def run_wrapper(self, *argv, program=None, stdin=b"", extra_env=None) -> subprocess.CompletedProcess:
        env = {"HOME": str(self.home), "PATH": os.pathsep.join([str(self.tools)] + SYSTEM_DIRS), "LANG": "C",
               "STUB_LOG": str(self.log)}
        env.update(extra_env or {})
        return subprocess.run([str(program or self.wrapper), *argv], input=stdin, env=env, cwd=str(self.tmp),
                              capture_output=True, timeout=60)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_is_posix_sh(self):
        text = WRAPPER.read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertEqual(subprocess.run(["/bin/sh", "-n", str(WRAPPER)], capture_output=True).returncode, 0)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
    def test_shellcheck(self):
        result = subprocess.run(["shellcheck", str(WRAPPER)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_runs_the_entry_point_isolated_with_bytecode_under_home(self):
        self.without_command_line_tools()
        result = self.run_wrapper("ls", "--json")
        self.assert_ok(result)
        (call,) = self.calls()
        self.assertEqual(call["argv"], ["ls", "--json"])
        self.assertEqual(call["isolated"], 1)
        self.assertEqual(call["pycache_prefix"], str(self.home / ".cache" / "agent-resurrect" / "pycache"))
        self.assertEqual(Path(call["executable"]).resolve(), Path(sys.executable).resolve())

    def test_passes_arguments_stdin_and_exit_code_through(self):
        self.without_command_line_tools()
        argv = ["find", "it's $HOME and `date`", "", "two  spaces", "--limit", "3"]
        result = self.run_wrapper(*argv, stdin=b"pasted text\n", extra_env={"STUB_EXIT": "3"})
        self.assertEqual(result.returncode, 3)
        (call,) = self.calls()
        self.assertEqual(call["argv"], argv)
        self.assertEqual(call["stdin"], "pasted text\n")  # the version probe must not eat stdin

    def test_works_through_symlinks(self):
        self.without_command_line_tools()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "absolute").symlink_to(self.wrapper)
        (elsewhere / "relative").symlink_to(os.path.relpath(self.wrapper, elsewhere))
        (elsewhere / "chained").symlink_to("relative")
        for name in ("absolute", "relative", "chained"):
            self.assert_ok(self.run_wrapper("version", program=elsewhere / name))
        self.assertEqual(len(self.calls()), 3)

    def test_dry_runs_write_no_bytecode_under_home(self):
        self.without_command_line_tools()
        for argv in (["install", "--dry-run", "--json"], ["uninstall", "--dry-run"]):
            self.assert_ok(self.run_wrapper(*argv))
            call = self.calls()[-1]
            self.assertEqual(call["argv"], argv)
            self.assertEqual(call["isolated"], 1)
            self.assertTrue(call["no_bytecode"], argv)
            self.assertIsNone(call["pycache_prefix"], argv)
        self.assertFalse((self.home / ".cache").exists())

    def test_without_home_runs_without_a_bytecode_cache(self):
        self.without_command_line_tools()
        env = {"PATH": os.pathsep.join([str(self.tools)] + SYSTEM_DIRS), "STUB_LOG": str(self.log)}
        result = subprocess.run([str(self.wrapper), "version"], env=env, capture_output=True, timeout=60)
        self.assert_ok(result)
        (call,) = self.calls()
        self.assertIsNone(call["pycache_prefix"])

    @unittest.skipUnless(subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True).returncode == 0
                         and Path("/usr/bin/python3").exists(), "Apple's command line tools are not installed")
    def test_prefers_the_system_python_when_the_command_line_tools_are_installed(self):
        version = subprocess.run(["/usr/bin/python3", "-c", "import sys; print(sys.version_info >= (3, 9))"],
                                 capture_output=True, text=True).stdout.strip()
        if version != "True":
            self.skipTest("/usr/bin/python3 is older than 3.9")
        self.tool("xcode-select", "#!/bin/sh\nexit 0\n")
        self.tool("python3", "#!/bin/sh\necho 'python3 from PATH must not run' >&2\nexit 9\n")
        self.assert_ok(self.run_wrapper("version"))
        (call,) = self.calls()
        system = subprocess.run(["/usr/bin/python3", "-I", "-c", "import sys; print(sys.executable)"],
                                capture_output=True, text=True).stdout.strip()
        self.assertEqual(call["executable"], system)

    def test_never_runs_the_system_python_stub_without_the_command_line_tools(self):
        # /usr/bin/python3 opens an install dialog when the tools are missing; with nothing else on PATH,
        # the wrapper must give up instead of trying it.
        self.without_command_line_tools(python3=False)
        result = self.run_wrapper("ls")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr.decode().strip(), NO_PYTHON_MESSAGE)
        self.assertEqual(self.calls(), [])

    def test_rejects_a_python_older_than_3_9(self):
        self.without_command_line_tools(python3=False)
        self.tool("python3", "#!/bin/sh\nexit 1\n")  # what the version probe sees from Python 3.8
        result = self.run_wrapper("ls")
        self.assertEqual((result.returncode, result.stdout), (1, b""))
        self.assertIn(NO_PYTHON_MESSAGE, result.stderr.decode())
        self.assertEqual(self.calls(), [])

    def test_hook_without_python_prints_nothing_and_exits_0(self):
        self.without_command_line_tools(python3=False)
        result = self.run_wrapper("hook", "claude", "SessionStart", stdin=b"{}")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_hook_output_is_discarded(self):
        self.without_command_line_tools()
        (self.root / "libexec" / "agent-resurrect.py").write_text(
            STUB_ENTRY.replace("sys.exit(", "print('noise'); print('more', file=sys.stderr); sys.exit("))
        result = self.run_wrapper("hook", "claude", "SessionEnd", stdin=b"{}")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        self.assertEqual(self.calls()[0]["argv"], ["hook", "claude", "SessionEnd"])


class RealEntryPointTest(unittest.TestCase):
    """The checked-in wrapper running the real code, with an empty HOME."""

    def run_real(self, *argv, stdin=b""):
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C"}
            return subprocess.run([str(WRAPPER), *argv], input=stdin, env=env, capture_output=True, timeout=60)

    def test_version(self):
        result = self.run_real("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode().strip(), f"agent-resurrect {__version__}")

    def test_hook_is_silent(self):
        result = self.run_real("hook", "claude", "SessionStart", stdin=b"{garbage")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))


class SkillsTest(unittest.TestCase):
    TOOL = "${CLAUDE_PLUGIN_ROOT}/bin/agent-resurrect"

    def test_only_the_two_skills_ship(self):
        self.assertEqual(sorted(p.name for p in SKILLS.iterdir()), ["resume-session", "setup"])

    def test_resume_session_triggers_on_lost_sessions(self):
        fields, body = frontmatter(SKILLS / "resume-session" / "SKILL.md")
        self.assertEqual(fields["name"], "resume-session")
        self.assertNotIn("disable-model-invocation", fields)  # Claude should offer it when a session is lost
        listing = fields["description"] + " " + fields["when_to_use"]
        self.assertLessEqual(len(listing), 1536)  # Claude Code truncates the combined text there
        for phrase in ("restart", "lost my Claude session", "resume where I left off", "pastes text"):
            self.assertIn(phrase, listing)
        self.assertIn("Not for", fields["when_to_use"])
        self.assertTrue(fields["argument-hint"])
        # Only read-only commands are pre-approved.
        self.assertEqual(fields["allowed-tools"], f"Bash({self.TOOL} ls *), Bash({self.TOOL} find *)")

    def test_resume_session_prints_commands_and_never_launches(self):
        _, body = frontmatter(SKILLS / "resume-session" / "SKILL.md")
        self.assertIn(f"{self.TOOL} ls --json", body)
        self.assertIn(f"{self.TOOL} find --json <<'PASTED_TEXT_END'\n", body)
        self.assertIn("**Never run that command yourself.**", body)
        # Each form that launches an agent is forbidden by name.
        self.assertIn("Never run `agent-resurrect` with no arguments, `agent-resurrect here` or "
                      "`agent-resurrect resume` either", body)
        self.assertNotIn(f"{self.TOOL} resume", body)
        self.assertNotIn(f"{self.TOOL} here", body)
        for field in ("resume.command", "confidence", "session_id", "in_conversation"):
            self.assertIn(field, body)

    def test_setup_runs_only_when_the_user_asks(self):
        fields, body = frontmatter(SKILLS / "setup" / "SKILL.md")
        self.assertEqual(fields["name"], "setup")
        self.assertEqual(fields["disable-model-invocation"], "true")
        # The installer itself must still ask for permission: only the read-only steps are pre-approved.
        self.assertEqual(fields["allowed-tools"],
                         f"Bash({self.TOOL} install --dry-run *), Bash({self.TOOL} doctor *)")

    def test_setup_installs_from_the_plugin_without_downloading(self):
        _, body = frontmatter(SKILLS / "setup" / "SKILL.md")
        steps = [f"{self.TOOL} install --dry-run --json", "bash ${CLAUDE_PLUGIN_ROOT}/install.sh",
                 f"{self.TOOL} doctor --json", f"{self.TOOL} adopt --json"]
        positions = [body.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        self.assertNotRegex(body, r"curl|wget|https?://\S+install\.sh")
        self.assertIn("agent_may_run", body)
        self.assertIn("stop until they answer", body)
        for line in body.splitlines():
            if "sudo" in line:
                self.assertRegex(line, r"[Nn]ever use sudo|no sudo")


if __name__ == "__main__":
    unittest.main()
