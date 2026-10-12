"""The `--json` envelope shared by every command.

Contract: exactly one JSON object on stdout, newline-terminated, ASCII-only (non-ASCII escaped), carrying
"schema" ("agent-resurrect.<command>/1"), "version", "ok" and "error" (null, or {"code", "message"} where
code is the exitcodes name). Within schema /1 fields are only ever added, never renamed or removed.
"""
import json
import sys
from datetime import datetime, timezone
from typing import Optional, TextIO

from . import __version__, exitcodes


def document(command: str, code: int = exitcodes.OK, message: Optional[str] = None, **fields) -> dict:
    doc = {
        "schema": f"agent-resurrect.{command}/1",
        "version": __version__,
        "ok": code == exitcodes.OK,
        "error": None if code == exitcodes.OK else {"code": exitcodes.name(code), "message": message or ""},
    }
    doc.update(fields)
    return doc


def emit(doc: dict, out: Optional[TextIO] = None) -> None:
    out = out if out is not None else sys.stdout
    out.write(json.dumps(doc, ensure_ascii=True) + "\n")
    out.flush()


def iso_from_epoch(seconds: Optional[float]) -> Optional[str]:
    """Epoch seconds to the ISO 8601 UTC form used in every document, e.g. "2026-10-12T10:54:16Z"."""
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
