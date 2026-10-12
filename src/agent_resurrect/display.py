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
#   U+200E U+200F U+202A-U+202E U+2066-U+2069  bidi marks, embeddings, overrides and isolates
#   U+D800-U+DFFF  lone surrogates (json.loads makes them from a "\ud83d" escape; print() cannot encode them)
# Everything else is kept, including ZWJ U+200D and ZWNJ U+200C (emoji sequences, Persian, Indic scripts)
# and variation selectors (emoji or text presentation).
_UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩\ud800-\udfff]")


def printable(text: object) -> str:
    """`text` (any object, via str) with every terminal control, bidi control and lone surrogate as "?"."""
    return _UNSAFE.sub("?", str(text))
