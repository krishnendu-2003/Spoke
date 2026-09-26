"""One Spoke at a time. Two copies (say the app and a terminal `python -m spoke`) would both
hear the hotkey and paste every dictation twice."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import IO

from .config import spoke_home


def lock_path() -> Path:
    return spoke_home() / "spoke.lock"


def acquire(wait_seconds: float = 0.0) -> IO | None:
    """Take the lock, retrying for up to `wait_seconds` (a relaunch overlaps the old process by
    a moment). Returns the open file, which holds the lock until it's closed or the process
    exits, or None if another Spoke has it. Windows has no flock, so there it always succeeds."""
    try:
        import fcntl
    except ImportError:  # Windows
        return open(os.devnull, "w")
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if time.monotonic() >= deadline:
                f.close()
                return None
            time.sleep(0.2)
            continue
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        return f


def holder_pid() -> int | None:
    try:
        return int(lock_path().read_text().strip() or 0) or None
    except (OSError, ValueError):
        return None
