"""Diagnostic log, capped at 256 KB with one rotation. Logging never raises."""
import os
from datetime import datetime

from . import paths

MAX_BYTES = 256 * 1024


def log(message: str) -> None:
    try:
        path = paths.log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a") as fh:
            fh.write(f"{stamp} [{os.getpid()}] {message}\n")
    except OSError:
        pass
