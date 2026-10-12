"""Apply, check and revert for every system setting.

Every test runs in a temporary HOME with the fake `defaults`, `launchctl` and `claude` from tests/fakes
first on PATH, and asserts that the fakes (never the real tools) are what the settings will run.
"""
import contextlib
import json
import os
import plistlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_resurrect import installer, paths
from agent_resurrect.settings import all_settings, legacy_settings
from agent_resurrect.settings.base import Check, Setting
from agent_resurrect.settings.claude_plugin import MARKETPLACE, PLUGIN_ID, ClaudePlugin
from agent_resurrect.settings.iterm import LABEL, ITermKeepWindows, ITermLaunchAtLogin, ITermRestorePolicy, iterm_app
from agent_resurrect.settings.macos import LegacyKeepWindowsOnQuit
from agent_resurrect.settings.shellrc import BEGIN, END, ShellRcBlock, login_shell
from agent_resurrect.terminals.base import TerminalAdapter
from tests.helpers import temp_home

FAKES = Path(__file__).resolve().parent / "fakes"
GLOBAL = "NSGlobalDomain"
ITERM = "com.googlecode.iterm2"


class FakeSystem:
    """Handles to the fake tools' state for one test."""

    def __init__(self, home: Path, state: Path):
        self.home = home
        self.state = state

    def defaults(self) -> dict:
        path = Path(os.environ["FAKE_DEFAULTS_DB"])
        return json.loads(path.read_text()) if path.exists() else {}

    def set_default(self, domain: str, key: str, type_: str, value: str) -> None:
        db = self.defaults()
        db.setdefault(domain, {})[key] = {"type": type_, "value": value}
        Path(os.environ["FAKE_DEFAULTS_DB"]).write_text(json.dumps(db))

    def get_default(self, domain: str, key: str):
        return self.defaults().get(domain, {}).get(key)

    def claude_db(self) -> dict:
        path = Path(os.environ["FAKE_CLAUDE_DB"])
        return json.loads(path.read_text()) if path.exists() else {"marketplaces": {}, "plugins": {}}

    def calls(self, tool: str):
        path = Path(os.environ["FAKE_CALLS_LOG"])
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        return [row[1:] for row in rows if row[0] == tool]

    def clear_calls(self) -> None:
        _unlink(Path(os.environ["FAKE_CALLS_LOG"]))

    def claude_changes(self):
        """`claude` calls that can change state (everything except the list commands)."""
        return [c for c in self.calls("claude") if "list" not in c]


def _unlink(path: Path) -> None:
    if path.exists():
        path.unlink()


def _dirs_without(name: str):
    return [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and not (Path(d) / name).exists()]


@contextlib.contextmanager
def fake_system(iterm: bool = True, claude: bool = True):
    with temp_home() as home:
        state = Path(tempfile.mkdtemp(prefix="ar-fakes-")).resolve()
        try:
            if claude:
                path = [str(FAKES)] + os.environ.get("PATH", "").split(os.pathsep)
            else:
                only = state / "bin"
                only.mkdir()
                for tool in ("defaults", "launchctl"):
                    (only / tool).symlink_to(FAKES / tool)
                path = [str(only)] + _dirs_without("claude")
            os.environ["PATH"] = os.pathsep.join(path)
            os.environ["FAKE_DEFAULTS_DB"] = str(state / "defaults.json")
            os.environ["FAKE_CLAUDE_DB"] = str(state / "claude.json")
            os.environ["FAKE_LAUNCHCTL_DB"] = str(state / "launchctl.json")
            os.environ["FAKE_CALLS_LOG"] = str(state / "calls.jsonl")
            app = state / "Applications" / "iTerm.app"
            if iterm:
                app.mkdir(parents=True)
            os.environ["AGENT_RESURRECT_ITERM_APP"] = str(app)
            # Safety net: the settings must resolve to the fakes, never to the real system tools.
            for tool in ("defaults", "launchctl"):
                assert Path(shutil.which(tool)).resolve() == (FAKES / tool).resolve(), tool
            if claude:
                assert Path(shutil.which("claude")).resolve() == (FAKES / "claude").resolve()
            else:
                assert shutil.which("claude") is None
            yield FakeSystem(home, state)
        finally:
            shutil.rmtree(state, ignore_errors=True)


def make_marketplace() -> None:
    """What install.sh copies into the data dir: the marketplace manifest the plugin is installed from."""
    manifest = paths.data_dir() / ".claude-plugin" / "marketplace.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"name": "agent-resurrect", "owner": {"name": "imgovind"},
                                    "plugins": [{"name": "agent-resurrect", "source": "./"}]}))


class DefaultsSettingTest(unittest.TestCase):
    def test_keep_windows_is_scoped_to_iterm(self):
        with fake_system() as fs:
            setting = ITermKeepWindows()
            self.assertEqual(setting.id, "iterm.keep_windows")
            self.assertTrue(setting.applicable())
            self.assertFalse(setting.check().ok)
            original = setting.apply()
            self.assertEqual(original, {"keys": {"NSQuitAlwaysKeepsWindows": {
                "domain": ITERM, "existed": False, "type": None, "value": None}}})
            self.assertEqual(fs.get_default(ITERM, "NSQuitAlwaysKeepsWindows"), {"type": "boolean", "value": "1"})
            self.assertNotIn(GLOBAL, fs.defaults())  # every other app keeps the user's macOS-wide choice
            self.assertTrue(setting.check().ok)
            setting.revert(original)
            self.assertEqual(fs.defaults(), {})
            self.assertFalse(setting.check().ok)
            writes = [c for c in fs.calls("defaults") if c[0] in ("write", "delete")]
            self.assertEqual(writes, [["write", ITERM, "NSQuitAlwaysKeepsWindows", "-bool", "TRUE"],
                                      ["delete", ITERM, "NSQuitAlwaysKeepsWindows"]])

    def test_keep_windows_does_not_rely_on_the_global_value(self):
        # The user may turn the macOS-wide setting back on at any time; iTerm2's own value keeps it restoring.
        with fake_system() as fs:
            fs.set_default(GLOBAL, "NSQuitAlwaysKeepsWindows", "boolean", "1")
            setting = ITermKeepWindows()
            self.assertFalse(setting.check().ok)
            original = setting.apply()
            self.assertEqual(fs.get_default(ITERM, "NSQuitAlwaysKeepsWindows"), {"type": "boolean", "value": "1"})
            setting.revert(original)
            self.assertIsNone(fs.get_default(ITERM, "NSQuitAlwaysKeepsWindows"))
            self.assertEqual(fs.get_default(GLOBAL, "NSQuitAlwaysKeepsWindows"), {"type": "boolean", "value": "1"})

    def test_keep_windows_already_true_returns_none(self):
        with fake_system() as fs:
            for type_, value in (("boolean", "1"), ("integer", "1"), ("string", "YES")):
                fs.set_default(ITERM, "NSQuitAlwaysKeepsWindows", type_, value)
                self.assertIsNone(ITermKeepWindows().apply())
                self.assertTrue(ITermKeepWindows().check().ok)
                self.assertEqual(fs.get_default(ITERM, "NSQuitAlwaysKeepsWindows"), {"type": type_, "value": value})
            self.assertEqual([c for c in fs.calls("defaults") if c[0] in ("write", "delete")], [])

    def test_keep_windows_revert_restores_false(self):
        with fake_system() as fs:
            for type_ in ("boolean", "integer"):
                fs.set_default(ITERM, "NSQuitAlwaysKeepsWindows", type_, "0")
                setting = ITermKeepWindows()
                original = setting.apply()
                self.assertEqual(original["keys"]["NSQuitAlwaysKeepsWindows"],
                                 {"domain": ITERM, "existed": True, "type": type_, "value": "0"})
                self.assertEqual(fs.get_default(ITERM, "NSQuitAlwaysKeepsWindows"), {"type": "boolean", "value": "1"})
                setting.revert(original)
                self.assertEqual(fs.get_default(ITERM, "NSQuitAlwaysKeepsWindows"), {"type": type_, "value": "0"})

    def test_keep_windows_refuses_a_value_it_could_not_restore(self):
        with fake_system() as fs:
            fs.set_default(ITERM, "NSQuitAlwaysKeepsWindows", "array", "(\n)")
            with self.assertRaises(RuntimeError):
                ITermKeepWindows().apply()
            self.assertEqual(fs.get_default(ITERM, "NSQuitAlwaysKeepsWindows"), {"type": "array", "value": "(\n)"})

    def test_iterm_settings_need_iterm(self):
        with fake_system(iterm=False) as fs:
            fs.set_default(ITERM, "OpenNoWindowsAtStartup", "boolean", "1")
            fs.clear_calls()
            for setting in (ITermKeepWindows(), ITermRestorePolicy(), ITermLaunchAtLogin()):
                self.assertFalse(setting.applicable(), setting.id)
                self.assertEqual(setting.skip_detail, "iTerm2 is not installed")
                # Doctor's fix is to install iTerm2: rerunning install would only skip it again.
                self.assertEqual(setting.check(), Check(False, "iTerm2 is not installed"))
                self.assertTrue(setting.fix_hint.startswith("install iTerm2 from https://iterm2.com"), setting.id)
                self.assertIsNone(setting.apply())
            self.assertEqual([c for c in fs.calls("defaults") if c[0] in ("write", "delete")], [])
            self.assertEqual([(r.id, r.status) for r in installer.install([ITermKeepWindows(), ITermRestorePolicy()])],
                             [("iterm.keep_windows", "skipped"), ("iterm.restore_policy", "skipped")])
        with fake_system(iterm=True):
            for setting in (ITermKeepWindows(), ITermRestorePolicy(), ITermLaunchAtLogin()):
                self.assertTrue(setting.applicable(), setting.id)
                self.assertTrue(setting.fix_hint.startswith("run: ~/.local/bin/agent-resurrect install"), setting.id)

    def test_iterm_found_in_the_users_applications_folder(self):
        with temp_home() as home:
            os.environ.pop("AGENT_RESURRECT_ITERM_APP", None)
            if Path("/Applications/iTerm.app").exists():
                self.assertEqual(iterm_app(), Path("/Applications/iTerm.app"))
                return
            self.assertFalse(iterm_app().exists())
            (home / "Applications" / "iTerm.app").mkdir(parents=True)
            self.assertEqual(iterm_app(), home / "Applications" / "iTerm.app")

    def test_legacy_global_keep_windows_reverts_exactly(self):
        with fake_system() as fs:
            self.assertEqual([s.id for s in legacy_settings()], ["macos.keep_windows"])
            legacy = LegacyKeepWindowsOnQuit()
            self.assertEqual(legacy.id, "macos.keep_windows")
            self.assertNotIn("macos.keep_windows", [s.id for s in all_settings()])  # never applied any more
            fs.set_default(GLOBAL, "NSQuitAlwaysKeepsWindows", "boolean", "1")
            legacy.revert({"keys": {"NSQuitAlwaysKeepsWindows": {"domain": GLOBAL, "existed": True,
                                                                  "type": "integer", "value": "0"}}})
            self.assertEqual(fs.get_default(GLOBAL, "NSQuitAlwaysKeepsWindows"), {"type": "integer", "value": "0"})
            legacy.revert({"keys": {"NSQuitAlwaysKeepsWindows": {"domain": GLOBAL, "existed": False,
                                                                  "type": None, "value": None}}})
            self.assertIsNone(fs.get_default(GLOBAL, "NSQuitAlwaysKeepsWindows"))

    def test_iterm_restore_policy(self):
        with fake_system() as fs:
            setting = ITermRestorePolicy()
            self.assertEqual(setting.id, "iterm.restore_policy")
            # Absent keys mean NO in iTerm2, which is already "Use System Window Restoration Setting".
            self.assertTrue(setting.check().ok)
            self.assertIsNone(setting.apply())
            self.assertEqual(fs.defaults(), {})
            # Explicit NO for both keys is also already in the desired state.
            fs.set_default(ITERM, "OpenArrangementAtStartup", "boolean", "0")
            fs.set_default(ITERM, "OpenNoWindowsAtStartup", "boolean", "0")
            self.assertIsNone(setting.apply())

            # "Don't Open Any Windows": apply turns it off, check passes, revert puts it back.
            fs.set_default(ITERM, "OpenNoWindowsAtStartup", "boolean", "1")
            self.assertFalse(setting.check().ok)
            original = setting.apply()
            self.assertEqual(set(original["keys"]), {"OpenNoWindowsAtStartup"})
            self.assertEqual(fs.get_default(ITERM, "OpenNoWindowsAtStartup"), {"type": "boolean", "value": "0"})
            self.assertTrue(setting.check().ok)
            setting.revert(original)
            self.assertEqual(fs.get_default(ITERM, "OpenNoWindowsAtStartup"), {"type": "boolean", "value": "1"})
            self.assertEqual(fs.get_default(ITERM, "OpenArrangementAtStartup"), {"type": "boolean", "value": "0"})

    def test_iterm_restore_policy_revert_restores_open_arrangement(self):
        with fake_system() as fs:
            fs.set_default(ITERM, "OpenArrangementAtStartup", "boolean", "1")
            setting = ITermRestorePolicy()
            original = setting.apply()
            self.assertEqual(original["keys"]["OpenArrangementAtStartup"]["existed"], True)
            self.assertEqual(fs.get_default(ITERM, "OpenArrangementAtStartup"), {"type": "boolean", "value": "0"})
            self.assertIsNone(fs.get_default(ITERM, "OpenNoWindowsAtStartup"))
            setting.revert(original)
            self.assertEqual(fs.get_default(ITERM, "OpenArrangementAtStartup"), {"type": "boolean", "value": "1"})
            self.assertIsNone(fs.get_default(ITERM, "OpenNoWindowsAtStartup"))


class LaunchAgentSettingTest(unittest.TestCase):
    def test_launch_agent_plist_written_but_not_loaded(self):
        # Loading it now (launchctl bootstrap) would run RunAtLoad at once: `open -a iTerm` would pull iTerm2 to
        # the front in the middle of the install. macOS loads ~/Library/LaunchAgents at the next login.
        with fake_system() as fs:
            setting = ITermLaunchAtLogin()
            self.assertEqual(setting.id, "iterm.launch_at_login")
            self.assertEqual(LABEL, "io.github.imgovind.agent-resurrect.iterm")
            self.assertFalse(setting.check().ok)
            original = setting.apply()
            self.assertEqual(original, {"existed": False})
            plist = fs.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            self.assertEqual(plistlib.loads(plist.read_bytes()),
                             {"Label": LABEL, "ProgramArguments": ["/usr/bin/open", "-a", "iTerm"],
                              "RunAtLoad": True, "LimitLoadToSessionType": "Aqua"})
            self.assertTrue(setting.check().ok)
            uid = os.getuid()
            self.assertEqual(fs.calls("launchctl"), [])

            self.assertIsNone(setting.apply())  # idempotent: no rewrite
            self.assertEqual(fs.calls("launchctl"), [])

            setting.revert(original)
            self.assertEqual(fs.calls("launchctl"), [["bootout", f"gui/{uid}/{LABEL}"]])
            self.assertFalse(plist.exists())
            self.assertFalse(setting.check().ok)

    def test_launch_agent_upgrade_rewrites_the_plist_without_loading_it(self):
        with fake_system() as fs:
            plist = fs.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
            plist.parent.mkdir(parents=True)
            plist.write_bytes(plistlib.dumps({"Label": LABEL, "ProgramArguments": ["/usr/bin/open", "-a", "iTerm2"]}))
            self.assertFalse(ITermLaunchAtLogin().check().ok)
            self.assertIsNone(ITermLaunchAtLogin().apply())  # ours from an earlier install: keep its first original
            self.assertTrue(ITermLaunchAtLogin().check().ok)
            self.assertEqual(fs.calls("launchctl"), [])

    def test_launch_agent_already_loaded_is_fine(self):
        with fake_system():
            Path(os.environ["FAKE_LAUNCHCTL_DB"]).write_text(json.dumps([LABEL]))
            self.assertEqual(ITermLaunchAtLogin().apply(), {"existed": False})
            self.assertTrue(ITermLaunchAtLogin().check().ok)
            # Revert ignores a bootout failure (job not loaded) and still deletes the plist.
            Path(os.environ["FAKE_LAUNCHCTL_DB"]).write_text("[]")
            ITermLaunchAtLogin().revert({"existed": False})
            self.assertFalse(ITermLaunchAtLogin().check().ok)

    def test_launch_agent_skipped_without_iterm(self):
        with fake_system(iterm=False) as fs:
            setting = ITermLaunchAtLogin()
            self.assertIsNone(setting.apply())
            check = setting.check()
            self.assertFalse(check.ok)
            self.assertIn("iTerm2 is not installed", check.detail)
            self.assertFalse((fs.home / "Library" / "LaunchAgents").exists())
            self.assertEqual(fs.calls("launchctl"), [])


class ShellRcSettingTest(unittest.TestCase):
    def test_block_content(self):
        with temp_home():
            block = ShellRcBlock("zsh").block()
            lines = block.splitlines()
            self.assertEqual(lines[0], BEGIN)
            self.assertEqual(lines[-1], END)
            self.assertEqual(BEGIN, "# >>> agent-resurrect >>>")
            self.assertEqual(END, "# <<< agent-resurrect <<<")
            # Appended, not prepended: ~/.local/bin must never shadow the user's pyenv, Homebrew or uv tools.
            self.assertIn('case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$PATH:$HOME/.local/bin" ;; esac',
                          lines)
            self.assertIn('[ -f "$HOME/.local/share/agent-resurrect/shell/agent-resurrect.zsh" ] && '
                          '. "$HOME/.local/share/agent-resurrect/shell/agent-resurrect.zsh"', lines)
            self.assertTrue(block.endswith("\n"))
            self.assertIn("agent-resurrect.bash", ShellRcBlock("bash").block())

    def test_block_uses_absolute_data_dir_outside_home(self):
        with temp_home():
            os.environ["XDG_DATA_HOME"] = "/opt/data $x"
            self.assertIn('"/opt/data \\$x/agent-resurrect/shell/agent-resurrect.zsh"', ShellRcBlock("zsh").block())

    def test_rc_block_added_once_and_removed_exactly(self):
        with temp_home() as home:
            rc = home / ".zshrc"
            rc.write_bytes(b"export A=1")
            setting = ShellRcBlock("zsh")
            self.assertEqual(setting.id, "shell.zsh")
            self.assertFalse(setting.check().ok)
            original = setting.apply()
            self.assertEqual(original["existed"], True)
            after = rc.read_bytes()
            self.assertEqual(after, b"export A=1\n" + setting.block().encode())
            self.assertTrue(setting.check().ok)
            self.assertIsNone(setting.apply())
            self.assertEqual(rc.read_bytes(), after)
            setting.revert(original)
            self.assertEqual(rc.read_bytes(), b"export A=1")

    def test_rc_symlink_and_bytes_preserved(self):
        with temp_home() as home:
            dotfiles = home / "dotfiles"
            dotfiles.mkdir()
            target = dotfiles / "zshrc"
            original_bytes = b"# mine\r\nalias ll='ls -l'\n\xff\xfe not utf-8\n\n"
            target.write_bytes(original_bytes)
            link = home / ".zshrc"
            link.symlink_to(target)
            inode = target.stat().st_ino
            setting = ShellRcBlock("zsh")
            original = setting.apply()
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), str(target))
            self.assertEqual(target.stat().st_ino, inode)
            self.assertEqual(target.read_bytes(), original_bytes + setting.block().encode())
            self.assertTrue(setting.check().ok)
            setting.revert(original)
            self.assertTrue(link.is_symlink())
            self.assertEqual(target.read_bytes(), original_bytes)
            self.assertEqual(sorted(p.name for p in dotfiles.iterdir()), ["zshrc"])

    def test_rc_created_then_removed_when_absent(self):
        with temp_home() as home:
            os.environ["SHELL"] = "/bin/zsh"
            setting = ShellRcBlock("zsh")
            self.assertTrue(setting.applicable())
            original = setting.apply()
            self.assertEqual(original["existed"], False)
            self.assertEqual((home / ".zshrc").read_text(), setting.block())
            setting.revert(original)
            self.assertFalse((home / ".zshrc").exists())

            zdot = home / "zdot"
            zdot.mkdir()
            os.environ["ZDOTDIR"] = str(zdot)
            original = setting.apply()
            self.assertTrue((zdot / ".zshrc").exists())
            self.assertFalse((home / ".zshrc").exists())
            setting.revert(original)
            self.assertFalse((zdot / ".zshrc").exists())

    def test_zshrc_not_created_when_the_login_shell_is_not_zsh(self):
        for shell in ("/opt/homebrew/bin/fish", "/bin/bash", "/usr/local/bin/nu"):
            with self.subTest(shell), temp_home() as home:
                os.environ["SHELL"] = shell
                self.assertEqual(login_shell(), os.path.basename(shell))
                setting = ShellRcBlock("zsh")
                self.assertFalse(setting.applicable())
                self.assertIn(f"zsh is not your login shell ({os.path.basename(shell)})", setting.skip_detail)
                self.assertIsNone(setting.apply())
                check = setting.check()
                self.assertTrue(check.ok)  # nothing to load: doctor reports the login shell itself
                self.assertIn("zsh not configured", check.detail)
                self.assertEqual(list(home.iterdir()), [])
                self.assertEqual([(r.id, r.status) for r in installer.install([setting])], [("shell.zsh", "skipped")])
                self.assertFalse((home / ".zshrc").exists())

    def test_existing_zshrc_gets_the_block_whatever_the_login_shell(self):
        with temp_home() as home:
            os.environ["SHELL"] = "/opt/homebrew/bin/fish"
            rc = home / ".zshrc"
            rc.write_text("export A=1\n")
            setting = ShellRcBlock("zsh")
            self.assertTrue(setting.applicable())
            original = setting.apply()
            self.assertIn(BEGIN, rc.read_text())
            setting.revert(original)
            self.assertEqual(rc.read_text(), "export A=1\n")

    def test_login_shell_falls_back_to_the_account(self):
        with temp_home():
            os.environ.pop("SHELL", None)
            with mock.patch("pwd.getpwuid") as getpwuid:
                getpwuid.return_value.pw_shell = "/bin/zsh"
                self.assertEqual(login_shell(), "zsh")
                getpwuid.side_effect = KeyError("no such user")
                self.assertEqual(login_shell(), "")
                # Unknown: assume macOS's default shell, zsh.
                self.assertTrue(ShellRcBlock("zsh").applicable())

    def test_v01_block_that_prepended_path_is_upgraded_in_place(self):
        with temp_home() as home:
            os.environ["SHELL"] = "/bin/zsh"
            rc = home / ".zshrc"
            rc.write_bytes(b"export A=1")
            setting = ShellRcBlock("zsh")
            v01 = setting.block().replace('export PATH="$PATH:$HOME/.local/bin"',
                                          'export PATH="$HOME/.local/bin:$PATH"')
            self.assertNotEqual(v01, setting.block())
            rc.write_bytes(b"export A=1\n" + v01.encode() + b"export B=2\n")
            original = {"existed": True, "path": str(rc), "added_newline": True}  # what 0.1 recorded
            self.assertFalse(setting.check().ok)
            self.assertIn("outdated", setting.check().detail)
            self.assertIsNone(setting.apply())  # the manifest keeps 0.1's original
            self.assertEqual(rc.read_bytes(), b"export A=1\n" + setting.block().encode() + b"export B=2\n")
            self.assertTrue(setting.check().ok)
            setting.revert(original)
            self.assertEqual(rc.read_bytes(), b"export A=1\nexport B=2\n")

    def test_rc_outdated_block_replaced_in_place(self):
        with temp_home() as home:
            rc = home / ".zshrc"
            old_block = f"{BEGIN}\nsource /old/path\n{END}\n"
            rc.write_text(f"export A=1\n{old_block}export B=2\n")
            setting = ShellRcBlock("zsh")
            self.assertFalse(setting.check().ok)
            self.assertIsNone(setting.apply())
            self.assertEqual(rc.read_text(), f"export A=1\n{setting.block()}export B=2\n")
            self.assertTrue(setting.check().ok)

    def test_rc_revert_keeps_user_lines_added_after_the_block(self):
        with temp_home() as home:
            rc = home / ".zshrc"
            rc.write_bytes(b"export A=1")
            setting = ShellRcBlock("zsh")
            original = setting.apply()
            with open(rc, "ab") as fh:
                fh.write(b"export B=2\n")
            setting.revert(original)
            # The newline the installer added now ends a line the user kept, so it stays.
            self.assertEqual(rc.read_bytes(), b"export A=1\nexport B=2\n")

    def test_rc_unterminated_begin_marker_is_not_a_block(self):
        with temp_home() as home:
            rc = home / ".zshrc"
            rc.write_text(f"{BEGIN}\nexport KEEP=1\n")
            setting = ShellRcBlock("zsh")
            original = setting.apply()
            setting.revert(original)
            self.assertEqual(rc.read_text(), f"{BEGIN}\nexport KEEP=1\n")

    def test_bash_prefers_bash_profile_and_skips_when_unconfigured(self):
        with temp_home() as home:
            setting = ShellRcBlock("bash")
            self.assertEqual(setting.id, "shell.bash")
            self.assertFalse(setting.applicable())
            self.assertEqual(setting.skip_detail, "bash not configured (no ~/.bash_profile or ~/.bashrc)")
            self.assertIsNone(setting.apply())
            check = setting.check()
            self.assertTrue(check.ok)
            self.assertIn("bash not configured", check.detail)
            self.assertEqual(sorted(p.name for p in home.iterdir()), [])

            profile, bashrc = home / ".bash_profile", home / ".bashrc"
            profile.write_text("P=1\n")
            bashrc.write_text("R=1\n")
            original = setting.apply()
            self.assertIn(BEGIN, profile.read_text())
            self.assertEqual(bashrc.read_text(), "R=1\n")
            setting.revert(original)
            self.assertEqual(profile.read_text(), "P=1\n")

            profile.unlink()
            original = setting.apply()
            self.assertIn(BEGIN, bashrc.read_text())
            setting.revert(original)
            self.assertEqual(bashrc.read_text(), "R=1\n")

    def test_revert_uses_the_file_that_was_edited(self):
        with temp_home() as home:
            bashrc = home / ".bashrc"
            bashrc.write_text("R=1\n")
            setting = ShellRcBlock("bash")
            original = setting.apply()
            (home / ".bash_profile").write_text("P=1\n")  # created after install; now preferred
            setting.revert(original)
            self.assertEqual(bashrc.read_text(), "R=1\n")
            self.assertEqual((home / ".bash_profile").read_text(), "P=1\n")


class ClaudePluginSettingTest(unittest.TestCase):
    def test_claude_plugin_install_update_uninstall(self):
        with fake_system() as fs:
            make_marketplace()
            setting = ClaudePlugin()
            self.assertEqual(setting.id, "claude.plugin")
            self.assertEqual(PLUGIN_ID, "agent-resurrect@agent-resurrect")
            self.assertFalse(setting.check().ok)

            original = setting.apply()
            self.assertEqual(original, {"installed": False, "marketplace_existed": False})
            self.assertEqual(fs.claude_changes(), [
                ["plugin", "marketplace", "add", str(paths.data_dir()), "--scope", "user"],
                ["plugin", "install", PLUGIN_ID, "--scope", "user"],
            ])
            self.assertTrue(setting.check().ok)

            fs.clear_calls()
            self.assertIsNone(setting.apply())
            self.assertEqual(fs.claude_changes(), [
                ["plugin", "marketplace", "update", MARKETPLACE],
                ["plugin", "update", PLUGIN_ID, "--scope", "user"],
            ])

            fs.clear_calls()
            setting.revert(original)
            self.assertEqual(fs.claude_changes(), [
                ["plugin", "uninstall", PLUGIN_ID, "--scope", "user"],
                ["plugin", "marketplace", "remove", MARKETPLACE],
            ])
            self.assertEqual(fs.claude_db(), {"marketplaces": {}, "plugins": {}})
            self.assertFalse(setting.check().ok)

    def test_claude_plugin_keeps_a_marketplace_that_existed(self):
        with fake_system() as fs:
            make_marketplace()
            ClaudePlugin()._claude("plugin", "marketplace", "add", str(paths.data_dir()))
            setting = ClaudePlugin()
            original = setting.apply()
            self.assertEqual(original, {"installed": False, "marketplace_existed": True})
            setting.revert(original)
            self.assertIn(MARKETPLACE, fs.claude_db()["marketplaces"])
            self.assertEqual(fs.claude_db()["plugins"], {})

    def test_failed_install_removes_the_marketplace_it_added(self):
        with fake_system() as fs:
            make_marketplace()
            os.environ["FAKE_CLAUDE_FAIL"] = "plugin install"
            with self.assertRaises(RuntimeError) as ctx:
                ClaudePlugin().apply()
            self.assertIn("plugin install", str(ctx.exception))
            self.assertEqual(fs.claude_db()["marketplaces"], {})

    def test_disabled_plugin_is_enabled_and_revert_disables_it(self):
        with fake_system() as fs:
            make_marketplace()
            ClaudePlugin().apply()
            ClaudePlugin()._claude("plugin", "disable", PLUGIN_ID, "--scope", "user")
            setting = ClaudePlugin()
            check = setting.check()
            self.assertFalse(check.ok)
            self.assertIn("disabled", check.detail)
            original = setting.apply()
            self.assertEqual(original, {"installed": True, "enabled": False})
            self.assertTrue(setting.check().ok)
            setting.revert(original)
            self.assertFalse(fs.claude_db()["plugins"][PLUGIN_ID]["enabled"])
            self.assertIn(MARKETPLACE, fs.claude_db()["marketplaces"])

    def seed_claude(self, fs, marketplaces=None, plugins=None):
        Path(os.environ["FAKE_CLAUDE_DB"]).write_text(json.dumps({"marketplaces": marketplaces or {},
                                                                   "plugins": plugins or {}}))

    GITHUB = {MARKETPLACE: {"source": "github", "repo": "imgovind/agent-resurrect", "plugins": ["agent-resurrect"]}}

    def test_an_existing_marketplace_of_the_same_name_is_used_not_added_again(self):
        # /plugin marketplace add imgovind/agent-resurrect declares the same marketplace name from GitHub.
        with fake_system() as fs:
            make_marketplace()
            self.seed_claude(fs, marketplaces=self.GITHUB)
            setting = ClaudePlugin()
            self.assertTrue(setting.applicable())
            original = setting.apply()
            self.assertEqual(original, {"installed": False, "marketplace_existed": True})
            self.assertEqual(fs.claude_changes(), [["plugin", "install", PLUGIN_ID, "--scope", "user"]])
            self.assertTrue(setting.check().ok)
            fs.clear_calls()
            setting.revert(original)
            self.assertEqual(fs.claude_changes(), [["plugin", "uninstall", PLUGIN_ID, "--scope", "user"]])
            self.assertEqual(fs.claude_db()["marketplaces"], self.GITHUB)

    def test_plugin_installed_from_github_is_left_alone(self):
        for enabled, status in ((True, "unchanged"), (False, "skipped")):
            with self.subTest(enabled=enabled), fake_system() as fs:
                make_marketplace()
                plugins = {PLUGIN_ID: {"scope": "user", "enabled": enabled, "version": "0.2.0"}}
                self.seed_claude(fs, marketplaces=self.GITHUB, plugins=plugins)
                setting = ClaudePlugin()
                self.assertEqual(setting.check().ok, enabled)
                self.assertIsNone(setting.apply())
                results = installer.install([setting])
                self.assertEqual([(r.id, r.status) for r in results], [("claude.plugin", status)])
                self.assertEqual(fs.claude_changes(), [])
                self.assertEqual(fs.claude_db()["plugins"], plugins)
                # Recorded as unchanged (or not at all), so uninstall never removes the user's own install.
                expected = {"status": "unchanged", "original": None} if enabled else None
                self.assertEqual(installer.load_manifest()["settings"].get("claude.plugin"), expected)
                installer.uninstall([setting])
                self.assertEqual(fs.claude_db()["plugins"], plugins)

    def test_plugin_installed_another_way_is_left_alone(self):
        local = {"source": "directory", "plugins": ["agent-resurrect"]}
        cases = {
            "project scope": {PLUGIN_ID: {"scope": "project", "enabled": True, "version": "0.2.0"}},
            "local scope": {PLUGIN_ID: {"scope": "local", "enabled": True, "version": "0.2.0"}},
            "other marketplace": {"agent-resurrect@claude-plugins-official":
                                  {"scope": "user", "enabled": True, "version": "0.2.0"}},
        }
        for name, plugins in cases.items():
            with self.subTest(name), fake_system() as fs:
                make_marketplace()
                self.seed_claude(fs, marketplaces={MARKETPLACE: dict(local, path=str(paths.data_dir()))},
                                 plugins=plugins)
                setting = ClaudePlugin()
                self.assertTrue(setting.check().ok)
                self.assertIsNone(setting.apply())
                self.assertEqual(fs.claude_changes(), [])
                self.assertEqual(fs.claude_db()["plugins"], plugins)

    def test_plugin_from_another_directory_marketplace_is_left_alone(self):
        # For example a developer's checkout added with /plugin marketplace add ~/src/agent-resurrect.
        with fake_system() as fs:
            make_marketplace()
            plugins = {PLUGIN_ID: {"scope": "user", "enabled": True, "version": "0.2.0"}}
            self.seed_claude(fs, marketplaces={MARKETPLACE: {"source": "directory", "path": "/src/agent-resurrect",
                                                             "plugins": ["agent-resurrect"]}}, plugins=plugins)
            self.assertIsNone(ClaudePlugin().apply())
            self.assertEqual(fs.claude_changes(), [])

    def test_marketplace_row_shapes(self):
        from agent_resurrect.settings.claude_plugin import _is_local_marketplace
        with temp_home():
            data = str(paths.data_dir())
            self.assertTrue(_is_local_marketplace({"name": MARKETPLACE, "source": "directory", "path": data}))
            self.assertTrue(_is_local_marketplace({"name": MARKETPLACE, "source": "directory",
                                                   "installLocation": data}))
            self.assertTrue(_is_local_marketplace({"name": MARKETPLACE, "source": {"source": "directory", "path": data},
                                                   "installLocation": data}))
            self.assertTrue(_is_local_marketplace({"name": MARKETPLACE, "source": "directory"}))  # no path given
            self.assertTrue(_is_local_marketplace({"name": MARKETPLACE}))
            self.assertFalse(_is_local_marketplace({"name": MARKETPLACE, "source": "github",
                                                    "repo": "imgovind/agent-resurrect"}))
            self.assertFalse(_is_local_marketplace({"name": MARKETPLACE, "source": {"source": "github", "repo": "x/y"},
                                                    "installLocation": data}))
            self.assertFalse(_is_local_marketplace({"name": MARKETPLACE, "source": "directory", "path": "/elsewhere"}))

    def test_claude_plugin_without_claude_is_skipped(self):
        with fake_system(claude=False) as fs:
            setting = ClaudePlugin()
            self.assertFalse(setting.applicable())
            self.assertEqual(setting.skip_detail, "Claude Code (claude) is not on PATH")
            self.assertIsNone(setting.apply())
            check = setting.check()
            self.assertFalse(check.ok)
            self.assertIn("install Claude Code, then run: ~/.local/bin/agent-resurrect install", setting.fix_hint)
            self.assertEqual(fs.calls("claude"), [])


class AllSettingsTest(unittest.TestCase):
    def test_order_includes_terminal_settings(self):
        class Named(Setting):
            def __init__(self, id_):
                self.id = id_

            def check(self):
                return Check(True, "")

            def apply(self):
                return None

            def revert(self, original):
                pass

        class Term(TerminalAdapter):
            name = "t"

            def detect(self, env):
                return False

            def pane_key(self, env):
                return None

            def pane_label(self, env):
                return None

            def settings(self):
                return [Named("t.one"), Named("t.two")]

        with mock.patch("agent_resurrect.terminals.all_terminals", return_value=[Term()]):
            ids = [s.id for s in all_settings()]
        self.assertEqual(ids, ["iterm.keep_windows", "t.one", "t.two", "shell.zsh", "shell.bash", "claude.plugin"])

    def test_descriptions_and_fix_hints_are_plain(self):
        with fake_system(iterm=False, claude=False):
            settings = [ITermKeepWindows(), ITermRestorePolicy(), ITermLaunchAtLogin(), ShellRcBlock("zsh"),
                        ShellRcBlock("bash"), ClaudePlugin(), LegacyKeepWindowsOnQuit()]
            for setting in settings:
                for text in (setting.description, setting.fix_hint, setting.skip_detail):
                    self.assertTrue(text, setting.id)
                    self.assertNotIn("\u2014", text)
                    self.assertNotIn("\n", text)


class LegacyMigrationTest(unittest.TestCase):
    """0.1 changed NSQuitAlwaysKeepsWindows for every app (manifest id macos.keep_windows). From 0.2 the key is
    set for iTerm2 alone, and both install (the upgrade) and uninstall give the user back their global value."""

    def legacy_manifest(self, status="changed", existed=True, type_="boolean", value="0"):
        original = {"keys": {"NSQuitAlwaysKeepsWindows": {"domain": GLOBAL, "existed": existed,
                                                           "type": type_ if existed else None,
                                                           "value": value if existed else None}}}
        paths.state_dir().mkdir(parents=True, exist_ok=True)
        paths.manifest_path().write_text(json.dumps({
            "version": 1, "installed_at": "2026-10-01T00:00:00Z",
            "settings": {"macos.keep_windows": {"status": status,
                                                "original": original if status == "changed" else None},
                         "shell.zsh": {"status": "unchanged", "original": None}}}))

    def test_install_migrates_the_global_change_to_iterm(self):
        with fake_system() as fs:
            fs.set_default(GLOBAL, "NSQuitAlwaysKeepsWindows", "boolean", "1")  # what 0.1 wrote
            self.legacy_manifest(existed=True, value="0")
            settings = [ITermKeepWindows()]

            before = (fs.defaults(), paths.manifest_path().read_text())
            plan = installer.install(settings, dry_run=True)
            self.assertEqual([(r.id, r.status) for r in plan],
                             [("macos.keep_windows", "would-revert"), ("iterm.keep_windows", "would-change")])
            self.assertIn("Close windows when quitting an application", plan[0].description)
            self.assertEqual((fs.defaults(), paths.manifest_path().read_text()), before)

            results = installer.install(settings)
            self.assertEqual([(r.id, r.status) for r in results],
                             [("macos.keep_windows", "reverted"), ("iterm.keep_windows", "changed")])
            self.assertEqual(fs.get_default(GLOBAL, "NSQuitAlwaysKeepsWindows"), {"type": "boolean", "value": "0"})
            self.assertEqual(fs.get_default(ITERM, "NSQuitAlwaysKeepsWindows"), {"type": "boolean", "value": "1"})
            manifest = installer.load_manifest()
            self.assertNotIn("macos.keep_windows", manifest["settings"])
            self.assertEqual(manifest["settings"]["shell.zsh"], {"status": "unchanged", "original": None})
            self.assertEqual(manifest["installed_at"], "2026-10-01T00:00:00Z")

            # Done once: the next install has nothing to migrate, and uninstall reverts only iTerm2's value.
            self.assertEqual([(r.id, r.status) for r in installer.install(settings)],
                             [("iterm.keep_windows", "unchanged")])
            installer.uninstall(settings)
            self.assertEqual(fs.defaults(), {GLOBAL: {"NSQuitAlwaysKeepsWindows": {"type": "boolean", "value": "0"}}})

    def test_install_migration_deletes_a_key_that_did_not_exist(self):
        with fake_system() as fs:
            fs.set_default(GLOBAL, "NSQuitAlwaysKeepsWindows", "boolean", "1")
            self.legacy_manifest(existed=False)
            installer.install([ITermKeepWindows()])
            self.assertIsNone(fs.get_default(GLOBAL, "NSQuitAlwaysKeepsWindows"))

    def test_install_drops_an_unchanged_legacy_entry_silently(self):
        with fake_system() as fs:
            fs.set_default(GLOBAL, "NSQuitAlwaysKeepsWindows", "boolean", "1")  # the user's own choice
            self.legacy_manifest(status="unchanged")
            results = installer.install([ITermKeepWindows()])
            self.assertEqual([(r.id, r.status) for r in results], [("iterm.keep_windows", "changed")])
            self.assertEqual(fs.get_default(GLOBAL, "NSQuitAlwaysKeepsWindows"), {"type": "boolean", "value": "1"})
            self.assertNotIn("macos.keep_windows", installer.load_manifest()["settings"])

    def test_failed_migration_keeps_the_entry_and_install_continues(self):
        with fake_system() as fs:
            fs.set_default(GLOBAL, "NSQuitAlwaysKeepsWindows", "boolean", "1")
            self.legacy_manifest()
            with mock.patch.object(LegacyKeepWindowsOnQuit, "revert", side_effect=RuntimeError("defaults broke")):
                results = installer.install([ITermKeepWindows()])
            self.assertEqual([(r.id, r.status) for r in results],
                             [("macos.keep_windows", "failed"), ("iterm.keep_windows", "changed")])
            self.assertIn("defaults broke", results[0].detail)
            self.assertIn("macos.keep_windows", installer.load_manifest()["settings"])
            # A retry migrates it.
            self.assertEqual([(r.id, r.status) for r in installer.install([ITermKeepWindows()])],
                             [("macos.keep_windows", "reverted"), ("iterm.keep_windows", "unchanged")])
            self.assertEqual(fs.get_default(GLOBAL, "NSQuitAlwaysKeepsWindows"), {"type": "boolean", "value": "0"})

    def test_uninstall_reverts_the_legacy_entry(self):
        with fake_system() as fs:
            fs.set_default(GLOBAL, "NSQuitAlwaysKeepsWindows", "boolean", "1")
            self.legacy_manifest(existed=True, value="0")
            settings = [ITermKeepWindows(), ShellRcBlock("zsh")]
            plan = installer.uninstall(settings, dry_run=True)
            self.assertEqual([(r.id, r.status) for r in plan],
                             [("shell.zsh", "unchanged"), ("iterm.keep_windows", "skipped"),
                              ("macos.keep_windows", "would-revert"), ("files", "would-revert")])
            self.assertEqual(fs.get_default(GLOBAL, "NSQuitAlwaysKeepsWindows"), {"type": "boolean", "value": "1"})
            results = installer.uninstall(settings)
            self.assertEqual([(r.id, r.status) for r in results],
                             [("shell.zsh", "unchanged"), ("iterm.keep_windows", "skipped"),
                              ("macos.keep_windows", "reverted"), ("files", "reverted")])
            self.assertEqual(fs.get_default(GLOBAL, "NSQuitAlwaysKeepsWindows"), {"type": "boolean", "value": "0"})
            self.assertFalse(paths.manifest_path().exists())

    def test_uninstall_leaves_an_unchanged_legacy_value(self):
        with fake_system() as fs:
            fs.set_default(GLOBAL, "NSQuitAlwaysKeepsWindows", "boolean", "1")
            self.legacy_manifest(status="unchanged")
            results = installer.uninstall([ITermKeepWindows()])
            self.assertEqual([(r.id, r.status) for r in results],
                             [("iterm.keep_windows", "skipped"), ("macos.keep_windows", "unchanged"),
                              ("files", "reverted")])
            self.assertEqual(fs.get_default(GLOBAL, "NSQuitAlwaysKeepsWindows"), {"type": "boolean", "value": "1"})

    def test_failed_legacy_revert_on_uninstall_keeps_the_entry(self):
        with fake_system():
            self.legacy_manifest()
            with mock.patch.object(LegacyKeepWindowsOnQuit, "revert", side_effect=RuntimeError("nope")):
                results = installer.uninstall([ITermKeepWindows()])
            self.assertEqual([(r.id, r.status) for r in results],
                             [("iterm.keep_windows", "skipped"), ("macos.keep_windows", "failed"),
                              ("files", "skipped")])
            self.assertEqual(set(installer.load_manifest()["settings"]), {"macos.keep_windows"})


class FullInstallTest(unittest.TestCase):
    """The real settings, in order, against the fakes: install, dry runs and uninstall leave no trace."""

    def test_install_and_uninstall_round_trip(self):
        with fake_system() as fs:
            make_marketplace()
            os.environ["SHELL"] = "/bin/zsh"
            rc = fs.home / ".zshrc"
            rc.write_text("export A=1\n")

            plan = installer.install(dry_run=True)
            self.assertEqual([(r.id, r.status) for r in plan],
                             [("iterm.keep_windows", "would-change"), ("iterm.restore_policy", "unchanged"),
                              ("iterm.launch_at_login", "would-change"), ("shell.zsh", "would-change"),
                              ("shell.bash", "skipped"), ("claude.plugin", "would-change")])
            self.assertEqual(fs.claude_changes(), [])
            self.assertEqual([c for c in fs.calls("defaults") if c[0] in ("write", "delete")], [])
            self.assertFalse(paths.state_dir().exists())

            results = installer.install()
            self.assertEqual([(r.id, r.status) for r in results],
                             [("iterm.keep_windows", "changed"), ("iterm.restore_policy", "unchanged"),
                              ("iterm.launch_at_login", "changed"), ("shell.zsh", "changed"),
                              ("shell.bash", "skipped"), ("claude.plugin", "changed")])
            self.assertEqual([(r.id, r.status) for r in installer.install(dry_run=True)],
                             [("iterm.keep_windows", "unchanged"), ("iterm.restore_policy", "unchanged"),
                              ("iterm.launch_at_login", "unchanged"), ("shell.zsh", "unchanged"),
                              ("shell.bash", "skipped"), ("claude.plugin", "unchanged")])

            plan = installer.uninstall(dry_run=True)
            self.assertEqual([(r.id, r.status) for r in plan],
                             [("claude.plugin", "would-revert"), ("shell.bash", "skipped"),
                              ("shell.zsh", "would-revert"), ("iterm.launch_at_login", "would-revert"),
                              ("iterm.restore_policy", "unchanged"), ("iterm.keep_windows", "would-revert"),
                              ("files", "would-revert")])
            results = installer.uninstall()
            self.assertEqual([(r.id, r.status) for r in results],
                             [("claude.plugin", "reverted"), ("shell.bash", "skipped"), ("shell.zsh", "reverted"),
                              ("iterm.launch_at_login", "reverted"), ("iterm.restore_policy", "unchanged"),
                              ("iterm.keep_windows", "reverted"), ("files", "reverted")])
            self.assertEqual(rc.read_text(), "export A=1\n")
            self.assertEqual(fs.defaults(), {})
            self.assertEqual(fs.claude_db(), {"marketplaces": {}, "plugins": {}})
            self.assertFalse((fs.home / "Library" / "LaunchAgents" / f"{LABEL}.plist").exists())


if __name__ == "__main__":
    unittest.main()
