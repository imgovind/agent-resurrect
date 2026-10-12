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

from agent_resurrect import paths
from agent_resurrect.settings import all_settings
from agent_resurrect.settings.base import Check, Setting
from agent_resurrect.settings.claude_plugin import MARKETPLACE, PLUGIN_ID, ClaudePlugin
from agent_resurrect.settings.iterm import LABEL, ITermLaunchAtLogin, ITermRestorePolicy
from agent_resurrect.settings.macos import KeepWindowsOnQuit
from agent_resurrect.settings.shellrc import BEGIN, END, ShellRcBlock
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
                                    "plugins": [{"name": "agent-resurrect", "source": "./plugin"}]}))


class DefaultsSettingTest(unittest.TestCase):
    def test_keep_windows_apply_check_revert_absent_key(self):
        with fake_system() as fs:
            setting = KeepWindowsOnQuit()
            self.assertEqual(setting.id, "macos.keep_windows")
            self.assertFalse(setting.check().ok)
            original = setting.apply()
            self.assertEqual(original["keys"]["NSQuitAlwaysKeepsWindows"]["existed"], False)
            self.assertEqual(fs.get_default(GLOBAL, "NSQuitAlwaysKeepsWindows"), {"type": "boolean", "value": "1"})
            self.assertTrue(setting.check().ok)
            setting.revert(original)
            self.assertIsNone(fs.get_default(GLOBAL, "NSQuitAlwaysKeepsWindows"))
            self.assertFalse(setting.check().ok)

    def test_keep_windows_already_true_returns_none(self):
        with fake_system() as fs:
            for type_, value in (("boolean", "1"), ("integer", "1"), ("string", "YES")):
                fs.set_default(GLOBAL, "NSQuitAlwaysKeepsWindows", type_, value)
                self.assertIsNone(KeepWindowsOnQuit().apply())
                self.assertTrue(KeepWindowsOnQuit().check().ok)
                self.assertEqual(fs.get_default(GLOBAL, "NSQuitAlwaysKeepsWindows"), {"type": type_, "value": value})
            self.assertEqual([c for c in fs.calls("defaults") if c[0] in ("write", "delete")], [])

    def test_keep_windows_revert_restores_false(self):
        with fake_system() as fs:
            for type_ in ("boolean", "integer"):
                fs.set_default(GLOBAL, "NSQuitAlwaysKeepsWindows", type_, "0")
                setting = KeepWindowsOnQuit()
                original = setting.apply()
                self.assertEqual(original["keys"]["NSQuitAlwaysKeepsWindows"],
                                 {"domain": GLOBAL, "existed": True, "type": type_, "value": "0"})
                self.assertEqual(fs.get_default(GLOBAL, "NSQuitAlwaysKeepsWindows"), {"type": "boolean", "value": "1"})
                setting.revert(original)
                self.assertEqual(fs.get_default(GLOBAL, "NSQuitAlwaysKeepsWindows"), {"type": type_, "value": "0"})

    def test_keep_windows_refuses_a_value_it_could_not_restore(self):
        with fake_system() as fs:
            fs.set_default(GLOBAL, "NSQuitAlwaysKeepsWindows", "array", "(\n)")
            with self.assertRaises(RuntimeError):
                KeepWindowsOnQuit().apply()
            self.assertEqual(fs.get_default(GLOBAL, "NSQuitAlwaysKeepsWindows"), {"type": "array", "value": "(\n)"})

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
    def test_launch_agent_plist_and_bootstrap(self):
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
            self.assertEqual(fs.calls("launchctl"), [["bootstrap", f"gui/{uid}", str(plist)]])

            self.assertIsNone(setting.apply())  # idempotent: no rewrite, no second bootstrap
            self.assertEqual(len(fs.calls("launchctl")), 1)

            setting.revert(original)
            self.assertEqual(fs.calls("launchctl")[-1], ["bootout", f"gui/{uid}/{LABEL}"])
            self.assertFalse(plist.exists())
            self.assertFalse(setting.check().ok)

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
            self.assertIn('case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac',
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
            setting = ShellRcBlock("zsh")
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

    def test_claude_plugin_without_claude_is_skipped(self):
        with fake_system(claude=False) as fs:
            setting = ClaudePlugin()
            self.assertIsNone(setting.apply())
            check = setting.check()
            self.assertFalse(check.ok)
            self.assertIn("install Claude Code, then run: agent-resurrect install", setting.fix_hint)
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
        self.assertEqual(ids, ["macos.keep_windows", "t.one", "t.two", "shell.zsh", "shell.bash", "claude.plugin"])

    def test_descriptions_and_fix_hints_are_plain(self):
        with fake_system():
            settings = [KeepWindowsOnQuit(), ITermRestorePolicy(), ITermLaunchAtLogin(), ShellRcBlock("zsh"),
                        ShellRcBlock("bash"), ClaudePlugin()]
            for setting in settings:
                for text in (setting.description, setting.fix_hint):
                    self.assertTrue(text, setting.id)
                    self.assertNotIn("\u2014", text)


if __name__ == "__main__":
    unittest.main()
