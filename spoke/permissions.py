"""OS permission checks with exact fix instructions."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from .platform_info import CURRENT, Platform


@dataclass
class Check:
    name: str
    ok: bool | None  # None = couldn't determine
    detail: str = ""
    fix: str = ""


def _mac_binary_note() -> str:
    exe = os.path.realpath(sys.executable)
    parent = os.environ.get("TERM_PROGRAM") or os.environ.get("__CFBundleIdentifier") or ""
    who = f"your terminal app ({parent})" if parent else "your terminal app"
    return (
        f"Grant it to {who} when running Spoke from a terminal, and to the Python binary\n"
        f"      {exe}\n"
        "      when Spoke runs via the LaunchAgent (autostart). Use the + button, press Cmd+Shift+G\n"
        "      to paste that path, then quit and relaunch the terminal / Spoke."
    )


def mac_checks() -> list[Check]:
    checks: list[Check] = []
    note = _mac_binary_note()

    try:
        from ApplicationServices import AXIsProcessTrusted

        ok = bool(AXIsProcessTrusted())
        checks.append(Check(
            "Accessibility (needed to send Cmd+V)", ok,
            fix="System Settings > Privacy & Security > Accessibility > enable it.\n      " + note,
        ))
    except Exception as e:
        checks.append(Check("Accessibility", None, f"couldn't check: {e}"))

    try:
        import Quartz

        preflight = getattr(Quartz, "CGPreflightListenEventAccess", None)
        ok = bool(preflight()) if preflight else None
        checks.append(Check(
            "Input Monitoring (needed to see the hotkey)", ok,
            fix="System Settings > Privacy & Security > Input Monitoring > enable it.\n      " + note,
        ))
    except Exception as e:
        checks.append(Check("Input Monitoring", None, f"couldn't check: {e}"))

    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

        status = int(AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio))
        # 0 notDetermined, 1 restricted, 2 denied, 3 authorized
        ok = {3: True, 2: False, 1: False}.get(status)
        detail = {0: "not asked yet -- macOS will prompt on first recording", 1: "restricted by policy"}.get(status, "")
        checks.append(Check(
            "Microphone", ok, detail,
            fix="System Settings > Privacy & Security > Microphone > enable it.\n      " + note,
        ))
    except Exception as e:
        checks.append(Check("Microphone", None, f"couldn't check: {e}"))
    return checks


def mac_request_prompts() -> None:
    """Ask macOS to show its permission prompts (used by `setup`)."""
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

        AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
    except Exception:
        pass
    try:
        import Quartz

        req = getattr(Quartz, "CGRequestListenEventAccess", None)
        if req:
            req()
    except Exception:
        pass


def windows_checks() -> list[Check]:
    import ctypes

    try:
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        admin = False
    return [Check(
        "Elevated windows", None,
        "Spoke is running " + ("elevated." if admin else "non-elevated.")
        + " Windows blocks a non-elevated process from sending keys to apps 'Run as administrator'"
        " (UIPI), so pasting into admin terminals/installers won't work.",
        fix="Run Spoke elevated only if you really need that; it's not required otherwise.",
    )]


def linux_checks(plat: Platform) -> list[Check]:
    from .hotkey import wayland_input_access_ok
    from .inject import linux_install_hint

    checks: list[Check] = []
    hint = linux_install_hint(plat)
    checks.append(Check("Paste tools", hint is None, fix=hint or ""))
    if plat.is_wayland:
        ok = wayland_input_access_ok()
        checks.append(Check(
            "Global hotkey on Wayland (/dev/input access)", ok,
            "Wayland doesn't let apps see global keys; Spoke reads /dev/input via evdev instead.",
            fix="sudo usermod -aG input $USER   then log out and back in.",
        ))
    return checks


def run_checks(plat: Platform = CURRENT) -> list[Check]:
    if plat.is_mac:
        return mac_checks()
    if plat.is_windows:
        return windows_checks()
    return linux_checks(plat)


def format_checks(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        mark = {True: "OK  ", False: "FAIL", None: "??  "}[c.ok]
        lines.append(f"  [{mark}] {c.name}" + (f" -- {c.detail}" if c.detail else ""))
        if c.ok is not True and c.fix:
            lines.append(f"      fix: {c.fix}")
    return "\n".join(lines)
