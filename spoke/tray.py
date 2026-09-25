"""Optional system-tray icon: grey = idle, red = recording, amber = processing."""

from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)

COLORS = {
    "idle": (140, 140, 140),
    "recording": (220, 40, 40),
    "processing": (240, 170, 20),
    "error": (120, 0, 160),
}


def _icon_image(state: str):
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, size - 6, size - 6), fill=COLORS.get(state, COLORS["idle"]) + (255,))
    # simple mic glyph
    d.rounded_rectangle((26, 16, 38, 38), radius=6, fill=(255, 255, 255, 255))
    d.rectangle((31, 38, 33, 46), fill=(255, 255, 255, 255))
    d.rectangle((24, 46, 40, 48), fill=(255, 255, 255, 255))
    return img


class Tray:
    def __init__(self, title: str, on_quit: Callable[[], None], on_open_history: Callable[[], None] | None = None):
        import pystray

        self._pystray = pystray
        self._images = {s: _icon_image(s) for s in COLORS}
        self.state = "idle"
        items = [pystray.MenuItem(lambda _i: f"Spoke: {self.state}", None, enabled=False)]
        if on_open_history:
            items.append(pystray.MenuItem("Show history file", lambda _i, _it: on_open_history()))
        items.append(pystray.MenuItem("Quit", lambda _i, _it: on_quit()))
        self.icon = pystray.Icon("spoke", self._images["idle"], title, pystray.Menu(*items))

    def set_state(self, state: str) -> None:
        self.state = state
        try:
            self.icon.icon = self._images.get(state, self._images["idle"])
            self.icon.title = f"Spoke: {state}"
        except Exception as e:
            log.debug("tray update failed: %s", e)

    def notify(self, title: str, message: str) -> None:
        try:
            self.icon.notify(message, title)
        except Exception as e:
            log.debug("tray notify failed: %s", e)

    def run(self, setup: Callable[[], None]) -> None:
        """Blocks on the calling (main) thread. `setup` runs once the icon is visible."""

        def _setup(icon):
            icon.visible = True
            setup()

        self.icon.run(setup=_setup)

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass


def try_create(title: str, on_quit, on_open_history=None) -> Tray | None:
    # With no system tray to dock into (e.g. GNOME without the AppIndicator extension)
    # pystray logs a traceback on every icon update; the icon is optional, so keep it quiet.
    logging.getLogger("pystray").setLevel(logging.CRITICAL)
    try:
        return Tray(title, on_quit, on_open_history)
    except Exception as e:
        log.info("tray unavailable (%s); running without an icon", e)
        return None
