"""A copy installed by a package manager (Homebrew), which runs it through a wrapper that sets
AGENT_RESURRECT_HOME, AGENT_RESURRECT_BIN and AGENT_RESURRECT_PACKAGE.

The unit tests check each place that names the program's location. PackageLayoutTest builds the layout Homebrew
makes, a versioned Cellar copy, an `opt` symlink that survives upgrades and the wrapper Homebrew's
write_env_script writes, and runs the real program through it with a temporary HOME and the fakes first on PATH."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_resurrect import installer, paths
from agent_resurrect.settings.base import Check, Setting
from agent_resurrect.settings.claude_plugin import _is_local_marketplace
from agent_resurrect.settings.shellrc import ShellRcBlock
from tests.helpers import temp_home

REPO = Path(__file__).resolve().parent.parent
FAKES = REPO / "tests" / "fakes"
# What the formula installs into libexec: the same items install.sh copies.
INSTALLED = ["src", "libexec", "bin", "shell", "hooks", "skills", ".claude-plugin", "install.sh", "README.md",
             "LICENSE"]


def package_env(home: Path, launcher: Path, package: str = "homebrew") -> dict:
    return {"AGENT_RESURRECT_HOME": str(home), "AGENT_RESURRECT_BIN": str(launcher),
            "AGENT_RESURRECT_PACKAGE": package}


class Noop(Setting):
    id = "test.noop"
    description = "does nothing"

    def check(self) -> Check:
        return Check(True, "fine")

    def apply(self):
        return None

    def revert(self, original):
        pass


class LocationTest(unittest.TestCase):
    def test_one_line_installer_locations_are_unchanged(self):
        with temp_home() as home:
            self.assertEqual(paths.program_dir(), home / ".local/share/agent-resurrect")
            self.assertEqual(paths.launcher(), home / ".local/bin/agent-resurrect")
            self.assertIsNone(paths.package_manager())
            self.assertEqual(paths.command(), "~/.local/bin/agent-resurrect")

    def test_package_locations_come_from_the_wrapper(self):
        with temp_home(), mock.patch.dict(os.environ, package_env(Path("/opt/x/libexec"), Path("/opt/x/bin/ar"))):
            self.assertEqual(paths.program_dir(), Path("/opt/x/libexec"))
            self.assertEqual(paths.launcher(), Path("/opt/x/bin/ar"))
            self.assertEqual(paths.package_manager(), "homebrew")
            self.assertEqual(paths.command(), "agent-resurrect")

    def test_agent_resurrect_bin_alone_does_not_make_a_package(self):
        # The shell snippet has long honoured AGENT_RESURRECT_BIN as an override; on its own it changes nothing.
        with temp_home() as home, mock.patch.dict(os.environ, {"AGENT_RESURRECT_BIN": "/elsewhere/ar"}):
            self.assertEqual(paths.launcher(), home / ".local/bin/agent-resurrect")
            self.assertIsNone(paths.package_manager())


class PackageSettingsTest(unittest.TestCase):
    def test_rc_block_runs_the_package_command_and_leaves_path_alone(self):
        with temp_home(), mock.patch.dict(os.environ, package_env(Path("/opt/homebrew/opt/agent-resurrect/libexec"),
                                                                  Path("/opt/homebrew/opt/agent-resurrect/bin/agent-resurrect"))):
            block = ShellRcBlock("zsh").block()
        self.assertIn('AGENT_RESURRECT_BIN="/opt/homebrew/opt/agent-resurrect/bin/agent-resurrect"\n', block)
        self.assertIn('[ -f "/opt/homebrew/opt/agent-resurrect/libexec/shell/agent-resurrect.zsh" ] && '
                      '. "/opt/homebrew/opt/agent-resurrect/libexec/shell/agent-resurrect.zsh"', block)
        self.assertNotIn(".local", block)
        self.assertNotIn("export", block)  # only the snippet reads it

    def test_rc_block_quotes_a_hostile_package_path(self):
        odd = Path('/tmp/a "b" $c `d`/libexec')
        with temp_home(), mock.patch.dict(os.environ, package_env(odd, odd.parent / "bin/agent-resurrect")):
            block = ShellRcBlock("bash").block()
        self.assertIn('AGENT_RESURRECT_BIN="/tmp/a \\"b\\" \\$c \\`d\\`/bin/agent-resurrect"', block)

    def test_plugin_marketplace_is_the_package_tree(self):
        with temp_home(), mock.patch.dict(os.environ, package_env(Path("/opt/p/libexec"), Path("/opt/p/bin/ar"))):
            self.assertTrue(_is_local_marketplace({"name": "agent-resurrect", "source": "directory",
                                                   "path": "/opt/p/libexec"}))
            self.assertFalse(_is_local_marketplace({"name": "agent-resurrect", "source": "directory",
                                                    "path": str(paths.data_dir())}))

    def test_fix_hints_name_the_command_on_path(self):
        with temp_home(), mock.patch.dict(os.environ, package_env(Path("/opt/p/libexec"), Path("/opt/p/bin/ar"))):
            self.assertEqual(ShellRcBlock("zsh").fix_hint, "run: agent-resurrect install")


class OneCopyAtATimeTest(unittest.TestCase):
    def package_tree(self, root: Path) -> Path:
        tree = root / "pkg" / "libexec"
        (tree / "shell").mkdir(parents=True)
        (tree / ".claude-plugin").mkdir()
        return tree

    def test_install_records_the_program_tree_and_command(self):
        with temp_home() as home:
            tree = self.package_tree(home)
            with mock.patch.dict(os.environ, package_env(tree, tree.parent / "bin/agent-resurrect")):
                results = installer.install([Noop()])
            manifest = installer.read_manifest()
        self.assertEqual([r.status for r in results], ["unchanged"])
        self.assertEqual((manifest["home"], manifest["command"]), (str(tree), "agent-resurrect"))

    def test_a_package_install_refuses_while_the_one_line_copy_is_installed(self):
        with temp_home() as home:
            installer.install([Noop()])                        # the one-line installer's copy, as before 0.3
            paths.data_dir().mkdir(parents=True)               # ...whose files are still there
            tree = self.package_tree(home)
            with mock.patch.dict(os.environ, package_env(tree, tree.parent / "bin/agent-resurrect")):
                results = installer.install([Noop()])
            manifest, one_line = installer.read_manifest(), str(paths.data_dir())
        self.assertEqual([(r.id, r.status) for r in results], [("location", "failed")])
        self.assertIn("already installed from the one-line installer", results[0].detail)
        self.assertIn("first run: ~/.local/bin/agent-resurrect uninstall", results[0].detail)
        self.assertEqual(manifest["home"], one_line)  # untouched

    def test_the_one_line_copy_refuses_while_a_package_is_installed(self):
        with temp_home() as home:
            tree = self.package_tree(home)
            with mock.patch.dict(os.environ, package_env(tree, tree.parent / "bin/agent-resurrect")):
                installer.install([Noop()])
            results = installer.install([Noop()])              # now as the one-line installer's copy
        self.assertEqual([(r.id, r.status) for r in results], [("location", "failed")])
        self.assertIn(f"already installed from {tree}", results[0].detail)
        self.assertIn("first run: agent-resurrect uninstall", results[0].detail)

    def test_a_copy_whose_files_are_gone_is_taken_over(self):
        with temp_home() as home:
            tree = self.package_tree(home)
            with mock.patch.dict(os.environ, package_env(tree, tree.parent / "bin/agent-resurrect")):
                installer.install([Noop()])
            shutil.rmtree(tree.parent)                          # e.g. brew uninstall without agent-resurrect uninstall
            results = installer.install([Noop()])
            manifest, one_line = installer.read_manifest(), str(paths.data_dir())
        self.assertEqual([r.status for r in results], ["unchanged"])
        self.assertEqual(manifest["home"], one_line)

    def test_a_package_home_that_is_not_a_program_tree_is_refused(self):
        with temp_home() as home:
            with mock.patch.dict(os.environ, package_env(home / "nowhere", home / "bin/ar")):
                results = installer.install([Noop()])
        self.assertEqual([(r.id, r.status) for r in results], [("location", "failed")])
        self.assertIn("is not an agent-resurrect program directory", results[0].detail)

    def test_uninstall_leaves_the_package_and_says_how_to_remove_it(self):
        with temp_home() as home:
            tree = self.package_tree(home)
            with mock.patch.dict(os.environ, package_env(tree, tree.parent / "bin/agent-resurrect")):
                installer.install([Noop()])
                results = installer.uninstall([Noop()])
            self.assertTrue(tree.is_dir())
        self.assertIn("remove the program itself with: brew uninstall agent-resurrect", results[-1].detail)


class PackageLayoutTest(unittest.TestCase):
    """The real program, laid out and wrapped as Homebrew does it, run end to end in a temporary HOME."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ar-brew-")).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.state = self.tmp / "state"
        self.state.mkdir()
        (self.tmp / "iTerm.app").mkdir()
        self.prefix = self.tmp / "homebrew"
        self.opt = self.prefix / "opt" / "agent-resurrect"
        self.keg("0.3.0")
        self.opt.parent.mkdir(parents=True)
        self.opt.symlink_to(self.prefix / "Cellar" / "agent-resurrect" / "0.3.0")
        (self.prefix / "bin").mkdir()
        (self.prefix / "bin" / "agent-resurrect").symlink_to("../Cellar/agent-resurrect/0.3.0/bin/agent-resurrect")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def keg(self, version: str) -> Path:
        """Cellar/agent-resurrect/<version>: libexec holds the program, bin the wrapper write_env_script makes."""
        keg = self.prefix / "Cellar" / "agent-resurrect" / version
        libexec = keg / "libexec"
        libexec.mkdir(parents=True)
        for item in INSTALLED:
            source = REPO / item
            if source.is_dir():
                shutil.copytree(source, libexec / item, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(source, libexec / item)
        (keg / "bin").mkdir()
        wrapper = keg / "bin" / "agent-resurrect"
        # Homebrew's Pathname#write_env_script: KEY="value" ... exec "target" "$@"
        wrapper.write_text("#!/bin/bash\n"
                           f'AGENT_RESURRECT_HOME="{self.opt}/libexec" AGENT_RESURRECT_BIN="{self.opt}/bin/agent-resurrect" '
                           f'AGENT_RESURRECT_PACKAGE="homebrew" exec "{libexec}/bin/agent-resurrect" "$@"\n')
        wrapper.chmod(0o755)
        return keg

    def env(self) -> dict:
        return {"HOME": str(self.home), "PATH": os.pathsep.join([str(self.prefix / "bin"), str(FAKES), "/usr/bin",
                                                                 "/bin", "/usr/sbin", "/sbin"]),
                "LANG": "C", "SHELL": "/bin/zsh", "TMPDIR": str(self.tmp),
                "AGENT_RESURRECT_ITERM_APP": str(self.tmp / "iTerm.app"),
                "FAKE_DEFAULTS_DB": str(self.state / "defaults.json"), "FAKE_CLAUDE_DB": str(self.state / "claude.json"),
                "FAKE_CALLS_LOG": str(self.state / "calls.jsonl")}

    def run_cli(self, *argv: str, command=None) -> subprocess.CompletedProcess:
        command = command or ["agent-resurrect"]
        return subprocess.run([*command, *argv], env=self.env(), cwd=str(self.home), capture_output=True,
                              text=True, timeout=180)

    def zshrc(self) -> str:
        return (self.home / ".zshrc").read_text()

    def marketplaces(self) -> dict:
        return json.loads((self.state / "claude.json").read_text())["marketplaces"]

    def test_install_upgrade_switch_and_uninstall(self):
        done = self.run_cli("install", "--yes")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        rc = self.zshrc()
        self.assertIn(f'AGENT_RESURRECT_BIN="{self.opt}/bin/agent-resurrect"', rc)
        self.assertIn(f'. "{self.opt}/libexec/shell/agent-resurrect.zsh"', rc)
        self.assertNotIn(".local/bin", rc)
        self.assertTrue(os.access(self.opt / "bin" / "agent-resurrect", os.X_OK))
        self.assertTrue((self.opt / "libexec" / "shell" / "agent-resurrect.zsh").is_file())
        self.assertEqual(self.marketplaces()["agent-resurrect"]["path"], f"{self.opt}/libexec")
        self.assertFalse((self.home / ".local" / "bin" / "agent-resurrect").exists())
        self.assertFalse((self.home / ".local" / "share" / "agent-resurrect").exists())
        manifest = json.loads((self.home / ".local/state/agent-resurrect/manifest.json").read_text())
        self.assertEqual((manifest["home"], manifest["command"]), (f"{self.opt}/libexec", "agent-resurrect"))

        # brew upgrade: a new keg, opt repointed. The shell and the plugin already point at opt: nothing moves.
        self.keg("0.3.1")
        self.opt.unlink()
        self.opt.symlink_to(self.prefix / "Cellar" / "agent-resurrect" / "0.3.1")
        again = self.run_cli("install", "--yes")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("unchanged  shell.zsh", again.stdout)
        self.assertEqual(self.zshrc(), rc)

        # The one-line installer's copy (the same program without the wrapper) refuses while this one is installed.
        bare = self.run_cli("install", "--yes",
                            command=[str(self.prefix / "Cellar/agent-resurrect/0.3.1/libexec/bin/agent-resurrect")])
        self.assertNotEqual(bare.returncode, 0)
        self.assertIn("first run: agent-resurrect uninstall", bare.stdout + bare.stderr)
        self.assertEqual(self.zshrc(), rc)

        gone = self.run_cli("uninstall", "--yes")
        self.assertEqual(gone.returncode, 0, gone.stdout + gone.stderr)
        self.assertIn("brew uninstall agent-resurrect", gone.stdout)
        self.assertNotIn("agent-resurrect", self.zshrc() if (self.home / ".zshrc").exists() else "")
        self.assertNotIn("agent-resurrect", self.marketplaces())
        self.assertTrue((self.opt / "libexec" / "src").is_dir())  # Homebrew's files are Homebrew's to remove


if __name__ == "__main__":
    unittest.main()
