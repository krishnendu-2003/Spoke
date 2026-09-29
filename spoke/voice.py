"""Voice lock: on-device noise suppression + "only my voice" gating.

While you hold the hotkey, every 20 ms mic block also goes to a VoiceSession worker thread
that denoises it (GTCRN, streaming), finds speech with Silero VAD, and scores each ~1.5 s
piece of speech against your enrolled voiceprint (TitaNet-small speaker embeddings). Pieces
that don't sound like you are cut. Only the kept spans of your ORIGINAL audio go to STT
(the denoised copy is only used to find and match speech: on a real MacBook test, sending
denoised audio mangled words that Whisper got right from the original), so most of the work
happens while you are still talking and key release only waits for the last piece.

Everything here runs on the CPU through sherpa-onnx. Audio and the voiceprint never leave
the machine; the models are downloaded once from the sherpa-onnx GitHub releases into
~/.spoke/models and checked against pinned SHA-256 hashes.

Limit: this removes OTHER people's speech when they talk before, after or between your
sentences. When someone talks at the same moment as you, that piece is a mix; it is kept
if you dominate it (you're closer to the mic) and cut if they do. Pulling your voice out
of overlapping speech is target-speaker extraction, a different and much heavier model.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .config import Config, spoke_home

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"


@dataclass(frozen=True)
class ModelFile:
    filename: str
    url: str
    sha256: str
    size_mb: float


# Pinned 2026-09-25. TitaNet-small beat CAM++, ERes2Net and WeSpeaker ResNet34 on 1-1.5 s
# pieces in a 12-voice test (0.0-0.8% EER vs 1.5-40%) and is the fastest (~10-13 ms/piece).
MODELS = {
    "speaker": ModelFile(
        "nemo_en_titanet_small.onnx",
        f"{RELEASES}/speaker-recongition-models/nemo_en_titanet_small.onnx",
        "ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e",
        40.3,
    ),
    "vad": ModelFile(
        "silero_vad.onnx",
        f"{RELEASES}/asr-models/silero_vad.onnx",
        "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
        0.6,
    ),
    "denoiser": ModelFile(
        "gtcrn_simple.onnx",
        f"{RELEASES}/speech-enhancement-models/gtcrn_simple.onnx",
        "e77603ac0c23dac3227dd2d7135b3a585cbee2679048aecfa886657d3ae1b534",
        0.5,
    ),
}
SPEAKER_MODEL_ID = "nemo_en_titanet_small"

PIECE_SECONDS = 1.5  # speech is scored in pieces of ~this length
MIN_EMBED_SECONDS = 1.0  # shorter pieces are scored with surrounding audio as context
PAD_SECONDS = 0.2  # kept speech is padded so word edges aren't clipped
GAP_SECONDS = 0.15  # silence inserted between kept runs
FADE_SECONDS = 0.01  # kept runs fade in/out so cuts don't click
DEFAULT_THRESHOLD = 0.50
THRESHOLD_MIN, THRESHOLD_MAX = 0.42, 0.58
# How a recording is judged (tuned on simulated crowds, see decide()):
#  1. Your best piece must reach the threshold, or nothing is sent ("not you").
#  2. Then every piece within RELATIVE_MARGIN of that best piece (and above SCORE_FLOOR) is
#     kept. Noise and crowd babble drag all of YOUR scores down together (0.8 clean -> ~0.5
#     with babble 10 dB below you), while another person's turn scores far below your best.
RELATIVE_MARGIN = 0.20
SCORE_FLOOR = 0.30


def models_dir() -> Path:
    return Path(os.environ.get("SPOKE_VOICE_MODELS") or spoke_home() / "models")


def profile_path() -> Path:
    return spoke_home() / "voiceprint.json"


def missing_models(directory: Path | None = None) -> list[ModelFile]:
    directory = directory or models_dir()
    return [m for m in MODELS.values() if not (directory / m.filename).exists()]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download_models(directory: Path | None = None, say: Callable[[str], None] = print) -> None:
    """Fetch any missing model, verify its hash, then move it into place."""
    import httpx

    directory = directory or models_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for m in missing_models(directory):
        say(f"Downloading {m.filename} ({m.size_mb:.1f} MB)...")
        part = directory / (m.filename + ".part")
        with httpx.stream("GET", m.url, follow_redirects=True, timeout=60.0) as r:
            r.raise_for_status()
            with part.open("wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)
        digest = _sha256(part)
        if digest != m.sha256:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"{m.filename}: SHA-256 mismatch ({digest}); not using it")
        part.replace(directory / m.filename)


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


# --- engine ------------------------------------------------------------------------------

class _Vad:
    """Silero VAD behind a tiny interface: feed audio, get (start_sample, n_samples) back."""

    def __init__(self, cfg) -> None:
        import sherpa_onnx

        self._vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=400)
        self._window = cfg.silero_vad.window_size
        self._rest = np.zeros(0, dtype=np.float32)

    def accept(self, x: np.ndarray) -> list[tuple[int, int]]:
        buf = np.concatenate([self._rest, x]) if self._rest.size else x
        n = len(buf) - len(buf) % self._window
        for i in range(0, n, self._window):
            self._vad.accept_waveform(buf[i : i + self._window])
        self._rest = buf[n:]
        return self._pop()

    def flush(self) -> list[tuple[int, int]]:
        if self._rest.size:
            self._vad.accept_waveform(self._rest)
            self._rest = np.zeros(0, dtype=np.float32)
        self._vad.flush()
        return self._pop()

    def _pop(self) -> list[tuple[int, int]]:
        out = []
        while not self._vad.empty():
            seg = self._vad.front
            out.append((int(seg.start), len(seg.samples)))
            self._vad.pop()
        return out


class _Denoiser:
    def __init__(self, cfg) -> None:
        import sherpa_onnx

        self._d = sherpa_onnx.OnlineSpeechDenoiser(cfg)

    def run(self, x: np.ndarray) -> np.ndarray:
        return np.asarray(self._d.run(x, SAMPLE_RATE).samples, dtype=np.float32)

    def flush(self) -> np.ndarray:
        return np.asarray(self._d.flush().samples, dtype=np.float32)


class Engine:
    """Loads the models once. The speaker extractor is shared (stateless per call); every
    session gets its own streaming denoiser and VAD (~50 ms to create, done off the hotkey
    thread)."""

    def __init__(self, directory: Path | None = None, threads: int = 2) -> None:
        import sherpa_onnx

        d = directory or models_dir()
        missing = missing_models(d)
        if missing:
            raise FileNotFoundError(
                "voice models missing: " + ", ".join(m.filename for m in missing) + " (run `spoke enroll`)"
            )
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(d / MODELS["speaker"].filename), num_threads=threads)
        )
        vc = sherpa_onnx.VadModelConfig()
        vc.silero_vad.model = str(d / MODELS["vad"].filename)
        vc.silero_vad.threshold = 0.5
        vc.silero_vad.min_silence_duration = 0.25
        vc.silero_vad.min_speech_duration = 0.2
        vc.silero_vad.max_speech_duration = 20.0
        vc.sample_rate = SAMPLE_RATE
        vc.num_threads = 1
        self._vad_cfg = vc
        self._den_cfg = sherpa_onnx.OnlineSpeechDenoiserConfig(
            model=sherpa_onnx.OfflineSpeechDenoiserModelConfig(
                gtcrn=sherpa_onnx.OfflineSpeechDenoiserGtcrnModelConfig(model=str(d / MODELS["denoiser"].filename)),
                num_threads=1,
            )
        )

    @property
    def dim(self) -> int:
        return int(self._extractor.dim)

    def new_vad(self) -> _Vad:
        return _Vad(self._vad_cfg)

    def new_denoiser(self) -> _Denoiser:
        return _Denoiser(self._den_cfg)

    def embed(self, x: np.ndarray) -> np.ndarray:
        s = self._extractor.create_stream()
        s.accept_waveform(SAMPLE_RATE, x)
        s.input_finished()
        return _unit(np.asarray(self._extractor.compute(s), dtype=np.float32))


# --- voiceprint --------------------------------------------------------------------------

@dataclass
class Profile:
    embeddings: list[list[float]]
    threshold: float = DEFAULT_THRESHOLD
    model: str = SPEAKER_MODEL_ID
    created: str = ""
    self_scores: list[float] = field(default_factory=list)

    @property
    def centroid(self) -> np.ndarray:
        return _unit(np.mean(np.asarray(self.embeddings, dtype=np.float32), axis=0))

    def save(self, path: Path | None = None) -> Path:
        path = path or profile_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        return path

    @classmethod
    def load(cls, path: Path | None = None) -> Profile | None:
        path = path or profile_path()
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        p = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        if p.model != SPEAKER_MODEL_ID:
            raise ValueError(f"voiceprint was made with {p.model}; re-run `spoke enroll`")
        return p


def calibrate_threshold(self_scores: list[float]) -> float:
    """From how well your own enrollment pieces match your voiceprint: sit a little below
    your weakest typical piece, within safe bounds."""
    if len(self_scores) < 3:
        return DEFAULT_THRESHOLD
    return float(np.clip(np.quantile(self_scores, 0.05) - 0.22, THRESHOLD_MIN, THRESHOLD_MAX))


# --- per-recording session -----------------------------------------------------------------

@dataclass
class Piece:
    start: int
    end: int
    score: float | None = None  # None = not scored (no voiceprint, enrollment mode)
    kept: bool = True


@dataclass
class VoiceResult:
    status: str  # ok | no-speech | not-you | error
    samples: np.ndarray  # int16 audio to send (empty when nothing is kept)
    speech_s: float = 0.0
    kept_s: float = 0.0
    tail_ms: float = 0.0  # work left after key release
    pieces: list[Piece] = field(default_factory=list)
    clean: np.ndarray | None = None  # full processed float audio (enrollment uses it)

    @property
    def best_score(self) -> float | None:
        s = [p.score for p in self.pieces if p.score is not None]
        return max(s) if s else None

    def describe(self) -> str:
        scores = " ".join(f"{p.score:.2f}{'' if p.kept else 'x'}" for p in self.pieces if p.score is not None)
        return f"{self.status}: kept {self.kept_s:.1f}s of {self.speech_s:.1f}s speech [{scores}] tail {self.tail_ms:.0f}ms"


def split_segment(start: int, n: int, piece: int = int(PIECE_SECONDS * SAMPLE_RATE)) -> list[tuple[int, int]]:
    """Cut one speech segment into ~equal pieces of about `piece` samples."""
    k = max(1, round(n / piece))
    edges = np.linspace(start, start + n, k + 1).astype(int)
    return [(int(a), int(b)) for a, b in zip(edges[:-1], edges[1:])]


def decide(pieces: list[Piece], threshold: float, margin: float = RELATIVE_MARGIN) -> None:
    """Mark which pieces are you. Unscored pieces (no voiceprint) are always kept."""
    scored = [p.score for p in pieces if p.score is not None]
    if not scored:
        return
    best = max(scored)
    cut = max(SCORE_FLOOR, best - margin) if best >= threshold else float("inf")
    for p in pieces:
        if p.score is not None:
            p.kept = p.score >= cut


class VoiceSession:
    """One recording's worth of streaming voice processing. `feed` is called from the
    PortAudio callback and only enqueues; all model work (including loading the engine the
    first time) happens on this session's own thread, never on the hotkey thread.

    Analysis always runs on denoised audio; `send_denoised` only picks which audio is sent.
    Without a centroid (noise suppression only, or enrollment) nothing is cut."""

    def __init__(
        self,
        engine,  # an Engine, or a zero-arg callable returning one
        centroid: np.ndarray | None,
        threshold: float,
        send_denoised: bool = False,
    ) -> None:
        self._engine_src = engine
        self.centroid = centroid
        self.threshold = threshold
        self.send_denoised = send_denoised
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._raw: list[np.ndarray] = []
        self._clean: list[np.ndarray] = []
        self._pieces: list[Piece] = []
        self._error: BaseException | None = None
        self._done = threading.Event()
        self._finished_at = 0.0
        self._thread = threading.Thread(target=self._run, name="spoke-voice", daemon=True)
        self._thread.start()

    def feed(self, chunk_int16: np.ndarray) -> None:
        self._q.put(chunk_int16)

    def finish(self, timeout: float = 10.0) -> VoiceResult:
        released = time.perf_counter()
        self._q.put(None)
        if not self._done.wait(timeout):
            log.warning("voice filter timed out after %.0f s", timeout)
            return VoiceResult("error", np.zeros(0, dtype=np.int16))
        tail_ms = max(0.0, (self._finished_at - released) * 1000)
        if self._error is not None:
            log.warning("voice filter failed: %s: %s", type(self._error).__name__, self._error)
            return VoiceResult("error", np.zeros(0, dtype=np.int16), tail_ms=tail_ms)
        return self._result(tail_ms)

    def cancel(self) -> None:
        self._q.put(None)

    # --- worker thread ---
    def _run(self) -> None:
        try:
            src = self._engine_src
            self.engine = src() if callable(src) else src
            den = self.engine.new_denoiser()
            vad = self.engine.new_vad()
            while True:
                item = self._q.get()
                if item is None:
                    break
                x = item.astype(np.float32) / 32768.0
                self._raw.append(x)
                y = den.run(x)
                self._push(y, vad.accept(y))
            tail = den.flush()
            self._push(tail, vad.accept(tail))
            self._push(np.zeros(0, dtype=np.float32), vad.flush())
        except BaseException as e:  # never let a model error kill dictation
            self._error = e
        finally:
            self._finished_at = time.perf_counter()
            self._done.set()

    def _push(self, y: np.ndarray, segments: list[tuple[int, int]]) -> None:
        if y.size:
            self._clean.append(y)
        if not segments:
            return
        audio = _joined(self._clean)
        self._clean = [audio]
        for start, n in segments:
            for a, b in split_segment(start, n):
                p = Piece(a, b)
                if self.centroid is not None:
                    p.score = float(self.engine.embed(context(audio, a, b)) @ self.centroid)
                self._pieces.append(p)

    def _result(self, tail_ms: float) -> VoiceResult:
        clean = _joined(self._clean)
        out_audio = clean if self.send_denoised else _joined(self._raw)
        n = min(len(clean), len(out_audio))
        speech = sum(p.end - p.start for p in self._pieces)
        res = VoiceResult("ok", _to_int16(out_audio), speech_s=speech / SAMPLE_RATE,
                          kept_s=len(out_audio) / SAMPLE_RATE, tail_ms=tail_ms,
                          pieces=self._pieces, clean=clean)
        if self.centroid is None:  # noise suppression only: send everything
            return res
        res.samples, res.kept_s = np.zeros(0, dtype=np.int16), 0.0
        if not self._pieces:
            res.status = "no-speech"
            return res
        decide(self._pieces, self.threshold)
        mask = np.zeros(n, dtype=bool)
        pad = int(PAD_SECONDS * SAMPLE_RATE)
        for p in self._pieces:
            if p.kept:
                mask[max(0, p.start - pad) : min(n, p.end + pad)] = True
        for p in self._pieces:  # padding never reaches into someone else's speech
            if not p.kept:
                mask[p.start : min(n, p.end)] = False
        if not mask.any():
            res.status = "not-you"
            return res
        edges = np.flatnonzero(np.diff(np.concatenate([[0], mask.astype(np.int8), [0]])))
        gap = np.zeros(int(GAP_SECONDS * SAMPLE_RATE), dtype=np.float32)
        runs: list[np.ndarray] = []
        for a, b in zip(edges[::2], edges[1::2]):
            if runs:
                runs.append(gap)
            runs.append(_faded(out_audio[a:b]))
        res.samples = _to_int16(np.concatenate(runs))
        res.kept_s = float(mask.sum()) / SAMPLE_RATE
        return res


def _joined(parts: list[np.ndarray]) -> np.ndarray:
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return parts[0] if len(parts) == 1 else np.concatenate(parts)


def _faded(x: np.ndarray) -> np.ndarray:
    k = min(int(FADE_SECONDS * SAMPLE_RATE), len(x) // 2)
    if k == 0:
        return x
    x = x.copy()
    ramp = np.linspace(0.0, 1.0, k, dtype=np.float32)
    x[:k] *= ramp
    x[-k:] *= ramp[::-1]
    return x


def _to_int16(x: np.ndarray) -> np.ndarray:
    return np.clip(x * 32768.0, -32768, 32767).astype(np.int16)


def context(audio: np.ndarray, a: int, b: int) -> np.ndarray:
    """audio[a:b], widened with surrounding audio to at least MIN_EMBED_SECONDS."""
    need = int(MIN_EMBED_SECONDS * SAMPLE_RATE) - (b - a)
    if need > 0:
        a, b = max(0, a - need // 2), min(len(audio), b + need - need // 2)
    return audio[a:b]


# --- what the daemon / CLI use -------------------------------------------------------------

class VoiceFilter:
    """The configured voice mode for the daemon and CLI.

    `active` is False when neither voice_lock nor noise_suppression is on, or when they
    can't run; then Spoke dictates exactly as before and `problem` says why."""

    def __init__(self, cfg: Config, engine=None, profile: Profile | None = None) -> None:
        self.cfg = cfg
        self.problem = ""
        self.profile = profile
        self._engine = engine
        self._lock = threading.Lock()
        self.lock = cfg.voice_lock
        self.active = cfg.voice_lock or cfg.noise_suppression
        if not self.active:
            return
        if engine is None:
            try:
                import sherpa_onnx  # noqa: F401
            except ImportError:
                self._off("sherpa-onnx isn't installed (pip install -r requirements.txt)")
                return
            if missing_models():
                self._off(f"voice models aren't downloaded yet (run `{_enroll_cmd()}`)")
                return
        if self.lock and self.profile is None:
            try:
                self.profile = Profile.load()
            except Exception as e:
                self.problem = f"voiceprint unreadable: {e}"
            if self.profile is None:
                self.problem = self.problem or f"voice_lock is on but you haven't enrolled (run `{_enroll_cmd()}`)"
                self.lock = False
                self.active = cfg.noise_suppression

    def _off(self, why: str) -> None:
        self.problem, self.lock, self.active = why, False, False

    @property
    def threshold(self) -> float:
        if self.cfg.voice_lock_threshold > 0:
            return self.cfg.voice_lock_threshold
        return self.profile.threshold if self.profile else DEFAULT_THRESHOLD

    def engine(self):
        with self._lock:
            if self._engine is None:
                t0 = time.perf_counter()
                self._engine = Engine()
                log.info("voice models loaded in %.0f ms", (time.perf_counter() - t0) * 1000)
            return self._engine

    def warm(self) -> None:
        """Load the models in the background so the first dictation doesn't wait for them."""
        if self.active:
            threading.Thread(target=self._warm, name="spoke-voice-load", daemon=True).start()

    def _warm(self) -> None:
        try:
            self.engine()
        except Exception as e:
            log.warning("couldn't load voice models: %s", e)

    def session(self) -> VoiceSession | None:
        if not self.active:
            return None
        return VoiceSession(
            self.engine,
            self.profile.centroid if (self.lock and self.profile) else None,
            self.threshold,
            send_denoised=self.cfg.noise_suppression,
        )

    def process(self, samples: np.ndarray) -> VoiceResult | None:
        """Whole-recording path (test-mic, tests): the same session, fed after the fact."""
        s = self.session()
        if s is None:
            return None
        feed_all(s, samples)
        return s.finish()


def _enroll_cmd() -> str:
    from .config import spoke_cmd

    return spoke_cmd("enroll")


def feed_all(session: VoiceSession, samples: np.ndarray) -> None:
    block = SAMPLE_RATE // 50
    for i in range(0, len(samples), block):
        session.feed(samples[i : i + block])


# --- enrollment ----------------------------------------------------------------------------

@dataclass
class EnrollClip:
    clean: np.ndarray
    pieces: list[Piece]
    embedding: np.ndarray
    speech_s: float


def analyse_clip(engine, samples: np.ndarray) -> EnrollClip:
    """Denoise + VAD one enrollment recording, embed all of its speech."""
    s = VoiceSession(engine, None, 0.0)
    feed_all(s, samples)
    r = s.finish()
    if r.status == "error":
        raise RuntimeError("voice processing failed; see ~/.spoke/spoke.log")
    speech = [r.clean[p.start : p.end] for p in r.pieces]
    if not speech:
        return EnrollClip(r.clean, [], np.zeros(0, dtype=np.float32), 0.0)
    x = np.concatenate(speech)
    return EnrollClip(r.clean, r.pieces, engine.embed(x), len(x) / SAMPLE_RATE)


def piece_scores(engine, clips: list[EnrollClip], earlier: list[np.ndarray] | None = None) -> list[float]:
    """Leave-one-out: each clip's pieces scored against a voiceprint built from everything
    EXCEPT that clip, i.e. how well your unseen dictation will match."""
    earlier = list(earlier or [])
    out: list[float] = []
    for i, clip in enumerate(clips):
        others = earlier + [c.embedding for j, c in enumerate(clips) if j != i]
        if not others:
            continue
        ref = _unit(np.mean(others, axis=0))
        out += [float(engine.embed(context(clip.clean, p.start, p.end)) @ ref) for p in clip.pieces]
    return out


def build_profile(engine, clips: list[EnrollClip], base: Profile | None = None) -> Profile:
    earlier = [np.asarray(e, dtype=np.float32) for e in base.embeddings] if base else []
    scores = (base.self_scores if base else []) + piece_scores(engine, clips, earlier)
    return Profile(
        embeddings=[e.tolist() for e in earlier] + [c.embedding.tolist() for c in clips],
        threshold=calibrate_threshold(scores),
        created=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        self_scores=[round(x, 4) for x in scores],
    )


def resample(x: np.ndarray, sr: int) -> np.ndarray:
    """Linear resample to 16 kHz (enough for voiceprints from existing recordings)."""
    if sr == SAMPLE_RATE or len(x) == 0:
        return x
    n = int(round(len(x) * SAMPLE_RATE / sr))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(x.dtype)


ENROLL_PROMPTS = [
    "The quick brown fox jumps over the lazy dog, and then it runs back home.",
    "Please move the deploy to six tomorrow and ping me on Slack when it's done.",
    "I'll send the invoice after lunch, so remind me on Thursday if I forget.",
    "Honestly the weather here has been really humid for the last few weeks.",
    "Open the settings page, turn on the dark theme, and save the changes.",
    "We should run the reconciliation job before the tax calculation step.",
    "My number ends in four seven two, and my email is on the website.",
    "Let's grab coffee at eleven and go over the plan for next quarter.",
]
