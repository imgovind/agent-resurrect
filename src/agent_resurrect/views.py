"""JSON views: the Session and Pane objects, and the other pieces of the --json documents (docs/json/).

Pure functions from records, the agent's description of a session and its resume plan to plain dicts. Text
from transcripts is returned raw: jsonout prints with ensure_ascii, so control characters arrive escaped.
Times are ISO 8601 UTC with whole seconds ("2026-10-12T10:54:16Z"); unknown values are None (JSON null).
"""
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Mapping, Optional, Sequence

from . import jsonout, logs, restore
from .agents.base import AgentAdapter
from .config import Config
from .model import Candidate, Record, SessionInfo

# Flags with which a resume command starts the agent with its permission checks off. Reported as
# resume.skips_permissions so a reader can warn before running the command.
SKIP_PERMISSION_FLAGS = ("--dangerously-skip-permissions",)

# Role names the finders use -> the stable keys of a find match's "roles" object. Anything else is "other".
ROLES = ("user", "assistant", "tool_output", "injected_context", "other")
_ROLE_KEYS = {"you": "user", "user": "user", "Claude": "assistant", "assistant": "assistant",
              "tool output": "tool_output", "tool_output": "tool_output",
              "injected context": "injected_context", "injected_context": "injected_context"}

_FRACTION = re.compile(r"(?<=:\d\d)\.\d+")


def iso(value: object) -> Optional[str]:
    """Epoch seconds, or an ISO 8601 timestamp with any offset and any fraction, as UTC whole seconds.
    A timestamp without an offset is taken as UTC. None for a missing or unreadable value."""
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return jsonout.iso_from_epoch(value)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    # Python 3.9's fromisoformat takes neither "Z" nor fractions other than 3 or 6 digits.
    text = _FRACTION.sub("", value.strip()).replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def describe(agent: Optional[AgentAdapter], record: Record) -> SessionInfo:
    """The agent's description of the session; empty when there is no adapter or it cannot be read."""
    if agent is None:
        return SessionInfo()
    try:
        return agent.describe(record)
    except Exception as exc:  # one unreadable transcript must not break a listing
        logs.log(f"describe {record.session_id}: {exc!r}")
        return SessionInfo()


def pane(record: Record) -> Optional[dict]:
    """The Pane object of a record, or None for a session that has no pane record."""
    if not record.pane_key:
        return None
    return {"terminal": record.terminal or None, "key": record.pane_key, "label": record.pane_label or None}


def resume(record: Record, agent: Optional[AgentAdapter], config: Config) -> Optional[dict]:
    """How to resume the session (the restore plan), or None when no adapter knows its agent."""
    if agent is None:
        return None
    plan = restore.plan_resume(record, agent, config)
    return {
        "argv": list(plan.argv),
        "cwd": plan.cwd,
        "env": dict(getattr(plan, "env", None) or {}),
        "command": restore.format_command(plan),
        "skips_permissions": any(flag in plan.argv for flag in SKIP_PERMISSION_FLAGS),
        "warning": plan.warning,
    }


def session(record: Record, agent: Optional[AgentAdapter], config: Config,
            info: Optional[SessionInfo] = None) -> dict:
    """The Session object. `info` defaults to the agent's description of the record."""
    info = info if info is not None else describe(agent, record)
    cwd = record.cwd or None
    return {
        "session_id": record.session_id,
        "agent": record.agent,
        "title": info.title,
        "first_prompt": info.first_prompt,
        "cwd": cwd,
        "cwd_exists": bool(cwd) and os.path.isdir(cwd),
        "transcript_path": record.transcript_path or None,
        "started_at": iso(info.started),
        "last_active_at": iso(info.last_active),
        "permission_mode": record.permission_mode,
        "resume": resume(record, agent, config),
    }


def entry(number: Optional[int], record: Record, agent: Optional[AgentAdapter], config: Config,
          info: SessionInfo, running: bool, eligible: bool, reason: str) -> dict:
    """One `ls` entry. `number` is None for a running entry, as in the text listing."""
    return {
        "number": number,
        "status": record.status,
        "running": running,
        "auto_restore": {"eligible": eligible, "reason": reason},
        "pane": pane(record),
        "agent_pid": record.agent_pid,
        "recorded_at": iso(record.recorded_at),
        "updated_at": iso(record.updated_at),
        "session": session(record, agent, config, info),
    }


def candidate_record(agent: AgentAdapter, found: Candidate) -> Record:
    """A record for a session found by text, which has no pane (never saved)."""
    return Record(terminal="", pane_key="", agent=agent.name, session_id=found.session_id,
                  cwd=found.cwd or "", transcript_path=found.transcript_path)


def roles(counts: Mapping[str, int]) -> Dict[str, int]:
    """Matching records per author, under the stable keys of ROLES."""
    out = dict.fromkeys(ROLES, 0)
    for role, count in counts.items():
        out[_ROLE_KEYS.get(role, "other")] += int(count)
    return out


def confidence(found: Sequence[Candidate]) -> str:
    """strong, weak (one probe of several matched), ambiguous (the runner-up has as many hits) or none.
    The same rules as the NOTE lines of `find`'s text output, checked in the same order."""
    if not found:
        return "none"
    best = found[0]
    if best.hits == 1 and best.total_probes > 1:
        return "weak"
    if len(found) > 1 and found[1].hits == best.hits:
        return "ambiguous"
    return "strong"


def match(agent: AgentAdapter, found: Candidate, config: Config) -> dict:
    """One `find` match."""
    by_role = roles(found.roles)
    return {
        "hits": found.hits,
        "total_probes": found.total_probes,
        "roles": by_role,
        "in_conversation": by_role["user"] + by_role["assistant"] > 0,
        "cwd_trusted": bool(found.cwd_trusted),
        "session": session(candidate_record(agent, found), agent, config, found.info),
    }


def step(result: object, description: str) -> dict:
    """One install or uninstall step (an installer.StepResult)."""
    return {"id": str(getattr(result, "id", "")), "status": str(getattr(result, "status", "")),
            "description": description or "", "detail": str(getattr(result, "detail", "") or "")}


def steps(results: List[object], descriptions: Mapping[str, str]) -> List[dict]:
    return [step(result, descriptions.get(getattr(result, "id", ""), "")) for result in results]
