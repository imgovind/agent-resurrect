"""Tests for the iTerm2 terminal adapter: pane key, label and detection."""
import unittest

from agent_resurrect import store
from agent_resurrect.terminals.iterm import ITermAdapter

GUID = "10FE02F5-C525-405E-9E86-C8255ED17032"
SESSION = f"w0t12p0:{GUID}"


def iterm(session=SESSION, **extra):
    """The environment of a shell that iTerm2 started directly in a pane, plus `extra`."""
    env = {"TERM_PROGRAM": "iTerm.app", "ITERM_SESSION_ID": session}
    env.update(extra)
    return env


# Environments that inherit an iTerm2 pane's ITERM_SESSION_ID without being that pane's own shell. Every
# shell inside one of them would otherwise share the pane's key and resume the same session (review C1).
INHERITED = {
    "tmux": iterm(TERM_PROGRAM="tmux", TMUX="/private/tmp/tmux-501/default,4242,0"),
    "tmux, TERM_PROGRAM passed through": iterm(TMUX="/private/tmp/tmux-501/default,4242,0"),
    "tmux -CC (iTerm2 integration)": iterm(TERM_PROGRAM="tmux", TMUX="/tmp/tmux-501/default,1,2"),
    "screen": iterm(STY="4242.ttys003.host"),
    "zellij": iterm(ZELLIJ="0"),
    "vscode started with code . from a pane": iterm(TERM_PROGRAM="vscode"),
    "Terminal.app": iterm(TERM_PROGRAM="Apple_Terminal"),
    "no TERM_PROGRAM": {"ITERM_SESSION_ID": SESSION},
    "empty TERM_PROGRAM": iterm(TERM_PROGRAM=""),
    "TERM_PROGRAM differs in case": iterm(TERM_PROGRAM="iterm.app"),
}


class ITermAdapterTest(unittest.TestCase):
    def setUp(self):
        self.adapter = ITermAdapter()

    def test_identity(self):
        self.assertEqual(self.adapter.name, "iterm")
        self.assertTrue(self.adapter.restores_in_place)

    def test_pane_key_and_label(self):
        env = iterm()
        self.assertEqual(self.adapter.pane_key(env), f"iterm-{GUID}")
        self.assertEqual(self.adapter.pane_label(env), "w0t12p0")
        self.assertTrue(self.adapter.detect(env))

    def test_case_is_preserved(self):
        env = iterm(f"w0t0p0:{GUID.lower()}")
        self.assertEqual(self.adapter.pane_key(env), f"iterm-{GUID.lower()}")

    def test_rejects_malformed(self):
        for value in ["", "w0t0p0", "w0t0p0:", "w0t0p0:../../etc", "w0t0p0:not-a-guid",
                      f"w0t0p0:{GUID}\n", f"w0t0p0:{GUID}:x", f"w0t0p0: {GUID}", f"{GUID}"]:
            env = iterm(value)
            self.assertIsNone(self.adapter.pane_key(env), repr(value))
            self.assertFalse(self.adapter.detect(env), repr(value))

    def test_missing_variable(self):
        for env in ({}, {"TERM_PROGRAM": "iTerm.app"}):
            self.assertIsNone(self.adapter.pane_key(env))
            self.assertIsNone(self.adapter.pane_label(env))
            self.assertFalse(self.adapter.detect(env))

    def test_label_needs_a_separator(self):
        self.assertIsNone(self.adapter.pane_label(iterm("w0t0p0")))
        self.assertIsNone(self.adapter.pane_label(iterm(f":{GUID}")))

    def test_key_is_a_valid_store_key(self):
        key = self.adapter.pane_key(iterm())
        self.assertTrue(store.valid_pane_key(key))
        self.assertEqual(store.record_path(key).name, f"{key}.json")

    def test_only_a_direct_iterm_child_has_a_pane(self):
        for name, env in INHERITED.items():
            with self.subTest(name):
                self.assertIsNone(self.adapter.pane_key(env))
                self.assertFalse(self.adapter.detect(env))

    def test_empty_multiplexer_variables_count_as_unset(self):
        # The shell snippets test `-z "$TMUX$STY$ZELLIJ"`, so an exported empty value means "not inside".
        env = iterm(TMUX="", STY="", ZELLIJ="")
        self.assertEqual(self.adapter.pane_key(env), f"iterm-{GUID}")
        self.assertTrue(self.adapter.detect(env))


if __name__ == "__main__":
    unittest.main()
