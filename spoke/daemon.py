"""The dictation pipeline: hotkey -> record -> STT -> filter -> cleanup -> vocab -> paste.

Recording runs on the hotkey thread (start must be < 50 ms); everything after key release
runs on one worker thread so utterances are processed in order and the hotkey never blocks.
"""

from __future__ import annotations

import logging
import logging.handlers
import queue
import sys
import threading
import time
from dataclasses import dataclass

from . import cleanup as cleanup_mod
from . import notify
from .config import Config, get_api_key, spoke_home
from .groq_api import GroqClient
from .history import History
from .hotkey import CANCEL, START, STOP, HotkeyListener
from .inject import Continuation, Injector, Keys, active_app, make_clipboard
from .platform_info import CURRENT
from .recorder import Recorder, Recording, rejection_reason, save_debug_audio
from .transcribe import TranscriptionError, filter_hallucination, make_transcriber
from .vocab import apply_replacements

log = logging.getLogger("spoke")


def setup_logging(cfg: Config, console: bool = True) -> None:
    home = spoke_home()
    home.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(home / "spoke.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(fmt)
        root.addHandler(ch)
    # httpx logs every request at INFO; keep it quiet (it never logs headers/keys either way).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("keyring").setLevel(logging.WARNING)


@dataclass
class Timings:
    record_s: float = 0.0
    stt_ms: float = 0.0
    cleanup_ms: float = 0.0
    paste_ms: float = 0.0
    total_ms: float = 0.0  # key release -> text pasted

    def as_dict(self) -> dict:
        return {k: round(v, 3 if k == "record_s" else 0) for k, v in self.__dict__.items()}


class Pipeline:
    """Everything after key release. Separate from the daemon so it can be driven by
    `test-mic` and by tests."""

    def __init__(
        self,
        cfg: Config,
        client: GroqClient | None,
        injector: Injector | None,
        history: History | None,
        sounds: notify.Sounds | None = None,
    ):
        self.cfg = cfg
        self.sounds = sounds or notify.Sounds(False)
        self.client = client
        self.transcriber = make_transcriber(cfg, client)
        self.injector = injector
        self.history = history
        self.continuation = Continuation(cfg.trailing_space, cfg.trailing_space_window_seconds)
        # Resolved against the key's /models list (resolve_cleanup_model). Until then, use the
        # configured name ("auto" -> first preference).
        self.cleanup_model = (
            cfg.cleanup_model if cfg.cleanup_model != "auto" else cleanup_mod.PREFERRED_CLEANUP_MODELS[0]
        )
        self._bad_models: set[str] = set()

    def warm(self) -> None:
        self.transcriber.warm()

    def resolve_cleanup_model(self) -> str | None:
        """Pick a cleanup model the key can actually use. One GET /models (~300 ms); called at
        startup and again whenever the current model is rejected."""
        if not (self.cfg.effective_cleanup and self.client):
            return None
        try:
            available = [m for m in self.client.list_models() if m not in self._bad_models]
        except Exception as e:
            log.warning("couldn't list Groq models (%s); keeping cleanup model %s", e, self.cleanup_model)
            return self.cleanup_model
        wanted = self.cfg.cleanup_model if self.cfg.cleanup_model not in self._bad_models else "auto"
        picked = cleanup_mod.pick_cleanup_model(wanted, available)
        if picked is None:
            log.error("no usable cleanup model for this key; cleanup disabled until restart")
        elif picked != self.cfg.cleanup_model and self.cfg.cleanup_model != "auto":
            log.warning(
                "cleanup_model %r isn't available to this key; using %r instead "
                "(set cleanup_model in config.toml to silence this)", self.cfg.cleanup_model, picked,
            )
        else:
            log.info("cleanup model: %s", picked)
        self.cleanup_model = picked
        return picked

    def text_for(self, rec: Recording) -> tuple[str, dict]:
        """Recording -> final text (no paste). Returns (text, info)."""
        info: dict = {"stt_ms": 0.0, "cleanup_ms": 0.0, "cleanup": "n/a", "raw": ""}
        t0 = time.perf_counter()
        raw = self.transcriber.transcribe(rec)
        info["stt_ms"] = (time.perf_counter() - t0) * 1000
        info["raw"] = raw
        text = filter_hallucination(raw, self.cfg)
        if not text:
            info["cleanup"] = "dropped-hallucination"
            return "", info
        t1 = time.perf_counter()
        text, status = cleanup_mod.clean(
            text,
            self.client if self.cfg.effective_cleanup else None,
            model=self.cleanup_model or "",
            enabled=self.cfg.effective_cleanup and self.cleanup_model is not None,
            min_words=self.cfg.cleanup_min_words,
            timeout=self.cfg.cleanup_timeout_seconds,
            mode=self.cfg.cleanup_mode,
        )
        info["cleanup_ms"] = (time.perf_counter() - t1) * 1000
        info["cleanup"] = status
        info["cleanup_model"] = self.cleanup_model
        if status == "fallback-model-error" and self.cleanup_model:
            # Model gone or rejecting our params: stop using it, pick another for next time.
            self._bad_models.add(self.cleanup_model)
            self.resolve_cleanup_model()
        text = apply_replacements(text, self.cfg.replacements)
        return text.strip(), info

    def process(self, rec: Recording, released_at: float) -> str | None:
        cfg = self.cfg
        reason = rejection_reason(rec, cfg.min_seconds, cfg.silence_rms_threshold)
        if reason:
            log.info("discarded recording: %s", reason)
            return None
        if cfg.debug_save_audio:
            log.debug("saved debug audio to %s", save_debug_audio(rec, spoke_home() / "debug_audio"))
        try:
            text, info = self.text_for(rec)
        except TranscriptionError as e:
            log.error("transcription failed: %s", e)
            notify.notify("Spoke: transcription failed", str(e)[:200], cfg.notifications)
            self.sounds.play("error")
            return None
        if not text:
            return None
        t_paste = time.perf_counter()
        app = active_app()
        final = self.continuation.prefix(text, app) + text
        method = "none"
        if self.injector is not None:
            try:
                method = self.injector.inject(final, app)
                self.continuation.record(app)
            except Exception as e:
                log.exception("paste failed")
                notify.notify("Spoke: paste failed", f"{type(e).__name__}: {e}"[:200], cfg.notifications)
                self.sounds.play("error")
                return None
        # "Visible" = the paste keystroke was sent; the clipboard restore wait comes after
        # and isn't user-facing latency.
        visible = getattr(self.injector, "last_visible_at", 0.0) or time.perf_counter()
        t = Timings(
            record_s=rec.duration,
            stt_ms=info["stt_ms"],
            cleanup_ms=info["cleanup_ms"],
            paste_ms=(visible - t_paste) * 1000,
            total_ms=(visible - released_at) * 1000,
        )
        log.debug(
            "latency: audio=%.2fs stt=%.0fms cleanup=%.0fms(%s) paste=%.0fms total=%.0fms (release->text visible)",
            t.record_s, t.stt_ms, t.cleanup_ms, info["cleanup"], t.paste_ms, t.total_ms,
        )
        if self.history:
            self.history.append(
                raw=info["raw"], text=text, app=app, method=method, cleanup=info["cleanup"],
                language=cfg.language, backend=cfg.effective_stt_backend, latency=t.as_dict(),
            )
        return final


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        key, _src = get_api_key()
        needs_cloud = cfg.effective_stt_backend == "groq" or cfg.effective_cleanup
        self.client = GroqClient(key) if (key and needs_cloud) else None
        self.sounds = notify.Sounds(cfg.sounds)
        self.recorder = Recorder(cfg.max_seconds, cfg.keep_mic_open, cfg.input_device, on_max_reached=self._on_max)
        self.injector = Injector(
            make_clipboard(),
            Keys(terminal_classes=cfg.linux_terminal_classes),
            method=cfg.paste_method,
            restore_delay=cfg.paste_restore_delay_ms / 1000.0,
        )
        self.history = History(spoke_home() / "history.jsonl", cfg.history, int(cfg.history_max_mb * 1024 * 1024))
        self.pipeline = Pipeline(cfg, self.client, self.injector, self.history, self.sounds)
        self.listener = HotkeyListener(
            cfg.hotkey, cfg.mode, self.on_action, cfg.toggle_double_tap_ms, is_injecting=lambda: self.injector.injecting
        )
        self.tray = None
        self._jobs: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._pending = 0
        self._stop = threading.Event()

    # --- hotkey actions (listener thread) ---
    def on_action(self, action: str) -> None:
        if action == START:
            self._start()
        elif action == STOP:
            self._finish(auto=False)
        elif action == CANCEL:
            with self._lock:
                if self.recorder.is_recording:
                    self.recorder.cancel()
                    log.info("recording cancelled (hotkey used in a key combo)")
            self._set_state()

    def _start(self) -> None:
        t0 = time.perf_counter()
        with self._lock:
            if self.recorder.is_recording:
                return
            try:
                self.recorder.start()
            except Exception as e:
                self.listener.machine.reset()
                notify.notify("Spoke: microphone error", str(e)[:200], self.cfg.notifications)
                self.sounds.play("error")
                return
        log.debug("capture started %.1f ms after key-down", (time.perf_counter() - t0) * 1000)
        self.sounds.play("start")
        self.pipeline.warm()
        self._set_state("recording")

    def _finish(self, auto: bool) -> None:
        released = time.perf_counter()
        with self._lock:
            if not self.recorder.is_recording:
                return
            rec = self.recorder.stop()
            self._pending += 1
        self.sounds.play("stop")
        self._set_state("processing")
        self._jobs.put((rec, released))

    def _on_max(self) -> None:
        self.listener.machine.reset()
        notify.notify(
            "Spoke: recording stopped",
            f"Hit the {self.cfg.max_seconds:.0f}s limit (max_seconds); transcribing what you said.",
            self.cfg.notifications,
        )
        self._finish(auto=True)

    # --- worker ---
    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                rec, released = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.pipeline.process(rec, released)
            except Exception:
                log.exception("pipeline crashed")
                self.sounds.play("error")
            finally:
                with self._lock:
                    self._pending -= 1
                self._set_state()

    def _set_state(self, state: str | None = None) -> None:
        if state is None:
            state = "recording" if self.recorder.is_recording else ("processing" if self._pending else "idle")
        if self.tray:
            self.tray.set_state(state)

    # --- lifecycle ---
    def quit(self) -> None:
        self._stop.set()
        self.listener.stop()
        self.recorder.close()
        if self.tray:
            self.tray.stop()

    def run(self) -> None:
        threading.Thread(target=self._worker, name="spoke-worker", daemon=True).start()
        self.recorder.warm()
        if self.client:
            # Also opens the TLS connection, so the first dictation starts warm; the
            # keepalive then stops it going cold between dictations.
            threading.Thread(target=self.pipeline.resolve_cleanup_model, daemon=True).start()
            self.client.start_keepalive()
        mode = "hold the key" if self.cfg.mode == "hold" else "double-tap the key to start, tap to stop"
        log.info("Spoke running on %s: %s (%s). Ctrl+C to quit.", CURRENT.describe(), self.cfg.hotkey, mode)

        if self.cfg.tray:
            from . import tray as tray_mod

            self.tray = tray_mod.try_create(
                "Spoke", self.quit, self._open_history, level=lambda: self.recorder.level
            )
        if self.tray:
            notify.set_tray_notifier(self.tray.notify)
            # pystray needs the main thread on macOS; pynput listens on its own thread.
            self.listener.start()
            try:
                self.tray.run(setup=lambda: self._set_state("idle"))
            except KeyboardInterrupt:
                pass
            self.quit()
            return

        self.listener.start()
        try:
            while not self._stop.is_set() and self.listener._listener.is_alive():
                self.listener._listener.join(0.5)
        except KeyboardInterrupt:
            pass
        self.quit()

    def _open_history(self) -> None:
        import subprocess

        path = str(self.history.path)
        try:
            if CURRENT.is_mac:
                subprocess.Popen(["open", "-R", path])
            elif CURRENT.is_windows:
                subprocess.Popen(["explorer", "/select,", path])
            else:
                subprocess.Popen(["xdg-open", str(self.history.path.parent)])
        except Exception as e:
            log.warning("couldn't open history: %s", e)
