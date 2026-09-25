"""Microphone capture: 16 kHz mono int16 into an in-memory buffer. Audio never hits disk
unless debug_save_audio is on."""

from __future__ import annotations

import io
import logging
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHANNELS = 1
DTYPE = "int16"
BLOCK_MS = 20


@dataclass
class Recording:
    samples: np.ndarray  # int16, mono
    sample_rate: int = SAMPLE_RATE

    @property
    def duration(self) -> float:
        return len(self.samples) / self.sample_rate

    @property
    def rms(self) -> float:
        return rms(self.samples)

    def to_wav(self) -> bytes:
        return encode_wav(self.samples, self.sample_rate)

    def to_upload(self, fmt: str = "flac") -> tuple[bytes, str, str]:
        """(bytes, filename, mime) for the STT upload. FLAC is lossless and ~1/3 smaller
        than WAV for speech (less upload time on a slow uplink); falls back to WAV."""
        if fmt == "flac":
            try:
                return encode_flac(self.samples, self.sample_rate), "audio.flac", "audio/flac"
            except Exception as e:  # soundfile/libsndfile missing or broken
                log.debug("FLAC encode unavailable (%s); sending WAV", e)
        return self.to_wav(), "audio.wav", "audio/wav"


def rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    x = samples.astype(np.float64)
    return float(np.sqrt(np.mean(x * x)))


def encode_wav(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    """16-bit PCM WAV in memory. WAV over FLAC: zero encode cost, and Groq recommends WAV
    for latency; 5 min at 16 kHz mono is ~9.6 MB, under the 25 MB free-tier upload cap."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(samples.astype("<i2", copy=False).tobytes())
    return buf.getvalue()


def block_level(samples: np.ndarray) -> float:
    """Loudness of one audio block mapped to 0..1 on a log scale: room noise (~RMS 30) ~ 0,
    normal speech (~RMS 1000-3000) ~ 0.75-1."""
    r = rms(samples)
    db = 20 * np.log10(r + 1.0)
    return float(np.clip((db - 30.0) / 40.0, 0.0, 1.0))


def smooth_level(prev: float, new: float) -> float:
    """Fast attack, slow release, so the waveform jumps with syllables and settles gently."""
    return new if new > prev else prev * 0.85 + new * 0.15


def encode_flac(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, samples.astype(np.int16, copy=False), sample_rate, format="FLAC", subtype="PCM_16")
    return buf.getvalue()


def rejection_reason(rec: Recording, min_seconds: float, silence_rms: float) -> str | None:
    """Why a recording should NOT be sent to the API, or None if it's fine."""
    if rec.duration < min_seconds:
        return f"too short ({rec.duration:.2f}s < {min_seconds}s)"
    level = rec.rms
    if level < silence_rms:
        return f"silence (rms {level:.0f} < {silence_rms:.0f})"
    return None


def save_debug_audio(rec: Recording, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / time.strftime("spoke-%Y%m%d-%H%M%S.wav")
    path.write_bytes(rec.to_wav())
    return path


def _parse_device(device: str):
    if not device:
        return None
    return int(device) if device.strip().isdigit() else device


class Recorder:
    """Start/stop capture. The callback thread only appends to a list, so start() returns
    as soon as the stream is running."""

    def __init__(
        self,
        max_seconds: float = 300.0,
        keep_open: bool = False,
        device: str = "",
        on_max_reached: Callable[[], None] | None = None,
    ) -> None:
        self.max_frames = int(max_seconds * SAMPLE_RATE)
        self.keep_open = keep_open
        self.device = _parse_device(device)
        self.on_max_reached = on_max_reached
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._frames = 0
        self._recording = False
        self._max_fired = False
        self._stream = None
        self.last_open_ms: float = 0.0
        self.level: float = 0.0  # smoothed 0..1 loudness of the live input, for the tray waveform

    # sounddevice is imported lazily so `import spoke` works without PortAudio (tests, CI).
    def _open_stream(self) -> None:
        if self._stream is not None:
            return
        import sounddevice as sd

        t0 = time.perf_counter()
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=int(SAMPLE_RATE * BLOCK_MS / 1000),
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()
        self.last_open_ms = (time.perf_counter() - t0) * 1000
        log.debug("mic stream opened in %.1f ms", self.last_open_ms)

    def _close_stream(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        finally:
            self._stream = None

    def warm(self) -> None:
        """Open the stream ahead of time (keep_mic_open mode)."""
        if self.keep_open:
            self._open_stream()

    def _callback(self, indata, frames, time_info, status) -> None:  # PortAudio thread
        if status:
            log.debug("audio status: %s", status)
        fire = False
        with self._lock:
            if not self._recording:
                return
            room = self.max_frames - self._frames
            if room <= 0:
                return
            chunk = indata[:room, 0].copy()
            self.level = smooth_level(self.level, block_level(chunk))
            self._chunks.append(chunk)
            self._frames += len(chunk)
            if self._frames >= self.max_frames and not self._max_fired:
                self._max_fired = True
                fire = True
        if fire and self.on_max_reached:
            threading.Thread(target=self.on_max_reached, daemon=True).start()

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self) -> None:
        with self._lock:
            self.level = 0.0
            self._chunks = []
            self._frames = 0
            self._max_fired = False
            self._recording = True
        try:
            self._open_stream()
        except Exception:
            self._recording = False
            raise

    def stop(self) -> Recording:
        with self._lock:
            self._recording = False
            chunks, self._chunks = self._chunks, []
        if not self.keep_open:
            self._close_stream()
        samples = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
        return Recording(samples)

    def cancel(self) -> None:
        self.stop()

    def close(self) -> None:
        with self._lock:
            self._recording = False
        self._close_stream()

    def record_for(self, seconds: float) -> Recording:
        self.start()
        time.sleep(seconds)
        return self.stop()
