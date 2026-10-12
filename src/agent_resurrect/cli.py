"""Command line: argument parsing, exit statuses and output. The behaviour lives in the modules this calls.

Machine interface (review 03 "Machine interface spec", schemas in docs/json/): with --json a command prints
exactly one ASCII JSON object (jsonout) and nothing else on stdout, never prompts (a needed confirmation is
exit 4), and puts diagnostics on stderr. Every exit status is an exitcodes constant and error.code names it.
resume and here never launch an agent unless stdin and stdout are both terminals, and never with --json, which
implies --print: the document carries the command to run (session.resume.command) and the exit status is 0.
"""
import argparse
import io
import json
import os
import platform
import subprocess
import sys
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import __version__, doctor, installer, jsonout, logs, paths, restore, store, views
from .agents import all_agents, get_agent
from .agents.base import AgentAdapter, HookContext
from .config import CHOICES as CONFIG_CHOICES
from .config import DEFAULTS as CONFIG_DEFAULTS
from .config import load_config, set_value
from .display import printable
from .exitcodes import BUSY, ERROR, GONE, INTERRUPTED, NEEDS_TTY, NOT_FOUND, OK, USAGE
from .model import Candidate, Record, SessionInfo, parse_iso
from .procs import Procs
from .settings import all_settings
from .terminals import detect_terminal
from .terminals.base import TerminalAdapter

PROG = "agent-resurrect"
HOOK_STDIN_LIMIT = 1024 * 1024
PLUGIN_SETTING = "claude.plugin"
NO_MATCH = ("No session contains that text. Try a different snippet: plain prose from one line, "
            "not a table, code block or heavily formatted output.")
NOTHING_RECORDED = "Nothing recorded for this pane."
PANE_LINES = 2000  # recent scrollback lines searched by --from-pane and the `here` fallback
PANE_UNREADABLE = ("cannot read this pane's scrollback; this works in an iTerm2 pane (not inside tmux, screen "
                   "or zellij). Search by text instead: agent-resurrect find \"text you remember\"")
EXIT_CODES = ("Exit codes: 0 ok, 1 error, 2 usage, 3 not found, 4 needs a terminal (or --yes), 5 still running, "
              "6 transcript gone, 130 interrupted. With --json every command prints one JSON object "
              "(schemas in docs/json/).")
INSTALL_NEXT_STEPS = [
    {"kind": "new-shell", "text": "Open a new terminal tab"},
    {"kind": "restart-agent",
     "text": f"Restart Claude Code sessions that are running now, or run {PROG} adopt in each"},
]
UNINSTALL_NEXT_STEPS = [{"kind": "new-shell", "text": "Open a new terminal tab"}]
FILES_STEP = "Remove the launcher, the installed copy and the bytecode cache"


def _error(message: str) -> None:
    print(f"{PROG}: {printable(message)}", file=sys.stderr)


def _say(line: str) -> None:
    """Print one line built from transcripts, records or other untrusted text, with control characters
    neutralised so it can never act as a terminal escape sequence."""
    print(printable(line))


def _log_quietly(message: str) -> None:
    try:
        logs.log(message)
    except Exception:
        pass


def _emit(command: str, code: int, message: Optional[str] = None, **fields) -> int:
    """Print the command's one JSON document and return its exit status."""
    jsonout.emit(jsonout.document(command, code, message, **fields))
    return code


def _fail(args: argparse.Namespace, command: str, code: int, message: str, **fields) -> int:
    """An error: the JSON document (with whatever fields are known), or one line on stderr."""
    if getattr(args, "json", False):
        return _emit(command, code, message, **fields)
    _error(message)
    return code


def _set_flags(**flags) -> dict:
    """Only the keyword flags that are on. Their defaults are off, so a call stays valid for a module from
    before a flag existed (the hooks and the restore half can briefly run different versions)."""
    return {name: value for name, value in flags.items() if value}


def _isatty(stream) -> bool:
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


# ---------------------------------------------------------------------------------------------------- hook

def _hook(args: List[str]) -> int:
    """`hook <agent> <event>`, run by the agent's plugin hooks. Fails open: whatever happens it prints
    nothing (stdout or stderr) and returns 0, so the agent is never blocked. Problems go to the log."""
    swallowed = io.StringIO()
    try:
        with redirect_stdout(swallowed), redirect_stderr(swallowed):
            _record_hook(args)
    except BaseException as exc:  # includes SystemExit and KeyboardInterrupt: a hook never fails
        _log_quietly(f"hook {' '.join(args[:2])}: {exc.__class__.__name__}: {exc}\n{traceback.format_exc()}")
    if swallowed.getvalue():
        _log_quietly(f"hook {' '.join(args[:2])}: suppressed output: {swallowed.getvalue()[:2000]!r}")
    return OK


def _read_hook_stdin() -> str:
    stream = sys.stdin
    if stream is None or stream.isatty():  # run by hand in a terminal: never wait for typing
        return ""
    data = getattr(stream, "buffer", stream).read(HOOK_STDIN_LIMIT + 1)
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    if len(data) > HOOK_STDIN_LIMIT:
        logs.log("hook: payload larger than 1 MB ignored")
        return ""
    return data


def _pane_context() -> Tuple[Optional[object], Optional[str]]:
    """(terminal adapter, pane key) of the pane this process runs in; (None, None) outside a supported pane."""
    env = os.environ
    terminal = detect_terminal(env)
    key = terminal.pane_key(env) if terminal is not None else None
    if not key or not store.valid_pane_key(key):
        return terminal, None
    return terminal, key


def _hook_context(terminal, pane_key: str) -> HookContext:
    env = os.environ
    return HookContext(env=env, terminal=terminal, pane_key=pane_key, pane_label=terminal.pane_label(env),
                       procs=Procs(), pid=os.getpid())


def _record_hook(args: List[str]) -> None:
    if len(args) < 2:
        logs.log(f"hook: expected <agent> <event>, got {args!r}")
        return
    agent_name, event = args[0], args[1]
    raw = _read_hook_stdin()
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        logs.log(f"hook {agent_name} {event}: unreadable payload: {exc}")
        return
    if not isinstance(payload, dict):
        logs.log(f"hook {agent_name} {event}: payload is not a JSON object")
        return
    agent = get_agent(agent_name)
    if agent is None:
        logs.log(f"hook: unknown agent {agent_name!r}")
        return
    terminal, pane_key = _pane_context()
    if pane_key is None:
        return  # not in a pane of a supported terminal: nothing to record
    agent.on_hook(event, payload, _hook_context(terminal, pane_key))


# ---------------------------------------------------------------------------------------------------- listing

@dataclass
class _Entry:
    record: Record
    info: SessionInfo
    running: bool


def _gc(max_age_days: int, procs: Procs) -> None:
    """Forget old records, except those whose agent still runs (a session can run for weeks)."""
    store.gc(max_age_days, is_running=lambda record: procs.is_alive(record.agent_pid, record.agent_started))


def _entries(procs: Procs, include_running: bool, describe: bool = True) -> List[_Entry]:
    """Stored records, newest first. Without include_running, those whose agent still runs are left out."""
    entries = []
    for record in store.list_records():
        running = procs.is_alive(record.agent_pid, record.agent_started)
        if running and not include_running:
            continue
        info = views.describe(get_agent(record.agent), record) if describe else SessionInfo()
        entries.append(_Entry(record, info, running))
    return entries


def _numbered(entries: List[_Entry]) -> List[Tuple[Optional[int], _Entry]]:
    """Running entries get no number, so the numbers always match the default listing `resume <n>` uses."""
    out, number = [], 0
    for entry in entries:
        if entry.running:
            out.append((None, entry))
        else:
            number += 1
            out.append((number, entry))
    return out


def _one_line(text: Optional[str], width: int) -> str:
    text = " ".join((text or "").split())
    return printable(text if len(text) <= width else text[:width - 3] + "...")


def _title(info: SessionInfo, record: Record, width: int = 48) -> str:
    return _one_line(info.title or info.first_prompt or record.session_id, width)


def _short_path(path: str) -> str:
    home = str(paths.home())
    if path == home:
        return "~"
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def _local_time(iso: Optional[str] = None, epoch: Optional[float] = None) -> Optional[str]:
    """Format an epoch or an ISO-8601 timestamp (with or without fractions) as local "YYYY-MM-DD HH:MM"."""
    try:
        if epoch is not None:
            moment = datetime.fromtimestamp(epoch)
        elif iso:
            moment = datetime.fromisoformat(views.iso(iso).replace("Z", "+00:00")).astimezone()
        else:
            return None
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None
    return moment.strftime("%Y-%m-%d %H:%M")


def _last_active(entry: _Entry) -> str:
    when = _local_time(epoch=entry.info.last_active)
    if when is None:
        updated = parse_iso(entry.record.updated_at)
        when = updated.astimezone().strftime("%Y-%m-%d %H:%M") if updated else None
    return when or "-"


def _table(rows: List[List[str]], right_align_first: bool = True) -> List[str]:
    """Rows as aligned lines (two spaces between columns), not yet sanitised."""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = []
    for row in rows:
        first = row[0].rjust(widths[0]) if right_align_first else row[0].ljust(widths[0])
        cells = [first] + [cell.ljust(width) for cell, width in zip(row[1:], widths[1:])]
        lines.append("  ".join(cells).rstrip())
    return lines


def _show_listing(include_running: bool, ids: bool = False) -> int:
    procs = Procs()
    _gc(load_config().max_age_days, procs)
    entries = _entries(procs, include_running)
    if not entries:
        print("No sessions to resurrect.")
        return OK
    rows = []
    for number, entry in _numbered(entries):
        record = entry.record
        rows.append(["-" if number is None else str(number)] + ([record.session_id] if ids else []) + [
            record.agent, f'"{_title(entry.info, record)}"', _short_path(record.cwd), _last_active(entry),
            record.pane_label or "-", "running" if entry.running else record.status])
    for line in _table(rows):
        _say(line)
    return OK


def cmd_ls(args: argparse.Namespace) -> int:
    if not args.json:
        return _show_listing(include_running=args.all, ids=args.ids)
    cfg, procs = load_config(), Procs()
    _gc(cfg.max_age_days, procs)
    items = []
    for number, entry in _numbered(_entries(procs, args.all)):
        agent = get_agent(entry.record.agent)
        if agent is None:
            eligible, reason = False, "unknown-agent"
        else:
            decision = restore.check_eligible(entry.record, agent, procs, cfg)
            eligible, reason = decision.eligible, decision.reason
        items.append(views.entry(number, entry.record, agent, cfg, entry.info, entry.running, eligible, reason))
    return _emit("ls", OK, include_running=args.all, entries=items)


# ---------------------------------------------------------------------------------------------------- resume

def _resume(args: argparse.Namespace, command: str, record: Record) -> int:
    """Resume one record in this terminal, or print how to. Refusals (unknown agent, still running,
    transcript gone) come first; then --print (which --json implies: a JSON caller is a program, so it gets the
    command and nothing is launched); then nothing is launched without a terminal on stdin and stdout."""
    agent = get_agent(record.agent)
    cfg = load_config()
    pane = views.pane(record)

    def finish(code: int, message: Optional[str], action: Optional[str]) -> int:
        if args.json:
            return _emit(command, code, message, action=action, pane=pane,
                         session=views.session(record, agent, cfg))
        if message:
            _error(message)
        return code

    if agent is None:
        return finish(ERROR, f'unknown agent "{record.agent}" for session {record.session_id}', "refused")
    decision = restore.check_eligible(record, agent, Procs(), cfg, require_live=False)
    if decision.reason == restore.AGENT_RUNNING:
        return finish(BUSY, f"that session is still running (pid {record.agent_pid})", "refused")
    if decision.reason == restore.SESSION_GONE:
        return finish(GONE, f"session {record.session_id} no longer exists, so it cannot be resumed", "refused")
    if args.json:
        return finish(OK, None, "printed")  # session.resume.command is the command to run
    plan = restore.plan_resume(record, agent, cfg)
    if args.print or not restore.has_terminal():
        if plan.warning:
            _error(plan.warning)  # stderr: stdout carries the command alone, ready for $(...)
        if not args.print:
            _error(f"not launching {record.agent} without a terminal (stdin and stdout must both be "
                   "terminals); run this command in a terminal pane, or pass --print to just print it:")
        _say(restore.format_command(plan))
        return OK if args.print else NEEDS_TTY
    # A terminal on both ends: hand it to the agent. restore.launch is looked up now, not bound as
    # resume_record's default, so it can be replaced; isatty=True passes on the check made just above.
    return restore.resume_record(record, agent, cfg, launcher=restore.launch, isatty=True)


def _record_for_session(session_id: str) -> Optional[Record]:
    for record in store.list_records():
        if record.session_id == session_id:
            return record
    for agent in all_agents():
        record = agent.session_record(session_id)
        if record is not None:
            return record
    return None


def cmd_here(args: argparse.Namespace) -> int:
    terminal, pane_key = _pane_context()
    record = store.load(pane_key) if pane_key else None
    if record is not None:
        return _resume(args, "here", record)
    message = "nothing recorded for this pane"
    if args.json:
        pane = None if pane_key is None else {"terminal": terminal.name, "key": pane_key,
                                              "label": terminal.pane_label(os.environ)}
        return _emit("here", NOT_FOUND, message, action=None, pane=pane, session=None)
    print(NOTHING_RECORDED)
    if pane_key is not None and _reads_pane_text(terminal) and not args.print:
        if restore.has_terminal():
            if args.yes or _confirm("Search this pane's scrollback for the session that ran here? [Y/n] "):
                return _here_from_pane(args)
        else:  # never prompt without a terminal; say how instead
            _say(f"To search this pane's scrollback for the session, run: {PROG} find --from-pane")
    print()
    _show_listing(include_running=False)
    return NOT_FOUND


def _here_from_pane(args: argparse.Namespace) -> int:
    """`here` with nothing recorded: search this pane's scrollback, show the match, and resume it only after
    the user says yes (default yes only for a strong match). --yes skips that question for a strong match only:
    a weak or ambiguous one is a guess, so it is still asked, and a session running elsewhere is still refused."""
    text = _pane_scrollback(marker=NOTHING_RECORDED)
    if not text or not text.strip():
        _error(PANE_UNREADABLE if text is None else "this pane's scrollback is empty; nothing to search")
        return NOT_FOUND
    try:
        with _Progress("Searching your Claude Code sessions"):
            found = _search(text, limit=5)
    except ValueError as exc:
        _error(str(exc))
        return NOT_FOUND
    if not found:
        print(NO_MATCH)
        return NOT_FOUND
    agent, best = found[0]
    confidence = views.confidence([c for _, c in found])
    print()
    _print_best(agent, best, confidence)
    _print_others(found)
    print()
    strong = confidence == "strong"
    if args.yes and not strong:
        _say(f"--yes resumes only a strong match; this one is {confidence}.")
    if not (args.yes and strong) and not _confirm(
            "Resume it here? [Y/n] " if strong else "Resume the best match here anyway? [y/N] ", default=strong):
        _say(f"Not resumed. Resume it later with: {PROG} resume {best.session_id}")
        return OK
    # A stored record carries the agent's pid, so a session still running in another pane is refused (busy)
    # instead of being opened twice.
    record = _record_for_session(best.session_id) or views.candidate_record(agent, best)
    return _resume(args, "here", record)


def cmd_resume(args: argparse.Namespace) -> int:
    target = args.target.strip()
    not_found = {"action": None, "pane": None, "session": None}
    if not target:
        return _fail(args, "resume", USAGE, "no session given; pass a session id or a number from ls")
    if target.isdigit():
        procs = Procs()
        _gc(load_config().max_age_days, procs)
        entries = _entries(procs, include_running=False, describe=False)
        index = int(target)
        if not 1 <= index <= len(entries):
            return _fail(args, "resume", NOT_FOUND, f"no session number {index}; run: {PROG} ls", **not_found)
        return _resume(args, "resume", entries[index - 1].record)
    record = _record_for_session(target)
    if record is None:
        return _fail(args, "resume", NOT_FOUND,
                     f"no session with id {target}; run: {PROG} ls, or {PROG} find <text>", **not_found)
    return _resume(args, "resume", record)


def cmd_auto(args: argparse.Namespace) -> int:
    try:
        return restore.auto(os.environ)
    except Exception as exc:  # runs at a new shell's first prompt: one line, not a traceback
        _log_quietly(f"auto: {traceback.format_exc()}")
        _error(f"automatic restore failed: {exc} (details in {paths.log_path()})")
        return ERROR


# ---------------------------------------------------------------------------------------------------- simulate

def cmd_simulate(args: argparse.Namespace) -> int:
    """What `auto` would decide for each recorded pane: same rules, no terminal, nothing launched or changed
    (no gc, no status change, no restore event)."""
    cfg, procs = load_config(), Procs()
    if args.pane_key is not None:
        if not store.valid_pane_key(args.pane_key):
            return _fail(args, "simulate", USAGE, f"invalid pane key {args.pane_key!r}")
        targets = [(args.pane_key, store.load(args.pane_key))]
    else:
        targets = [(record.pane_key, record) for record in store.list_records()]
    panes = []
    lines = []  # (row, command or None) for the text output
    for key, record in targets:
        if record is None:
            panes.append({"pane": {"terminal": None, "key": key, "label": None}, "decision": "no-record",
                          "reason": None, "mode": cfg.auto_resume, "session": None})
            lines.append((["no-record", key, "", ""], None))
            continue
        agent = get_agent(record.agent)
        if agent is None:
            eligible, reason = False, "unknown-agent"
        else:
            decision = restore.simulate(record, agent, procs, cfg, assume_restart=args.assume_restart)
            eligible, reason = decision.eligible, decision.reason
        session = views.session(record, agent, cfg)
        verdict = "would-resume" if eligible else "would-skip"
        panes.append({"pane": views.pane(record), "decision": verdict, "reason": reason, "mode": cfg.auto_resume,
                      "session": session})
        info = SessionInfo(title=session["title"], first_prompt=session["first_prompt"])
        command = session["resume"]["command"] if eligible and session["resume"] else None
        lines.append(([verdict, record.pane_label or key, f'"{_title(info, record)}"', reason], command))
    if args.json:
        return _emit("simulate", OK, assume_restart=args.assume_restart, panes=panes)
    if not panes:
        print("No panes recorded.")
        return OK
    assumption = (" Assuming a restart: recorded agent processes count as stopped." if args.assume_restart
                  else "")
    _say(f"Automatic restore mode: {cfg.auto_resume}.{assumption}")
    for line, (_, command) in zip(_table([row for row, _ in lines], right_align_first=False), lines):
        _say(line)
        if command:
            _say(f"    {command}")
    return OK


# ---------------------------------------------------------------------------------------------------- adopt

def cmd_adopt(args: argparse.Namespace) -> int:
    """Record the agent session this command runs inside (a tool call of that session)."""
    terminal, pane_key = _pane_context()
    if pane_key is None:
        return _fail(args, "adopt", NOT_FOUND,
                     "not in a pane of a supported terminal (iTerm2, outside tmux, screen and zellij); "
                     "nothing to record", recorded=False, pane=None, session=None)
    ctx = _hook_context(terminal, pane_key)
    for agent in all_agents():
        try:
            record = agent.adopt(ctx)  # the saved record, or None when not inside a recordable session
        except OSError as exc:  # the record could not be saved
            _log_quietly(f"adopt: {traceback.format_exc()}")
            return _fail(args, "adopt", ERROR, f"could not record the {agent.name} session: {exc}",
                         recorded=False, pane=None, session=None)
        if record is not None:
            break
    else:
        return _fail(args, "adopt", NOT_FOUND,
                     "no agent session found to adopt; run this from inside the running agent session "
                     "(for Claude Code, in its Bash tool)", recorded=False,
                     pane={"terminal": terminal.name, "key": pane_key, "label": ctx.pane_label}, session=None)
    cfg = load_config()
    if args.json:
        return _emit("adopt", OK, recorded=True, pane=views.pane(record), session=views.session(record, agent, cfg))
    info = views.describe(agent, record)
    _say(f'Recorded "{_title(info, record, 60)}" ({record.agent}) for pane {record.pane_label or record.pane_key}; '
         "it will be resumed there after a restart.")
    return OK


# ---------------------------------------------------------------------------------------------------- find

def _read_clipboard() -> Optional[str]:
    try:
        result = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


class _Progress:
    """A one-line spinner with elapsed seconds on stderr while a slow step runs, cleared when it ends.
    Silent unless stderr is a terminal, so pipes, logs and --json output never see it."""

    FRAMES = "|/-\\"

    def __init__(self, message: str, enabled: bool = True):
        self.message = message
        self.enabled = enabled and _isatty(sys.stderr)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = 0.0

    def __enter__(self) -> "_Progress":
        self._started = time.monotonic()
        if self.enabled:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def _spin(self) -> None:
        frame = 0
        while True:
            elapsed = time.monotonic() - self._started
            sys.stderr.write(f"\r{self.message} {self.FRAMES[frame % len(self.FRAMES)]} {elapsed:4.1f}s")
            sys.stderr.flush()
            frame += 1
            if self._stop.wait(0.1):
                break

    def __exit__(self, *exc) -> bool:
        if self._thread is not None:
            self._stop.set()
            self._thread.join()
            sys.stderr.write("\r\033[K")  # clear the spinner line
            sys.stderr.flush()
        return False


def _reads_pane_text(terminal) -> bool:
    """True if this terminal adapter can return a pane's scrollback (it overrides TerminalAdapter.pane_text)."""
    return terminal is not None and type(terminal).pane_text is not TerminalAdapter.pane_text


def _pane_scrollback(marker: str, show_progress: bool = True) -> Optional[str]:
    """This pane's recent scrollback, to search for the session that ran here; None if it cannot be read.
    The text is only searched: never stored, logged or printed. Everything from the last line containing
    `marker` (the command doing the reading) onward is dropped, as are trailing agent-resurrect command lines,
    so the search sees what was in the pane before."""
    terminal, key = _pane_context()
    if key is None or not _reads_pane_text(terminal):
        return None
    with _Progress("Reading this pane's scrollback", enabled=show_progress):
        text = terminal.pane_text(os.environ, max_lines=PANE_LINES)
    if text is None:
        return None
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if marker in lines[index]:
            del lines[index:]
            break
    while lines and (not lines[-1].strip() or PROG in lines[-1]):
        lines.pop()
    return "\n".join(lines)


def _find_text(args: argparse.Namespace) -> Optional[str]:
    """The text to search for, or None if the clipboard cannot be read."""
    if args.clipboard:
        return _read_clipboard()
    if args.text:
        return " ".join(args.text)
    stream = sys.stdin
    if stream is not None and not _isatty(stream):
        return stream.read()
    return ""  # a terminal: never wait for typing


def _roles(roles: Dict[str, int]) -> str:
    ordered = sorted(roles.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{role} x{count}" for role, count in ordered) or "n/a"


def _active(info: SessionInfo) -> str:
    started, last = _local_time(iso=info.started), _local_time(epoch=info.last_active)
    if started and last:
        return f"{started} -> {last}"
    return started or last or "(unknown)"


def _print_best(agent: AgentAdapter, best: Candidate, confidence: str) -> None:
    plan = restore.plan_resume(views.candidate_record(agent, best), agent, load_config())
    _say(f"BEST MATCH  ({best.hits}/{best.total_probes} probes; text appears in: {_roles(best.roles)})")
    _say(f"  session : {best.session_id}")
    _say(f"  agent   : {agent.name}")
    _say(f"  title   : {_one_line(best.info.title, 100) or '(none)'}")
    _say(f"  opens   : {_one_line(best.info.first_prompt, 100) or '(unknown)'}")
    _say(f"  active  : {_active(best.info)}")
    _say(f"  cwd     : {best.cwd or '(unknown)'}")
    if best.cwd and plan.warning:
        _say(f"  WARNING : {plan.warning}")
    elif best.cwd and not best.cwd_trusted:
        print("  WARNING : the recorded cwd does not match the transcript's project folder; if resume "
              "says no conversation found, try the folder's original directory.")
    # Pastes copied from a terminal lose markdown, so partial coverage is normal; only a lone probe
    # or a tie with the runner-up is a real reason to doubt the match.
    if confidence == "weak":
        print("  NOTE    : weak match, only 1 probe found; check the title and opening prompt first.")
    elif confidence == "ambiguous":
        print("  NOTE    : ambiguous, another session matches equally well (listed below); compare titles.")
    print("\nResume command:\n")
    _say(f"  {restore.format_command(plan)}")


def _search(text: str, limit: int) -> List[Tuple[AgentAdapter, Candidate]]:
    """Sessions whose history contains the text, best first. Raises ValueError when the text is too short."""
    found: List[Tuple[AgentAdapter, Candidate]] = []
    for agent in all_agents():
        found.extend((agent, c) for c in agent.find_by_text(text, limit=limit))
    # Each agent ranks its own results; across agents, higher probe coverage first (stable sort).
    found.sort(key=lambda item: -(item[1].hits / max(item[1].total_probes, 1)))
    return found[:limit]


def _coverage(found: Candidate) -> float:
    return found.hits / max(found.total_probes, 1)


def _contenders(found: List[Tuple[AgentAdapter, Candidate]]) -> List[Tuple[AgentAdapter, Candidate]]:
    """The runners-up worth listing: those matching at least half as many probes as the best. A session that
    shares a line or two of the text (a subagent the session started, or another session the text was pasted
    into) is no rival to a match on most probes, and listing it only invites resuming the wrong one. --json
    still lists every candidate."""
    if len(found) < 2:
        return []
    best = _coverage(found[0][1])
    return [item for item in found[1:] if 2 * _coverage(item[1]) >= best]


def _print_others(found: List[Tuple[AgentAdapter, Candidate]]) -> None:
    others = _contenders(found)
    if not others:
        return
    # Unnumbered on purpose: `resume <n>` takes numbers from `ls`, so a number here would resume another
    # session. Each candidate starts with its session id, which is what `resume` should be given.
    print("\nOther candidates:")
    for _, c in others:
        when = _local_time(iso=c.info.started) or _local_time(epoch=c.info.last_active) or ""
        title = _one_line(c.info.title or c.info.first_prompt, 60)
        _say(f"  {c.session_id}  {c.hits}/{c.total_probes}  [{_roles(c.roles)}]  {when}  {title}".rstrip())
    print(f"\nResume one of them with: {PROG} resume <session id>")


def cmd_find(args: argparse.Namespace) -> int:
    """Print the sessions whose history contains the text and how to resume the best one. Never launches."""
    if args.from_pane:
        if not restore.has_terminal():  # a person typed it: never read a pane on behalf of a script or tool
            return _fail(args, "find", NEEDS_TTY,
                         "--from-pane reads this pane's scrollback, so it only runs when you type it in a terminal",
                         query={"characters": 0, "probes": None, "source": "pane"})
        text = _pane_scrollback(marker="--from-pane", show_progress=not args.json)
        if text is None:
            return _fail(args, "find", ERROR, PANE_UNREADABLE,
                         query={"characters": 0, "probes": None, "source": "pane"})
    else:
        text = _find_text(args)
        if text is None:
            return _fail(args, "find", ERROR, "cannot read the clipboard (pbpaste failed)")
    query = {"characters": len(text), "probes": None}
    if args.from_pane:
        query["source"] = "pane"
    if not text.strip():
        return _fail(args, "find", USAGE, "no text to search for; pass it as arguments, pipe it on stdin, "
                                          "or use --clipboard or --from-pane", query=query)
    try:
        with _Progress("Searching your Claude Code sessions", enabled=not args.json):
            found = _search(text, args.limit)
    except ValueError as exc:  # e.g. too little text to build search probes from
        return _fail(args, "find", USAGE, str(exc), query=query)
    confidence = views.confidence([c for _, c in found])
    if args.json:
        cfg = load_config()
        matches = [views.match(agent, c, cfg) for agent, c in found]
        if found:
            query["probes"] = found[0][1].total_probes
        return _emit("find", OK if found else NOT_FOUND, None if found else NO_MATCH, query=query,
                     confidence=confidence, best=matches[0] if matches else None, candidates=matches)
    if not found:
        print(NO_MATCH)
        return NOT_FOUND
    agent, best = found[0]
    _print_best(agent, best, confidence)
    _print_others(found)
    return OK


# ---------------------------------------------------------------------------------------------------- setup

def _confirm(prompt: str, default: bool = True) -> bool:
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        print()
        return False
    return default if not answer else answer in ("y", "yes")


def _print_steps(results) -> bool:
    """Print one line per installer step; True if any failed."""
    width = max([9] + [len(str(result.status)) for result in results])
    for result in results:
        _say(f"{result.status:<{width}}  {result.id}  {result.detail}".rstrip())
    return any(result.status == "failed" for result in results)


def _confirmation_refused(args: argparse.Namespace, command: str, **fields) -> Optional[int]:
    """Exit NEEDS_TTY when a confirmation is needed but cannot be asked (--json never asks); else None."""
    if args.yes or args.dry_run or (restore.has_terminal() and not args.json):
        return None
    return _fail(args, command, NEEDS_TTY,
                 f"{command} changes system settings and needs confirmation; run it in a terminal, or pass --yes "
                 "(preview with --dry-run)", **fields)


def _setup_document(command: str, args: argparse.Namespace, results, descriptions: Dict[str, str],
                    next_steps: List[dict], **fields) -> int:
    failed = any(result.status == "failed" for result in results)
    return _emit(command, ERROR if failed else OK, f"a {command} step failed; see steps" if failed else None,
                 dry_run=args.dry_run, **fields, steps=views.steps(results, descriptions),
                 manifest_path=str(paths.manifest_path()), log_path=str(paths.log_path()),
                 next_steps=[] if failed or args.dry_run else next_steps)


def cmd_install(args: argparse.Namespace) -> int:
    settings = [s for s in all_settings() if s.id != PLUGIN_SETTING] if args.no_plugin else None
    shown = settings if settings is not None else all_settings()
    descriptions = {setting.id: setting.description for setting in shown}
    refused = _confirmation_refused(args, "install", dry_run=False, steps=[])
    if refused is not None:
        return refused
    if not (args.yes or args.dry_run or args.json):
        print(f"{PROG} will make these changes ({PROG} uninstall reverts them):")
        for setting in shown:
            _say(f"  - {setting.description or setting.id}")
        if not _confirm("Proceed? [Y/n] "):
            print("Cancelled. Nothing was changed.")
            return ERROR
    results = installer.install(settings=settings, **_set_flags(dry_run=args.dry_run))
    if args.json:
        return _setup_document("install", args, results, descriptions, INSTALL_NEXT_STEPS)
    if args.dry_run:
        print("Dry run: nothing was changed. install would do this:")
    failed = _print_steps(results)
    if not failed and not args.dry_run:
        print(f"\nNext: open a new terminal tab. Claude Code sessions running now are recorded from their next "
              f'start; to record one now, run "{PROG} adopt" inside it.')
    return ERROR if failed else OK


def cmd_uninstall(args: argparse.Namespace) -> int:
    descriptions = {setting.id: setting.description for setting in all_settings()}  # before anything is removed
    descriptions["files"] = FILES_STEP
    refused = _confirmation_refused(args, "uninstall", dry_run=False, purge=args.purge, steps=[])
    if refused is not None:
        return refused
    if not (args.yes or args.dry_run or args.json):
        extra = ", and delete its records, log and configuration" if args.purge else ""
        if not _confirm(f"This reverts every setting {PROG} changed{extra}. Proceed? [Y/n] "):
            print("Cancelled. Nothing was changed.")
            return ERROR
    results = installer.uninstall(purge=args.purge, **_set_flags(dry_run=args.dry_run))
    if args.json:
        return _setup_document("uninstall", args, results, descriptions, UNINSTALL_NEXT_STEPS, purge=args.purge)
    if args.dry_run:
        print("Dry run: nothing was changed. uninstall would do this:")
    return ERROR if _print_steps(results) else OK


def cmd_doctor(args: argparse.Namespace) -> int:
    return doctor.run(**_set_flags(as_json=args.json, strict=args.strict))


def cmd_config(args: argparse.Namespace) -> int:
    def document(code: int = OK, key: Optional[str] = None, value: object = None, written: bool = False) -> int:
        cfg = load_config()
        return _emit("config", code, path=str(paths.config_path()),
                     values={name: getattr(cfg, name) for name in CONFIG_DEFAULTS},
                     choices={name: list(options) for name, options in CONFIG_CHOICES.items()},
                     key=key, value=value, written=written)

    current = load_config()
    if args.key is None:
        if args.json:
            return document()
        for key in CONFIG_DEFAULTS:
            print(f"{key} = {getattr(current, key)}")
        return OK
    if args.key not in CONFIG_DEFAULTS:
        return _fail(args, "config", USAGE,
                     f'unknown config key "{args.key}"; known keys: {", ".join(CONFIG_DEFAULTS)}')
    if args.value is None:
        if args.json:
            return document(key=args.key, value=getattr(current, args.key))
        print(getattr(current, args.key))
        return OK
    try:
        set_value(args.key, args.value)
    except ValueError as exc:
        return _fail(args, "config", USAGE, str(exc))
    if args.json:
        return document(key=args.key, value=getattr(load_config(), args.key), written=True)
    print(f"{args.key} = {args.value}")
    return OK


def cmd_version(args: argparse.Namespace) -> int:
    if args.json:
        return _emit("version", OK, python=platform.python_version(), executable=sys.executable,
                     package_dir=str(Path(__file__).resolve().parent))
    print(f"{PROG} {__version__}")
    return OK


# ---------------------------------------------------------------------------------------------------- main

class _UsageError(Exception):
    def __init__(self, parser: argparse.ArgumentParser, message: str):
        super().__init__(message)
        self.parser, self.message = parser, message


class _Parser(argparse.ArgumentParser):
    """Raises instead of exiting on bad arguments, so a --json run can still print its one document.
    Subparsers inherit the class (add_subparsers passes parser_class=type(self))."""

    def error(self, message):
        raise _UsageError(self, message)


def _positive_int(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return int(value)


def _json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true",
                        help="print one JSON object instead of text (schema in docs/json/)")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=PROG, epilog=EXIT_CODES,
        description="Resume AI coding agent sessions in their terminal panes after a restart. "
                    "With no command, resumes what died in this pane (like here).")
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    def launching(name: str, help_text: str) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text, epilog=EXIT_CODES)
        command.add_argument("--print", action="store_true",
                             help="print the resume command and exit 0; launch nothing, change nothing")
        command.add_argument("--json", action="store_true",
                             help="print one JSON object with the resume command (implies --print; schema in "
                                  "docs/json/)")
        return command

    here = launching("here", "resume what died in this pane (the default); needs a terminal")
    here.add_argument("--yes", "-y", action="store_true",
                      help="if nothing is recorded for this pane, search its scrollback and resume a strong "
                           "match without asking (a weak or ambiguous match still asks)")
    ls = sub.add_parser("ls", help="list sessions that can be resurrected, newest first")
    ls.add_argument("--all", action="store_true", help="also list sessions whose agent is still running")
    ls.add_argument("--ids", action="store_true", help="add a session id column")
    _json_flag(ls)
    resume = launching("resume", "resume a session by id (or by number from ls) in this pane; needs a terminal")
    resume.add_argument("target", metavar="SESSION_ID|N")
    find = sub.add_parser("find", help="find a session by text from its conversation and print its resume command")
    find.add_argument("text", nargs="*", help="text to search for (default: stdin)")
    find.add_argument("--clipboard", action="store_true", help="search for the text on the clipboard")
    find.add_argument("--from-pane", action="store_true",
                      help="search this pane's recent scrollback (iTerm2), so nothing needs to be copied")
    find.add_argument("--limit", type=_positive_int, default=5, help="candidates to show (default 5)")
    _json_flag(find)
    simulate = sub.add_parser("simulate", help="show what automatic restore would do in each recorded pane, "
                                               "without doing it")
    simulate.add_argument("--pane-key", metavar="KEY", help="only this pane (e.g. iterm-<GUID>)")
    simulate.add_argument("--assume-restart", action="store_true",
                          help="treat recorded agent processes as stopped, as after a restart")
    _json_flag(simulate)
    adopt = sub.add_parser("adopt", help="record the agent session this runs inside (for sessions started "
                                         "before installing)")
    _json_flag(adopt)
    sub.add_parser("auto", help="automatic restore; run by the shell on its first prompt")
    sub.add_parser("hook", help="record a session; run by the agent's plugin hooks")
    doctor_parser = sub.add_parser("doctor", help="check the installation and print a fix for each problem")
    doctor_parser.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    _json_flag(doctor_parser)
    install = sub.add_parser("install", help="apply the settings agent-resurrect needs")
    install.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    install.add_argument("--dry-run", action="store_true", help="show what would change; change nothing")
    install.add_argument("--no-plugin", action="store_true",
                         help=f"leave out the Claude Code plugin ({PLUGIN_SETTING})")
    _json_flag(install)
    uninstall = sub.add_parser("uninstall", help="revert every setting agent-resurrect changed")
    uninstall.add_argument("--purge", action="store_true", help="also delete records, log and configuration")
    uninstall.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    uninstall.add_argument("--dry-run", action="store_true", help="show what would be reverted; change nothing")
    _json_flag(uninstall)
    config = sub.add_parser("config", help="show or change configuration")
    config.add_argument("key", nargs="?", help=", ".join(CONFIG_DEFAULTS))
    config.add_argument("value", nargs="?")
    _json_flag(config)
    version = sub.add_parser("version", help="print the version")
    _json_flag(version)
    return parser


COMMANDS: Dict[str, Callable[[argparse.Namespace], int]] = {
    "here": cmd_here, "ls": cmd_ls, "resume": cmd_resume, "find": cmd_find, "simulate": cmd_simulate,
    "adopt": cmd_adopt, "auto": cmd_auto, "doctor": cmd_doctor, "install": cmd_install,
    "uninstall": cmd_uninstall, "config": cmd_config, "version": cmd_version,
}
TOP_LEVEL_FLAGS = ("-h", "--help", "--version")


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["hook"]:
        return _hook(argv[1:])  # before argparse, which would print usage and exit 2 on odd arguments
    if not argv or (argv[0].startswith("-") and argv[0] not in TOP_LEVEL_FLAGS):
        argv = ["here", *argv]  # the bare command is `here`, flags included
    command = argv[0] if argv[0] in COMMANDS else "cli"
    try:
        args = _parser().parse_args(argv)
    except _UsageError as exc:
        exc.parser.print_usage(sys.stderr)
        print(f"{exc.parser.prog}: error: {printable(exc.message)}", file=sys.stderr)
        if "--json" in argv:
            return _emit(command, USAGE, exc.message)
        return USAGE
    except SystemExit as exc:  # --help and --version
        return exc.code if isinstance(exc.code, int) else (OK if exc.code is None else USAGE)
    command = args.command or "here"
    wants_json = getattr(args, "json", False)
    try:
        return COMMANDS[command](args)
    except KeyboardInterrupt:
        if wants_json:
            return _emit(command, INTERRUPTED, "interrupted")
        print()
        return INTERRUPTED
    except Exception as exc:  # one line and a log entry, never a traceback
        _log_quietly(f"{command}: {traceback.format_exc()}")
        message = f"{command} failed: {exc.__class__.__name__}: {exc} (details in {paths.log_path()})"
        if wants_json:
            return _emit(command, ERROR, message)
        _error(message)
        return ERROR
