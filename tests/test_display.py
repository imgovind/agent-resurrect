"""display.printable: the one sanitiser for text printed to a terminal.

Every special character below is written as an escape, never raw, so this file reads the same in any viewer.
"""
import json
import subprocess
import unicodedata
import unittest
from pathlib import Path

from agent_resurrect import doctor, restore
from agent_resurrect.display import printable

REPO = Path(__file__).resolve().parent.parent
BIDI_CONTROLS = "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"


class PrintableTest(unittest.TestCase):
    def test_emoji_zwj_sequence_is_kept(self):
        family = "\U0001F468\u200d\U0001F469\u200d\U0001F467\u200d\U0001F466"  # man, woman, girl, boy joined
        self.assertEqual(printable(f"fix {family} bug"), f"fix {family} bug")

    def test_zwnj_variation_selectors_and_ordinary_text_are_kept(self):
        for text in ["\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",  # Persian with ZWNJ
                     "\u2764\ufe0f red heart", "\u263a\ufe0e text style", "caf\u00e9 \u65e5\u672c\u8a9e /a b/c",
                     "no\u00a0break space and en dash \u2013 kept"]:
            self.assertEqual(printable(text), text)

    def test_invisible_but_harmless_characters_are_kept(self):
        # Tag characters make subdivision flags (England); private use covers Nerd Font icons.
        england = "\U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F"
        for text in [england, "\ue0b0 prompt", "zero\u200bwidth", "\ufeffbom"]:
            self.assertEqual(printable(text), text)

    def test_bidi_controls_are_replaced(self):
        self.assertEqual(printable("invoice\u202etxt.exe"), "invoice?txt.exe")
        for ch in BIDI_CONTROLS:  # the whole Unicode Bidi_Control set, including U+061C ARABIC LETTER MARK
            self.assertEqual(printable(f"a{ch}b"), "a?b", repr(ch))

    def test_line_and_paragraph_separators_are_replaced(self):
        self.assertEqual(printable("one\u2028two\u2029three"), "one?two?three")

    def test_osc_52_clipboard_write_is_neutralised(self):
        out = printable("\x1b]52;c;SGVsbG8=\x07")
        self.assertEqual(out, "?]52;c;SGVsbG8=?")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)

    def test_c0_del_and_c1_controls_are_replaced_one_for_one(self):
        self.assertEqual(printable("a\x9b31mb"), "a?31mb")  # one-character CSI
        self.assertEqual(printable("".join(map(chr, range(0x20)))), "?" * 0x20)
        self.assertEqual(printable("\x7f" + "".join(map(chr, range(0x80, 0xA0)))), "?" * 33)
        self.assertEqual(printable("a\tb\nc\r"), "a?b?c?")

    def test_lone_surrogates_are_replaced(self):
        # json.loads turns a transcript's "\\ud83d" escape into a lone surrogate, which print() cannot encode.
        title = json.loads('"cut emoji \\ud83d"')
        self.assertEqual(printable(title), "cut emoji ?")
        printable(title).encode("utf-8")

    def test_accepts_any_object(self):
        self.assertEqual(printable(42), "42")
        self.assertEqual(printable(ValueError("bad\x1b[2J")), "bad?[2J")

    def test_restore_and_doctor_share_it(self):
        self.assertIs(restore.printable, printable)
        self.assertIs(doctor.printable, printable)


class SourceHygieneTest(unittest.TestCase):
    def test_no_tracked_file_contains_raw_control_or_bidi_characters(self):
        # A raw bidi control can make code or docs read differently from what they do (Trojan Source).
        try:
            listed = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, timeout=30)
        except OSError:
            self.skipTest("git is not available")
        if listed.returncode != 0:
            self.skipTest("not a git checkout")
        offenders = []
        for name in filter(None, listed.stdout.decode().split("\0")):
            try:
                text = (REPO / name).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                bad = sorted({f"U+{ord(ch):04X}" for ch in line
                              if ch in BIDI_CONTROLS or (unicodedata.category(ch) == "Cc" and ch != "\t")})
                if bad:
                    offenders.append(f"{name}:{number}: {', '.join(bad)}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
