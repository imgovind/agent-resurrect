"""Claude Code adapter: plugin hook events become pane records, and sessions resume with `claude --resume`."""
import os
from pathlib import Path
from typing import List, Optional

from .. import finder, logs, paths, store
from ..config import Config
from ..model import STATUS_LIVE, Candidate, Record, SessionInfo
from ..procs import ProcInfo
from .base import AgentAdapter, HookContext

NAME = "claude"
SESSION_START = "SessionStart"
SESSION_END = "SessionEnd"
# SessionEnd reasons that mean the user quit on purpose. Every other reason ("other" after a reboot,
# crash or closed tab; "clear" and "resume", which a SessionStart follows) keeps the record.
DELIBERATE_EXITS = ("prompt_input_exit", "logout")
BYPASS_MODE = "bypassPermissions"
SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"


def is_claude_process(p: ProcInfo) -> bool:
    """True for an interactive or headless Claude Code process, however it was installed."""
    if p.name == NAME:
        return True
    if p.name in ("node", "bun") and "claude-code" in p.args:
        return True
    tokens = p.args.split()
    return bool(tokens) and os.path.basename(tokens[0]) == NAME


def _print_mode(args: str) -> bool:
    return any(t in ("-p", "--print") or t.startswith("--print=") for t in args.split())


def _mode_from_args(args: str) -> Optional[str]:
    """The permission mode the agent was started with. SessionStart payloads (Claude Code 2.1.296) carry
    no permission_mode, so this is the fallback that keeps a bypass session resumable as one."""
    tokens = args.split()
    if "--dangerously-skip-permissions" in tokens:
        return "bypassPermissions"
    for i, token in enumerate(tokens):
        if token.startswith("--permission-mode="):
            return token.split("=", 1)[1] or None
        if token == "--permission-mode" and i + 1 < len(tokens):
            return tokens[i + 1]
    return None


def _skip_reason(agent: ProcInfo) -> Optional[str]:
    """Why this claude process is not an interactive session worth resuming, or None if it is."""
    if not agent.has_tty:
        return "no terminal"
    if not agent.foreground:
        return "not the foreground job of its terminal"
    if _print_mode(agent.args):
        return "print mode"
    return None


def _text(payload: dict, key: str) -> Optional[str]:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _projects_dir() -> Path:
    return paths.claude_config_dir() / "projects"


class ClaudeAdapter(AgentAdapter):
    name = NAME

    # Recording

    def on_hook(self, event: str, payload: dict, ctx: HookContext) -> None:
        try:
            self._on_hook(event, payload, ctx)
        except Exception as exc:  # a hook must never fail the agent; leave a trace in the log instead
            logs.log(f"claude {event}: {type(exc).__name__}: {exc}")

    def _on_hook(self, event: str, payload: dict, ctx: HookContext) -> None:
        if event not in (SESSION_START, SESSION_END):
            logs.log(f"claude: ignoring unknown hook event {event!r}")
            return
        session_id = _text(payload, "session_id") if isinstance(payload, dict) else None
        if session_id is None:
            logs.log(f"claude {event}: payload has no session_id")
            return
        chain = ctx.procs.ancestors(ctx.pid)
        index = next((i for i, p in enumerate(chain) if is_claude_process(p)), None)
        if index is None:
            logs.log(f"claude {event}: no claude process among the ancestors of pid {ctx.pid}")
            return
        agent = chain[index]
        # A claude started by another claude's tool call (e.g. `claude -p`) shares the pane's environment;
        # it must neither overwrite the pane's record nor delete it.
        if any(is_claude_process(p) for p in chain[index + 1:]):
            logs.log(f"claude {event}: ignoring nested claude pid {agent.pid} (session {session_id})")
            return
        if event == SESSION_START:
            self._record(session_id, payload, agent, ctx)
        else:
            self._forget(session_id, payload, ctx)

    def _record(self, session_id: str, payload: dict, agent: ProcInfo, ctx: HookContext) -> None:
        reason = _skip_reason(agent)
        if reason:
            logs.log(f"claude SessionStart: not recording pid {agent.pid} ({reason})")
            return
        record = Record(terminal=ctx.terminal.name, pane_key=ctx.pane_key, pane_label=ctx.pane_label,
                        agent=NAME, session_id=session_id, cwd=_text(payload, "cwd") or "",
                        transcript_path=_text(payload, "transcript_path"),
                        permission_mode=_text(payload, "permission_mode") or _mode_from_args(agent.args),
                        source=_text(payload, "source"),
                        agent_pid=agent.pid, agent_started=agent.started, status=STATUS_LIVE)
        existing = store.load(ctx.pane_key)
        if existing is not None and existing.session_id == session_id:
            record.recorded_at = existing.recorded_at
        store.save(record)

    def _forget(self, session_id: str, payload: dict, ctx: HookContext) -> None:
        if payload.get("reason") not in DELIBERATE_EXITS:
            return
        existing = store.load(ctx.pane_key)
        if existing is None or existing.session_id != session_id:
            return  # the pane now runs another session (or nothing recorded); leave it alone
        store.delete(ctx.pane_key)
        logs.log(f"claude SessionEnd: {payload.get('reason')}; forgot session {session_id} in {ctx.pane_key}")

    # Resuming

    def _transcript(self, record: Record) -> Optional[Path]:
        if record.transcript_path:
            path = Path(record.transcript_path)
            if path.is_file():
                return path
        return finder.find_transcript(record.session_id, _projects_dir())

    def session_exists(self, record: Record) -> bool:
        return self._transcript(record) is not None

    def _current_mode(self, record: Record) -> Optional[str]:
        """The mode the session was last in: the transcript is newer than the SessionStart payload."""
        path = self._transcript(record)
        return (finder.last_permission_mode(path) if path else None) or record.permission_mode

    def resume_argv(self, record: Record, config: Config) -> List[str]:
        argv = [NAME, "--resume", record.session_id]
        flag = config.permission_flag
        # Claude Code restores every other mode itself; bypass must be asked for again.
        if flag == "always" or (flag == "mirror" and self._current_mode(record) == BYPASS_MODE):
            argv.append(SKIP_PERMISSIONS_FLAG)
        return argv

    def describe(self, record: Record) -> SessionInfo:
        path = self._transcript(record)
        if path is None:
            return SessionInfo()
        try:
            return finder.transcript_meta(path).info
        except OSError as exc:
            logs.log(f"claude: cannot read transcript {path}: {exc}")
            return SessionInfo()

    def session_record(self, session_id: str) -> Optional[Record]:
        path = finder.find_transcript(session_id, _projects_dir())
        if path is None:
            return None
        try:
            meta = finder.transcript_meta(path)
        except OSError as exc:
            logs.log(f"claude: cannot read transcript {path}: {exc}")
            return None
        return Record(terminal="", pane_key="", agent=NAME, session_id=session_id, cwd=meta.cwd or "",
                      transcript_path=str(path), permission_mode=meta.permission_mode)

    def find_by_text(self, text: str, exclude: Optional[str] = None, limit: int = 5) -> List[Candidate]:
        """Raises ValueError if the text is too short to search for."""
        exclude = exclude or os.environ.get("CLAUDE_CODE_SESSION_ID")
        return finder.search(text, _projects_dir(), exclude=exclude, limit=limit)
