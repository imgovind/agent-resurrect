"""Agent adapter interface: record sessions from hook events and resume them."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Mapping, Optional

from ..config import Config
from ..model import Candidate, Record, SessionInfo
from ..procs import Procs
from ..terminals.base import TerminalAdapter


@dataclass
class HookContext:
    env: Mapping[str, str]
    terminal: TerminalAdapter
    pane_key: str
    pane_label: Optional[str]
    procs: Procs
    pid: int  # the hook process's own pid; the agent process is found among its ancestors


class AgentAdapter(ABC):
    name: str = ""

    @abstractmethod
    def on_hook(self, event: str, payload: dict, ctx: HookContext) -> None:
        """Create, update or delete the pane's record for one hook event. Must not raise on bad input."""

    @abstractmethod
    def session_exists(self, record: Record) -> bool:
        """True if the agent can still resume this session (e.g. its transcript is on disk)."""

    @abstractmethod
    def resume_argv(self, record: Record, config: Config) -> List[str]:
        """The command that resumes the session, e.g. ["claude", "--resume", id, ...]."""

    @abstractmethod
    def describe(self, record: Record) -> SessionInfo:
        """Title, first prompt and timestamps for display."""

    @abstractmethod
    def session_record(self, session_id: str) -> Optional[Record]:
        """A Record rebuilt from the agent's own session store (terminal and pane_key empty, never saved),
        used to resume a session that has no pane record. None if the session is unknown."""

    @abstractmethod
    def find_by_text(self, text: str, exclude: Optional[str] = None, limit: int = 5) -> List[Candidate]:
        """Sessions whose history contains the text, best match first."""

    def adopt(self, ctx: HookContext) -> Optional[Record]:
        """Record the agent session this process is running inside (a tool call of that session), for
        sessions started before agent-resurrect was installed. Returns the saved record, or None when not
        inside a recordable session. Default: unsupported."""
        return None
