"""`agent-resurrect doctor`: check everything a restore depends on, one line per check, with a fix for each failure."""
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, TextIO

from . import paths, store
from .agents import get_agent
from .config import load_config
from .model import STATUS_LIVE, Record, parse_iso
from .procs import Procs
from .settings import all_settings
from .terminals import detect_terminal

# C0 controls, DEL and C1 controls (0x9b is a one-byte CSI). Printed raw, text from transcripts, records or the
# environment could drive the terminal: rewrite the screen, set the title, or write the clipboard (OSC 52).
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def printable(value: object) -> str:
    """`value` as one line that is safe to print: each control character becomes "?" (1:1, so columns align)."""
    return _CONTROL.sub("?", str(value))


# `sysctl -n kern.boottime` prints "{ sec = 1791785041, usec = 851234 } Mon Oct 12 11:34:01 2026".
# Anchor on "{ sec =": a bare "sec =" also matches inside "usec =".
BOOT_TIME = re.compile(r"\{ sec = (\d+),")


def parse_boot_time(text: str) -> Optional[int]:
    match = BOOT_TIME.search(text or "")
    return int(match.group(1)) if match else None


def _run_sysctl(argv: List[str]) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def read_boot_time(run: Optional[Callable[[List[str]], str]] = None) -> Optional[int]:
    """Epoch seconds of the last boot, or None if it cannot be read."""
    return parse_boot_time((run or _run_sysctl)(["sysctl", "-n", "kern.boottime"]))


class _Report:
    def __init__(self, out: TextIO):
        self.out = out
        self.failed = False

    def ok(self, text: str) -> None:
        self._line("OK", text)

    def warn(self, text: str) -> None:
        self._line("WARN", text)

    def fail(self, text: str, fix: str) -> None:
        self.failed = True
        self._line("FAIL", text)
        if fix:
            print(f"      fix: {printable(fix)}", file=self.out)

    def _line(self, level: str, text: str) -> None:
        print(f"{level:<6}{printable(text)}", file=self.out)


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _check_python(report: _Report) -> None:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info >= (3, 9):
        report.ok(f"python {version} ({sys.executable})")
    else:
        report.fail(f"python {version} is too old; 3.9 or newer is required",
                    "install Apple's command line tools with: xcode-select --install")


def _check_launcher(report: _Report) -> None:
    found = shutil.which("agent-resurrect")
    if found:
        report.ok(f"agent-resurrect is on PATH ({found})")
        return
    launcher = paths.bin_dir() / "agent-resurrect"
    if launcher.exists():
        report.warn(f"{launcher} is not on PATH; open a new terminal tab, or add ~/.local/bin to PATH")
    else:
        report.warn(f"{launcher} is missing; reinstall with install.sh")


def _check_settings(report: _Report) -> None:
    for setting in all_settings():
        try:
            check = setting.check()
        except Exception as exc:  # one broken check must not hide the others
            report.fail(f"{setting.id}: {exc}", setting.fix_hint)
            continue
        if check.ok:
            report.ok(f"{setting.id}: {check.detail}")
        else:
            report.fail(f"{setting.id}: {check.detail}", setting.fix_hint)


def _check_pane(report: _Report) -> None:
    env = os.environ
    terminal = detect_terminal(env)
    if terminal is None:
        report.warn("not running inside a supported terminal (iTerm2)")
    else:
        report.ok(f"{terminal.name} pane {terminal.pane_label(env) or terminal.pane_key(env)}")


def _session_exists(record: Record) -> bool:
    agent = get_agent(record.agent)
    try:
        return agent is not None and agent.session_exists(record)
    except Exception:
        return False


def _check_records(report: _Report, records: List[Record], running: Dict[str, bool]) -> None:
    live = [r for r in records if r.status == STATUS_LIVE]
    restorable = [r for r in live if not running[r.pane_key] and _session_exists(r)]
    report.ok(f"{_plural(len(records), 'recorded session', 'recorded sessions')} "
              f"({len(live)} live, {len(restorable)} restorable)")


def _check_since_boot(report: _Report, records: List[Record], running: Dict[str, bool],
                      boot_time: Optional[float]) -> None:
    if boot_time is None:
        report.warn("could not read the boot time (sysctl -n kern.boottime)")
        return
    boot = datetime.fromtimestamp(boot_time, timezone.utc)
    since = boot.astimezone().strftime("%Y-%m-%d %H:%M")

    counts: Counter = Counter()
    for event in store.read_restore_events():
        when = parse_iso(event.get("ts", "")) if isinstance(event, dict) else None
        if when is not None and when >= boot:
            counts[str(event.get("result"))] += 1
    if not counts:
        report.ok(f"nothing restored since boot ({since})")
    else:
        summary = ", ".join(f"{count} {result}" for result, count in sorted(counts.items()))
        line = f"since boot ({since}): {summary}"
        if counts.get("launch-failed"):
            report.warn(line)
        else:
            report.ok(line)

    orphans = []
    for record in records:
        updated = parse_iso(record.updated_at)
        if record.status == STATUS_LIVE and not running[record.pane_key] and updated is not None and updated < boot:
            orphans.append(record)
    if orphans:
        died = _plural(len(orphans), "session died in a pane", "sessions died in panes")
        report.warn(f"{died} that did not come back; run: agent-resurrect ls")


def run(out: Optional[TextIO] = None, *, procs: Optional[Procs] = None, boot_time: Optional[float] = None) -> int:
    """Print the report. Return 1 if any check failed, else 0. `boot_time` (epoch seconds) defaults to sysctl's."""
    report = _Report(out or sys.stdout)
    procs = procs or Procs()
    _check_python(report)
    _check_launcher(report)
    _check_settings(report)
    _check_pane(report)
    store.gc(load_config().max_age_days)
    records = store.list_records()
    running = {r.pane_key: procs.is_alive(r.agent_pid, r.agent_started) for r in records}
    _check_records(report, records, running)
    _check_since_boot(report, records, running, read_boot_time() if boot_time is None else boot_time)
    return 1 if report.failed else 0
