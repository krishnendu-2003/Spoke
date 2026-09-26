"""Spoke.app: the macOS menu-bar app around the same daemon `python -m spoke` runs.

Opening the app does, in dialogs instead of a terminal:
  1. the Groq key, if the Keychain doesn't have one yet
  2. Microphone, Accessibility and Input Monitoring for "Spoke" (not for a terminal or Python)
  3. on first launch only: offer voice lock enrollment and "open at login"
then shows the waveform in the menu bar and listens for the hotkey.

The bundle's binary also takes the CLI commands, so
`/Applications/Spoke.app/Contents/MacOS/Spoke doctor` works, and `--self-test` checks that
every native piece made it into the bundle (run by CI on the built app).

Every AppKit call here runs on the main thread: before the tray starts, or inside a tray
menu callback (pystray runs those on the main thread).
"""

from __future__ import annotations

import logging
import os
import plistlib
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import __version__
from . import config as config_mod
from .config import Config, config_path, get_api_key, set_api_key, spoke_home

log = logging.getLogger("spoke.app")

BUNDLE_ID = "com.krishnendu.spoke"
LOGIN_LABEL = BUNDLE_ID + ".login"
LEGACY_LABEL = "com.spoke.dictation"  # scripts/install_autostart.sh (terminal version)
ENROLL_CLIPS = 6
ENROLL_SECONDS = 6.0
PANES = {
    "mic": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    "ax": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "im": "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
}
PERM_TEXT = {
    "mic": "Microphone, to record you",
    "ax": "Accessibility, to paste the text",
    "im": "Input Monitoring, to hear the hotkey",
}


# --- pure helpers (unit-tested) ----------------------------------------------------------

def bundle_path(executable: str | None = None, frozen: bool | None = None) -> Path | None:
    """The Spoke.app folder when running from the bundle, else None."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not frozen:
        return None
    for parent in Path(executable or sys.executable).resolve().parents:
        if parent.suffix == ".app":
            return parent
    return None


def clean_argv(argv: list[str]) -> list[str]:
    """Finder used to pass -psn_0_12345 to apps; it isn't a CLI command."""
    return [a for a in argv if not a.startswith("-psn_")]


def launch_agent_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def login_plist(app: Path) -> bytes:
    """Open Spoke at login through LaunchServices (`open`), so macOS treats it exactly like
    double-clicking the app: same permissions, no Dock icon, no second copy if it's open."""
    return plistlib.dumps({
        "Label": LOGIN_LABEL,
        "ProgramArguments": ["/usr/bin/open", "-a", str(app)],
        "RunAtLoad": True,
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Interactive",
    })


def login_enabled() -> bool:
    return launch_agent_path(LOGIN_LABEL).exists()


def set_login(enabled: bool, app: Path | None) -> None:
    path = launch_agent_path(LOGIN_LABEL)
    if enabled:
        if app is None:
            raise RuntimeError("only the installed Spoke.app can open at login")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(login_plist(app))  # launchd picks it up at the next login
    else:
        path.unlink(missing_ok=True)


def hotkey_label(name: str) -> str:
    raw = (name or "auto").strip().lower()
    if raw in ("auto", "right_option", "right_alt", "alt_r"):
        return "Right Option (⌥)"
    return raw.replace("_", " ").title()


def onboarded_marker() -> Path:
    return spoke_home() / "app-onboarded"


def terminal_copies() -> list[int]:
    """PIDs of `python -m spoke` daemons started from a terminal or the old LaunchAgent."""
    try:
        out = subprocess.run(["pgrep", "-f", r"[Pp]ython[0-9.]* -m spoke( --debug)?$"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    return [int(p) for p in out.split() if p.isdigit() and int(p) != os.getpid()]


def relaunch_command(app: Path | None) -> list[str]:
    if app is not None:
        # -n: a new instance even while this one is still exiting; it waits for our lock.
        return ["/bin/sh", "-c", 'sleep 1; exec /usr/bin/open -n "$1"', "sh", str(app)]
    return [sys.executable, "-m", "spoke.app"]


# --- AppKit UI -----------------------------------------------------------------------------

class UI:
    """Alerts, a key prompt and a small status window, usable before and during the tray."""

    def __init__(self) -> None:
        import AppKit
        import Foundation

        self.AppKit, self.Foundation = AppKit, Foundation
        self.app = AppKit.NSApplication.sharedApplication()
        # No Dock icon (the bundle also says so with LSUIElement; this covers `python -m spoke.app`).
        self.app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        self._install_edit_menu()
        self._panel = None
        self._label = None

    def _install_edit_menu(self) -> None:
        """Cmd+V in a text field is routed through the main menu's Edit > Paste. A menu-bar-only
        app has no menu, so without this you couldn't paste your API key."""
        AK = self.AppKit
        main = AK.NSMenu.alloc().init()
        app_item = AK.NSMenuItem.alloc().init()
        main.addItem_(app_item)
        app_item.setSubmenu_(AK.NSMenu.alloc().initWithTitle_("Spoke"))
        edit_item = AK.NSMenuItem.alloc().init()
        main.addItem_(edit_item)
        edit = AK.NSMenu.alloc().initWithTitle_("Edit")
        for title, sel, key in (("Undo", "undo:", "z"), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                                ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")):
            edit.addItemWithTitle_action_keyEquivalent_(title, sel, key)
        edit_item.setSubmenu_(edit)
        self.app.setMainMenu_(main)

    def _front(self) -> None:
        self.app.activateIgnoringOtherApps_(True)

    def alert(self, title: str, text: str, buttons: list[str], warning: bool = False) -> int:
        """Modal alert; returns the index of the button clicked."""
        AK = self.AppKit
        self._hide_panel()
        a = AK.NSAlert.alloc().init()
        a.setMessageText_(title)
        a.setInformativeText_(text)
        if warning:
            a.setAlertStyle_(AK.NSAlertStyleWarning)
        for b in buttons:
            a.addButtonWithTitle_(b)
        self._front()
        return int(a.runModal()) - AK.NSAlertFirstButtonReturn

    def ask_secret(self, title: str, text: str, buttons: list[str]) -> tuple[int, str]:
        AK, F = self.AppKit, self.Foundation
        self._hide_panel()
        a = AK.NSAlert.alloc().init()
        a.setMessageText_(title)
        a.setInformativeText_(text)
        for b in buttons:
            a.addButtonWithTitle_(b)
        field = AK.NSSecureTextField.alloc().initWithFrame_(F.NSMakeRect(0, 0, 340, 24))
        field.setPlaceholderString_("gsk_...")
        a.setAccessoryView_(field)
        a.window().setInitialFirstResponder_(field)
        self._front()
        idx = int(a.runModal()) - AK.NSAlertFirstButtonReturn
        return idx, str(field.stringValue() or "")

    # status window, for work that takes a few seconds (the alerts are modal, this isn't)
    def status(self, text: str) -> None:
        AK, F = self.AppKit, self.Foundation
        if self._panel is None:
            panel = AK.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                F.NSMakeRect(0, 0, 480, 150), AK.NSWindowStyleMaskTitled, AK.NSBackingStoreBuffered, False)
            panel.setTitle_("Spoke")
            panel.setReleasedWhenClosed_(False)
            panel.setLevel_(AK.NSFloatingWindowLevel)
            label = AK.NSTextField.wrappingLabelWithString_("")
            label.setFrame_(F.NSMakeRect(20, 16, 440, 118))
            label.setFont_(AK.NSFont.systemFontOfSize_(14))
            panel.contentView().addSubview_(label)
            panel.center()
            self._panel, self._label = panel, label
        if str(self._label.stringValue()) != text:
            self._label.setStringValue_(text)
        if not self._panel.isVisible():
            self._front()
            self._panel.makeKeyAndOrderFront_(None)
        self._panel.displayIfNeeded()

    def _hide_panel(self) -> None:
        if self._panel is not None and self._panel.isVisible():
            self._panel.orderOut_(None)

    def done(self) -> None:
        self._hide_panel()

    def pump(self, seconds: float) -> None:
        """Handle window events for a while (we may be outside NSApp.run())."""
        AK, F = self.AppKit, self.Foundation
        mask = getattr(AK, "NSEventMaskAny", None) or getattr(AK, "NSAnyEventMask")
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            ev = self.app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                mask, F.NSDate.dateWithTimeIntervalSinceNow_(min(left, 0.05)), F.NSDefaultRunLoopMode, True)
            if ev is not None:
                self.app.sendEvent_(ev)

    def run_with_status(self, fn: Callable, text: str | Callable[[], str]):
        """Run fn on a worker thread while the status window shows `text`; returns its result
        or re-raises its exception."""
        box: dict = {}

        def work():
            try:
                box["r"] = fn()
            except BaseException as e:  # noqa: BLE001 -- re-raised on the main thread
                box["e"] = e

        t = threading.Thread(target=work, name="spoke-app-work", daemon=True)
        t.start()
        while t.is_alive():
            self.status(text() if callable(text) else text)
            self.pump(0.1)
        if "e" in box:
            raise box["e"]
        return box.get("r")


def open_url(url: str) -> None:
    subprocess.Popen(["/usr/bin/open", url])


# --- flows ---------------------------------------------------------------------------------

def ask_key(ui: UI) -> bool:
    """Ask for the Groq key, check it with Groq, store it in the Keychain."""
    from .groq_api import GroqClient, GroqError

    note = ""
    while True:
        idx, key = ui.ask_secret(
            "Paste your Groq API key",
            note + "Spoke sends what you say to Groq's Whisper to turn it into text. Get a free key at "
            "console.groq.com/keys. It's kept in your macOS Keychain, never in a file.",
            ["Save", "Get a Key…", "Cancel"],
        )
        if idx == 1:
            open_url("https://console.groq.com/keys")
            continue
        if idx != 0:
            return False
        key = key.strip()
        if not key:
            note = "The field was empty. "
            continue
        client = GroqClient(key)
        try:
            ui.run_with_status(client.list_models, "Checking the key with Groq…")
        except GroqError as e:
            note = f"Groq didn't accept that key ({e}). "
            continue
        except Exception as e:
            log.warning("couldn't reach Groq to check the key: %s", e)
            ui.alert("Couldn't reach Groq", "Spoke couldn't check the key right now (no internet?). "
                     "It's saved anyway; dictation will tell you if it's wrong.", ["OK"])
        finally:
            client.close()
        try:
            set_api_key(key)
        except Exception as e:
            ui.alert("Couldn't save the key", f"The Keychain refused it: {type(e).__name__}: {e}", ["OK"], warning=True)
            return False
        log.info("Groq key saved to the Keychain")
        return True


def permission_state() -> dict[str, bool | None]:
    """mic/ax/im -> True granted, False missing, None mic not asked yet."""
    state: dict[str, bool | None] = {}
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

        s = int(AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio))
        state["mic"] = {3: True, 0: None}.get(s, False)
    except Exception as e:
        log.warning("couldn't read microphone permission: %s", e)
        state["mic"] = True  # don't block on a check that can't run; recording will say
    try:
        from ApplicationServices import AXIsProcessTrusted

        state["ax"] = bool(AXIsProcessTrusted())
    except Exception as e:
        log.warning("couldn't read accessibility permission: %s", e)
        state["ax"] = True
    try:
        import Quartz

        state["im"] = bool(Quartz.CGPreflightListenEventAccess())
    except Exception as e:
        log.warning("couldn't read input monitoring permission: %s", e)
        state["im"] = True
    return state


def _request_mic(ui: UI) -> None:
    from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

    done = threading.Event()
    AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVMediaTypeAudio, lambda granted: done.set())
    ui.run_with_status(lambda: done.wait(120), "macOS is asking whether Spoke may use the microphone.\n\nClick Allow.")


def _request(which: str) -> None:
    """Make macOS list Spoke under that permission (and show its own prompt)."""
    try:
        if which == "ax":
            from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

            AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
        elif which == "im":
            import Quartz

            Quartz.CGRequestListenEventAccess()
    except Exception as e:
        log.warning("permission request for %s failed: %s", which, e)


def ensure_permissions(ui: UI, always_report: bool = False) -> str:
    """Walk the user through the three permissions. Returns "ok", "restart" (Input Monitoring
    was just granted: macOS only applies it to a fresh process) or "skipped"."""
    im_was_missing = False
    while True:
        st = permission_state()
        im_was_missing |= st["im"] is False
        if st["mic"] is None:
            _request_mic(ui)
            continue
        missing = [k for k in ("mic", "ax", "im") if st[k] is False]
        if not missing:
            if im_was_missing:
                return "restart"
            if always_report:
                ui.alert("All set", "Spoke has Microphone, Accessibility and Input Monitoring.", ["OK"])
            return "ok"
        lines = "\n".join(f"• {PERM_TEXT[k]}" for k in missing)
        idx = ui.alert(
            "Spoke needs your permission",
            f"Turn on Spoke in System Settings > Privacy & Security for:\n\n{lines}\n\n"
            "Click Open Settings, switch Spoke on, then come back and click Check Again. "
            "If Spoke is already switched on there, click Restart Spoke.",
            ["Open Settings", "Check Again", "Restart Spoke", "Not Now"],
        )
        if idx == 0:
            first = missing[0]
            _request(first)
            open_url(PANES[first])
        elif idx == 2:
            return "restart"
        elif idx == 3:
            log.warning("continuing without: %s", ", ".join(missing))
            return "skipped"


def enroll_voice(ui: UI, cfg: Config) -> bool:
    """Record ENROLL_CLIPS sentences and save a voiceprint; turns voice_lock on."""
    from . import voice
    from .recorder import Recorder

    idx = ui.alert(
        "Teach Spoke your voice?",
        "With voice lock, Spoke only types what YOU say and ignores people talking around you. "
        f"You'll read {ENROLL_CLIPS} short sentences aloud, about a minute. It all runs on this Mac.\n\n"
        "A quiet room is best.",
        ["Set Up Voice Lock", "Not Now"],
    )
    if idx != 0:
        return False
    progress = ["Starting…"]
    try:
        ui.run_with_status(
            lambda: voice.download_models(say=lambda m: progress.__setitem__(0, m)),
            lambda: f"Getting the on-device voice models (about 41 MB, only once).\n\n{progress[0]}",
        )
        engine = ui.run_with_status(voice.Engine, "Loading the voice models…")
    except Exception as e:
        log.exception("voice model setup failed")
        ui.alert("Couldn't set up voice lock", f"{type(e).__name__}: {e}", ["OK"], warning=True)
        return False

    rec_ = Recorder(max_seconds=ENROLL_SECONDS + 1, device=cfg.input_device)
    clips: list = []
    retries = 0
    i = 0
    try:
        while len(clips) < ENROLL_CLIPS:
            line = voice.ENROLL_PROMPTS[i % len(voice.ENROLL_PROMPTS)]
            idx = ui.alert(
                f"Sentence {len(clips) + 1} of {ENROLL_CLIPS}",
                f"Click Record, then read this aloud in your normal dictation voice:\n\n“{line}”\n\n"
                f"You'll have {ENROLL_SECONDS:.0f} seconds.",
                ["Record", "Cancel"],
            )
            if idx != 0:
                return False
            t0 = time.monotonic()
            rec = ui.run_with_status(
                lambda: rec_.record_for(ENROLL_SECONDS),
                lambda: f"● Recording, read aloud:\n\n“{line}”\n\n"
                f"{max(0.0, ENROLL_SECONDS - (time.monotonic() - t0)):.0f} s left",
            )
            clip = voice.analyse_clip(engine, rec.samples)
            if clip.speech_s < 2.0:
                retries += 1
                if retries > 4:
                    ui.alert("Spoke can't hear you well", "Check the microphone in System Settings > Sound > "
                             "Input, then try again from the Spoke menu.", ["OK"], warning=True)
                    return False
                ui.alert("Let's redo that one", f"Only caught {clip.speech_s:.1f} s of speech. "
                         "Try a little louder or closer to the mic.", ["OK"])
                continue
            clips.append(clip)
            i += 1
        profile = ui.run_with_status(lambda: voice.build_profile(engine, clips, None), "Building your voiceprint…")
        profile.save()
    except Exception as e:
        log.exception("enrollment failed")
        ui.alert("Voice setup failed", f"{type(e).__name__}: {e}", ["OK"], warning=True)
        return False
    finally:
        rec_.close()
    config_mod.set_values({"voice_lock": True})
    log.info("voiceprint saved (%d clips, threshold %.2f); voice_lock on", len(profile.embeddings), profile.threshold)
    ui.alert("Voice lock is on", "Spoke now only types your voice. You can turn it off or re-record "
             "from the waveform menu.", ["OK"])
    return True


def handle_other_copies(ui: UI) -> bool:
    """The terminal version (and its LaunchAgent) would paste every dictation a second time.
    Returns False if the user wants to keep the other copy running (then we quit)."""
    legacy = launch_agent_path(LEGACY_LABEL)
    if legacy.exists():
        idx = ui.alert(
            "Turn off the Terminal version's autostart?",
            "You set up the Terminal version of Spoke to start at login. With the app you don't need "
            "it, and two copies would type every dictation twice. The app can open at login instead.",
            ["Turn It Off", "Keep It"],
        )
        if idx == 0:
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LEGACY_LABEL}"], capture_output=True)
            legacy.unlink(missing_ok=True)
            log.info("removed legacy LaunchAgent %s", legacy)
    pids = terminal_copies()
    if pids:
        idx = ui.alert(
            "Spoke is already running from Terminal",
            "Two copies would type every dictation twice. Quit the Terminal one and use the app?",
            ["Quit It", "Cancel"],
        )
        if idx != 0:
            return False
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        ui.run_with_status(lambda: _wait_gone(pids, 5.0), "Stopping the Terminal copy…")
    return True


def _wait_gone(pids: list[int], timeout: float) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except OSError:
                pass
        if not alive:
            return
        time.sleep(0.2)


def relaunch(app: Path | None) -> None:
    cmd = relaunch_command(app)
    log.info("relaunching: %s", cmd)
    if app is not None:
        subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.execv(cmd[0], cmd)


# --- the running app -----------------------------------------------------------------------

class SpokeApp:
    def __init__(self, ui: UI, cfg: Config) -> None:
        from .daemon import Daemon

        self.ui = ui
        self.bundle = bundle_path()
        self.restart = False
        self.daemon = Daemon(cfg)
        items: list = [
            (None, None, None),
            ("Voice lock", self._menu(self.toggle_voice_lock), lambda: self.daemon.cfg.voice_lock),
            ("Re-record my voice…", self._menu(self.rerecord), None),
            ("Groq API key…", self._menu(self.change_key), None),
            ("Check permissions…", self._menu(self.permissions), None),
            ("Open settings file", self._menu(self.open_config), None),
            ("Open log", self._menu(self.open_log), None),
            (None, None, None),
        ]
        if self.bundle is not None:
            items.append(("Open at login", self._menu(self.toggle_login), login_enabled))
        self.daemon.menu_items = items

    def _menu(self, fn: Callable[[], None]) -> Callable[[], None]:
        def run():
            try:
                fn()
            except Exception as e:
                log.exception("menu action failed")
                self.ui.alert("Something went wrong", f"{type(e).__name__}: {e}\n\nDetails are in {spoke_home() / 'spoke.log'}",
                              ["OK"], warning=True)
        return run

    def run(self) -> int:
        self.daemon.run()
        if self.restart:
            relaunch(self.bundle)
        return 0

    def request_restart(self) -> None:
        self.restart = True
        self.daemon.quit()

    # menu actions
    def toggle_voice_lock(self) -> None:
        from . import voice

        if self.daemon.cfg.voice_lock:
            config_mod.set_values({"voice_lock": False})
            self.request_restart()
            return
        profile = None
        try:
            profile = voice.Profile.load()
        except Exception as e:
            log.warning("voiceprint unreadable: %s", e)
        if profile is not None:
            config_mod.set_values({"voice_lock": True})
            self.request_restart()
        else:
            self.rerecord()

    def rerecord(self) -> None:
        self.daemon.paused = True
        try:
            ok = enroll_voice(self.ui, self.daemon.cfg)
        finally:
            self.daemon.paused = False
            self.ui.done()
        if ok:
            self.request_restart()

    def change_key(self) -> None:
        if ask_key(self.ui):
            self.request_restart()

    def permissions(self) -> None:
        if ensure_permissions(self.ui, always_report=True) == "restart":
            self.request_restart()

    def open_config(self) -> None:
        subprocess.Popen(["/usr/bin/open", "-t", str(config_path())])
        if self.ui.alert("Settings file opened", "Save your changes, then restart Spoke to apply them.",
                         ["Restart Spoke", "Later"]) == 0:
            self.request_restart()

    def open_log(self) -> None:
        subprocess.Popen(["/usr/bin/open", "-R", str(spoke_home() / "spoke.log")])

    def toggle_login(self) -> None:
        set_login(not login_enabled(), self.bundle)
        if self.daemon.tray:
            self.daemon.tray.icon.update_menu()


def run_app() -> int:
    from . import instance
    from .daemon import setup_logging

    ui = UI()
    try:
        cfg = config_mod.load()
    except Exception as e:
        ui.alert("Spoke couldn't read its settings", f"{config_path()}\n\n{e}", ["Quit"], warning=True)
        return 2
    setup_logging(cfg, console=not getattr(sys, "frozen", False))
    app = bundle_path()
    log.info("Spoke.app %s starting from %s", __version__, app or sys.executable)

    if not handle_other_copies(ui):
        return 0
    lock = instance.acquire(wait_seconds=5.0)  # noqa: F841 -- held for the life of the process
    if lock is None:
        ui.alert("Spoke is already running", "Look for the waveform icon in the menu bar.", ["OK"])
        return 0

    first_run = not onboarded_marker().exists()
    needs_cloud = cfg.effective_stt_backend == "groq" or cfg.effective_cleanup
    key, _ = get_api_key()
    if needs_cloud and not key and not ask_key(ui):
        ui.alert("Spoke needs a Groq key", "Open Spoke again when you have one (console.groq.com/keys).", ["Quit"])
        return 1

    if ensure_permissions(ui) == "restart":
        ui.done()
        relaunch(app)
        return 0

    if first_run:
        from . import voice

        try:
            has_voiceprint = voice.Profile.load() is not None
        except Exception:
            has_voiceprint = False
        if not has_voiceprint and enroll_voice(ui, cfg):
            cfg = config_mod.load()
        if app is not None and not login_enabled():
            if ui.alert("Open Spoke when you log in?", "Then it's always ready in the menu bar. "
                        "You can change this later from the waveform menu.", ["Open at Login", "Not Now"]) == 0:
                set_login(True, app)
        onboarded_marker().parent.mkdir(parents=True, exist_ok=True)
        onboarded_marker().touch()
        ui.alert("Spoke is ready", f"Hold {hotkey_label(cfg.hotkey)}, speak, and let go: the text is typed "
                 "wherever your cursor is.\n\nSpoke lives in the menu bar as a small waveform. Click it for "
                 "settings or to quit.", ["Start Dictating"])
    ui.done()

    try:
        return SpokeApp(ui, cfg).run()
    except ValueError as e:  # bad hotkey name etc.
        ui.alert("Spoke couldn't start", f"{e}\n\nFix it in {config_path()}.", ["Quit"], warning=True)
        return 2


# --- self-test (CI runs this on the built bundle) ----------------------------------------

def self_test() -> int:
    """Import every native piece from inside the bundle and exercise it without a mic, a
    display permission or a Groq key. Exit code = number of failures."""
    import importlib

    import numpy as np

    failures = 0

    def check(name: str, fn: Callable[[], str]) -> None:
        nonlocal failures
        try:
            print(f"  [OK  ] {name}: {fn()}")
        except Exception as e:
            failures += 1
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")

    def mod(name: str) -> str:
        m = importlib.import_module(name)
        return str(getattr(m, "__version__", "") or getattr(m, "__file__", ""))

    print(f"Spoke {__version__} self-test ({sys.executable}, frozen={getattr(sys, 'frozen', False)})")
    for name in ("numpy", "httpx", "PIL", "spoke.daemon", "spoke.cli"):
        check(name, lambda n=name: mod(n))

    def certs() -> str:
        import certifi

        p = Path(certifi.where())
        if not p.is_file():
            raise FileNotFoundError(p)
        return str(p)

    check("TLS certificates", certs)

    def flac() -> str:
        from .recorder import Recording

        data, fname, _ = Recording((np.random.default_rng(0).standard_normal(16000) * 300).astype(np.int16)).to_upload("flac")
        return f"{fname}, {len(data)} bytes"

    check("soundfile (FLAC encode)", flac)
    check("sounddevice (PortAudio)", lambda: __import__("sounddevice").get_portaudio_version()[1])

    def keychain() -> str:
        import keyring

        name = type(keyring.get_keyring()).__module__
        if sys.platform == "darwin" and "macOS" not in name:
            raise RuntimeError(f"expected the macOS Keychain backend, got {name}")
        return name

    check("keyring backend", keychain)

    def pynput_backend() -> str:
        from pynput import keyboard

        return keyboard.Listener.__module__

    check("pynput backend", pynput_backend)

    def pystray_backend() -> str:
        import pystray

        return pystray.Icon.__module__

    check("pystray backend", pystray_backend)
    if sys.platform == "darwin":
        for name in ("AppKit", "Foundation", "Quartz", "ApplicationServices", "AVFoundation"):
            check(name, lambda n=name: mod(n))
        check("permission checks", lambda: str(permission_state()))

    def sherpa() -> str:
        from . import voice

        import sherpa_onnx  # noqa: F401

        if voice.missing_models():
            return f"imports; models not in {voice.models_dir()}, engine not exercised"
        engine = voice.Engine()
        emb = engine.embed((np.random.default_rng(1).standard_normal(16000 * 2) * 0.05).astype(np.float32))
        engine.new_vad()
        engine.new_denoiser()
        return f"speaker, VAD and denoiser models load; embedding dim {len(emb)}"

    check("sherpa-onnx (voice lock)", sherpa)
    print("All good." if not failures else f"{failures} failure(s).")
    return failures


def main(argv: list[str] | None = None) -> int:
    argv = clean_argv(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--self-test"]:
        return self_test()
    if argv:  # `Spoke.app/Contents/MacOS/Spoke doctor`, `... enroll`, etc.
        from .cli import main as cli_main

        return cli_main(argv)
    if sys.platform != "darwin":
        from .config import spoke_cmd

        print(f"The Spoke app is macOS only. Run `{spoke_cmd()}` instead.", file=sys.stderr)
        return 2
    return run_app()


if __name__ == "__main__":
    sys.exit(main())
