"""`agent-resurrect doctor`: check everything a restore depends on, one line per check, with a fix for each
problem. `--json` prints the same checks as one document for agents; `--strict` makes a WARN exit ERROR.

Check ids are stable (agents key on them): `python`, `launcher.on_path`, `shell.login`, every `Setting.id`,
`shell.zdotdir`, `shell.exec_before_block`, `terminal.pane`, `terminal.multiplexer`, `records`,
`restores.since_boot` and `records.died_since_boot`. The `shell.zdotdir`, `shell.exec_before_block`,
`terminal.multiplexer` and `records.died_since_boot` checks only appear when they find a problem.

Every FAIL has a fix, and so does every WARN that someone can act on. `fix.kind` says who acts:
  command        the agent may run `argv` once (when `agent_may_run`), then run doctor again
  new-shell      fixes itself in a new terminal tab
  restart-agent  restart the running agent sessions, or run /reload-plugins
  user-action    a person edits a file, changes a setting or works differently; agents only report it
  install-app    a person installs iTerm2, Claude Code or the Command Line Tools; agents ask first
`argv` is set only for `command`, and agents never run anything for the other kinds.
"""
import os
import pwd
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, TextIO, Tuple

from . import exitcodes, jsonout, paths, store
from .agents import get_agent
from .config import load_config
from .display import printable
from .model import STATUS_LIVE, Record, parse_iso
from .procs import Procs
from .settings import all_settings
from .settings.base import Setting
from .terminals import detect_terminal

OK, WARN, FAIL = "ok", "warn", "fail"
COMMAND, NEW_SHELL, RESTART_AGENT, USER_ACTION, INSTALL_APP = (
    "command", "new-shell", "restart-agent", "user-action", "install-app")
FIX_KINDS = (COMMAND, NEW_SHELL, RESTART_AGENT, USER_ACTION, INSTALL_APP)

# A failing setting whose `applicable()` is False is missing its app (iTerm2 or Claude Code): a person installs it.
_APP_PREFIXES = ("iterm.", "claude.")
# A shell setting that does not apply is simply not needed (no bash rc file; zsh is not the login shell).
_OPTIONAL_PREFIXES = ("shell.",)
SUPPORTED_SHELLS = ("zsh", "bash")
MULTIPLEXERS = (("TMUX", "tmux"), ("STY", "screen"), ("ZELLIJ", "zellij"))
# Restore events always counted in the JSON document, so agents can read them without a key check.
SINCE_BOOT_RESULTS = ("resumed", "skipped", "launch-failed", "offered", "not-a-tty")

# `sysctl -n kern.boottime` prints "{ sec = 1791785041, usec = 851234 } Mon Oct 12 11:34:01 2026".
# Anchor on "{ sec =": a bare "sec =" also matches inside "usec =".
BOOT_TIME = re.compile(r"\{ sec = (\d+),")


def parse_boot_time(text: str) -> Optional[int]:
    match = BOOT_TIME.search(text or "")
    return int(match.group(1)) if match else None


def _run_sysctl(argv: List[str]) -> str:
    # sysctl lives in /usr/sbin, which a minimal PATH (an agent's tool shell, launchd) can lack.
    executable = shutil.which(argv[0]) or os.path.join("/usr/sbin", argv[0])
    try:
        result = subprocess.run([executable, *argv[1:]], capture_output=True, text=True, timeout=5,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def read_boot_time(run: Optional[Callable[[List[str]], str]] = None) -> Optional[int]:
    """Epoch seconds of the last boot, or None if it cannot be read."""
    return parse_boot_time((run or _run_sysctl)(["sysctl", "-n", "kern.boottime"]))


# --- results ---------------------------------------------------------------------------------------------

@dataclass
class Fix:
    kind: str
    agent_may_run: bool
    text: str
    argv: Optional[List[str]] = None

    def to_dict(self) -> dict:
        return {"kind": self.kind, "agent_may_run": self.agent_may_run, "text": self.text,
                "argv": list(self.argv) if self.argv is not None else None}


def _person(kind: str, text: str) -> Fix:
    return Fix(kind, False, text)


@dataclass
class Result:
    id: str
    status: str
    detail: str
    fix: Optional[Fix] = None
    line: Optional[str] = None  # the text line when it differs from `detail` (settings prefix their id)
    gates_ready: bool = False   # a non-OK status means automatic restore will not work

    def to_dict(self) -> dict:
        return {"id": self.id, "status": self.status, "detail": self.detail,
                "fix": self.fix.to_dict() if self.fix else None}


@dataclass
class _Context:
    launcher: Path
    procs: Procs
    boot: Optional[datetime]
    env: Mapping[str, str]
    login_shell: Optional[str] = None
    results: List[Result] = field(default_factory=list)

    def add(self, result: Result) -> None:
        self.results.append(result)


def _tilde(path: Path) -> str:
    home = str(paths.home()).rstrip("/")
    text = str(path)
    return "~" + text[len(home):] if home and text.startswith(home + "/") else text


def _install_fix(ctx: _Context, text: str = "") -> Fix:
    """Re-run the installer step: `<launcher> install --yes`. Agents may run it only if the launcher exists."""
    launcher = ctx.launcher
    return Fix(COMMAND, os.access(launcher, os.X_OK), text or f"run: {_tilde(launcher)} install",
               [str(launcher), "install", "--yes"])


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


# --- checks ----------------------------------------------------------------------------------------------

def _check_python(ctx: _Context) -> None:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info >= (3, 9):
        ctx.add(Result("python", OK, f"python {version} ({sys.executable})", gates_ready=True))
    else:
        ctx.add(Result("python", FAIL, f"python {version} is too old; 3.9 or newer is required",
                       _person(INSTALL_APP, "install Apple's command line tools with: xcode-select --install "
                                            "(it opens a dialog that needs a click)"), gates_ready=True))


def _check_launcher(ctx: _Context) -> None:
    found = shutil.which("agent-resurrect")
    if found:
        ctx.add(Result("launcher.on_path", OK, f"agent-resurrect is on PATH ({found})"))
    elif ctx.launcher.exists():
        ctx.add(Result("launcher.on_path", WARN, f"{ctx.launcher} is not on PATH",
                       _person(NEW_SHELL, "open a new terminal tab; the installer added ~/.local/bin to PATH "
                                          "in your shell rc file" if paths.package_home() is None else
                                          "open a new terminal tab; your package manager's bin directory must "
                                          "be on PATH")))
    else:
        reinstall = ("reinstall agent-resurrect with install.sh (see the README)"
                     if paths.package_manager() != "homebrew" else "reinstall it: brew reinstall agent-resurrect")
        ctx.add(Result("launcher.on_path", WARN, f"{ctx.launcher} is missing", _person(USER_ACTION, reinstall)))


def login_shell(env: Mapping[str, str]) -> Optional[str]:
    """The login shell: $SHELL, else the user database entry (which macOS fills from UserShell)."""
    shell = env.get("SHELL") or ""
    if not shell:
        try:
            shell = pwd.getpwuid(os.getuid()).pw_shell or ""
        except (KeyError, OSError):
            shell = ""
    return shell or None


def _shell_name(path: str) -> str:
    return os.path.basename(path).lstrip("-")


def _check_login_shell(ctx: _Context) -> None:
    shell = ctx.login_shell
    if shell is None:
        ctx.add(Result("shell.login", WARN, "could not tell the login shell ($SHELL is not set)",
                       _person(USER_ACTION, "make sure your login shell is zsh or bash")))
        return
    name = _shell_name(shell)
    if name in SUPPORTED_SHELLS:
        ctx.add(Result("shell.login", OK, f"login shell {name} ({shell})", gates_ready=True))
        return
    ctx.add(Result("shell.login", FAIL,
                   f"login shell {name} ({shell}) is not supported; agent-resurrect restores panes in zsh and bash",
                   _person(USER_ACTION, "change your login shell to zsh or bash: chsh -s /bin/zsh (it asks for "
                                        "your password), then open a new terminal tab"), gates_ready=True))


def _applicable(setting: Setting) -> bool:
    try:
        return bool(setting.applicable())
    except Exception:  # cannot tell: assume it applies, so a real problem is never hidden
        return True


def _fix_hint(setting: Setting) -> str:
    try:
        return str(setting.fix_hint or "")
    except Exception:  # fix_hint can be a property that probes the system
        return ""


def _setting_result(ctx: _Context, setting: Setting) -> Result:
    """One setting's check. The fix kind is keyed by id prefix, so a renamed or new setting id still works:
    a missing app (iterm.*, claude.* not applicable) is `install-app`, anything else is re-running install."""
    setting_id = setting.id
    try:
        check = setting.check()
        ok, detail = bool(check.ok), str(check.detail)
    except Exception as exc:  # one broken check must not hide the others
        ok, detail = False, str(exc)
    if ok:
        return Result(setting_id, OK, detail, line=f"{setting_id}: {detail}", gates_ready=True)
    applicable = _applicable(setting)
    if not applicable and setting_id.startswith(_OPTIONAL_PREFIXES):
        detail = "does not apply on this Mac"
        return Result(setting_id, OK, detail, line=f"{setting_id}: {detail}", gates_ready=True)
    hint = _fix_hint(setting)
    if not applicable and setting_id.startswith(_APP_PREFIXES):
        fix = _person(INSTALL_APP, hint or "install the missing app, then run: "
                                           f"{_tilde(ctx.launcher)} install")
    else:
        fix = _install_fix(ctx, hint)
    return Result(setting_id, FAIL, detail, fix, line=f"{setting_id}: {detail}", gates_ready=True)


def _rc_targets(settings: List[Setting]) -> Dict[str, Path]:
    """{"zsh": path, "bash": path} of the shell settings that know their rc file."""
    targets = {}
    for setting in settings:
        shell = setting.id[len("shell."):] if setting.id.startswith("shell.") else ""
        target = getattr(setting, "target", None)
        if shell not in SUPPORTED_SHELLS or not callable(target):
            continue
        try:
            path = target()
        except Exception:
            continue
        if path is not None:
            targets[shell] = Path(path)
    return targets


_ZDOTDIR_MARK = "agent-resurrect-zdotdir="


def _run_probe(argv: List[str], env: Dict[str, str]) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=5, env=env,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def probe_zsh_dir(zsh: str = "zsh", run: Optional[Callable[[List[str], Dict[str, str]], str]] = None
                  ) -> Optional[Path]:
    """The directory a new zsh reads .zshrc from, or None if it cannot be told.

    `zsh -c` reads /etc/zshenv and $ZDOTDIR/.zshenv (~/.zshenv), and no other startup file, so a ZDOTDIR set
    there, exported or not, shows up. ZDOTDIR is dropped from the environment first, as in a new iTerm2 pane:
    an editor's terminal (VS Code's shell integration) exports its own ZDOTDIR, which a new pane never sees."""
    env = {key: value for key, value in os.environ.items() if key != "ZDOTDIR"}
    output = (run or _run_probe)([zsh, "-c", f'print -r -- "{_ZDOTDIR_MARK}${{ZDOTDIR:-$HOME}}"'], env)
    for line in reversed(output.splitlines()):  # .zshenv may print things first
        if line.startswith(_ZDOTDIR_MARK):
            value = line[len(_ZDOTDIR_MARK):]
            return Path(value) if value.startswith("/") else None
    return None


def _same_path(a: Path, b: Path) -> bool:
    return os.path.realpath(str(a)) == os.path.realpath(str(b))


def _check_zdotdir(ctx: _Context, targets: Dict[str, Path]) -> None:
    """The rc block's file is ${ZDOTDIR:-$HOME}/.zshrc as this process sees ZDOTDIR. A ZDOTDIR that zsh sets in
    ~/.zshenv without `export` is invisible here, so the block lands in a file zsh never reads."""
    shell = ctx.login_shell
    block_rc = targets.get("zsh")
    if block_rc is None or shell is None or _shell_name(shell) != "zsh":
        return
    zsh_dir = probe_zsh_dir(shell if os.path.isabs(shell) else "zsh")
    if zsh_dir is None:
        return
    zsh_rc = zsh_dir / ".zshrc"
    if _same_path(zsh_rc, block_rc):
        return
    ctx.add(Result("shell.zdotdir", WARN,
                   f"zsh reads {_tilde(zsh_rc)}, but the agent-resurrect block goes in {_tilde(block_rc)}",
                   _person(USER_ACTION, "if ~/.zshenv sets ZDOTDIR, make it `export ZDOTDIR=...`; then run "
                                        f"{_tilde(ctx.launcher)} install in a new iTerm2 tab"),
                   gates_ready=True))


# A command that replaces the shell or holds the pane before the block runs. Matched at command position (line
# start, or after ; && || | ( { then do else), on a line with comments and quoted strings removed.
_AT_COMMAND = r"(?:^|[;&|({]|\bthen\b|\bdo\b|\belse\b)\s*"
_END = r"(?=$|[\s;&|)])"
_EXEC = re.compile(_AT_COMMAND + r"exec\s+(?:-\w+\s+)*(?P<cmd>[A-Za-z_./~$][^\s;&|)]*)")
_MULTIPLEXER = re.compile(_AT_COMMAND + r"(?:command\s+)?(?:tmux\s+(?:[^;&|]*\s)?"
                                        r"(?:a|at|attach|attach-session|new|new-session)" + _END +
                                        r"|zellij(?:\s+(?:a|attach)" + _END + r"|\s*(?:$|[;&|)])))")
_AUTO_START = re.compile(r"\bzellij\s+setup\s+--generate-auto-start\b")  # zellij's documented rc line
_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
_COMMENT = re.compile(r"(?:^|\s)#.*$")
# A function definition: `name()`, `name() {`, `function name {`, or a whole one-line body `name() { ...; }`.
_FUNCTION = re.compile(r"^\s*(?:function\s+[\w.:+-]+(?:\s*\(\s*\))?|[\w.:+-]+\s*\(\s*\))\s*(?P<body>\{.*)?$")
_SAME_SHELL = ("$SHELL", "${SHELL}")


def find_takeover(text: str, shell: str) -> Optional[Tuple[int, str]]:
    """(line number, command) of the first line in `text` that runs `exec <program>`, or starts or attaches
    tmux or zellij, at the top level of an rc file. Function definitions are skipped (defining one runs
    nothing), and so is `exec` of the same shell, which reads the rc file again and reaches the block."""
    in_function = False
    for number, raw in enumerate(text.splitlines(), 1):
        line = _COMMENT.sub("", _QUOTED.sub("''", raw)).rstrip()
        if in_function:
            if line.strip().startswith("}"):
                in_function = False
            continue
        function = _FUNCTION.match(line)
        if function:
            body = function.group("body") or ""
            in_function = not body.endswith("}")
            continue
        match = _EXEC.search(line)
        if match and match.group("cmd") not in _SAME_SHELL and _shell_name(match.group("cmd")) != shell:
            return number, raw.strip()
        if _MULTIPLEXER.search(line) or _AUTO_START.search(raw):
            return number, raw.strip()
    return None


def _check_exec_before_block(ctx: _Context, targets: Dict[str, Path]) -> None:
    from .settings.shellrc import find_blocks  # imported on use, like the settings registry

    findings = []
    for shell, path in sorted(targets.items(), key=lambda item: SUPPORTED_SHELLS.index(item[0])):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        spans = find_blocks(data)
        if not spans:
            continue
        found = find_takeover(data[:spans[0][0]].decode("utf-8", "replace"), shell)
        if found:
            findings.append((path, found[0], found[1]))
    if not findings:
        return
    detail = "; ".join(f"{_tilde(path)} line {number} runs `{command[:60]}` before the agent-resurrect block, "
                       "so the block may never run in a new pane" for path, number, command in findings)
    text = "; ".join(f"move the agent-resurrect block in {_tilde(path)} above line {number}"
                     for path, number, _ in findings)
    ctx.add(Result("shell.exec_before_block", WARN, detail,
                   _person(USER_ACTION, text + " (reinstalling keeps the block where you put it)"),
                   gates_ready=True))


def multiplexer(env: Mapping[str, str]) -> Optional[str]:
    for variable, name in MULTIPLEXERS:
        if env.get(variable):
            return name
    return None


def _check_pane(ctx: _Context) -> Tuple[Optional[str], Optional[str]]:
    """Add terminal.pane (and terminal.multiplexer); return (terminal name, pane key) for the context."""
    terminal = detect_terminal(ctx.env)
    if terminal is None:
        ctx.add(Result("terminal.pane", WARN, "not running inside a supported terminal (iTerm2)",
                       _person(USER_ACTION, "only iTerm2 panes are recorded and restored; expected when doctor "
                                            "runs elsewhere, for example in an agent's or an editor's shell")))
        name, key = None, None
    else:
        name, key = terminal.name, terminal.pane_key(ctx.env)
        ctx.add(Result("terminal.pane", OK, f"{name} pane {terminal.pane_label(ctx.env) or key}"))
    inside = multiplexer(ctx.env)
    if inside:
        ctx.add(Result("terminal.multiplexer", WARN,
                       f"running inside {inside}: panes inside tmux, screen or zellij are not recorded or restored",
                       _person(USER_ACTION, f"run agents directly in iTerm2 panes, outside {inside}, "
                                            "to have them restored")))
    return name, key


def _session_exists(record: Record) -> bool:
    agent = get_agent(record.agent)
    try:
        return agent is not None and agent.session_exists(record)
    except Exception:
        return False


def _check_records(ctx: _Context, records: List[Record], running: Dict[str, bool]) -> dict:
    live = [r for r in records if r.status == STATUS_LIVE]
    restorable = [r for r in live if not running[r.pane_key] and _session_exists(r)]
    ctx.add(Result("records", OK, f"{_plural(len(records), 'recorded session', 'recorded sessions')} "
                                  f"({len(live)} live, {len(restorable)} restorable)"))
    return {"total": len(records), "live": len(live), "restorable": len(restorable), "died_since_boot": None}


def _check_since_boot(ctx: _Context) -> Optional[Dict[str, int]]:
    boot = ctx.boot
    if boot is None:
        ctx.add(Result("restores.since_boot", WARN, "could not read the boot time (sysctl -n kern.boottime)"))
        return None
    since = boot.astimezone().strftime("%Y-%m-%d %H:%M")
    counts: Counter = Counter()
    for event in store.read_restore_events():
        when = parse_iso(event.get("ts", "")) if isinstance(event, dict) else None
        if when is not None and when >= boot:
            counts[str(event.get("result"))] += 1
    if not counts:
        ctx.add(Result("restores.since_boot", OK, f"nothing restored since boot ({since})"))
    else:
        summary = ", ".join(f"{count} {result}" for result, count in sorted(counts.items()))
        line = f"since boot ({since}): {summary}"
        if counts.get("launch-failed"):
            ctx.add(Result("restores.since_boot", WARN, line,
                           _person(USER_ACTION, "a resumed agent failed to start; see the log at "
                                                f"{paths.log_path()}, then resume it by hand with: "
                                                f"{_tilde(ctx.launcher)} resume <n>")))
        else:
            ctx.add(Result("restores.since_boot", OK, line))
    result = {name: 0 for name in SINCE_BOOT_RESULTS}
    result.update(counts)
    return result


def _check_died(ctx: _Context, records: List[Record], running: Dict[str, bool]) -> Optional[int]:
    """Live records last updated before this boot whose agent is not running: they died and did not come back."""
    if ctx.boot is None:
        return None
    died = 0
    for record in records:
        updated = parse_iso(record.updated_at)
        if record.status == STATUS_LIVE and not running[record.pane_key] and updated is not None \
                and updated < ctx.boot:
            died += 1
    if died:
        what = _plural(died, "session died in a pane", "sessions died in panes")
        them = "it" if died == 1 else "them"
        ctx.add(Result("records.died_since_boot", WARN, f"{what} and did not come back",
                       _person(USER_ACTION, f"list {them} with: {_tilde(ctx.launcher)} ls, then resume each in "
                                            f"a terminal with: {_tilde(ctx.launcher)} resume <n>")))
    return died


# --- report ----------------------------------------------------------------------------------------------

def _print_text(results: List[Result], out: TextIO) -> None:
    for result in results:
        print(f"{result.status.upper():<6}{printable(result.line or result.detail)}", file=out)
        if result.fix is not None and result.status != OK:
            print(f"      fix: {printable(result.fix.text)}", file=out)


def _exit_code(failed: int, warned: int, strict: bool) -> Tuple[int, Optional[str]]:
    parts = []
    if failed:
        parts.append(f"{_plural(failed, 'check', 'checks')} failed")
    if strict and warned:
        parts.append(f"{_plural(warned, 'check', 'checks')} warned (--strict)")
    return (exitcodes.ERROR, ", ".join(parts)) if parts else (exitcodes.OK, None)


def run(out: Optional[TextIO] = None, *, procs: Optional[Procs] = None, boot_time: Optional[float] = None,
        as_json: bool = False, strict: bool = False) -> int:
    """Print the report (text, or one JSON document with `as_json`). Return ERROR if a check failed, or with
    `strict` if one warned, else OK. `boot_time` (epoch seconds) defaults to sysctl's."""
    out = out or sys.stdout
    procs = procs or Procs()
    if boot_time is None:
        boot_time = read_boot_time()
    ctx = _Context(launcher=paths.launcher(), procs=procs, env=os.environ,
                   boot=datetime.fromtimestamp(boot_time, timezone.utc) if boot_time is not None else None)
    ctx.login_shell = login_shell(ctx.env)

    _check_python(ctx)
    _check_launcher(ctx)
    _check_login_shell(ctx)
    settings = list(all_settings())
    for setting in settings:
        ctx.add(_setting_result(ctx, setting))
    targets = _rc_targets(settings)
    _check_zdotdir(ctx, targets)
    _check_exec_before_block(ctx, targets)
    terminal, pane_key = _check_pane(ctx)

    def is_running(record: Record) -> bool:
        return procs.is_alive(record.agent_pid, record.agent_started)

    store.gc(load_config().max_age_days, is_running=is_running)
    records = store.list_records()
    running = {r.pane_key: is_running(r) for r in records}
    record_counts = _check_records(ctx, records, running)
    since_boot = _check_since_boot(ctx)
    record_counts["died_since_boot"] = _check_died(ctx, records, running)

    results = ctx.results
    failed = sum(1 for r in results if r.status == FAIL)
    warned = sum(1 for r in results if r.status == WARN)
    code, message = _exit_code(failed, warned, strict)
    if not as_json:
        _print_text(results, out)
        return code
    jsonout.emit(jsonout.document(
        "doctor", code, message,
        ready=all(r.status == OK for r in results if r.gates_ready),
        summary={"ok": len(results) - failed - warned, "warn": warned, "fail": failed},
        checks=[r.to_dict() for r in results],
        context={"terminal": terminal, "pane_key": pane_key, "launcher": str(ctx.launcher),
                 "log_path": str(paths.log_path()), "boot_time": jsonout.iso_from_epoch(boot_time),
                 "login_shell": ctx.login_shell, "multiplexer": multiplexer(ctx.env)},
        records=record_counts,
        since_boot=since_boot,
    ), out)
    return code
