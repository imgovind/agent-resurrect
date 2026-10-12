"""Tests for the iTerm2 terminal adapter: pane key, label and detection."""
import unittest

from agent_resurrect import store
from agent_resurrect.terminals.iterm import ITermAdapter

GUID = "10FE02F5-C525-405E-9E86-C8255ED17032"


class ITermAdapterTest(unittest.TestCase):
    def setUp(self):
        self.adapter = ITermAdapter()

    def test_identity(self):
        self.assertEqual(self.adapter.name, "iterm")
        self.assertTrue(self.adapter.restores_in_place)

    def test_pane_key_and_label(self):
        env = {"ITERM_SESSION_ID": f"w0t12p0:{GUID}"}
        self.assertEqual(self.adapter.pane_key(env), f"iterm-{GUID}")
        self.assertEqual(self.adapter.pane_label(env), "w0t12p0")
        self.assertTrue(self.adapter.detect(env))

    def test_case_is_preserved(self):
        env = {"ITERM_SESSION_ID": f"w0t0p0:{GUID.lower()}"}
        self.assertEqual(self.adapter.pane_key(env), f"iterm-{GUID.lower()}")

    def test_rejects_malformed(self):
        for value in ["", "w0t0p0", "w0t0p0:", "w0t0p0:../../etc", "w0t0p0:not-a-guid",
                      f"w0t0p0:{GUID}\n", f"w0t0p0:{GUID}:x", f"w0t0p0: {GUID}", f"{GUID}"]:
            env = {"ITERM_SESSION_ID": value}
            self.assertIsNone(self.adapter.pane_key(env), repr(value))
            self.assertFalse(self.adapter.detect(env), repr(value))

    def test_missing_variable(self):
        self.assertIsNone(self.adapter.pane_key({}))
        self.assertIsNone(self.adapter.pane_label({}))
        self.assertFalse(self.adapter.detect({}))

    def test_label_needs_a_separator(self):
        self.assertIsNone(self.adapter.pane_label({"ITERM_SESSION_ID": "w0t0p0"}))
        self.assertIsNone(self.adapter.pane_label({"ITERM_SESSION_ID": f":{GUID}"}))

    def test_key_is_a_valid_store_key(self):
        key = self.adapter.pane_key({"ITERM_SESSION_ID": f"w0t12p0:{GUID}"})
        self.assertTrue(store.valid_pane_key(key))
        self.assertEqual(store.record_path(key).name, f"{key}.json")


if __name__ == "__main__":
    unittest.main()
