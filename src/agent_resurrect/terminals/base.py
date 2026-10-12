"""Terminal adapter interface: identify the pane a shell or agent runs in, and declare required settings."""
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, List, Mapping, Optional

if TYPE_CHECKING:
    from ..settings.base import Setting


class TerminalAdapter(ABC):
    name: str = ""
    restores_in_place: bool = False

    @abstractmethod
    def detect(self, env: Mapping[str, str]) -> bool:
        """True if this environment belongs to a pane of this terminal."""

    @abstractmethod
    def pane_key(self, env: Mapping[str, str]) -> Optional[str]:
        """Stable identifier of the pane that survives a restart, e.g. "iterm-<GUID>"; None if unavailable.
        Must satisfy store.valid_pane_key()."""

    @abstractmethod
    def pane_label(self, env: Mapping[str, str]) -> Optional[str]:
        """Short display label, e.g. "w0t12p0". Display only, never used as a key."""

    def settings(self) -> List["Setting"]:
        """System settings this terminal needs for panes to be restored. Empty by default."""
        return []
