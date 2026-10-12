"""Pane records (one JSON file per pane key, written atomically) and the restore event log."""
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional

from . import logs, paths
from .model import STATUS_CONSUMED, STATUS_LIVE, Record, parse_iso, utc_now_iso

# Pane keys are derived from environment variables, so they are allow-listed: a key can never
# name a path outside panes_dir(). Matched with fullmatch: "$" would also accept a trailing newline.
PANE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _private_dir(directory: Path) -> None:
    """Create a state directory readable only by the user: records hold folder paths and session ids."""
    directory.mkdir(parents=True, exist_ok=True)
    for path in {directory, paths.state_dir()}:
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def valid_pane_key(pane_key: str) -> bool:
    return isinstance(pane_key, str) and bool(PANE_KEY.fullmatch(pane_key)) and ".." not in pane_key


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
    _private_dir(path.parent)
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


def gc(max_age_days: int, now: Optional[datetime] = None,
       is_running: Optional[Callable[[Record], bool]] = None) -> List[str]:
    """Delete records not updated within max_age_days; return their pane keys.
    A record whose timestamp cannot be parsed is kept (never deleted on a guess), and so is any record
    whose agent `is_running` (a session can run for weeks without a new SessionStart)."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max_age_days)
    removed = []
    for record in list_records():
        updated = parse_iso(record.updated_at)
        if updated is None or updated >= cutoff:
            continue
        if is_running is not None and is_running(record):
            continue
        if delete(record.pane_key):
            removed.append(record.pane_key)
    return removed


def claim(pane_key: str) -> Optional[Record]:
    """Atomically take a live record for restoring and mark it consumed; None if it is absent, not live,
    or another process got there first. When several shells race on one record (tmux, duplicated panes),
    exactly one wins: the record file is renamed to a per-process claim file, and rename is atomic.
    The claim file has no .json suffix, so list_records() never sees it."""
    path = record_path(pane_key)
    claim_path = path.with_name(f".claim-{pane_key}-{os.getpid()}")
    try:
        os.rename(path, claim_path)
    except FileNotFoundError:
        return None
    try:
        with open(claim_path) as fh:
            record = Record.from_dict(json.load(fh))
        if record.status != STATUS_LIVE:
            os.rename(claim_path, path)
            return None
        record.status = STATUS_CONSUMED
        save(record)
    except BaseException as exc:
        if claim_path.exists():
            os.replace(claim_path, path)  # put the record back exactly as it was
        if isinstance(exc, (OSError, ValueError)):
            logs.log(f"store: could not claim {pane_key!r}: {exc}")
            return None
        raise
    os.unlink(claim_path)
    return record


def append_restore_event(pane_key: str, result: str, detail: str = "") -> None:
    """One JSON line per automatic-restore decision; doctor summarises these."""
    try:
        path = paths.restore_events_path()
        _private_dir(path.parent)
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
