"""Global hold-to-talk / toggle hotkey.

The pure HotkeyMachine turns raw key events into start/stop/cancel actions (unit-tested);
HotkeyListener feeds it from a pynput listener. The listener never suppresses keys, so
other apps always see the hotkey too (a bare modifier like Right Option/Right Ctrl does
nothing on its own).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable

from .platform_info import CURRENT, Platform

log = logging.getLogger(__name__)

START, STOP, CANCEL = "start", "stop", "cancel"

ALIASES = {
    "right_option": "alt_r",
    "right_alt": "alt_r",
    "left_option": "alt_l",
    "right_ctrl": "ctrl_r",
    "right_control": "ctrl_r",
    "left_ctrl": "ctrl_l",
    "right_cmd": "cmd_r",
    "right_command": "cmd_r",
    "right_shift": "shift_r",
    "capslock": "caps_lock",
}


def default_hotkey_name(plat: Platform = CURRENT) -> str:
    return "right_option" if plat.is_mac else "right_ctrl"


def resolve_hotkey(name: str, plat: Platform = CURRENT):
    """Config string -> pynput Key/KeyCode."""
    from pynput.keyboard import Key, KeyCode

    raw = (name or "auto").strip().lower()
    if raw == "auto":
        raw = default_hotkey_name(plat)
    raw = ALIASES.get(raw, raw)
    if hasattr(Key, raw):
        return getattr(Key, raw)
    if len(raw) == 1:
        return KeyCode.from_char(raw)
    raise ValueError(f"Unknown hotkey {name!r}. Use e.g. right_option, right_ctrl, f13, or a pynput Key name.")


class HotkeyMachine:
    """Pure state machine. Feed it key events with timestamps; it returns actions.

    hold:   down -> start, up -> stop. Another key pressed while held -> cancel
            (you were doing Right Ctrl+C, not dictating).
    toggle: double-tap (second key-down within double_tap_s of the previous tap's key-up)
            -> start; next key-down -> stop.
    Key-repeat (down while already down) is ignored. A key-down within debounce_s of a
    key-up is treated as contact bounce and ignored.
    """

    def __init__(self, mode: str = "hold", double_tap_s: float = 0.4, debounce_s: float = 0.03, tap_max_s: float = 0.35):
        self.mode = mode
        self.double_tap_s = double_tap_s
        self.debounce_s = debounce_s
        self.tap_max_s = tap_max_s
        self.recording = False
        self._down = False
        self._down_at = 0.0
        self._last_up = -1e9
        self._last_tap_up = -1e9
        self._swallow_up = False  # the key-up belonging to a toggle start/stop press
        self._chord = False

    def reset(self) -> None:
        """Called when recording ends for reasons other than the key (auto-stop, error)."""
        self.recording = False

    def key_down(self, t: float) -> str | None:
        if self._down:
            return None  # OS key-repeat
        if t - self._last_up < self.debounce_s:
            self._down = True
            self._swallow_up = True
            return None
        self._down = True
        self._down_at = t
        self._chord = False
        if self.mode == "hold":
            if not self.recording:
                self.recording = True
                return START
            return None
        # toggle
        if self.recording:
            self.recording = False
            self._swallow_up = True
            return STOP
        if t - self._last_tap_up <= self.double_tap_s:
            self.recording = True
            self._swallow_up = True
            self._last_tap_up = -1e9
            return START
        return None

    def key_up(self, t: float) -> str | None:
        if not self._down:
            return None
        self._down = False
        self._last_up = t
        if self._swallow_up:
            self._swallow_up = False
            return None
        if self.mode == "hold":
            if self.recording:
                self.recording = False
                return STOP
            return None
        # toggle: remember a clean, short tap as the first half of a double-tap
        if not self._chord and (t - self._down_at) <= self.tap_max_s:
            self._last_tap_up = t
        else:
            self._last_tap_up = -1e9
        return None

    def other_key(self, t: float) -> str | None:
        if not self._down:
            return None
        self._chord = True
        self._last_tap_up = -1e9
        if self.mode == "hold" and self.recording:
            self.recording = False
            self._swallow_up = True
            return CANCEL
        return None


class HotkeyListener:
    def __init__(
        self,
        hotkey: str,
        mode: str,
        on_action: Callable[[str], None],
        double_tap_ms: int = 400,
        is_injecting: Callable[[], bool] = lambda: False,
        plat: Platform = CURRENT,
    ) -> None:
        _configure_linux_backend(plat)
        self.key = resolve_hotkey(hotkey, plat)
        self.machine = HotkeyMachine(mode, double_tap_ms / 1000.0)
        self.on_action = on_action
        self.is_injecting = is_injecting
        self._listener = None

    def _matches(self, key) -> bool:
        if key == self.key:
            return True
        # Some layouts report Right Alt as AltGr.
        try:
            from pynput.keyboard import Key

            return self.key == Key.alt_r and key == Key.alt_gr
        except Exception:
            return False

    def _emit(self, action: str | None) -> None:
        if action:
            try:
                self.on_action(action)
            except Exception:
                log.exception("hotkey action %s failed", action)

    def _on_press(self, key, injected=False) -> None:
        if injected or self.is_injecting():
            return  # our own paste keystrokes
        t = time.monotonic()
        if self._matches(key):
            self._emit(self.machine.key_down(t))
        else:
            self._emit(self.machine.other_key(t))

    def _on_release(self, key, injected=False) -> None:
        if injected or self.is_injecting():
            return
        if self._matches(key):
            self._emit(self.machine.key_up(time.monotonic()))

    def start(self) -> None:
        from pynput import keyboard

        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.daemon = True
        self._listener.start()

    def run_forever(self) -> None:
        """Run the listener on the calling thread (used when there's no tray)."""
        from pynput import keyboard

        with keyboard.Listener(on_press=self._on_press, on_release=self._on_release) as l:
            self._listener = l
            l.join()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()


def _configure_linux_backend(plat: Platform) -> None:
    """pynput's default Linux backend is Xlib, which can't see keys in native Wayland windows.
    On Wayland use the evdev ('uinput') backend, which reads /dev/input directly."""
    if plat.is_wayland and "PYNPUT_BACKEND_KEYBOARD" not in os.environ and "PYNPUT_BACKEND" not in os.environ:
        os.environ["PYNPUT_BACKEND_KEYBOARD"] = "uinput"


def wayland_input_access_ok() -> bool:
    """The uinput backend needs read access to /dev/input/event* (the `input` group)."""
    import glob

    devs = glob.glob("/dev/input/event*")
    return bool(devs) and any(os.access(d, os.R_OK) for d in devs)
