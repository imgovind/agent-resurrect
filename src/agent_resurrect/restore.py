"""Restore: decide whether a pane's record may be resumed, then hand the terminal to the agent.

`auto` runs on the first prompt of a new shell in a pane that has a record (see shell/). It never
reads input unless stdin and stdout are both terminals, and it resumes by exec-ing the agent so that
no Python parent survives to catch Ctrl-C or Ctrl-Z meant for the agent.
"""
import math
import os
import select
import shlex
import signal
import sys
import termios
import time
import traceback
import tty
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Mapping, Optional

from . import logs, paths, store
from .agents import get_agent
from .agents.base import AgentAdapter
from .config import Config, load_config
from .display import printable  # display only: format_command keeps exact values
from .model import STATUS_CONSUMED, STATUS_LIVE, STATUS_SKIPPED, Record, parse_iso
from .procs import Procs
from .terminals import detect_terminal
from .terminals.base import TerminalAdapter

OK = "ok"
NOT_LIVE = "not-live"
TOO_OLD = "too-old"
AGENT_RUNNING = "agent-running"
SESSION_GONE = "session-gone"

SKIP_HINT = 'Skipped. Run "agent-resurrect" in this pane to resume it later.'
TITLE_WIDTH = 60

# Python starts with these ignored, and an ignored disposition survives exec. subprocess resets them
# in its children (restore_signals=True); a bare exec has to do the same.
_SIGNALS_TO_RESET = tuple(getattr(signal, name) for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ")
                          if hasattr(signal, name))


@dataclass
class Decision:
    eligible: bool
    reason: str  # OK, NOT_LIVE, TOO_OLD, AGENT_RUNNING or SESSION_GONE


@dataclass
class ResumePlan:
    argv: List[str]
    cwd: Optional[str]       # directory to start in; None means stay in the current one
    warning: Optional[str]   # shown before launching, e.g. the recorded directory is gone


def check_eligible(record: Record, agent: AgentAdapter, procs: Procs, config: Config,
                   now: Optional[datetime] = None, require_live: bool = True) -> Decision:
    """Spec section 7, checked in a fixed order so the reason is predictable. Manual commands pass
    require_live=False to resume a consumed or skipped record."""
    if require_live and record.status != STATUS_LIVE:
        return Decision(False, NOT_LIVE)
    updated = parse_iso(record.updated_at)  # unparseable: never judged too old on a guess (as store.gc)
    if updated is not None and updated < (now or datetime.now(timezone.utc)) - timedelta(days=config.max_age_days):
        return Decision(False, TOO_OLD)
    if procs.is_alive(record.agent_pid, record.agent_started):
        return Decision(False, AGENT_RUNNING)
    if not agent.session_exists(record):
        return Decision(False, SESSION_GONE)
    return Decision(True, OK)


def plan_resume(record: Record, agent: AgentAdapter, config: Config) -> ResumePlan:
    argv = list(agent.resume_argv(record, config))
    if record.cwd and os.path.isdir(record.cwd):
        return ResumePlan(argv, record.cwd, None)
    warning = None
    if record.cwd:
        warning = (f"working directory {printable(record.cwd)} no longer exists; "
                   "resuming from the current directory")
    return ResumePlan(argv, None, warning)


def format_command(plan: ResumePlan) -> str:
    """A copy-paste ready shell command, e.g. "cd '/a b' && claude --resume x". Safe to print: it never
    contains a raw control character, yet the shell still gets every value exactly."""
    command = " ".join(_quote(part) for part in plan.argv)
    return f"cd {_quote(plan.cwd)} && {command}" if plan.cwd else command


def _quote(part: str) -> str:
    if part.isprintable():
        return shlex.quote(part)
    # shlex.quote would leave control characters raw inside '...'. ANSI-C quoting ($'...', understood by
    # bash 3.2 and zsh) spells them as \xHH bytes of their UTF-8 encoding instead.
    out = []
    for ch in part:
        if ch in "\\'":
            out.append("\\" + ch)
        elif ch.isprintable():
            out.append(ch)
        else:
            try:
                raw = ch.encode("utf-8", "surrogateescape")
            except UnicodeEncodeError:
                raw = b"?"
            out.extend(f"\\x{byte:02x}" for byte in raw)
    return "$'" + "".join(out) + "'"


def launch(plan: ResumePlan) -> None:
    """Replace this process with the agent. Returns only by raising OSError (for example, not found).

    exec, never subprocess: the agent must be the shell's foreground job itself, with no Python
    parent left to die on Ctrl-C and hand the terminal back to the shell while the agent runs on.
    """
    if plan.cwd:
        os.chdir(plan.cwd)
    for stream in (sys.stdout, sys.stderr):  # exec discards anything still buffered
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):
            pass
    previous = [(signum, signal.signal(signum, signal.SIG_DFL)) for signum in _SIGNALS_TO_RESET]
    try:
        os.execvp(plan.argv[0], plan.argv)
    except OSError:
        for signum, handler in previous:
            if handler is not None:
                signal.signal(signum, handler)
        raise


def wait_for_key(seconds: int, stdin=None, stdout=None) -> bool:
    """Count down on one line, rewritten each second. True if a key was pressed (or Ctrl-C), False at
    timeout. Keys typed before the countdown started are discarded, so they cannot skip it."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    fd = stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except termios.error:  # not a terminal device: wait for input without cbreak
        saved = None
    width = len(f"Resuming in {max(seconds, 0)}s")
    shown = None
    try:
        if saved is not None:
            tty.setcbreak(fd)  # TCSAFLUSH: drops typeahead
        deadline = time.monotonic() + max(seconds, 0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            whole = int(math.ceil(remaining))
            if whole != shown:
                shown = whole
                stdout.write(f"\r{f'Resuming in {whole}s'.ljust(width)}")
                stdout.flush()
            ready, _, _ = select.select([fd], [], [], remaining - (whole - 1))
            if ready:
                os.read(fd, 64)  # the whole key, including escape sequences such as arrow keys
                return True
    except KeyboardInterrupt:
        return True
    finally:
        if saved is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        if shown is not None:
            stdout.write("\n")
            stdout.flush()


def ask_yes_no(prompt: str, stdin=None, stdout=None) -> bool:
    """Ask "<prompt> [Y/n] ". Enter means yes; end of input or Ctrl-C means no."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stdout.write(f"{prompt} [Y/n] ")
    stdout.flush()
    try:
        answer = stdin.readline()
    except KeyboardInterrupt:
        answer = ""
    if not answer:
        stdout.write("\n")
        return False
    return answer.strip().lower() in ("", "y", "yes")


def resume_record(record: Record, agent: AgentAdapter, config: Config, *, stdout=None,
                  launcher: Callable[[ResumePlan], None] = launch, announce: bool = True) -> int:
    """Resume one record in this terminal. Marks the pane's record consumed first, so a crash loop
    cannot re-trigger it; puts the old status back if the agent cannot be started."""
    out = stdout if stdout is not None else sys.stdout
    plan = plan_resume(record, agent, config)
    if announce:
        print(f'Resurrecting "{_title(record, agent)}" ({printable(record.agent)}) in {_where(record)}', file=out)
    key = record.pane_key
    previous = _stored_status(record)
    if previous is not None:
        store.set_status(key, STATUS_CONSUMED)
    store.append_restore_event(key, "resumed", record.session_id)
    if plan.warning:
        print(plan.warning, file=out)
    out.flush()
    try:
        launcher(plan)
    except OSError as exc:
        print(f"agent-resurrect: could not start {printable(plan.argv[0])}: {printable(exc)}", file=out)
        if previous is not None:
            store.set_status(key, previous)
        store.append_restore_event(key, "launch-failed", f"{record.session_id}: {exc}")
        return 1
    return 0


def auto(env: Mapping[str, str], *, terminal: Optional[TerminalAdapter] = None,
         agent_lookup: Optional[Callable[[str], Optional[AgentAdapter]]] = None, procs: Optional[Procs] = None,
         config: Optional[Config] = None, stdin=None, stdout=None,
         keypress: Callable[..., bool] = wait_for_key, ask: Callable[..., bool] = ask_yes_no,
         launcher: Callable[[ResumePlan], None] = launch, isatty: Optional[Callable[[object], bool]] = None,
         now: Optional[datetime] = None) -> int:
    """First-prompt restore for this pane. Silent unless it has something to offer on a terminal."""
    terminal = terminal or detect_terminal(env)
    key = terminal.pane_key(env) if terminal is not None else None
    if not key or not store.valid_pane_key(key):
        return 0
    record = store.load(key)
    if record is None:
        return 0
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    isatty = isatty or _isatty
    if not (isatty(stdin) and isatty(stdout)):
        store.append_restore_event(key, "not-a-tty", record.session_id)
        return 0
    try:
        return _auto_on_terminal(record, agent_lookup or get_agent, procs or Procs(), config or load_config(),
                                 stdin, stdout, keypress, ask, launcher, now)
    except KeyboardInterrupt:
        stdout.write("\n")
        return 130
    except Exception as exc:  # a shell's first prompt must never show a traceback
        logs.log(f"restore: auto failed for {key}: {exc!r}\n{traceback.format_exc()}")
        print(f"agent-resurrect: could not restore this pane ({printable(exc)}); see {printable(paths.log_path())}",
              file=stdout)
        return 1


def _auto_on_terminal(record: Record, agent_lookup, procs: Procs, config: Config, stdin, stdout,
                      keypress, ask, launcher, now) -> int:
    key = record.pane_key
    store.gc(config.max_age_days)
    agent = agent_lookup(record.agent)
    if agent is None:
        store.append_restore_event(key, "unknown-agent", record.agent)
        return 0
    decision = check_eligible(record, agent, procs, config, now=now)
    if not decision.eligible:
        if decision.reason == SESSION_GONE:
            print(f"agent-resurrect: the {printable(record.agent)} session {printable(record.session_id)} "
                  "that ran in this pane "
                  "no longer exists; forgetting it.", file=stdout)
            store.delete(key)
        store.append_restore_event(key, decision.reason, record.session_id)
        return 0

    mode = config.auto_resume
    if mode == "off":
        print(f"agent-resurrect: resume with: {format_command(plan_resume(record, agent, config))}", file=stdout)
        store.set_status(key, STATUS_CONSUMED)
        store.append_restore_event(key, "offered", record.session_id)
        return 0
    if mode == "ask":
        question = f'Resurrect "{_title(record, agent)}" ({printable(record.agent)}) in {_where(record)}?'
        if not ask(question, stdin=stdin, stdout=stdout):
            return _skip(record, stdout)
    elif mode != "immediate":  # countdown, also the fallback for an unexpected value
        print(f'Resurrecting "{_title(record, agent)}" ({printable(record.agent)}) in {_where(record)}. '
              "Press any key to skip.", file=stdout)
        stdout.flush()
        if keypress(config.countdown_seconds, stdin=stdin, stdout=stdout):
            return _skip(record, stdout)
    # Countdown and ask have already named the session; only immediate announces it here.
    return resume_record(record, agent, config, stdout=stdout, launcher=launcher,
                         announce=(mode == "immediate"))


def _skip(record: Record, stdout) -> int:
    store.set_status(record.pane_key, STATUS_SKIPPED)
    store.append_restore_event(record.pane_key, "skipped", record.session_id)
    print(SKIP_HINT, file=stdout)
    return 0


def _stored_status(record: Record) -> Optional[str]:
    """Status of this pane's stored record if it is this same session, else None (nothing to mark)."""
    if not store.valid_pane_key(record.pane_key):
        return None
    stored = store.load(record.pane_key)
    if stored is None or stored.session_id != record.session_id:
        return None
    return stored.status


def _title(record: Record, agent: AgentAdapter) -> str:
    """Title, else first prompt, else session id: one printable line, at most TITLE_WIDTH characters."""
    try:
        info = agent.describe(record)
    except Exception as exc:  # display only; never block a restore on it
        logs.log(f"restore: cannot describe {record.session_id}: {exc!r}")
        info = None
    for candidate in ((info.title, info.first_prompt) if info is not None else ()):
        text = printable(" ".join(str(candidate or "").split()))
        if text:
            return text if len(text) <= TITLE_WIDTH else text[:TITLE_WIDTH - 3].rstrip() + "..."
    return printable(record.session_id)


def _where(record: Record) -> str:
    return printable(record.cwd) if record.cwd else "the current directory"


def _isatty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False

