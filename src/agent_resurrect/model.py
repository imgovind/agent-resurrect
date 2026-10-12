"""Shared data types. A Record is the only state passed from the recorder to the restorer."""
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Dict, Optional

SCHEMA = 1
STATUS_LIVE = "live"
STATUS_CONSUMED = "consumed"
STATUS_SKIPPED = "skipped"
STATUSES = (STATUS_LIVE, STATUS_CONSUMED, STATUS_SKIPPED)
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime(ISO_FORMAT)


def parse_iso(value: str) -> Optional[datetime]:
    """Parse a timestamp written by utc_now_iso(); None if it is not one."""
    try:
        return datetime.strptime(value, ISO_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


@dataclass
class Record:
    """What an agent session in a terminal pane needs in order to be resumed there later."""
    terminal: str
    pane_key: str
    agent: str
    session_id: str
    cwd: str
    pane_label: Optional[str] = None
    transcript_path: Optional[str] = None
    permission_mode: Optional[str] = None
    source: Optional[str] = None
    agent_pid: Optional[int] = None
    agent_started: Optional[str] = None
    # Environment variables the agent needs again on resume, e.g. {"CLAUDE_CONFIG_DIR": "..."}. Added in
    # 0.2.0 without a schema bump: older readers ignore unknown keys, newer readers default it to None.
    resume_env: Optional[Dict[str, str]] = None
    status: str = STATUS_LIVE
    recorded_at: str = ""
    updated_at: str = ""
    schema: int = SCHEMA

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Record":
        """Raise ValueError for another schema, a missing required field, or an unknown status."""
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise ValueError(f"unsupported record schema: {data.get('schema') if isinstance(data, dict) else data!r}")
        known = {f.name for f in fields(cls)}
        try:
            record = cls(**{k: v for k, v in data.items() if k in known})
        except TypeError as exc:
            raise ValueError(str(exc)) from None
        if record.status not in STATUSES:
            raise ValueError(f"unknown record status: {record.status!r}")
        return record


@dataclass
class SessionInfo:
    """Human-facing description of a session, for listings and prompts."""
    title: Optional[str] = None
    first_prompt: Optional[str] = None
    started: Optional[str] = None        # ISO-8601 timestamp of the session's first entry
    last_active: Optional[float] = None  # epoch seconds of the session's last write


@dataclass
class Candidate:
    """A session found by text search."""
    session_id: str
    transcript_path: str
    cwd: Optional[str]
    cwd_trusted: bool
    hits: int
    total_probes: int
    roles: Dict[str, int] = field(default_factory=dict)
    info: SessionInfo = field(default_factory=SessionInfo)
