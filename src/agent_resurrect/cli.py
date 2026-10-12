"""Command line: argument parsing and output. The behaviour lives in the modules this file calls."""
import argparse
import io
import json
import os
import subprocess
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from . import __version__, doctor, installer, logs, paths, restore, store
from .agents import all_agents, get_agent
from .agents.base import AgentAdapter, HookContext
from .config import DEFAULTS as CONFIG_DEFAULTS
from .config import load_config, set_value
from .model import Candidate, Record, SessionInfo, parse_iso
from .procs import Procs
from .settings import all_settings
from .terminals import detect_terminal

PROG = "agent-resurrect"
HOOK_STDIN_LIMIT = 1024 * 1024
NO_MATCH = ("No session contains that text. Try a different snippet: plain prose from one line, "
            "not a table, code block or heavily formatted output.")


def _error(message: str) -> None:
    print(f"{PROG}: {message}", file=sys.stderr)


def _log_quietly(message: str) -> None:
    try:
        logs.log(message)
    except Exception:
        pass


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
    return 0


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
    env = os.environ
    terminal = detect_terminal(env)
    pane_key = terminal.pane_key(env) if terminal is not None else None
    if not pane_key or not store.valid_pane_key(pane_key):
        return  # not in a pane of a supported terminal: nothing to record
    ctx = HookContext(env=env, terminal=terminal, pane_key=pane_key, pane_label=terminal.pane_label(env),
                      procs=Procs(), pid=os.getpid())
    agent.on_hook(event, payload, ctx)


# ---------------------------------------------------------------------------------------------------- listing

@dataclass
class _Entry:
    record: Record
    info: SessionInfo
    running: bool


def _describe(agent: Optional[AgentAdapter], record: Record) -> SessionInfo:
    if agent is None:
        return SessionInfo()
    try:
        return agent.describe(record)
    except Exception as exc:  # one unreadable transcript must not break the listing
        logs.log(f"describe {record.session_id}: {exc}")
        return SessionInfo()


def _entries(procs: Procs, include_running: bool) -> List[_Entry]:
    """Stored records, newest first. Without include_running, those whose agent still runs are left out."""
    entries = []
    for record in store.list_records():
        running = procs.is_alive(record.agent_pid, record.agent_started)
        if running and not include_running:
            continue
        entries.append(_Entry(record, _describe(get_agent(record.agent), record), running))
    return entries


def _one_line(text: Optional[str], width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[:width - 3] + "..."


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
            moment = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        else:
            return None
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return moment.strftime("%Y-%m-%d %H:%M")


def _last_active(entry: _Entry) -> str:
    when = _local_time(epoch=entry.info.last_active)
    if when is None:
        updated = parse_iso(entry.record.updated_at)
        when = updated.astimezone().strftime("%Y-%m-%d %H:%M") if updated else None
    return when or "-"


def _print_table(rows: List[List[str]]) -> None:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        cells = [row[0].rjust(widths[0])] + [cell.ljust(width) for cell, width in zip(row[1:], widths[1:])]
        print("  ".join(cells).rstrip())


def _show_listing(include_running: bool) -> int:
    store.gc(load_config().max_age_days)
    entries = _entries(Procs(), include_running)
    if not entries:
        print("No sessions to resurrect.")
        return 0
    rows, number = [], 0
    for entry in entries:
        record, info = entry.record, entry.info
        if entry.running:
            label = "-"  # unnumbered, so numbers always match the default listing that `resume <n>` uses
        else:
            number += 1
            label = str(number)
        title = _one_line(info.title or info.first_prompt or record.session_id, 48)
        rows.append([label, record.agent, f'"{title}"', _short_path(record.cwd), _last_active(entry),
                     record.pane_label or "-", "running" if entry.running else record.status])
    _print_table(rows)
    return 0


# ---------------------------------------------------------------------------------------------------- resume

def _resume(record: Record) -> int:
    agent = get_agent(record.agent)
    if agent is None:
        _error(f'unknown agent "{record.agent}" for session {record.session_id}')
        return 1
    cfg = load_config()
    decision = restore.check_eligible(record, agent, Procs(), cfg, require_live=False)
    if decision.reason == "agent-running":
        print(f"That session is still running (pid {record.agent_pid}).")
        return 1
    if decision.reason == "session-gone":
        print(f"Session {record.session_id} no longer exists, so it cannot be resumed.")
        return 1
    # restore.launch is looked up now, not bound as resume_record's default, so it can be replaced.
    return restore.resume_record(record, agent, cfg, stdout=sys.stdout, launcher=restore.launch)


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
    env = os.environ
    terminal = detect_terminal(env)
    pane_key = terminal.pane_key(env) if terminal is not None else None
    record = store.load(pane_key) if pane_key and store.valid_pane_key(pane_key) else None
    if record is None:
        print("Nothing recorded for this pane.")
        print()
        return _show_listing(include_running=False)
    return _resume(record)


def cmd_ls(args: argparse.Namespace) -> int:
    return _show_listing(include_running=args.all)


def cmd_resume(args: argparse.Namespace) -> int:
    target = args.target.strip()
    if target.isdigit():
        store.gc(load_config().max_age_days)
        entries = _entries(Procs(), include_running=False)
        index = int(target)
        if not 1 <= index <= len(entries):
            _error(f"no session number {index}; run: {PROG} ls")
            return 1
        return _resume(entries[index - 1].record)
    record = _record_for_session(target)
    if record is None:
        _error(f"no session with id {target}; run: {PROG} ls, or {PROG} find <text>")
        return 1
    return _resume(record)


def cmd_auto(args: argparse.Namespace) -> int:
    try:
        return restore.auto(os.environ)
    except Exception as exc:  # runs at a new shell's first prompt: one line, not a traceback
        _log_quietly(f"auto: {traceback.format_exc()}")
        _error(f"automatic restore failed: {exc} (details in {paths.log_path()})")
        return 1


# ---------------------------------------------------------------------------------------------------- find

def _read_clipboard() -> Optional[str]:
    try:
        result = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _find_text(args: argparse.Namespace) -> Optional[str]:
    """The text to search for, or None (after printing why) if it cannot be read."""
    if args.clipboard:
        text = _read_clipboard()
        if text is None:
            _error("cannot read the clipboard (pbpaste failed)")
        return text
    if args.text:
        return " ".join(args.text)
    stream = sys.stdin
    if stream is not None and not stream.isatty():
        return stream.read()
    return ""


def _roles(roles: Dict[str, int]) -> str:
    ordered = sorted(roles.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{role} x{count}" for role, count in ordered) or "n/a"


def _active(info: SessionInfo) -> str:
    started, last = _local_time(iso=info.started), _local_time(epoch=info.last_active)
    if started and last:
        return f"{started} -> {last}"
    return started or last or "(unknown)"


def _print_best(agent: AgentAdapter, best: Candidate, runner_up: Optional[Candidate]) -> None:
    record = Record(terminal="", pane_key="", agent=agent.name, session_id=best.session_id,
                    cwd=best.cwd or "", transcript_path=best.transcript_path)
    plan = restore.plan_resume(record, agent, load_config())
    print(f"BEST MATCH  ({best.hits}/{best.total_probes} probes; text appears in: {_roles(best.roles)})")
    print(f"  session : {best.session_id}")
    print(f"  agent   : {agent.name}")
    print(f"  title   : {_one_line(best.info.title, 100) or '(none)'}")
    print(f"  opens   : {_one_line(best.info.first_prompt, 100) or '(unknown)'}")
    print(f"  active  : {_active(best.info)}")
    print(f"  cwd     : {best.cwd or '(unknown)'}")
    if best.cwd and plan.warning:
        print(f"  WARNING : {plan.warning}")
    elif best.cwd and not best.cwd_trusted:
        print("  WARNING : the recorded cwd does not match the transcript's project folder; if resume "
              "says no conversation found, try the folder's original directory.")
    # Pastes copied from a terminal lose markdown, so partial coverage is normal; only a lone probe
    # or a tie with the runner-up is a real reason to doubt the match.
    if best.hits == 1 and best.total_probes > 1:
        print("  NOTE    : weak match, only 1 probe found; check the title and opening prompt first.")
    elif runner_up is not None and runner_up.hits == best.hits:
        print("  NOTE    : ambiguous, another session matches equally well (listed below); compare titles.")
    print("\nResume command:\n")
    print(f"  {restore.format_command(plan)}")


def cmd_find(args: argparse.Namespace) -> int:
    """Print the sessions whose history contains the text and how to resume the best one. Never launches."""
    text = _find_text(args)
    if text is None:
        return 2
    if not text.strip():
        _error("no text to search for; pass it as arguments, pipe it on stdin, or use --clipboard")
        return 2
    found: List[Tuple[AgentAdapter, Candidate]] = []
    try:
        for agent in all_agents():
            found.extend((agent, c) for c in agent.find_by_text(text, limit=args.limit))
    except ValueError as exc:  # e.g. too little text to build search probes from
        _error(str(exc))
        return 2
    if not found:
        print(NO_MATCH)
        return 1
    # Each agent ranks its own results; across agents, higher probe coverage first (stable sort).
    found.sort(key=lambda item: -(item[1].hits / max(item[1].total_probes, 1)))
    found = found[:args.limit]
    agent, best = found[0]
    _print_best(agent, best, found[1][1] if len(found) > 1 else None)
    if len(found) > 1:
        print("\nOther candidates:")
        for number, (other_agent, c) in enumerate(found[1:], 2):
            when = _local_time(iso=c.info.started) or _local_time(epoch=c.info.last_active) or ""
            title = _one_line(c.info.title or c.info.first_prompt, 60)
            print(f"  {number}. {c.session_id}  {c.hits}/{c.total_probes}  [{_roles(c.roles)}]  {when}  {title}".rstrip())
    return 0


# ---------------------------------------------------------------------------------------------------- setup

def _interactive() -> bool:
    return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())


def _confirm(prompt: str, default: bool = True) -> bool:
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        print()
        return False
    return default if not answer else answer in ("y", "yes")


def _print_steps(results) -> bool:
    """Print one line per installer step; True if any failed."""
    for result in results:
        print(f"{result.status:<9}  {result.id}  {result.detail}".rstrip())
    return any(result.status == "failed" for result in results)


def cmd_install(args: argparse.Namespace) -> int:
    if not args.yes and _interactive():
        print(f"{PROG} will make these changes ({PROG} uninstall reverts them):")
        for setting in all_settings():
            print(f"  - {setting.description or setting.id}")
        if not _confirm("Proceed? [Y/n] "):
            print("Cancelled. Nothing was changed.")
            return 1
    return 1 if _print_steps(installer.install()) else 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    if not args.yes:
        if not _interactive():
            _error("uninstall needs confirmation; run it in a terminal or pass --yes")
            return 1
        extra = ", and delete its records, log and configuration" if args.purge else ""
        if not _confirm(f"This reverts every setting {PROG} changed{extra}. Proceed? [Y/n] "):
            print("Cancelled. Nothing was changed.")
            return 1
    return 1 if _print_steps(installer.uninstall(purge=args.purge)) else 0


def cmd_doctor(args: argparse.Namespace) -> int:
    return doctor.run()


def cmd_config(args: argparse.Namespace) -> int:
    current = load_config()
    if args.key is None:
        for key in CONFIG_DEFAULTS:
            print(f"{key} = {getattr(current, key)}")
        return 0
    if args.key not in CONFIG_DEFAULTS:
        _error(f'unknown config key "{args.key}"; known keys: {", ".join(CONFIG_DEFAULTS)}')
        return 2
    if args.value is None:
        print(getattr(current, args.key))
        return 0
    try:
        set_value(args.key, args.value)
    except ValueError as exc:
        _error(str(exc))
        return 2
    print(f"{args.key} = {args.value}")
    return 0


# ---------------------------------------------------------------------------------------------------- main

def _positive_int(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return int(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description="Resume AI coding agent sessions in their terminal panes after a restart. "
                               "With no command, resumes what died in this pane.")
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.add_parser("here", help="resume what died in this pane (the default)")
    ls = sub.add_parser("ls", help="list sessions that can be resurrected, newest first")
    ls.add_argument("--all", action="store_true", help="also list sessions whose agent is still running")
    resume = sub.add_parser("resume", help="resume a listed session (by number) or any session id, in this pane")
    resume.add_argument("target", metavar="N|SESSION_ID")
    find = sub.add_parser("find", help="find a session by text from its conversation and print its resume command")
    find.add_argument("text", nargs="*", help="text to search for (default: stdin)")
    find.add_argument("--clipboard", action="store_true", help="search for the text on the clipboard")
    find.add_argument("--limit", type=_positive_int, default=5, help="candidates to show (default 5)")
    sub.add_parser("auto", help="automatic restore; run by the shell on its first prompt")
    sub.add_parser("hook", help="record a session; run by the agent's plugin hooks")
    sub.add_parser("doctor", help="check the installation and print a fix for each problem")
    install = sub.add_parser("install", help="apply the settings agent-resurrect needs")
    install.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    uninstall = sub.add_parser("uninstall", help="revert every setting agent-resurrect changed")
    uninstall.add_argument("--purge", action="store_true", help="also delete records, log and configuration")
    uninstall.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    config = sub.add_parser("config", help="show or change configuration")
    config.add_argument("key", nargs="?", help=", ".join(CONFIG_DEFAULTS))
    config.add_argument("value", nargs="?")
    return parser


COMMANDS: Dict[str, Callable[[argparse.Namespace], int]] = {
    "here": cmd_here, "ls": cmd_ls, "resume": cmd_resume, "find": cmd_find, "auto": cmd_auto,
    "doctor": cmd_doctor, "install": cmd_install, "uninstall": cmd_uninstall, "config": cmd_config,
}


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["hook"]:
        return _hook(argv[1:])  # before argparse, which would print usage and exit 2 on odd arguments
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # --help, --version and usage errors
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
    try:
        return COMMANDS[args.command or "here"](args)
    except KeyboardInterrupt:
        print()
        return 130
