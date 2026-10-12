"""Process inspection through `ps`, behind a small class that tests replace with a fake."""
import os
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    stat: str     # e.g. "S+"; "+" means the foreground process group of its terminal
    tty: str      # e.g. "ttys029"; "??" when it has no terminal
    started: str  # `ps -o lstart=`, whitespace-normalised, e.g. "Mon Oct 12 11:40:02 2026"
    comm: str     # as ps reports it: "claude", "-zsh", "/bin/zsh", "node"
    args: str     # full command line

    @property
    def name(self) -> str:
        """Executable basename without the login-shell dash: "-zsh" -> "zsh", "/bin/zsh" -> "zsh"."""
        return os.path.basename(self.comm.lstrip("-"))

    @property
    def has_tty(self) -> bool:
        return self.tty not in ("", "?", "??")

    @property
    def foreground(self) -> bool:
        return "+" in self.stat


def _run_ps(argv: List[str]) -> str:
    try:
        result = subprocess.run(["ps", *argv], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def walk_chain(pid: int, lookup: Callable[[int], Optional[ProcInfo]]) -> List[ProcInfo]:
    """The parent chain from `pid` upward, self first, via `lookup(pid) -> ProcInfo or None`. Stops before
    pid 1 (launchd), at a process that cannot be looked up, and at any pid already seen (a cycle)."""
    chain: List[ProcInfo] = []
    seen = set()
    current = lookup(pid)
    while current is not None and current.pid > 1 and current.pid not in seen:
        chain.append(current)
        seen.add(current.pid)
        current = lookup(current.ppid) if current.ppid > 1 else None
    return chain


class Procs:
    def __init__(self, run: Optional[Callable[[List[str]], str]] = None):
        self._run = run or _run_ps

    def get(self, pid: int) -> Optional[ProcInfo]:
        line = self._run(["-o", "pid=,ppid=,stat=,tty=,lstart=,comm=", "-p", str(pid)]).strip()
        parts = line.split(None, 9)  # lstart is five whitespace-separated fields
        if len(parts) < 10:
            return None
        try:
            pid_value, ppid_value = int(parts[0]), int(parts[1])
        except ValueError:
            return None
        args = self._run(["-ww", "-o", "args=", "-p", str(pid)]).strip()
        return ProcInfo(pid=pid_value, ppid=ppid_value, stat=parts[2], tty=parts[3],
                        started=" ".join(parts[4:9]), comm=parts[9], args=args)

    def ancestors(self, pid: int) -> List[ProcInfo]:
        """`pid` itself, then its parent, grandparent, ... stopping before pid 1 (launchd).

        One get() (two single-pid `ps` runs) per process, deliberately. Measured on macOS with ~1,000
        processes: a 6-deep chain this way took ~85-115 ms, while a `ps -A` snapshot took ~250 ms (its tty
        column does a device-name lookup per process) and any multi-pid `ps -p a,b,c` ~170-190 ms against
        6-9 ms for one pid. Avoiding `ps` altogether (sysctl/proc_pidinfo via ctypes) is the real speedup."""
        return walk_chain(pid, self.get)

    def is_alive(self, pid: Optional[int], started: Optional[str]) -> bool:
        """True only if `pid` exists and, when `started` is given, started then (a reused pid is a different process)."""
        if not pid:
            return False
        info = self.get(pid)
        return info is not None and (started is None or info.started == started)
