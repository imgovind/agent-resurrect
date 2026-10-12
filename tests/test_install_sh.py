"""install.sh, run with macOS's bash 3.2 when present, a temporary HOME and the fakes first on PATH."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_resurrect import __version__

REPO = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO / "install.sh"
FAKES = REPO / "tests" / "fakes"
BASH = "/bin/bash" if Path("/bin/bash").exists() else (shutil.which("bash") or "bash")
SYSTEM_DIRS = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
DONE = "Installed. Open a new terminal tab to activate. Uninstall any time with: agent-resurrect uninstall"

RELEASE_YML = REPO / ".github" / "workflows" / "release.yml"
# What install.sh copies into ~/.local/share/agent-resurrect: the code, and the plugin that runs from there.
INSTALLED = ["src", "libexec", "bin", "shell", "hooks", "skills", ".claude-plugin", "install.sh", "README.md",
             "LICENSE"]
SHA = "0123456789abcdef0123456789abcdef01234567"

STUB_CLI = '''"""Stand-in for libexec/agent-resurrect.py: records how the launcher ran it."""
import json, os, sys
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "isolated": sys.flags.isolated, "cwd": os.getcwd(),
                         "pycache_prefix": sys.pycache_prefix, "no_bytecode": sys.dont_write_bytecode,
                         "file": os.path.abspath(__file__)}) + "\\n")
'''


def workflow_step(name: str) -> str:
    """The `run: |` script of the release workflow step called `name`, dedented."""
    lines = RELEASE_YML.read_text().splitlines()
    start = lines.index(f"      - name: {name}")
    for i in range(start + 1, len(lines)):
        if lines[i].strip().startswith("- name:"):
            break
        if lines[i].strip() == "run: |":
            indent = len(lines[i]) - len(lines[i].lstrip()) + 2
            script = []
            for line in lines[i + 1:]:
                if line.strip() and len(line) - len(line.lstrip()) < indent:
                    break
                script.append(line[indent:])
            return "\n".join(script).rstrip() + "\n"
    raise AssertionError(f"step {name!r} has no run block")


def tree(root: Path):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


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

    def run_sh(self, script: Path = INSTALL_SH, *args: str, env=None) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(script), *args], env=env or self.env(), cwd=str(self.home),
                              capture_output=True, text=True, timeout=180)

    def pipe_sh(self, script_text: str, *args: str, env=None) -> subprocess.CompletedProcess:
        """`curl ... | bash -s -- args`: the script arrives on stdin, so it has no directory of its own."""
        return subprocess.run([BASH, "-s", "--", *args], input=script_text, env=env or self.env(),
                              cwd=str(self.home), capture_output=True, text=True, timeout=180)

    def make_clone(self) -> Path:
        """A copy of this checkout, with a stub CLI in place of libexec/agent-resurrect.py."""
        clone = self.tmp / "clone"
        clone.mkdir()
        shutil.copy2(INSTALL_SH, clone / "install.sh")
        for item in ("src", "bin", "shell", "hooks", "skills", ".claude-plugin", "tests", "docs"):
            shutil.copytree(REPO / item, clone / item, ignore=shutil.ignore_patterns("__pycache__"))
        (clone / "src" / "agent_resurrect" / "__pycache__").mkdir(exist_ok=True)  # must not be installed
        (clone / "libexec").mkdir()
        (clone / "libexec" / "agent-resurrect.py").write_text(STUB_CLI)
        (clone / "README.md").write_text("readme\n")
        shutil.copy2(REPO / "LICENSE", clone / "LICENSE")
        return clone

    def fake_download(self, clone: Path) -> Path:
        """A fake curl that serves `clone` as the release tarball and logs each URL it was asked for."""
        package = self.tmp / "package"
        shutil.copytree(clone, package / f"agent-resurrect-{__version__}", symlinks=True)
        tarball = self.tmp / "release.tar.gz"
        subprocess.run(["tar", "-czf", str(tarball), "-C", str(package), f"agent-resurrect-{__version__}"], check=True)
        urls = self.state / "curl-urls"
        self.tool("curl", f'#!/bin/sh\nfor url; do :; done\necho "$url" >> "{urls}"\ncat "{tarball}"\n')
        return urls

    def leftovers(self):
        """Temporary download and staging directories install.sh left in TMPDIR."""
        return [p.name for p in self.tmp.iterdir() if p.name.startswith("agent-resurrect.")]

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
        lines = text.splitlines()
        self.assertEqual(len(lines), 3, text)
        self.assertEqual(lines[0], "#!/bin/sh")
        self.assertTrue(lines[1].startswith('for arg in "$@"; do [ "$arg" = --dry-run ] && exec '), text)
        self.assertTrue(lines[1].endswith(f" -I -B '{data}/libexec/agent-resurrect.py' \"$@\"; done"), text)
        self.assertTrue(lines[2].startswith("exec "), text)
        pycache = self.home / ".cache" / "agent-resurrect" / "pycache"
        self.assertIn(f" -I -X 'pycache_prefix={pycache}' '{data}/libexec/agent-resurrect.py' \"$@\"\n", text)
        self.assertEqual(os.readlink(bin_dir / "resurrect"), "agent-resurrect")
        self.assertEqual(sorted(p.name for p in data.iterdir()), sorted(INSTALLED))
        self.assertTrue((data / "src" / "agent_resurrect" / "__init__.py").exists())
        # The data dir is the plugin root of the local marketplace: its hooks run the wrapper in bin/.
        self.assertTrue((data / ".claude-plugin" / "plugin.json").exists())
        self.assertTrue((data / "hooks" / "hooks.json").exists())
        self.assertTrue((data / "skills" / "setup" / "SKILL.md").exists())
        self.assertTrue(os.access(data / "bin" / "agent-resurrect", os.X_OK))
        self.assertEqual(list(data.rglob("__pycache__")), [])
        self.assertFalse((data.parent / "agent-resurrect.new").exists())
        calls = self.stub_calls()
        self.assertEqual([c["argv"] for c in calls], [["install", "--yes"], ["doctor"]] * 2)
        self.assertTrue(all(c["isolated"] for c in calls))
        # Bytecode, the stdlib's included, is cached outside the read-only system Python.
        self.assertEqual({c["pycache_prefix"] for c in calls}, {str(pycache)})

    def test_launcher_dry_runs_write_no_bytecode(self):
        """`agent-resurrect install --dry-run` and `uninstall --dry-run` write nothing under HOME, so the launcher
        runs them with -B and without the bytecode cache under ~/.cache."""
        self.assert_ok(self.run_sh(self.make_clone() / "install.sh"))
        launcher = self.home / ".local" / "bin" / "agent-resurrect"
        before = tree(self.home)
        for argv in (["install", "--dry-run"], ["uninstall", "--dry-run", "--json"], ["--dry-run"]):
            result = subprocess.run([str(launcher), *argv], env=self.env(), capture_output=True, text=True,
                                    timeout=60)
            self.assert_ok(result)
            call = self.stub_calls()[-1]
            self.assertEqual(call["argv"], argv)
            self.assertTrue(call["isolated"])
            self.assertTrue(call["no_bytecode"], argv)
            self.assertIsNone(call["pycache_prefix"], argv)
        self.assertEqual(tree(self.home), before)
        subprocess.run([str(launcher), "find", "--dry-run-ish"], env=self.env(), check=True, timeout=60)
        self.assertFalse(self.stub_calls()[-1]["no_bytecode"])  # only an exact --dry-run argument

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
        (clone / "libexec" / "agent-resurrect.py").write_text(
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
        urls = self.fake_download(self.make_clone())
        piped = self.pipe_sh(INSTALL_SH.read_text())
        self.assert_ok(piped)
        self.assertIn(DONE, piped.stdout)
        self.assertTrue((self.home / ".local/share/agent-resurrect/src/agent_resurrect/__init__.py").exists())
        self.assertEqual([c["argv"] for c in self.stub_calls()], [["install", "--yes"], ["doctor"]])
        self.assertEqual(self.leftovers(), [])

        env = self.env()
        env["AGENT_RESURRECT_REF"] = "v9.9.9"
        self.assert_ok(self.pipe_sh(INSTALL_SH.read_text(), env=env))
        self.assertEqual(urls.read_text().splitlines(),
                         [f"https://codeload.github.com/imgovind/agent-resurrect/tar.gz/v{__version__}",
                          "https://codeload.github.com/imgovind/agent-resurrect/tar.gz/v9.9.9"])

        self.tool("curl", "#!/bin/sh\necho 'curl: (22) The requested URL returned error: 404' >&2\nexit 22\n")
        failed = self.pipe_sh(INSTALL_SH.read_text())
        self.assertEqual(failed.returncode, 1)
        self.assertIn("could not download", failed.stderr)
        self.assertTrue((self.home / ".local/share/agent-resurrect/src").exists())  # the old install is untouched

    def test_dry_run_from_a_clone_writes_nothing_under_home(self):
        clone = self.make_clone()
        result = self.run_sh(clone / "install.sh", "--dry-run")
        self.assert_ok(result)
        self.assertIn("Dry run", result.stdout)
        self.assertNotIn(DONE, result.stdout)
        self.assertEqual(tree(self.home), [])
        (call,) = self.stub_calls()
        self.assertEqual(call["argv"], ["install", "--dry-run"])
        self.assertTrue(call["isolated"])
        self.assertTrue(call["no_bytecode"])
        # It ran the staged copy, exactly the files an install would copy, which is gone afterwards.
        self.assertFalse(call["file"].startswith(str(clone)))
        self.assertTrue(call["file"].endswith("/libexec/agent-resurrect.py"))
        self.assertEqual(self.leftovers(), [])

    def test_dry_run_piped_downloads_to_a_temporary_directory_only(self):
        urls = self.fake_download(self.make_clone())
        result = self.pipe_sh(INSTALL_SH.read_text(), "--dry-run")
        self.assert_ok(result)
        self.assertEqual(urls.read_text().splitlines(),
                         [f"https://codeload.github.com/imgovind/agent-resurrect/tar.gz/v{__version__}"])
        self.assertEqual([c["argv"] for c in self.stub_calls()], [["install", "--dry-run"]])
        self.assertEqual(tree(self.home), [])
        self.assertEqual(self.leftovers(), [])

    def test_dry_run_reports_failure(self):
        clone = self.make_clone()
        (clone / "libexec" / "agent-resurrect.py").write_text(STUB_CLI + "sys.exit(1)\n")
        result = self.run_sh(clone / "install.sh", "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(tree(self.home), [])

    def test_unknown_option_is_a_usage_error(self):
        result = self.run_sh(INSTALL_SH, "--bogus")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage: install.sh [--dry-run]", result.stderr)
        self.assertEqual(tree(self.home), [])
        help_result = self.run_sh(INSTALL_SH, "--help")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("usage: install.sh [--dry-run]", help_result.stdout)
        self.assertEqual(tree(self.home), [])

    def test_install_sh_from_clone(self):
        result = self.run_sh()
        self.assert_ok(result)
        launcher = self.home / ".local" / "bin" / "agent-resurrect"
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertTrue((self.home / ".local" / "share" / "agent-resurrect" / "src" / "agent_resurrect").is_dir())
        self.assertTrue((self.home / ".local" / "state" / "agent-resurrect" / "manifest.json").exists())

    def test_install_sh_is_pinnable(self):
        lines = INSTALL_SH.read_text().splitlines()
        self.assertEqual(lines.count('AGENT_RESURRECT_COMMIT=""'), 1)  # the release workflow fills this in
        self.assertEqual(len([line for line in lines if line.startswith("DEFAULT_REF=")]), 1)

    # ---------------------------------------------------------------- release.yml, run step by step locally

    def make_release_repo(self) -> Path:
        """A throwaway git repository holding the files the release workflow reads, tagged v<version>."""
        repo = self.tmp / "repo"
        for name in ("install.sh", "CHANGELOG.md", "src/agent_resurrect/__init__.py",
                     ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"):
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / name, repo / name)
        for argv in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "release"], ["tag", f"v{__version__}"]):
            self.git(repo, *argv)
        return repo

    def git(self, repo: Path, *argv: str) -> str:
        env = {"HOME": str(self.home), "PATH": os.pathsep.join(SYSTEM_DIRS), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.devnull}
        return subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                               "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *argv], cwd=str(repo),
                              env=env, capture_output=True, text=True, check=True, timeout=60).stdout.strip()

    def run_step(self, name: str, repo: Path, tag: str) -> subprocess.CompletedProcess:
        """Run one step as GitHub Actions does: `bash --noprofile --norc -eo pipefail` on the run block."""
        script = self.tmp / "step.sh"
        script.write_text(workflow_step(name))
        env = {"HOME": str(self.home), "PATH": os.pathsep.join([str(Path(sys.executable).parent)] + SYSTEM_DIRS),
               "LANG": "C", "GITHUB_REF_NAME": tag, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        return subprocess.run([BASH, "--noprofile", "--norc", "-eo", "pipefail", str(script)], cwd=str(repo),
                              env=env, capture_output=True, text=True, timeout=60)

    def test_release_refuses_a_tag_that_disagrees_with_any_version(self):
        repo = self.make_release_repo()
        self.assert_ok(self.run_step("Every version string matches the tag", repo, f"v{__version__}"))
        wrong_tag = self.run_step("Every version string matches the tag", repo, "v9.9.9")
        self.assertNotEqual(wrong_tag.returncode, 0)
        self.assertIn("9.9.9", wrong_tag.stdout + wrong_tag.stderr)
        plugin = repo / ".claude-plugin" / "plugin.json"
        plugin.write_text(plugin.read_text().replace(f'"version": "{__version__}"', '"version": "0.0.1"'))
        half_bumped = self.run_step("Every version string matches the tag", repo, f"v{__version__}")
        self.assertNotEqual(half_bumped.returncode, 0)
        self.assertIn("plugin.json", half_bumped.stdout + half_bumped.stderr)

    def test_release_notes_come_from_the_changelog_section(self):
        repo = self.tmp / "notes"
        repo.mkdir()
        (repo / "CHANGELOG.md").write_text(
            "# Changelog\n\nIntro.\n\n## [9.9.9] - 2026-11-01\n\n### Added\n\n- New thing.\n\n"
            "## [0.1.0] - 2026-10-12\n\n- Old thing.\n\n"
            "[9.9.9]: https://github.com/imgovind/agent-resurrect/releases/tag/v9.9.9\n"
            "[0.1.0]: https://github.com/imgovind/agent-resurrect/releases/tag/v0.1.0\n")
        self.assert_ok(self.run_step("Release notes from the changelog", repo, "v9.9.9"))
        self.assertEqual((repo / "notes.md").read_text().strip(), "### Added\n\n- New thing.")
        self.assertNotEqual(self.run_step("Release notes from the changelog", repo, "v1.2.3").returncode, 0)

    def test_release_installer_is_pinned_to_the_tagged_commit(self):
        repo = self.make_release_repo()
        sha = self.git(repo, "rev-parse", "HEAD")
        self.assert_ok(self.run_step("Pin the installer to the tagged commit", repo, f"v{__version__}"))
        dist = repo / "dist"
        self.assertEqual(sorted(p.name for p in dist.iterdir()), ["SHA256SUMS", "install.sh"])
        pinned = (dist / "install.sh").read_text()
        self.assertEqual(pinned, INSTALL_SH.read_text().replace('AGENT_RESURRECT_COMMIT=""',
                                                               f'AGENT_RESURRECT_COMMIT="{sha}"'))
        sums = (dist / "SHA256SUMS").read_text()
        self.assertRegex(sums, r"^[0-9a-f]{64}  install\.sh\n$")
        check = subprocess.run(["shasum", "-a", "256", "-c", "SHA256SUMS"], cwd=str(dist), capture_output=True,
                               text=True, timeout=60)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

        urls = self.fake_download(self.make_clone())
        result = self.pipe_sh(pinned)
        self.assert_ok(result)
        self.assertIn(f"commit {sha}", result.stdout)
        env = self.env()
        env["AGENT_RESURRECT_REF"] = "v9.9.9"  # an explicit choice still wins over the pin
        self.assert_ok(self.pipe_sh(pinned, env=env))
        self.assertEqual(urls.read_text().splitlines(),
                         [f"https://codeload.github.com/imgovind/agent-resurrect/tar.gz/{sha}",
                          "https://codeload.github.com/imgovind/agent-resurrect/tar.gz/v9.9.9"])

    def test_commit_pin_comes_only_from_the_script(self):
        urls = self.fake_download(self.make_clone())
        env = self.env()
        env["AGENT_RESURRECT_COMMIT"] = SHA  # ignored: the unpinned script downloads its release tag
        self.assert_ok(self.pipe_sh(INSTALL_SH.read_text(), env=env))
        self.assertEqual(urls.read_text().splitlines(),
                         [f"https://codeload.github.com/imgovind/agent-resurrect/tar.gz/v{__version__}"])

    def test_a_malformed_commit_pin_is_refused(self):
        self.fake_download(self.make_clone())
        bad = INSTALL_SH.read_text().replace('AGENT_RESURRECT_COMMIT=""', 'AGENT_RESURRECT_COMMIT="main"')
        result = self.pipe_sh(bad)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a full commit SHA", result.stderr)
        self.assertEqual(tree(self.home), [])

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
    def test_install_sh_shellcheck(self):
        result = subprocess.run(["shellcheck", str(INSTALL_SH)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
