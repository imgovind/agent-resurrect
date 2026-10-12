"""The test suite. When HOME is the user's real home folder, importing this package points it at a throwaway
directory for the whole test process, so a test that forgets tests.helpers.temp_home() writes its log lines,
records or settings there and never in the real home. A process that already runs with another HOME (a child a
test starts inside temp_home()) keeps it. Tests should still use temp_home(): it gives each test a fresh HOME."""
import atexit
import os
import pwd
import shutil
import tempfile


def _real_home() -> str:
    try:
        return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return ""


if os.path.realpath(os.environ.get("HOME") or _real_home()) == _real_home():
    _SUITE_HOME = tempfile.mkdtemp(prefix="ar-suite-home-")
    os.environ["HOME"] = _SUITE_HOME
    for _name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "CLAUDE_CONFIG_DIR"):
        os.environ.pop(_name, None)
    atexit.register(shutil.rmtree, _SUITE_HOME, True)
