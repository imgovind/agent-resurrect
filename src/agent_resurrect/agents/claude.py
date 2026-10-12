"""Claude Code adapter: plugin hook events become pane records, and sessions resume with `claude --resume`."""
import os
import shutil
from collections.abc import Mapping as AnyMapping
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .. import finder, logs, paths, store
from ..config import Config
from ..model import STATUS_LIVE, Candidate, Record, SessionInfo
from ..procs import ProcInfo
from .base import AgentAdapter, HookContext

NAME = "claude"
SESSION_START = "SessionStart"
SESSION_END = "SessionEnd"
ADOPT_SOURCE = "adopt"
# SessionEnd reasons that mean the user quit on purpose. Every other reason ("other" after a reboot,
# crash or closed tab; "clear" and "resume", which a SessionStart follows) keeps the record.
DELIBERATE_EXITS = ("prompt_input_exit", "logout")
BYPASS_MODE = "bypassPermissions"
SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
# Claude Code keeps sessions under this directory (default ~/.claude). A session started with it set (an
# alias such as `CLAUDE_CONFIG_DIR=~/.claude-work claude` for a second account) must be looked up and
# resumed with it set again, so the record carries it in resume_env.
CONFIG_DIR_VAR = "CLAUDE_CONFIG_DIR"
# Set by Claude Code in the environment of every tool call (Bash tool), together with CLAUDE_PID.
SESSION_ID_VAR = "CLAUDE_CODE_SESSION_ID"
PID_VAR = "CLAUDE_PID"
# Agent-team teammates. Investigated read-only on Claude Code 2.1.296 (2026-10-12), with teammates in iTerm2
# split panes:
# - In `ps`, a teammate is the native installer's versioned binary started by full path, e.g.
#   `~/.local/share/claude/versions/2.1.296 --agent-id w@team --agent-name w --team-name team
#   --agent-color purple --parent-session-id <lead session id>`. Its parent is the new pane's login shell,
#   and it has its own tty, its own ITERM_SESSION_ID and the foreground "+" stat. So the nested and
#   interactivity rules do not catch it; in 0.1.0 it was skipped only because its argv0 is not `claude`.
# - No environment variable marks a teammate. Its process environment adds only CLAUDECODE=1 (set by every
#   Claude Code for its children, so hooks always see it) and CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 (the
#   feature flag, which the lead has too). CLAUDE_CODE_CHILD_SESSION=1 and CLAUDE_PID, seen in a teammate's
#   tool calls, are set by every Claude Code for every tool call.
# So the command-line flags are the marker. Reading them from `ps` text is safe here, unlike the permission
# mode: a false match (a prompt that quotes a flag) only means a session is not recorded.
TEAMMATE_FLAGS = ("--agent-id", "--team-name", "--parent-session-id")


def _is_claude_path(path: str) -> bool:
    """`.../claude`, or the native installer's versioned binary `.../claude/versions/<version>`."""
    if os.path.basename(path) == NAME:
        return True
    versions = os.path.dirname(path)
    return os.path.basename(versions) == "versions" and os.path.basename(os.path.dirname(versions)) == NAME


def is_claude_process(p: ProcInfo) -> bool:
    """True for an interactive or headless Claude Code process, however it was installed."""
    if _is_claude_path(p.comm.lstrip("-")):  # comm is the full path when it was started by one
        return True
    if p.name in ("node", "bun") and "claude-code" in p.args:
        return True
    tokens = p.args.split()
    return bool(tokens) and _is_claude_path(tokens[0])


def _has_flag(args: str, flags: Sequence[str]) -> bool:
    """True if a token is one of the flags, alone or as --flag=value."""
    return any(t.split("=", 1)[0] in flags for t in args.split())


def _first_claude(chain: Sequence[ProcInfo]) -> Tuple[Optional[ProcInfo], bool]:
    """The first claude process in an ancestor chain, and whether another claude runs above it. A claude
    started by another claude's tool call (e.g. `claude -p`) shares the pane's environment; it must neither
    overwrite the pane's record nor delete it."""
    index = next((i for i, p in enumerate(chain) if is_claude_process(p)), None)
    if index is None:
        return None, False
    return chain[index], any(is_claude_process(p) for p in chain[index + 1:])


def _print_mode(args: str) -> bool:
    return any(t in ("-p", "--print") or t.startswith("--print=") for t in args.split())


def _skip_reason(agent: ProcInfo) -> Optional[str]:
    """Why this claude process is not an interactive session worth resuming, or None if it is."""
    if not agent.has_tty:
        return "no terminal"
    if not agent.foreground:
        return "not the foreground job of its terminal"
    if _print_mode(agent.args):
        return "print mode"
    if _has_flag(agent.args, TEAMMATE_FLAGS):
        return "agent-team teammate; its lead's session is the one to resume"
    return None


def _text(payload: dict, key: str) -> Optional[str]:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _config_dir(env: Optional[Mapping[str, str]]) -> Optional[str]:
    """CLAUDE_CONFIG_DIR from an environment (a process's, or a record's resume_env read from disk)."""
    value = env.get(CONFIG_DIR_VAR) if isinstance(env, AnyMapping) else None
    return value if isinstance(value, str) and value else None


def _resume_env(env: Mapping[str, str]) -> Optional[Dict[str, str]]:
    value = _config_dir(env)
    return {CONFIG_DIR_VAR: value} if value else None


def _projects_dir(config_dir: Optional[str] = None) -> Path:
    """<config dir>/projects: the given one (a record's), else the environment's (paths honours the variable)."""
    return (Path(config_dir) if config_dir else paths.claude_config_dir()) / "projects"


def _executable() -> str:
    """"claude" when PATH finds it. Otherwise the old "local install", ~/.claude/local/claude, which users
    reach through a shell alias that exec cannot see. Otherwise "claude", so a failed launch names it."""
    if shutil.which(NAME):
        return NAME
    local = paths.home() / ".claude" / "local" / NAME
    return str(local) if local.is_file() and os.access(local, os.X_OK) else NAME


class ClaudeAdapter(AgentAdapter):
    name = NAME

    # Recording

    def on_hook(self, event: str, payload: dict, ctx: HookContext) -> None:
        try:
            self._on_hook(event, payload, ctx)
        except Exception as exc:  # a hook must never fail the agent; leave a trace in the log instead
            logs.log(f"claude {event}: {type(exc).__name__}: {exc}")

    def _on_hook(self, event: str, payload: dict, ctx: HookContext) -> None:
        label = f"claude {event}"
        if event not in (SESSION_START, SESSION_END):
            logs.log(f"claude: ignoring unknown hook event {event!r}")
            return
        raw_id = payload.get("session_id") if isinstance(payload, dict) else None
        if not raw_id:
            logs.log(f"{label}: payload has no session_id")
            return
        if not finder.valid_session_id(raw_id):
            logs.log(f"{label}: unusable session_id {raw_id!r}")
            return
        if event == SESSION_END:
            self._forget(raw_id, payload, ctx)
            return
        agent = self._interactive_agent(ctx, label, raw_id)
        if agent is not None:
            self._save(Record(
                terminal=ctx.terminal.name, pane_key=ctx.pane_key, pane_label=ctx.pane_label, agent=NAME,
                session_id=raw_id, cwd=_text(payload, "cwd") or "", transcript_path=_text(payload, "transcript_path"),
                permission_mode=_text(payload, "permission_mode"), source=_text(payload, "source"),
                agent_pid=agent.pid, agent_started=agent.started, resume_env=_resume_env(ctx.env),
                status=STATUS_LIVE))

    def _interactive_agent(self, ctx: HookContext, label: str, session_id: str) -> Optional[ProcInfo]:
        """The claude process of this pane's session: the first claude among the ancestors of ctx.pid, if it
        is not nested in another claude and is an interactive, foreground, non-teammate session."""
        agent, nested = _first_claude(ctx.procs.ancestors(ctx.pid))
        if agent is None:
            logs.log(f"{label}: no claude process among the ancestors of pid {ctx.pid}")
            return None
        if nested:
            logs.log(f"{label}: ignoring nested claude pid {agent.pid} (session {session_id})")
            return None
        reason = _skip_reason(agent)
        if reason:
            logs.log(f"{label}: not recording pid {agent.pid} ({reason})")
            return None
        return agent

    def _save(self, record: Record) -> Record:
        existing = store.load(record.pane_key)
        if existing is not None and existing.session_id == record.session_id:
            record.recorded_at = existing.recorded_at
        return store.save(record)

    def _forget(self, session_id: str, payload: dict, ctx: HookContext) -> None:
        """SessionEnd. Hooks there share a 1.5 s budget that the plugin's own timeout does not raise, so the
        common cases return after one record read, and only a deliberate exit of the pane's recorded session
        pays for the process walk (~100 ms of `ps`)."""
        reason = payload.get("reason")
        if reason not in DELIBERATE_EXITS:
            return
        existing = store.load(ctx.pane_key)
        if existing is None or existing.session_id != session_id:
            return  # the pane now runs another session (or nothing recorded); leave it alone
        # Only positive evidence of nesting keeps the record: a record left live after /exit would resurrect
        # a session the user quit on purpose, so a walk that cannot see the agent (ps failed) still deletes.
        agent, nested = _first_claude(ctx.procs.ancestors(ctx.pid))
        if nested:
            logs.log(f"claude SessionEnd: ignoring nested claude pid {agent.pid} (session {session_id})")
            return
        store.delete(ctx.pane_key)
        logs.log(f"claude SessionEnd: {reason}; forgot session {session_id} in {ctx.pane_key}")

    def adopt(self, ctx: HookContext) -> Optional[Record]:
        """Record the Claude Code session that this process runs in, from one of its tool calls: for sessions
        started before the plugin was installed, whose SessionStart has already fired.

        Needs CLAUDE_CODE_SESSION_ID in ctx.env (Claude Code sets it for tool calls), a valid ctx.pane_key,
        an interactive claude ancestor (the SessionStart rules: not nested, tty, foreground, not print mode,
        not a teammate) that is CLAUDE_PID when that is set, the session's transcript in ctx.env's
        CLAUDE_CONFIG_DIR (or the default), and a working directory: the transcript's trusted cwd, else
        PWD. Returns the saved record, or None with a log line when anything is missing. Raises OSError only
        when the record cannot be written."""
        try:
            record = self._adoptable(ctx)
        except Exception as exc:  # inspecting must never crash the command; say why in the log
            logs.log(f"claude adopt: {type(exc).__name__}: {exc}")
            return None
        return self._save(record) if record is not None else None

    def _adoptable(self, ctx: HookContext) -> Optional[Record]:
        label = "claude adopt"
        if not store.valid_pane_key(ctx.pane_key):
            logs.log(f"{label}: invalid pane key {ctx.pane_key!r}")
            return None
        session_id = ctx.env.get(SESSION_ID_VAR)
        if not session_id:
            logs.log(f"{label}: {SESSION_ID_VAR} is not set; not running inside a Claude Code session")
            return None
        if not finder.valid_session_id(session_id):
            logs.log(f"{label}: unusable session id {session_id!r}")
            return None
        agent = self._interactive_agent(ctx, label, session_id)
        if agent is None:
            return None
        claude_pid = ctx.env.get(PID_VAR)
        if claude_pid and claude_pid != str(agent.pid):
            # The session id belongs to the claude that set CLAUDE_PID; a different ancestor is not it.
            logs.log(f"{label}: {PID_VAR}={claude_pid!r} is not the claude process found (pid {agent.pid})")
            return None
        projects = _projects_dir(_config_dir(ctx.env))
        path = finder.find_transcript(session_id, projects)
        if path is None:
            logs.log(f"{label}: no transcript for session {session_id} under {projects}")
            return None
        meta = finder.transcript_meta(path)
        cwd = meta.cwd if meta.cwd and meta.cwd_trusted else ctx.env.get("PWD")
        if not cwd:
            logs.log(f"{label}: no working directory (transcript cwd {meta.cwd!r} not trusted, PWD unset)")
            return None
        return Record(terminal=ctx.terminal.name, pane_key=ctx.pane_key, pane_label=ctx.pane_label, agent=NAME,
                      session_id=session_id, cwd=cwd, transcript_path=str(path),
                      permission_mode=meta.permission_mode, source=ADOPT_SOURCE, agent_pid=agent.pid,
                      agent_started=agent.started, resume_env=_resume_env(ctx.env), status=STATUS_LIVE)

    # Resuming

    def _transcript(self, record: Record) -> Optional[Path]:
        if record.transcript_path:
            path = Path(record.transcript_path)
            if path.is_file():
                return path
        return finder.find_transcript(record.session_id, _projects_dir(_config_dir(record.resume_env)))

    def session_exists(self, record: Record) -> bool:
        return self._transcript(record) is not None

    def _current_mode(self, record: Record) -> Optional[str]:
        """The mode the session was last in: the transcript is newer than the SessionStart payload."""
        path = self._transcript(record)
        return (finder.last_permission_mode(path) if path else None) or record.permission_mode

    def resume_argv(self, record: Record, config: Config) -> List[str]:
        argv = [_executable(), "--resume", record.session_id]
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
            return finder.session_info(path)  # head and tail only: this runs for every pane in `ls`
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
                      transcript_path=str(path), permission_mode=meta.permission_mode,
                      resume_env=_resume_env(os.environ))

    def find_by_text(self, text: str, exclude: Optional[str] = None, limit: int = 5) -> List[Candidate]:
        """Raises ValueError if the text is too short to search for."""
        exclude = exclude or os.environ.get(SESSION_ID_VAR)
        return finder.search(text, _projects_dir(), exclude=exclude, limit=limit)
