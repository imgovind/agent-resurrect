"""Setting interface: one system change the installer applies, doctor checks and uninstall reverts."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class Check:
    ok: bool
    detail: str  # one line, shown by doctor


class Setting(ABC):
    id: str = ""           # stable key in the install manifest, e.g. "macos.keep_windows"
    description: str = ""  # one sentence for the install summary
    fix_hint: str = ""     # what doctor tells the user when check() fails

    @abstractmethod
    def check(self) -> Check:
        """Is the system in the desired state?"""

    @abstractmethod
    def apply(self) -> Optional[dict]:
        """Bring the system to the desired state. Return the original state (JSON-serialisable) for
        revert(), or None if it was already in the desired state and nothing changed. Idempotent."""

    @abstractmethod
    def revert(self, original: dict) -> None:
        """Restore exactly the state apply() returned."""
