"""Runtime OS / display-server detection. Everything platform-specific keys off this."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Platform:
    os: str  # "macos" | "windows" | "linux"
    session: str  # "x11" | "wayland" | "" (non-Linux or headless)

    @property
    def is_mac(self) -> bool:
        return self.os == "macos"

    @property
    def is_windows(self) -> bool:
        return self.os == "windows"

    @property
    def is_linux(self) -> bool:
        return self.os == "linux"

    @property
    def is_wayland(self) -> bool:
        return self.session == "wayland"

    @property
    def is_x11(self) -> bool:
        return self.session == "x11"

    def describe(self) -> str:
        return f"{self.os}/{self.session}" if self.session else self.os


def detect() -> Platform:
    if sys.platform == "darwin":
        return Platform("macos", "")
    if sys.platform.startswith("win"):
        return Platform("windows", "")
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session not in ("x11", "wayland"):
        if os.environ.get("WAYLAND_DISPLAY"):
            session = "wayland"
        elif os.environ.get("DISPLAY"):
            session = "x11"
        else:
            session = ""
    return Platform("linux", session)


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


CURRENT = detect()
