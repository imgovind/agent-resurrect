"""Exit codes: a stable, documented contract for scripts and AI agents (see README "For AI agents")."""

OK = 0           # success; also `find` with any match, `ls` with an empty list
ERROR = 1        # unexpected error, a failed install/uninstall step, `doctor` found a FAIL
USAGE = 2        # bad arguments or config value, `find` text missing or too short
NOT_FOUND = 3    # no such session or list number, `find` found nothing, nothing recorded for this pane
NEEDS_TTY = 4    # would launch an agent or ask a question without a terminal; the command is printed instead
BUSY = 5         # the session's agent process is still running
GONE = 6         # the session's transcript no longer exists
INTERRUPTED = 130

NAMES = {
    OK: "ok",
    ERROR: "error",
    USAGE: "usage",
    NOT_FOUND: "not-found",
    NEEDS_TTY: "needs-tty",
    BUSY: "busy",
    GONE: "gone",
    INTERRUPTED: "interrupted",
}


def name(code: int) -> str:
    return NAMES.get(code, "error")
