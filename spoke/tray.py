"""Optional system-tray / menu-bar icon: a monochrome waveform.

idle        five short, still bars
recording   bars swing with your live mic level (~15 fps)
processing  a low wave travels across the bars until the text is pasted

macOS: the frames are template images (black + alpha), so the menu bar tints them for
light and dark mode, and they're set on the main thread (AppKit isn't thread-safe).
Windows/Linux: white bars on a black rounded square, which reads on light and dark trays.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable

from .platform_info import CURRENT

log = logging.getLogger(__name__)

N_BARS = 5
# Middle bars move most, so it reads as a voice waveform rather than a level meter.
ENVELOPE = [0.55, 0.85, 1.0, 0.85, 0.55]
PHASES = [0.0, 1.9, 0.7, 2.6, 1.3]
IDLE_HEIGHTS = [0.22, 0.34, 0.46, 0.34, 0.22]
MIN_H = 0.14


def bar_heights(state: str, level: float, t: float) -> list[float]:
    """Bar heights in [MIN_H, 1] for one animation frame. Pure, so it's unit-tested."""
    level = max(0.0, min(1.0, level))
    if state == "recording":
        out = []
        for env, ph in zip(ENVELOPE, PHASES):
            swing = 0.6 + 0.4 * math.sin(t * 9.0 + ph)  # keeps moving a little even when quiet
            out.append(MIN_H + (1 - MIN_H) * env * max(0.12, level) * swing)
        return out
    if state == "processing":
        return [MIN_H + 0.35 * (0.5 + 0.5 * math.sin(t * 7.0 - i * 0.9)) for i in range(N_BARS)]
    return list(IDLE_HEIGHTS)


def render(heights: list[float], size: int = 44, template: bool = CURRENT.is_mac):
    """One frame as a PIL image. template=True: black bars on transparent (macOS menu bar).
    Otherwise white bars on a black rounded square."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fg = (0, 0, 0, 255)
    pad = 2
    if not template:
        d.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 5, fill=(0, 0, 0, 255))
        fg = (255, 255, 255, 255)
        pad = size // 6
    inner = size - 2 * pad
    gap = inner / (N_BARS * 2 - 1)
    w = max(2, round(gap))
    mid = size / 2
    for i, h in enumerate(heights):
        x0 = pad + round(i * 2 * gap)
        half = max(w / 2, h * inner / 2)
        d.rounded_rectangle((x0, mid - half, x0 + w - 1, mid + half), radius=w // 2, fill=fg)
    return img


class Tray:
    FPS = {"recording": 15, "processing": 12}

    def __init__(
        self,
        title: str,
        on_quit: Callable[[], None],
        on_open_history: Callable[[], None] | None = None,
        level: Callable[[], float] = lambda: 0.0,
    ):
        import pystray

        self._pystray = pystray
        self.level = level
        self.state = "idle"
        self._anim_stop = threading.Event()
        self._anim_thread: threading.Thread | None = None
        items = [pystray.MenuItem(lambda _i: f"Spoke: {self.state}", None, enabled=False)]
        if on_open_history:
            items.append(pystray.MenuItem("Show history file", lambda _i, _it: on_open_history()))
        items.append(pystray.MenuItem("Quit", lambda _i, _it: on_quit()))
        self.icon = pystray.Icon("spoke", render(IDLE_HEIGHTS), title, pystray.Menu(*items))

    # --- frame output ---
    def _show(self, heights: list[float]) -> None:
        try:
            if CURRENT.is_mac and self._show_mac(heights):
                return
            self.icon.icon = render(heights)
        except Exception as e:
            log.debug("tray update failed: %s", e)

    def _show_mac(self, heights: list[float]) -> bool:
        """Set a template NSImage on the status item, on the main thread. Returns False if
        pystray's internals aren't there (falls back to pystray's own setter)."""
        import io

        import AppKit
        import Foundation

        item = getattr(self.icon, "_status_item", None)
        if item is None:
            return False
        buf = io.BytesIO()
        render(heights, size=36, template=True).save(buf, "png")
        img = AppKit.NSImage.alloc().initWithData_(Foundation.NSData.dataWithBytes_length_(buf.getvalue(), len(buf.getvalue())))
        img.setSize_((18, 18))  # 36 px = 18 pt @2x
        img.setTemplate_(True)  # menu bar tints it for light/dark mode
        item.button().performSelectorOnMainThread_withObject_waitUntilDone_("setImage:", img, False)
        return True

    # --- animation ---
    def _animate(self, state: str) -> None:
        interval = 1.0 / self.FPS[state]
        t0 = time.monotonic()
        while not self._anim_stop.is_set():
            level = self.level() if state == "recording" else 0.0
            self._show(bar_heights(state, level, time.monotonic() - t0))
            self._anim_stop.wait(interval)

    def set_state(self, state: str) -> None:
        if state == self.state and self._anim_thread and self._anim_thread.is_alive():
            return
        self.state = state
        self._anim_stop.set()
        if self._anim_thread and self._anim_thread is not threading.current_thread():
            self._anim_thread.join(timeout=0.5)
        self._anim_stop = threading.Event()
        try:
            self.icon.title = f"Spoke: {state}"
        except Exception:
            pass
        if state in self.FPS:
            self._anim_thread = threading.Thread(target=self._animate, args=(state,), name="tray-anim", daemon=True)
            self._anim_thread.start()
        else:
            self._anim_thread = None
            self._show(IDLE_HEIGHTS)

    def notify(self, title: str, message: str) -> None:
        try:
            self.icon.notify(message, title)
        except Exception as e:
            log.debug("tray notify failed: %s", e)

    def run(self, setup: Callable[[], None]) -> None:
        """Blocks on the calling (main) thread. `setup` runs once the icon is visible."""

        def _setup(icon):
            icon.visible = True
            self._show(IDLE_HEIGHTS)  # re-set as a template image on macOS
            setup()

        self.icon.run(setup=_setup)

    def stop(self) -> None:
        self._anim_stop.set()
        try:
            self.icon.stop()
        except Exception:
            pass


def try_create(title: str, on_quit, on_open_history=None, level: Callable[[], float] = lambda: 0.0) -> Tray | None:
    # With no system tray to dock into (e.g. GNOME without the AppIndicator extension)
    # pystray logs a traceback on every icon update; the icon is optional, so keep it quiet.
    logging.getLogger("pystray").setLevel(logging.CRITICAL)
    try:
        return Tray(title, on_quit, on_open_history, level)
    except Exception as e:
        log.info("tray unavailable (%s); running without an icon", e)
        return None
