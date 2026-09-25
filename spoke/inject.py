"""Text injection: save clipboard -> set text -> paste keystroke -> wait -> restore clipboard.

Each OS gets a clipboard backend with snapshot()/restore() that tries to preserve non-text
contents (images, rich text). If a snapshot can't be taken or restored, we log and carry on:
the dictated text is still pasted, and nothing crashes.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .platform_info import CURRENT, Platform, have

log = logging.getLogger(__name__)


# --- Clipboard snapshot model -------------------------------------------------------------

@dataclass
class ClipSnapshot:
    """Opaque saved clipboard state. `items` is backend-specific; `empty` = clipboard was empty."""

    items: Any = None
    empty: bool = False
    complete: bool = True  # False = some formats couldn't be captured (restore is best-effort)
    change_marker: Any = None  # backend-specific "has someone else written since?" token
    meta: dict = field(default_factory=dict)


class Clipboard(Protocol):
    def snapshot(self) -> ClipSnapshot | None: ...
    def set_text(self, text: str) -> None: ...
    def restore(self, snap: ClipSnapshot) -> None: ...
    def marker(self) -> Any: ...


class ClipboardError(Exception):
    pass


# --- macOS: NSPasteboard, all types of all items -------------------------------------------

class MacClipboard:
    def __init__(self) -> None:
        from AppKit import NSPasteboard  # pyobjc-framework-Cocoa (dependency of Quartz)

        self.pb = NSPasteboard.generalPasteboard()

    def marker(self):
        return int(self.pb.changeCount())

    def snapshot(self) -> ClipSnapshot | None:
        items = []
        for item in self.pb.pasteboardItems() or []:
            saved = {}
            for t in item.types():
                data = item.dataForType_(t)
                if data is not None:
                    saved[str(t)] = bytes(data)
            items.append(saved)
        return ClipSnapshot(items=items, empty=not items)

    def set_text(self, text: str) -> None:
        from AppKit import NSPasteboardTypeString

        self.pb.clearContents()
        if not self.pb.setString_forType_(text, NSPasteboardTypeString):
            raise ClipboardError("NSPasteboard refused setString")

    def restore(self, snap: ClipSnapshot) -> None:
        from AppKit import NSPasteboardItem
        from Foundation import NSData

        self.pb.clearContents()
        if snap.empty:
            return
        objs = []
        for saved in snap.items:
            item = NSPasteboardItem.alloc().init()
            for t, data in saved.items():
                item.setData_forType_(NSData.dataWithBytes_length_(data, len(data)), t)
            objs.append(item)
        self.pb.writeObjects_(objs)


# --- Windows: Win32 clipboard via ctypes, all HGLOBAL formats ------------------------------

class WindowsClipboard:
    CF_UNICODETEXT = 13
    # GDI-handle or owner-drawn formats: not HGLOBAL memory, can't be copied as bytes.
    # CF_TEXT/OEMTEXT/LOCALE and CF_BITMAP are synthesized by Windows from the ones we keep.
    SKIP = {1, 2, 3, 7, 9, 14, 16, 0x80, 0x82, 0x83, 0x8E}
    GMEM_MOVEABLE = 0x0002

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ct = ctypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        u.OpenClipboard.argtypes = [wintypes.HWND]
        u.OpenClipboard.restype = wintypes.BOOL
        u.CloseClipboard.restype = wintypes.BOOL
        u.EmptyClipboard.restype = wintypes.BOOL
        u.EnumClipboardFormats.argtypes = [wintypes.UINT]
        u.EnumClipboardFormats.restype = wintypes.UINT
        u.GetClipboardData.argtypes = [wintypes.UINT]
        u.GetClipboardData.restype = wintypes.HANDLE
        u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        u.SetClipboardData.restype = wintypes.HANDLE
        u.GetClipboardSequenceNumber.restype = wintypes.DWORD
        u.CountClipboardFormats.restype = ctypes.c_int
        k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k.GlobalAlloc.restype = wintypes.HGLOBAL
        k.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        k.GlobalUnlock.restype = wintypes.BOOL
        k.GlobalSize.argtypes = [wintypes.HGLOBAL]
        k.GlobalSize.restype = ctypes.c_size_t
        k.GlobalFree.argtypes = [wintypes.HGLOBAL]
        k.GlobalFree.restype = wintypes.HGLOBAL
        self.u, self.k = u, k

    def _open(self) -> None:
        for _ in range(20):  # another app may hold the clipboard briefly
            if self.u.OpenClipboard(None):
                return
            time.sleep(0.01)
        raise ClipboardError("OpenClipboard failed (held by another app)")

    def marker(self):
        return int(self.u.GetClipboardSequenceNumber())

    def snapshot(self) -> ClipSnapshot | None:
        self._open()
        try:
            if self.u.CountClipboardFormats() == 0:
                return ClipSnapshot(items=[], empty=True)
            items, complete, fmt = [], True, 0
            while True:
                fmt = self.u.EnumClipboardFormats(fmt)
                if fmt == 0:
                    break
                if fmt in self.SKIP:
                    if fmt in (2, 3, 9, 14):
                        complete = False
                    continue
                h = self.u.GetClipboardData(fmt)
                if not h:
                    continue
                size = self.k.GlobalSize(h)
                p = self.k.GlobalLock(h)
                if not p:
                    complete = False
                    continue
                try:
                    items.append((fmt, self.ct.string_at(p, size)))
                finally:
                    self.k.GlobalUnlock(h)
            return ClipSnapshot(items=items, empty=False, complete=complete)
        finally:
            self.u.CloseClipboard()

    def _put(self, fmt: int, data: bytes) -> None:
        h = self.k.GlobalAlloc(self.GMEM_MOVEABLE, len(data))
        if not h:
            raise ClipboardError("GlobalAlloc failed")
        p = self.k.GlobalLock(h)
        self.ct.memmove(p, data, len(data))
        self.k.GlobalUnlock(h)
        if not self.u.SetClipboardData(fmt, h):
            self.k.GlobalFree(h)  # ownership only transfers on success
            raise ClipboardError(f"SetClipboardData({fmt}) failed")

    def set_text(self, text: str) -> None:
        self._open()
        try:
            self.u.EmptyClipboard()
            self._put(self.CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))
        finally:
            self.u.CloseClipboard()

    def restore(self, snap: ClipSnapshot) -> None:
        self._open()
        try:
            self.u.EmptyClipboard()
            for fmt, data in snap.items:
                try:
                    self._put(fmt, data)
                except ClipboardError as e:
                    log.debug("restore skipped format %s: %s", fmt, e)
        finally:
            self.u.CloseClipboard()


# --- Linux: xclip (X11) / wl-clipboard (Wayland) -------------------------------------------

_TEXT_TARGETS = ("text/plain;charset=utf-8", "UTF8_STRING", "text/plain", "STRING", "TEXT")
_PREFERRED_BINARY = ("image/png", "text/html", "image/jpeg", "text/uri-list")


def pick_target(targets: list[str]) -> str | None:
    """Choose the single richest target we can round-trip through a CLI clipboard tool."""
    for t in _PREFERRED_BINARY:
        if t in targets:
            return t
    for t in _TEXT_TARGETS:
        if t in targets:
            return t
    for t in targets:
        if "/" in t:
            return t
    return None


class _CliClipboard:
    list_cmd: list[str]
    read_cmd: Callable[[str], list[str]]
    write_cmd: Callable[[str], list[str]]

    def marker(self):
        return None

    def _run(self, cmd: list[str], data: bytes | None = None, timeout: float = 1.0) -> bytes:
        res = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
        if res.returncode != 0:
            raise ClipboardError(f"{cmd[0]} exited {res.returncode}")
        return res.stdout

    def _write(self, cmd: list[str], data: bytes) -> None:
        # xclip / wl-copy fork a background owner process. Don't capture its stdout/stderr,
        # or subprocess waits on the forked child holding the pipe open.
        subprocess.run(cmd, input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0, check=True)

    def snapshot(self) -> ClipSnapshot | None:
        try:
            out = self._run(self.list_cmd)
        except (ClipboardError, subprocess.TimeoutExpired):
            return ClipSnapshot(items=None, empty=True)  # no owner = empty clipboard
        targets = [t.strip() for t in out.decode(errors="replace").splitlines() if t.strip()]
        if not targets:
            return ClipSnapshot(items=None, empty=True)
        target = pick_target(targets)
        if target is None:
            return ClipSnapshot(items=None, empty=False, complete=False)
        data = self._run(self.read_cmd(target), timeout=2.0)
        # Only the chosen target is restored; other targets (e.g. text alongside html) are lost.
        return ClipSnapshot(items=(target, data), empty=False)

    def set_text(self, text: str) -> None:
        self._write(self.write_cmd("text/plain;charset=utf-8"), text.encode("utf-8"))

    def restore(self, snap: ClipSnapshot) -> None:
        if snap.empty or snap.items is None:
            self.clear()
            return
        target, data = snap.items
        self._write(self.write_cmd(target), data)

    def clear(self) -> None:
        pass


class XClipboard(_CliClipboard):
    list_cmd = ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]

    @staticmethod
    def read_cmd(target):
        return ["xclip", "-selection", "clipboard", "-t", target, "-o"]

    @staticmethod
    def write_cmd(target):
        if target == "text/plain;charset=utf-8":
            target = "UTF8_STRING"
        return ["xclip", "-selection", "clipboard", "-t", target, "-i"]


class WaylandClipboard(_CliClipboard):
    list_cmd = ["wl-paste", "--list-types"]

    @staticmethod
    def read_cmd(target):
        return ["wl-paste", "--no-newline", "--type", target]

    @staticmethod
    def write_cmd(target):
        return ["wl-copy", "--type", target]

    def clear(self) -> None:
        subprocess.run(["wl-copy", "--clear"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0)


def make_clipboard(plat: Platform = CURRENT) -> Clipboard | None:
    try:
        if plat.is_mac:
            return MacClipboard()
        if plat.is_windows:
            return WindowsClipboard()
        if plat.is_wayland and have("wl-copy") and have("wl-paste"):
            return WaylandClipboard()
        if plat.is_x11 and have("xclip"):
            return XClipboard()
    except Exception as e:
        log.error("clipboard backend unavailable: %s", e)
    return None


# --- Keystrokes ---------------------------------------------------------------------------

# Linux evdev keycodes for ydotool.
_KEY_LEFTCTRL, _KEY_LEFTSHIFT, _KEY_V = 29, 42, 47


class Keys:
    """Sends the paste chord or types text, using the most reliable tool per platform."""

    def __init__(self, plat: Platform = CURRENT, terminal_classes: list[str] | None = None) -> None:
        self.plat = plat
        self.terminal_classes = {c.lower() for c in (terminal_classes or [])}
        self._pynput = None

    def _controller(self):
        if self._pynput is None:
            from pynput.keyboard import Controller

            self._pynput = Controller()
        return self._pynput

    def is_terminal(self, app_id: str | None) -> bool:
        return bool(app_id) and app_id.lower() in self.terminal_classes

    def paste(self, app_id: str | None = None) -> None:
        from pynput.keyboard import Key

        shift = self.plat.is_linux and self.is_terminal(app_id)
        if self.plat.is_mac:
            c = self._controller()
            with c.pressed(Key.cmd):
                c.tap("v")
        elif self.plat.is_windows:
            c = self._controller()
            with c.pressed(Key.ctrl):
                c.tap("v")
        elif self.plat.is_x11 and have("xdotool"):
            combo = "ctrl+shift+v" if shift else "ctrl+v"
            subprocess.run(["xdotool", "key", "--clearmodifiers", combo], check=True, timeout=2.0)
        elif self.plat.is_wayland and have("wtype"):
            cmd = ["wtype", "-M", "ctrl"] + (["-M", "shift"] if shift else []) + ["-k", "v"]
            cmd += (["-m", "shift"] if shift else []) + ["-m", "ctrl"]
            subprocess.run(cmd, check=True, timeout=2.0)
        elif self.plat.is_wayland and have("ydotool"):
            seq = [f"{_KEY_LEFTCTRL}:1"] + ([f"{_KEY_LEFTSHIFT}:1"] if shift else [])
            seq += [f"{_KEY_V}:1", f"{_KEY_V}:0"] + ([f"{_KEY_LEFTSHIFT}:0"] if shift else []) + [f"{_KEY_LEFTCTRL}:0"]
            subprocess.run(["ydotool", "key", *seq], check=True, timeout=2.0)
        else:
            c = self._controller()
            with c.pressed(Key.ctrl):
                if shift:
                    with c.pressed(Key.shift):
                        c.tap("v")
                else:
                    c.tap("v")

    def type_text(self, text: str) -> None:
        if self.plat.is_x11 and have("xdotool"):
            subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "2", "--", text], check=True, timeout=30)
        elif self.plat.is_wayland and have("wtype"):
            subprocess.run(["wtype", "--", text], check=True, timeout=30)
        elif self.plat.is_wayland and have("ydotool"):
            subprocess.run(["ydotool", "type", "--", text], check=True, timeout=30)
        else:
            self._controller().type(text)


# --- Frontmost app (for the continuation space and Linux terminal detection) --------------

def active_app(plat: Platform = CURRENT) -> str | None:
    try:
        if plat.is_mac:
            from AppKit import NSWorkspace

            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return str(app.bundleIdentifier() or app.localizedName()) if app else None
        if plat.is_windows:
            return _windows_foreground_exe()
        if plat.is_x11:
            return _x11_active_class()
    except Exception as e:
        log.debug("active_app failed: %s", e)
    return None  # Wayland has no portable way to ask; continuation falls back to time only


_xdisplay = None


def _x11_active_class() -> str | None:
    """WM_CLASS of the focused X11 window, lowercased (e.g. "gnome-terminal-server").
    Uses python-xlib (already installed with pynput) rather than xdotool, whose
    getwindowclassname is missing from older distro builds."""
    global _xdisplay
    from Xlib import X, display

    if _xdisplay is None:
        _xdisplay = display.Display()
    d = _xdisplay
    root = d.screen().root
    win = None
    prop = root.get_full_property(d.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
    if prop is not None and len(prop.value) and prop.value[0]:
        win = d.create_resource_object("window", int(prop.value[0]))
    else:  # no EWMH window manager
        win = d.get_input_focus().focus
    for _ in range(10):  # walk up from the focused child to the top-level that has WM_CLASS
        if win is None or isinstance(win, int) or win == root:
            return None
        cls = win.get_wm_class()
        if cls:
            instance, klass = cls
            return (instance or klass).lower()
        win = win.query_tree().parent
    return None


def _windows_foreground_exe() -> str | None:
    import ctypes
    from ctypes import wintypes

    u = ctypes.WinDLL("user32")
    k = ctypes.WinDLL("kernel32")
    u.GetForegroundWindow.restype = wintypes.HWND
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    hwnd = u.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = k.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return f"pid:{pid.value}"  # elevated window: can't inspect (and can't paste into it)
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1].lower()
        return f"pid:{pid.value}"
    finally:
        k.CloseHandle(h)


# --- Continuation spacing -----------------------------------------------------------------

class Continuation:
    """Adds a separating space when you dictate again into the same app within `window` s,
    so "First sentence." + "Second sentence." doesn't become "First sentence.Second sentence."."""

    def __init__(self, enabled: bool = True, window: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.enabled = enabled
        self.window = window
        self.clock = clock
        self._last_app: str | None = None
        self._last_time: float | None = None

    def prefix(self, text: str, app_id: str | None) -> str:
        if not self.enabled or not text or self._last_time is None:
            return ""
        if text[0] in ",.;:!?)]}" or text[0].isspace():
            return ""
        if self.clock() - self._last_time > self.window:
            return ""
        if app_id != self._last_app:
            return ""
        return " "

    def record(self, app_id: str | None) -> None:
        self._last_app = app_id
        self._last_time = self.clock()


# --- Orchestration ------------------------------------------------------------------------

class Injector:
    def __init__(
        self,
        clipboard: Clipboard | None,
        keys: Keys,
        *,
        method: str = "clipboard",
        restore_delay: float = 0.15,
        settle_delay: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        plat: Platform = CURRENT,
    ) -> None:
        self.clipboard = clipboard
        self.keys = keys
        self.method = method
        self.restore_delay = restore_delay
        # CLI clipboard owners (xclip/wl-copy) need a moment to take ownership.
        self.settle_delay = settle_delay if settle_delay is not None else (0.03 if plat.is_linux else 0.0)
        self.sleep = sleep
        self.injecting = False
        self.last_visible_at: float = 0.0  # perf_counter when the text hit the app

    def inject(self, text: str, app_id: str | None = None) -> str:
        """Put `text` into the focused field. Returns the method actually used."""
        if not text:
            return "none"
        self.injecting = True
        try:
            if self.method == "type" or self.clipboard is None:
                if self.clipboard is None and self.method != "type":
                    log.warning("no clipboard backend; typing text instead")
                self.keys.type_text(text)
                self.last_visible_at = time.perf_counter()
                return "type"
            return self._paste_via_clipboard(text, app_id)
        finally:
            self.injecting = False

    def _paste_via_clipboard(self, text: str, app_id: str | None) -> str:
        cb = self.clipboard
        snap: ClipSnapshot | None = None
        try:
            snap = cb.snapshot()
            if snap is not None and not snap.complete:
                log.info("clipboard has formats that can't be fully saved; restore will be partial")
        except Exception as e:
            log.warning("couldn't save clipboard (%s); it won't be restored", e)
            snap = None

        cb.set_text(text)
        expected = _safe_marker(cb)
        if self.settle_delay:
            self.sleep(self.settle_delay)
        try:
            self.keys.paste(app_id)
            self.last_visible_at = time.perf_counter()
        finally:
            self.sleep(self.restore_delay)
            self._restore(snap, expected)
        return "clipboard"

    def _restore(self, snap: ClipSnapshot | None, expected: Any) -> None:
        if snap is None:
            return
        current = _safe_marker(self.clipboard)
        if expected is not None and current is not None and current != expected:
            log.info("clipboard changed during paste (you copied something?); not restoring")
            return
        try:
            self.clipboard.restore(snap)
        except Exception as e:
            log.warning("couldn't restore clipboard: %s", e)


def _safe_marker(cb: Clipboard):
    try:
        return cb.marker()
    except Exception:
        return None


def linux_install_hint(plat: Platform = CURRENT) -> str | None:
    """Human-readable fix if the Linux paste toolchain is incomplete, else None."""
    if not plat.is_linux:
        return None
    if plat.is_wayland:
        missing = [t for t in ("wl-copy", "wl-paste") if not have(t)]
        keyer = have("wtype") or have("ydotool")
        if not missing and keyer:
            return None
        return (
            "Wayland paste needs wl-clipboard plus wtype (wlroots/KDE) or ydotool (GNOME):\n"
            "  Debian/Ubuntu: sudo apt install wl-clipboard wtype ydotool\n"
            "  Fedora:        sudo dnf install wl-clipboard wtype ydotool\n"
            "  Arch:          sudo pacman -S wl-clipboard wtype ydotool\n"
            "  (ydotool also needs its daemon: systemctl --user enable --now ydotool)"
        )
    if plat.is_x11:
        if have("xclip") and have("xdotool"):
            return None
        return (
            "X11 paste needs xclip and xdotool:\n"
            "  Debian/Ubuntu: sudo apt install xclip xdotool\n"
            "  Fedora:        sudo dnf install xclip xdotool\n"
            "  Arch:          sudo pacman -S xclip xdotool"
        )
    return "No X11/Wayland session detected (XDG_SESSION_TYPE unset); Spoke needs a graphical session."
