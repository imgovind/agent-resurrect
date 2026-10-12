"""Text that is safe to print to a terminal.

Titles, prompts, paths, ids and error messages come from transcripts, hook payloads, records and the
environment. Printed raw, a control character could drive the terminal: clear or rewrite the screen, set
the window title, or write the clipboard (OSC 52). A bidi control could make a path or command read
differently from what it is.
"""
import re

# Each match becomes "?", one for one, so columns still line up:
#   U+0000-U+001F  C0 controls (ESC, BEL, newline, tab, ...)
#   U+007F         DEL
#   U+0080-U+009F  C1 controls (U+009B is a one-character CSI)
#   U+061C U+200E U+200F U+202A-U+202E U+2066-U+2069  bidi controls: the whole Unicode Bidi_Control set
#   U+2028 U+2029  line and paragraph separators (line breaks, like newline)
#   U+D800-U+DFFF  lone surrogates (json.loads makes them from a "\ud83d" escape; print() cannot encode them)
# Everything else is kept, including ZWJ U+200D and ZWNJ U+200C (emoji sequences, Persian, Indic scripts),
# variation selectors (emoji or text presentation), tag characters (subdivision flags) and private use
# (Nerd Font icons). Invisible format characters such as U+200B cannot drive a terminal, and the resume
# commands this program prints spell every such character out (restore.format_command), so they stay exact.
# The pattern is written with escapes only: this file must never contain a raw control character.
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\u2028\u2029\ud800-\udfff]")


def printable(text: object) -> str:
    """`text` (any object, via str) with every terminal control, bidi control, line separator and lone
    surrogate as "?"."""
    return _UNSAFE.sub("?", str(text))
