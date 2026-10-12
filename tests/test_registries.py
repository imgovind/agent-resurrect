"""The adapter registries and the setting order the installer applies (and uninstall reverses)."""
import subprocess
import sys
import unittest
from pathlib import Path

from agent_resurrect.agents import all_agents, get_agent
from agent_resurrect.agents.claude import ClaudeAdapter
from agent_resurrect.settings import all_settings
from agent_resurrect.terminals import all_terminals, detect_terminal
from agent_resurrect.terminals.iterm import ITermAdapter
from tests.helpers import temp_home

SRC = Path(__file__).resolve().parent.parent / "src"
PANE = "w0t12p0:10FE02F5-C525-405E-9E86-C8255ED17032"


class RegistryTest(unittest.TestCase):
    def test_agents_registry_is_claude(self):
        self.assertEqual([type(agent) for agent in all_agents()], [ClaudeAdapter])
        self.assertIsInstance(get_agent("claude"), ClaudeAdapter)
        self.assertIsNone(get_agent("codex"))

    def test_terminals_registry_is_iterm(self):
        self.assertEqual([type(terminal) for terminal in all_terminals()], [ITermAdapter])
        self.assertIsInstance(detect_terminal({"ITERM_SESSION_ID": PANE}), ITermAdapter)
        self.assertIsNone(detect_terminal({}))
        self.assertIsNone(detect_terminal({"ITERM_SESSION_ID": "w0t0p0:not-a-guid"}))

    def test_all_settings_ids_in_order(self):
        with temp_home():
            self.assertEqual([setting.id for setting in all_settings()],
                             ["macos.keep_windows", "iterm.restore_policy", "iterm.launch_at_login",
                              "shell.zsh", "shell.bash", "claude.plugin"])

    def test_iterm_adapter_settings(self):
        self.assertEqual([setting.id for setting in ITermAdapter().settings()],
                         ["iterm.restore_policy", "iterm.launch_at_login"])

    def test_registries_import_adapters_lazily(self):
        # The hook imports the registries on every call; the adapters (and the settings code) load only when used.
        code = ("import sys; import agent_resurrect.agents, agent_resurrect.terminals; "
                "print(sorted(m for m in sys.modules if m.startswith('agent_resurrect.')))")
        result = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(SRC)!r}); {code}"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        for module in ("agent_resurrect.agents.claude", "agent_resurrect.terminals.iterm",
                       "agent_resurrect.settings", "agent_resurrect.finder"):
            self.assertNotIn(f"'{module}'", result.stdout)


if __name__ == "__main__":
    unittest.main()
