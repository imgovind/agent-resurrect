"""Restore: decide whether a pane's record may be resumed, then hand the terminal to the agent.

`auto` runs on the first prompt of a new shell in a pane that has a record (see shell/). It never
reads input unless stdin and stdout are both terminals, and it resumes by exec-ing the agent so that
no Python parent survives to catch Ctrl-C or Ctrl-Z meant for the agent.

Nothing here launches an agent without a terminal on stdin and stdout: `resume_record` prints the
command instead and returns NEEDS_TTY. A live record is taken with `store.claim` (atomic, exactly one
winner) immediately before launching, so two shells that read the same record never both launch it.
"""
import math
import os
import re
import select
import shlex
import signal
import sys
import termios
import time
import traceback
import tty
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Mapping, Optional, Union

from . import exitcodes, logs, paths, store
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

# Restore events (restores.jsonl) besides the decision reasons above.
RESUMED = "resumed"
LOST_RACE = "lost-race"          # another shell claimed the record first; this one stays quiet
LAUNCH_FAILED = "launch-failed"

SKIP_HINT = 'Skipped. Run "agent-resurrect" in this pane to resume it later.'
TITLE_WIDTH = 60
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

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
    # Variables the agent gets on top of this process's environment, e.g. the CLAUDE_CONFIG_DIR its session
    # was recorded under (Record.resume_env). Without it, a session from a second account resumes in the first.
    env: Dict[str, str] = field(default_factory=dict)


def check_eligible(record: Record, agent: AgentAdapter, procs: Procs, config: Config,
                   now: Optional[datetime] = None, require_live: bool = True) -> Decision:
    """Spec section 7, checked in a fixed order so the reason is predictable. Manual commands pass
    require_live=False to resume a consumed or skipped record."""
    return _decide(record, agent, procs, config, now, require_live=require_live, check_running=True)


def simulate(record: Record, agent: AgentAdapter, procs: Procs, config: Config, assume_restart: bool = False,
             now: Optional[datetime] = None) -> Decision:
    """The decision `auto` would make for this record, with no terminal, no launch and no change to any state.
    With assume_restart, as if the Mac had just restarted: a restart ends every agent process, so the
    agent-running step is skipped and every other rule still applies."""
    return _decide(record, agent, procs, config, now, require_live=True, check_running=not assume_restart)


def _decide(record: Record, agent: AgentAdapter, procs: Procs, config: Config, now: Optional[datetime], *,
            require_live: bool, check_running: bool) -> Decision:
    if require_live and record.status != STATUS_LIVE:
        return Decision(False, NOT_LIVE)
    updated = parse_iso(record.updated_at)  # unparseable: never judged too old on a guess (as store.gc)
    if updated is not None and updated < (now or datetime.now(timezone.utc)) - timedelta(days=config.max_age_days):
        return Decision(False, TOO_OLD)
    if check_running and procs.is_alive(record.agent_pid, record.agent_started):
        return Decision(False, AGENT_RUNNING)
    if not agent.session_exists(record):
        return Decision(False, SESSION_GONE)
    return Decision(True, OK)


def plan_resume(record: Record, agent: AgentAdapter, config: Config) -> ResumePlan:
    argv = list(agent.resume_argv(record, config))
    env = _resume_env(record)
    if record.cwd and os.path.isdir(record.cwd):
        return ResumePlan(argv, record.cwd, None, env)
    warning = None
    if record.cwd:
        warning = (f"working directory {printable(record.cwd)} no longer exists; "
                   "resuming from the current directory")
    return ResumePlan(argv, None, warning, env)


def _exportable(name: object, value: object) -> bool:
    """A variable a shell can set as NAME=value and exec can pass: a plain name, a string without NUL."""
    return (isinstance(name, str) and bool(_ENV_NAME.fullmatch(name))
            and isinstance(value, str) and "\0" not in value)


def _resume_env(record: Record) -> Dict[str, str]:
    """A copy of the record's resume_env without entries that cannot be exported. Records are files on disk,
    so a hand-edited one must neither put shell text into a printed command nor make exec raise."""
    recorded = record.resume_env
    if not recorded:
        return {}
    if not isinstance(recorded, dict):
        logs.log(f"restore: ignoring resume_env of {record.session_id!r}: not a mapping")
        return {}
    env = {}
    for name, value in recorded.items():
        if _exportable(name, value):
            env[name] = value
        else:
            logs.log(f"restore: ignoring resume_env entry {name!r} of {record.session_id!r}")
    return env


def format_command(plan: ResumePlan) -> str:
    """A copy-paste ready shell command, e.g. "cd '/a b' && CLAUDE_CONFIG_DIR=/x claude --resume x". Safe to
    print: it never contains a raw control character, yet the shell still gets every value exactly."""
    words = [f"{name}={_quote(plan.env[name])}" for name in sorted(plan.env) if _exportable(name, plan.env[name])]
    words.extend(_quote(part) for part in plan.argv)
    command = " ".join(words)
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
    The agent gets this process's environment with the plan's variables on top; PATH lookup uses that
    same environment.
    """
    if plan.cwd:
        os.chdir(plan.cwd)
    env = dict(os.environ)
    env.update((name, value) for name, value in plan.env.items() if _exportable(name, value))
    for stream in (sys.stdout, sys.stderr):  # exec discards anything still buffered
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):
            pass
    previous = [(signum, signal.signal(signum, signal.SIG_DFL)) for signum in _SIGNALS_TO_RESET]
    try:
        os.execvpe(plan.argv[0], plan.argv, env)
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


def has_terminal(stdin=None, stdout=None, isatty: Union[None, bool, Callable[[object], bool]] = None) -> bool:
    """True if stdin and stdout are both terminals, the condition for launching an agent or asking anything.
    `isatty` checks one stream (default: its isatty() method); a plain bool decides by itself (a caller that
    has already checked). Streams default to sys.stdin and sys.stdout, looked up now."""
    if isinstance(isatty, bool):
        return isatty
    check = isatty or _isatty
    return bool(check(stdin if stdin is not None else sys.stdin)) and \
        bool(check(stdout if stdout is not None else sys.stdout))


def resume_record(record: Record, agent: AgentAdapter, config: Config, *, stdin=None, stdout=None,
                  launcher: Optional[Callable[[ResumePlan], None]] = None, announce: bool = True,
                  isatty: Union[None, bool, Callable[[object], bool]] = None) -> int:
    """Resume one record in this terminal; returns an exitcodes value when it does not exec.

    Without a terminal (see has_terminal) it launches nothing: it prints the command to run and returns
    NEEDS_TTY, changing no state and logging no event. Otherwise a stored, live record of this session is
    claimed first (atomically marked consumed, so neither a crash loop nor a second shell can launch it again)
    and a lost claim returns BUSY. Records that are not stored, or not live, are launched without a claim.
    If the agent cannot be started, a claimed record becomes skipped, not live, so that new shells do not
    retry it forever. `launcher` defaults to `launch`, looked up when called so that it can be replaced."""
    out = stdout if stdout is not None else sys.stdout
    plan = plan_resume(record, agent, config)
    if not has_terminal(stdin, out, isatty):
        print(f"agent-resurrect: not starting {printable(plan.argv[0])} without a terminal. "
              "To resume, run this in a terminal:", file=out)
        if plan.warning:
            print(plan.warning, file=out)
        print(f"  {format_command(plan)}", file=out)
        return exitcodes.NEEDS_TTY
    claimed = False
    if _stored_status(record) == STATUS_LIVE:
        if _claim(record) is None:
            store.append_restore_event(record.pane_key, LOST_RACE, record.session_id)
            print("agent-resurrect: another shell is already resuming this session.", file=out)
            return exitcodes.BUSY
        claimed = True
    if announce:
        _announce(record, agent, out)
    return _launch(record, plan, out, launcher or launch, claimed)


def _launch(record: Record, plan: ResumePlan, out, launcher: Callable[[ResumePlan], None], claimed: bool) -> int:
    key = record.pane_key
    store.append_restore_event(key, RESUMED, record.session_id)
    if plan.warning:
        print(plan.warning, file=out)
    out.flush()
    try:
        launcher(plan)
    except OSError as exc:
        program = printable(plan.argv[0])
        print(f"agent-resurrect: could not start {program}: {printable(exc)}", file=out)
        print(f"When {program} is on your PATH, resume with: agent-resurrect resume {_quote(record.session_id)}",
              file=out)
        if claimed and _stored_status(record) is not None:
            store.set_status(key, STATUS_SKIPPED)
        store.append_restore_event(key, LAUNCH_FAILED, f"{record.session_id}: {exc}")
        return exitcodes.ERROR
    return exitcodes.OK


def _claim(record: Record) -> Optional[Record]:
    """Take this pane's live record for `record`'s session (store.claim: exactly one process wins), else None."""
    claimed = store.claim(record.pane_key)
    if claimed is not None and claimed.session_id != record.session_id:
        # A newer session was recorded in this pane after `record` was read: not ours to take.
        store.set_status(record.pane_key, STATUS_LIVE)
        return None
    return claimed


def auto(env: Mapping[str, str], *, terminal: Optional[TerminalAdapter] = None,
         agent_lookup: Optional[Callable[[str], Optional[AgentAdapter]]] = None, procs: Optional[Procs] = None,
         config: Optional[Config] = None, stdin=None, stdout=None,
         keypress: Callable[..., bool] = wait_for_key, ask: Callable[..., bool] = ask_yes_no,
         launcher: Optional[Callable[[ResumePlan], None]] = None, isatty: Optional[Callable[[object], bool]] = None,
         now: Optional[datetime] = None) -> int:
    """First-prompt restore for this pane. Silent unless it has something to offer on a terminal."""
    terminal = terminal or detect_terminal(env)
    key = terminal.pane_key(env) if terminal is not None else None
    if not key or not store.valid_pane_key(key):
        return exitcodes.OK
    record = store.load(key)
    if record is None:
        return exitcodes.OK
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    if not has_terminal(stdin, stdout, isatty):
        store.append_restore_event(key, "not-a-tty", record.session_id)
        return exitcodes.OK
    try:
        return _auto_on_terminal(record, agent_lookup or get_agent, procs or Procs(), config or load_config(),
                                 stdin, stdout, keypress, ask, launcher or launch, now)
    except KeyboardInterrupt:
        stdout.write("\n")
        return exitcodes.INTERRUPTED
    except Exception as exc:  # a shell's first prompt must never show a traceback
        logs.log(f"restore: auto failed for {key}: {exc!r}\n{traceback.format_exc()}")
        print(f"agent-resurrect: could not restore this pane ({printable(exc)}); see {printable(paths.log_path())}",
              file=stdout)
        return exitcodes.ERROR


def _auto_on_terminal(record: Record, agent_lookup, procs: Procs, config: Config, stdin, stdout,
                      keypress, ask, launcher, now) -> int:
    key = record.pane_key
    # A session can run for weeks without a new SessionStart; its record must outlive the age limit.
    store.gc(config.max_age_days, is_running=lambda r: procs.is_alive(r.agent_pid, r.agent_started))
    agent = agent_lookup(record.agent)
    if agent is None:
        store.append_restore_event(key, "unknown-agent", record.agent)
        return exitcodes.OK
    decision = check_eligible(record, agent, procs, config, now=now)
    if not decision.eligible:
        if decision.reason == SESSION_GONE:
            print(f"agent-resurrect: the {printable(record.agent)} session {printable(record.session_id)} "
                  "that ran in this pane "
                  "no longer exists; forgetting it.", file=stdout)
            store.delete(key)
        store.append_restore_event(key, decision.reason, record.session_id)
        return exitcodes.OK

    mode = config.auto_resume
    plan = plan_resume(record, agent, config)  # before any claim: if it raises, the record stays live
    if mode == "off":
        if _claim(record) is None:
            return _lost_race(record)
        print(f"agent-resurrect: resume with: {format_command(plan)}", file=stdout)
        store.append_restore_event(key, "offered", record.session_id)
        return exitcodes.OK
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
    # Claimed only now, after the countdown or question: every shell that read the record live got this far,
    # and exactly one of them may launch it.
    if _claim(record) is None:
        return _lost_race(record)
    # Countdown and ask have already named the session; only immediate announces it here.
    if mode == "immediate":
        _announce(record, agent, stdout)
    return _launch(record, plan, stdout, launcher, claimed=True)


def _lost_race(record: Record) -> int:
    """Another shell claimed this pane's record first and is resuming it: say nothing, launch nothing."""
    store.append_restore_event(record.pane_key, LOST_RACE, record.session_id)
    return exitcodes.OK


def _skip(record: Record, stdout) -> int:
    store.set_status(record.pane_key, STATUS_SKIPPED)
    store.append_restore_event(record.pane_key, "skipped", record.session_id)
    print(SKIP_HINT, file=stdout)
    return exitcodes.OK


def _announce(record: Record, agent: AgentAdapter, out) -> None:
    print(f'Resurrecting "{_title(record, agent)}" ({printable(record.agent)}) in {_where(record)}', file=out)


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

