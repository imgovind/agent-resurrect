"""Setting registry: everything the installer applies, in order."""
from typing import List

from .base import Check, Setting


def all_settings() -> List[Setting]:
    return []


__all__ = ["Check", "Setting", "all_settings"]
