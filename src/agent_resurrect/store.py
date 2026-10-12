"""Pane records (one JSON file per pane key, written atomically) and the restore event log."""
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from . import logs, paths
from .model import Record, parse_iso, utc_now_iso

# Pane keys are derived from environment variables, so they are allow-listed: a key can never
# name a path outside panes_dir().
PANE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def valid_pane_key(pane_key: str) -> bool:
    return isinstance(pane_key, str) and bool(PANE_KEY.match(pane_key)) and ".." not in pane_key


def record_path(pane_key: str) -> Path:
    if not valid_pane_key(pane_key):
        raise ValueError(f"invalid pane key: {pane_key!r}")
    return paths.panes_dir() / f"{pane_key}.json"


def load(pane_key: str) -> Optional[Record]:
    """The pane's record, or None if absent. Unreadable records are logged and left on disk."""
    try:
        with open(record_path(pane_key)) as fh:
            return Record.from_dict(json.load(fh))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
        logs.log(f"store: ignoring unreadable record {pane_key!r}: {exc}")
        return None


def save(record: Record) -> Record:
    """Write atomically (temp file + rename), stamping recorded_at (first save) and updated_at."""
    now = utc_now_iso()
    record.recorded_at = record.recorded_at or now
    record.updated_at = now
    path = record_path(record.pane_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(record.to_dict(), fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return record


def delete(pane_key: str) -> bool:
    try:
        record_path(pane_key).unlink()
        return True
    except FileNotFoundError:
        return False


def set_status(pane_key: str, status: str) -> Optional[Record]:
    record = load(pane_key)
    if record is None:
        return None
    record.status = status
    return save(record)


def list_records() -> List[Record]:
    """Every readable record, most recently updated first."""
    directory = paths.panes_dir()
    if not directory.is_dir():
        return []
    records = [r for r in (load(p.stem) for p in directory.glob("*.json")) if r is not None]
    records.sort(key=lambda r: r.updated_at, reverse=True)
    return records


def gc(max_age_days: int, now: Optional[datetime] = None) -> List[str]:
    """Delete records not updated within max_age_days; return their pane keys.
    A record whose timestamp cannot be parsed is kept (never deleted on a guess)."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max_age_days)
    removed = []
    for record in list_records():
        updated = parse_iso(record.updated_at)
        if updated is not None and updated < cutoff and delete(record.pane_key):
            removed.append(record.pane_key)
    return removed


def append_restore_event(pane_key: str, result: str, detail: str = "") -> None:
    """One JSON line per automatic-restore decision; doctor summarises these."""
    try:
        path = paths.restore_events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps({"ts": utc_now_iso(), "pane_key": pane_key, "result": result, "detail": detail}) + "\n")
    except OSError as exc:
        logs.log(f"store: cannot append restore event: {exc}")


def read_restore_events() -> List[dict]:
    try:
        with open(paths.restore_events_path()) as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return []
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events
