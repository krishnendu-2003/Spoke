"""Speech-to-text: Groq Whisper (default) or local faster-whisper (local_only / stt_backend=local)."""

from __future__ import annotations

import logging
import threading

import numpy as np

from .config import Config
from .groq_api import GroqClient, GroqError
from .recorder import Recording
from .vocab import build_prompt, is_hallucination

log = logging.getLogger(__name__)


class TranscriptionError(Exception):
    pass


def _language(cfg: Config) -> str | None:
    lang = (cfg.language or "").strip().lower()
    return None if lang in ("", "auto") else lang


class GroqTranscriber:
    def __init__(self, cfg: Config, client: GroqClient) -> None:
        self.cfg = cfg
        self.client = client
        self.prompt = build_prompt(cfg.vocab)

    def warm(self) -> None:
        self.client.warm()

    def transcribe(self, rec: Recording) -> str:
        # Scale timeout with length: a 5-minute upload needs more than 10 s on a slow uplink.
        timeout = self.cfg.stt_timeout_seconds + rec.duration / 10
        try:
            return self.client.transcribe(
                rec.to_wav(),
                model=self.cfg.stt_model,
                language=_language(self.cfg),
                prompt=self.prompt,
                timeout=timeout,
                retries=1,
            )
        except GroqError as e:
            raise TranscriptionError(str(e)) from e


class LocalTranscriber:
    """faster-whisper on CPU, int8. The model loads once (first call or warm())."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.prompt = build_prompt(cfg.vocab)
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as e:
                    raise TranscriptionError(
                        "local STT needs faster-whisper: pip install -r requirements-local.txt"
                    ) from e
                log.info("loading faster-whisper model %r (first run downloads it)", self.cfg.local_model_size)
                self._model = WhisperModel(self.cfg.local_model_size, device="cpu", compute_type="int8")
        return self._model

    def warm(self) -> None:
        threading.Thread(target=self._load, daemon=True).start()

    def transcribe(self, rec: Recording) -> str:
        model = self._load()
        audio = rec.samples.astype(np.float32) / 32768.0
        try:
            segments, _info = model.transcribe(
                audio,
                language=_language(self.cfg),
                initial_prompt=self.prompt or None,
                beam_size=1,  # greedy: ~2x faster on CPU, small accuracy cost
                vad_filter=True,
                condition_on_previous_text=False,
            )
            return " ".join(s.text.strip() for s in segments).strip()
        except Exception as e:
            raise TranscriptionError(f"local STT failed: {type(e).__name__}: {e}") from e


def make_transcriber(cfg: Config, client: GroqClient | None):
    if cfg.effective_stt_backend == "local":
        return LocalTranscriber(cfg)
    if client is None:
        raise TranscriptionError("No Groq API key. Run `python -m spoke setup` or set GROQ_API_KEY.")
    return GroqTranscriber(cfg, client)


def filter_hallucination(text: str, cfg: Config) -> str:
    """Return '' if the transcript is a known silence hallucination."""
    if is_hallucination(text, cfg.hallucination_blocklist):
        log.info("dropped likely hallucination (%d chars)", len(text))
        return ""
    return text
