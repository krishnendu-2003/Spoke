"""Audio cues (start/stop/error) and desktop notifications. Both are best-effort: a missing
audio device or notifier never breaks dictation."""

from __future__ import annotations

import logging
import subprocess
import sys
import threading

import numpy as np

from .platform_info import CURRENT, have

log = logging.getLogger(__name__)

_RATE = 44_100
_tray_notifier = None  # set by tray.py when a tray icon is running


def _tone(freqs: list[float], dur: float = 0.06, volume: float = 0.12) -> np.ndarray:
    parts = []
    for f in freqs:
        t = np.linspace(0, dur, int(_RATE * dur), endpoint=False)
        wave = np.sin(2 * np.pi * f * t)
        fade = np.minimum(1.0, np.minimum(t, dur - t) / 0.008)  # 8 ms fade, no clicks
        parts.append((wave * fade * volume).astype(np.float32))
    return np.concatenate(parts)


_CUES = {
    "start": lambda: _tone([660, 990]),
    "stop": lambda: _tone([990, 660]),
    "error": lambda: _tone([330, 250], dur=0.1),
}


class Sounds:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._cache: dict[str, np.ndarray] = {}

    def play(self, cue: str) -> None:
        if not self.enabled:
            return
        try:
            import sounddevice as sd

            if cue not in self._cache:
                self._cache[cue] = _CUES[cue]()
            sd.play(self._cache[cue], _RATE, blocking=False)
        except Exception as e:
            log.debug("sound cue failed: %s", e)


def set_tray_notifier(fn) -> None:
    global _tray_notifier
    _tray_notifier = fn


def notify(title: str, message: str, enabled: bool = True) -> None:
    """Show a desktop notification (non-blocking). Always also logs."""
    log.warning("%s: %s", title, message)
    if not enabled:
        return
    threading.Thread(target=_notify, args=(title, message), daemon=True).start()


def _notify(title: str, message: str) -> None:
    try:
        if _tray_notifier is not None:
            _tray_notifier(title, message)
            return
        if CURRENT.is_mac:
            script = f"display notification {_applescript_str(message)} with title {_applescript_str(title)}"
            subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
        elif CURRENT.is_linux and have("notify-send"):
            subprocess.run(["notify-send", "-a", "Spoke", title, message], timeout=5, capture_output=True)
        else:
            print(f"[Spoke] {title}: {message}", file=sys.stderr)
    except Exception as e:
        log.debug("notification failed: %s", e)


def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
