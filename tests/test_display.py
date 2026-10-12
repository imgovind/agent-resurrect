"""display.printable: the one sanitiser for text printed to a terminal."""
import json
import unittest

from agent_resurrect import doctor, restore
from agent_resurrect.display import printable


class PrintableTest(unittest.TestCase):
    def test_emoji_zwj_sequence_is_kept(self):
        family = "\U0001F468‍\U0001F469‍\U0001F467‍\U0001F466"  # man, woman, girl, boy joined
        self.assertEqual(printable(f"fix {family} bug"), f"fix {family} bug")

    def test_zwnj_variation_selectors_and_ordinary_text_are_kept(self):
        for text in ["می‌خواهم",  # Persian with ZWNJ
                     "❤️ red heart", "☺︎ text style", "café 日本語 /a b/c",
                     "no break space and en dash – kept"]:
            self.assertEqual(printable(text), text)

    def test_bidi_controls_are_replaced(self):
        self.assertEqual(printable("invoice‮txt.exe"), "invoice?txt.exe")
        for ch in "‎‏‪‫‬‭‮⁦⁧⁨⁩":
            self.assertEqual(printable(f"a{ch}b"), "a?b", repr(ch))

    def test_osc_52_clipboard_write_is_neutralised(self):
        out = printable("\x1b]52;c;SGVsbG8=\x07")
        self.assertEqual(out, "?]52;c;SGVsbG8=?")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)

    def test_c0_del_and_c1_controls_are_replaced_one_for_one(self):
        self.assertEqual(printable("a\x9b31mb"), "a?31mb")  # one-byte CSI
        self.assertEqual(printable("".join(map(chr, range(0x20)))), "?" * 0x20)
        self.assertEqual(printable("\x7f" + "".join(map(chr, range(0x80, 0xA0)))), "?" * 33)
        self.assertEqual(len(printable("a\tb\nc\r")), 6)

    def test_lone_surrogates_are_replaced(self):
        # json.loads turns a transcript's "\\ud83d" into a lone surrogate, which print() cannot encode.
        title = json.loads('"cut emoji \\ud83d"')
        self.assertEqual(printable(title), "cut emoji ?")
        printable(title).encode("utf-8")

    def test_accepts_any_object(self):
        self.assertEqual(printable(42), "42")
        self.assertEqual(printable(ValueError("bad\x1b[2J")), "bad?[2J")

    def test_restore_and_doctor_share_it(self):
        self.assertIs(restore.printable, printable)
        self.assertIs(doctor.printable, printable)


if __name__ == "__main__":
    unittest.main()
