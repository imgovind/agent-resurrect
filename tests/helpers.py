"""Shared test helpers: an isolated HOME, a fake process table, and fake adapters."""
import contextlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional
from unittest import mock

from agent_resurrect.agents.base import AgentAdapter, HookContext
from agent_resurrect.config import Config
from agent_resurrect.model import Candidate, Record, SessionInfo
from agent_resurrect.procs import ProcInfo, Procs
from agent_resurrect.terminals.base import TerminalAdapter

# Variables from the developer's own terminal or Claude session that must never leak into a test.
_SCRUB_PREFIXES = ("XDG_", "ITERM_", "CLAUDE_", "AGENT_RESURRECT_")
_SCRUB_NAMES = ("TERM_SESSION_ID", "TERM_PROGRAM", "LC_TERMINAL")


@contextlib.contextmanager
def temp_home():
    """Run with HOME pointing at a fresh temporary directory and no terminal/agent variables set."""
    tmp = Path(tempfile.mkdtemp(prefix="ar-home-")).resolve()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(_SCRUB_PREFIXES) and k not in _SCRUB_NAMES}
    env["HOME"] = str(tmp)
    with mock.patch.dict(os.environ, env, clear=True):
        try:
            yield tmp
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def proc(pid: int, ppid: int, comm: str, args: Optional[str] = None, stat: str = "S",
         tty: str = "ttys001", started: str = "Mon Oct 12 11:40:02 2026") -> ProcInfo:
    return ProcInfo(pid=pid, ppid=ppid, stat=stat, tty=tty, started=started, comm=comm,
                    args=comm if args is None else args)


class FakeProcs(Procs):
    """A fixed process table: FakeProcs(proc(...), proc(...))."""

    def __init__(self, *infos: ProcInfo):
        super().__init__(run=lambda argv: "")
        self.table: Dict[int, ProcInfo] = {p.pid: p for p in infos}

    def get(self, pid: int) -> Optional[ProcInfo]:
        return self.table.get(pid)


class FakeTerminal(TerminalAdapter):
    """Detects FAKE_PANE=<id> and keys the pane as "fake-<id>"."""
    name = "fake"
    restores_in_place = True

    def detect(self, env):
        return bool(env.get("FAKE_PANE"))

    def pane_key(self, env):
        return f"fake-{env['FAKE_PANE']}" if env.get("FAKE_PANE") else None

    def pane_label(self, env):
        return env.get("FAKE_PANE")


class FakeAgent(AgentAdapter):
    """Resumes with ["fake-agent", "--resume", <id>]; `exists` controls session_exists()."""
    name = "fake"

    def __init__(self, exists: bool = True):
        self.exists = exists
        self.hook_calls: List[tuple] = []

    def on_hook(self, event: str, payload: dict, ctx: HookContext) -> None:
        self.hook_calls.append((event, payload, ctx))

    def session_exists(self, record: Record) -> bool:
        return self.exists

    def resume_argv(self, record: Record, config: Config) -> List[str]:
        return ["fake-agent", "--resume", record.session_id]

    def describe(self, record: Record) -> SessionInfo:
        return SessionInfo(title=f"title of {record.session_id}", first_prompt="hello")

    def session_record(self, session_id: str) -> Optional[Record]:
        return None

    def find_by_text(self, text: str, exclude: Optional[str] = None, limit: int = 5) -> List[Candidate]:
        return []


def make_record(**overrides) -> Record:
    values = dict(terminal="fake", pane_key="fake-p1", agent="fake", session_id="s-1", cwd="/tmp",
                  pane_label="p1", agent_pid=4242, agent_started="Mon Oct 12 11:40:02 2026")
    values.update(overrides)
    return Record(**values)
