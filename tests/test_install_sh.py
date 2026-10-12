"""install.sh, run with macOS's bash 3.2 when present, a temporary HOME and the fakes first on PATH."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO / "install.sh"
FAKES = REPO / "tests" / "fakes"
BASH = "/bin/bash" if Path("/bin/bash").exists() else (shutil.which("bash") or "bash")
SYSTEM_DIRS = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
DONE = "Installed. Open a new terminal tab to activate. Uninstall any time with: agent-resurrect uninstall"

STUB_CLI = '''"""Stand-in for bin/agent-resurrect.py: records how the launcher ran it."""
import json, os, sys
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "isolated": sys.flags.isolated}) + "\\n")
'''


class InstallShTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ar-install-sh-")).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.tools = self.tmp / "tools"  # per-test stand-ins for system tools (uname, xcode-select, ...)
        self.tools.mkdir()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.iterm = self.tmp / "iTerm.app"
        self.iterm.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tool(self, name: str, script: str) -> None:
        path = self.tools / name
        path.write_text(script)
        path.chmod(0o755)

    def env(self, path_dirs=None) -> dict:
        dirs = [str(self.tools), str(FAKES)] + (SYSTEM_DIRS if path_dirs is None else path_dirs)
        return {
            "HOME": str(self.home),
            "PATH": os.pathsep.join(dirs),
            "LANG": "C",
            "TMPDIR": str(self.tmp),
            "AGENT_RESURRECT_ITERM_APP": str(self.iterm),
            "FAKE_DEFAULTS_DB": str(self.state / "defaults.json"),
            "FAKE_CLAUDE_DB": str(self.state / "claude.json"),
            "FAKE_CALLS_LOG": str(self.state / "calls.jsonl"),
            "STUB_LOG": str(self.state / "stub.jsonl"),
        }

    def run_sh(self, script: Path = INSTALL_SH, env=None) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(script)], env=env or self.env(), cwd=str(self.home),
                              capture_output=True, text=True, timeout=180)

    def make_clone(self) -> Path:
        """A copy of this checkout's installable parts, with a stub CLI in place of bin/agent-resurrect.py."""
        clone = self.tmp / "clone"
        clone.mkdir()
        shutil.copy2(INSTALL_SH, clone / "install.sh")
        shutil.copytree(REPO / "src", clone / "src", ignore=shutil.ignore_patterns("__pycache__"))
        (clone / "src" / "agent_resurrect" / "__pycache__").mkdir(exist_ok=True)  # must not be installed
        (clone / "bin").mkdir()
        (clone / "bin" / "agent-resurrect.py").write_text(STUB_CLI)
        (clone / "README.md").write_text("readme\n")
        return clone

    def stub_calls(self):
        path = self.state / "stub.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    def test_install_sh_rejects_non_macos(self):
        self.tool("uname", "#!/bin/sh\necho Linux\n")
        result = self.run_sh()
        self.assertEqual(result.returncode, 1)
        self.assertIn("macOS", result.stderr)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_install_sh_refuses_root(self):
        self.tool("id", '#!/bin/sh\n[ "$1" = "-u" ] && echo 0 && exit 0\nexec /usr/bin/id "$@"\n')
        result = self.run_sh()
        self.assertEqual(result.returncode, 1)
        self.assertIn("root", result.stderr)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_truncated_script_runs_nothing(self):
        lines = INSTALL_SH.read_text().splitlines(keepends=True)
        self.assertEqual(lines[-1].strip(), 'main "$@"')
        truncated = self.tmp / "truncated.sh"
        truncated.write_text("".join(lines[:-1]))
        result = self.run_sh(truncated)
        self.assert_ok(result)
        self.assertEqual(result.stdout + result.stderr, "")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_install_sh_with_stub_cli(self):
        clone = self.make_clone()
        bin_dir = self.home / ".local" / "bin"
        data = self.home / ".local" / "share" / "agent-resurrect"
        for _ in range(2):  # a second run upgrades in place
            result = self.run_sh(clone / "install.sh")
            self.assert_ok(result)
            self.assertIn(DONE, result.stdout)
        launcher = bin_dir / "agent-resurrect"
        self.assertTrue(os.access(launcher, os.X_OK))
        text = launcher.read_text()
        self.assertTrue(text.startswith("#!/bin/sh\nexec "), text)
        self.assertIn(f" -I '{data}/bin/agent-resurrect.py' \"$@\"\n", text)
        self.assertEqual(os.readlink(bin_dir / "resurrect"), "agent-resurrect")
        self.assertTrue((data / "src" / "agent_resurrect" / "__init__.py").exists())
        self.assertTrue((data / "install.sh").exists())
        self.assertTrue((data / "README.md").exists())
        self.assertEqual(list(data.rglob("__pycache__")), [])
        self.assertFalse((data.parent / "agent-resurrect.new").exists())
        calls = self.stub_calls()
        self.assertEqual([c["argv"] for c in calls], [["install", "--yes"], ["doctor"]] * 2)
        self.assertTrue(all(c["isolated"] for c in calls))

    def test_install_sh_keeps_a_resurrect_that_is_not_ours(self):
        clone = self.make_clone()
        bin_dir = self.home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "resurrect").write_text("someone else's tool\n")
        result = self.run_sh(clone / "install.sh")
        self.assert_ok(result)
        self.assertEqual((bin_dir / "resurrect").read_text(), "someone else's tool\n")
        self.assertIn("resurrect", result.stdout)

    def test_install_sh_reports_failed_install_and_still_runs_doctor(self):
        clone = self.make_clone()
        (clone / "bin" / "agent-resurrect.py").write_text(
            STUB_CLI + 'sys.exit(1 if sys.argv[1:2] == ["install"] else 0)\n')
        result = self.run_sh(clone / "install.sh")
        self.assertEqual(result.returncode, 1)
        self.assertEqual([c["argv"] for c in self.stub_calls()], [["install", "--yes"], ["doctor"]])
        self.assertNotIn(DONE, result.stdout)

    def test_install_sh_without_command_line_tools_uses_python3_from_path(self):
        self.tool("xcode-select", "#!/bin/sh\nexit 2\n")
        (self.tools / "python3").symlink_to(sys.executable)
        clone = self.make_clone()
        result = self.run_sh(clone / "install.sh")
        self.assert_ok(result)
        text = (self.home / ".local" / "bin" / "agent-resurrect").read_text()
        self.assertIn(f"exec '{self.tools / 'python3'}' -I", text)
        self.assertNotIn("/usr/bin/python3", text)
        self.assertEqual(len(self.stub_calls()), 2)

    def test_install_sh_needs_python_39(self):
        self.tool("xcode-select", "#!/bin/sh\nexit 2\n")
        self.tool("python3", "#!/bin/sh\nexit 1\n")  # too old: the version check fails
        clone = self.make_clone()
        result = self.run_sh(clone / "install.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Python 3.9+ is required. Install Apple's command line tools with: xcode-select --install",
                      result.stderr)
        self.assertFalse((self.home / ".local").exists())

    def test_install_sh_piped_downloads_the_pinned_release(self):
        """`curl ... | bash`: no script directory, so it downloads and unpacks the release tarball."""
        clone = self.make_clone()
        package = self.tmp / "package"
        shutil.copytree(clone, package / "agent-resurrect-0.1.0")
        tarball = self.tmp / "release.tar.gz"
        subprocess.run(["tar", "-czf", str(tarball), "-C", str(package), "agent-resurrect-0.1.0"], check=True)
        urls = self.state / "curl-urls"
        self.tool("curl", f'#!/bin/sh\nfor url; do :; done\necho "$url" >> "{urls}"\ncat "{tarball}"\n')

        piped = subprocess.run([BASH], input=INSTALL_SH.read_text(), env=self.env(), cwd=str(self.home),
                               capture_output=True, text=True, timeout=180)
        self.assert_ok(piped)
        self.assertIn(DONE, piped.stdout)
        self.assertTrue((self.home / ".local/share/agent-resurrect/src/agent_resurrect/__init__.py").exists())
        self.assertEqual([c["argv"] for c in self.stub_calls()], [["install", "--yes"], ["doctor"]])
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.startswith("agent-resurrect.")], [])

        env = self.env()
        env["AGENT_RESURRECT_REF"] = "v9.9.9"
        self.assert_ok(subprocess.run([BASH], input=INSTALL_SH.read_text(), env=env, cwd=str(self.home),
                                      capture_output=True, text=True, timeout=180))
        self.assertEqual(urls.read_text().splitlines(),
                         ["https://codeload.github.com/imgovind/agent-resurrect/tar.gz/v0.1.0",
                          "https://codeload.github.com/imgovind/agent-resurrect/tar.gz/v9.9.9"])

        self.tool("curl", "#!/bin/sh\necho 'curl: (22) The requested URL returned error: 404' >&2\nexit 22\n")
        failed = subprocess.run([BASH], input=INSTALL_SH.read_text(), env=self.env(), cwd=str(self.home),
                                capture_output=True, text=True, timeout=180)
        self.assertEqual(failed.returncode, 1)
        self.assertIn("could not download", failed.stderr)
        self.assertTrue((self.home / ".local/share/agent-resurrect/src").exists())  # the old install is untouched

    @unittest.skipUnless((REPO / "bin" / "agent-resurrect.py").exists(),
                         "bin/agent-resurrect.py comes from Task E; the integrator un-skips this")
    def test_install_sh_from_clone(self):
        result = self.run_sh()
        self.assert_ok(result)
        launcher = self.home / ".local" / "bin" / "agent-resurrect"
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertTrue((self.home / ".local" / "share" / "agent-resurrect" / "src" / "agent_resurrect").is_dir())
        self.assertTrue((self.home / ".local" / "state" / "agent-resurrect" / "manifest.json").exists())

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
    def test_install_sh_shellcheck(self):
        result = subprocess.run(["shellcheck", str(INSTALL_SH)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
