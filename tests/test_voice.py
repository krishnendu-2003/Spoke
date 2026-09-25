import io
import os
import sys

import numpy as np
import pytest

from spoke import config as config_mod
from spoke import daemon, voice
from spoke.config import Config
from spoke.history import History
from spoke.recorder import Recorder, Recording

SR = 16000
ME, OTHER = 200.0, 330.0


# --- fakes: a "speaker" is a tone frequency, the VAD is an energy gate --------------------

class FakeVad:
    FRAME = 320

    def __init__(self):
        self.pos = 0
        self.start = None
        self.quiet = 0
        self.rest = np.zeros(0, np.float32)

    def accept(self, x):
        buf = np.concatenate([self.rest, x])
        out = []
        n = len(buf) - len(buf) % self.FRAME
        for i in range(0, n, self.FRAME):
            loud = np.sqrt(np.mean(buf[i : i + self.FRAME] ** 2)) > 0.002
            if loud:
                if self.start is None:
                    self.start = self.pos
                self.quiet = 0
            elif self.start is not None:
                self.quiet += 1
                if self.quiet * self.FRAME >= 0.25 * SR:
                    end = self.pos - (self.quiet - 1) * self.FRAME
                    out.append((self.start, end - self.start))
                    self.start, self.quiet = None, 0
            self.pos += self.FRAME
        self.rest = buf[n:]
        return out

    def flush(self):
        self.pos += len(self.rest)
        self.rest = np.zeros(0, np.float32)
        if self.start is None:
            return []
        end = self.pos - self.quiet * self.FRAME
        seg = [(self.start, end - self.start)]
        self.start = None
        return seg


class FakeDenoiser:
    def run(self, x):
        return x

    def flush(self):
        return np.zeros(0, np.float32)


class FakeEngine:
    dim = 8

    def __init__(self, fail=False):
        self.fail = fail

    def new_vad(self):
        return FakeVad()

    def new_denoiser(self):
        if self.fail:
            raise RuntimeError("model exploded")
        return FakeDenoiser()

    def embed(self, x):
        v = np.zeros(self.dim, np.float32)
        if len(x):
            spec = np.abs(np.fft.rfft(x))
            freq = np.argmax(spec) * SR / len(x)
            v[int(freq // 50) % self.dim] = 1.0
        return v


def tone(freq, seconds, amp=0.3):
    t = np.arange(int(seconds * SR)) / SR
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.float32)


def silence(seconds):
    return np.zeros(int(seconds * SR), np.float32)


def i16(x):
    return (np.concatenate(x) * 32767).astype(np.int16)


def me_profile(engine=FakeEngine()):
    return voice.Profile(embeddings=[engine.embed(tone(ME, 1.0)).tolist()], threshold=0.5)


def run(samples, centroid, threshold=0.5, engine=None, send_denoised=True):
    s = voice.VoiceSession(engine or FakeEngine(), centroid, threshold, send_denoised)
    voice.feed_all(s, samples)
    return s.finish()


def dominant(samples):
    x = samples.astype(np.float32)
    return np.argmax(np.abs(np.fft.rfft(x))) * SR / len(x)


# --- session ----------------------------------------------------------------------------

def test_my_speech_is_kept():
    audio = i16([silence(0.5), tone(ME, 3.0), silence(0.5)])
    r = run(audio, me_profile().centroid)
    assert r.status == "ok"
    assert r.pieces and all(p.kept for p in r.pieces)
    assert 3.0 <= r.kept_s <= 3.0 + 2 * voice.PAD_SECONDS + 0.05
    assert abs(dominant(r.samples) - ME) < 5


def test_other_voices_before_and_after_are_cut():
    audio = i16([tone(OTHER, 2.0), silence(0.5), tone(ME, 3.0), silence(0.5), tone(OTHER, 2.0)])
    r = run(audio, me_profile().centroid)
    assert r.status == "ok"
    assert [p.kept for p in r.pieces].count(False) >= 2
    # only my tone survives, including the padding (which never reaches into their speech)
    assert abs(dominant(r.samples) - ME) < 5
    assert r.kept_s < 3.0 + 2 * voice.PAD_SECONDS + 0.05
    spec = np.abs(np.fft.rfft(r.samples.astype(np.float32)))
    other_bin = int(OTHER * len(r.samples) / SR)
    assert spec[other_bin - 3 : other_bin + 4].max() < spec.max() * 0.05


def test_only_other_people_talking_sends_nothing():
    r = run(i16([tone(OTHER, 3.0)]), me_profile().centroid)
    assert r.status == "not-you" and r.samples.size == 0


def test_silence_is_no_speech():
    r = run(i16([silence(2.0)]), me_profile().centroid)
    assert r.status == "no-speech" and r.samples.size == 0


def test_noise_suppression_only_sends_everything():
    audio = i16([tone(OTHER, 1.0), silence(1.0), tone(ME, 1.0)])
    r = run(audio, None)
    assert r.status == "ok" and len(r.samples) == len(audio)


def test_send_raw_when_noise_suppression_off():
    class Halver(FakeDenoiser):
        def run(self, x):
            return x * 0.5

    class E(FakeEngine):
        def new_denoiser(self):
            return Halver()

    audio = i16([tone(ME, 2.0)])
    raw = run(audio, None, engine=E(), send_denoised=False)
    den = run(audio, None, engine=E(), send_denoised=True)
    assert np.abs(raw.samples).max() > 1.8 * np.abs(den.samples).max()


def test_model_failure_is_an_error_not_a_crash():
    r = run(i16([tone(ME, 1.0)]), me_profile().centroid, engine=FakeEngine(fail=True))
    assert r.status == "error"


def test_engine_can_be_a_loader_callable():
    calls = []

    def load():
        calls.append(1)
        return FakeEngine()

    r = run(i16([tone(ME, 2.0)]), me_profile().centroid, engine=load)
    assert r.status == "ok" and calls == [1]


# --- decision rule ------------------------------------------------------------------------

def pieces(*scores):
    return [voice.Piece(i, i + 1, s) for i, s in enumerate(scores)]


def test_decide_keeps_pieces_near_my_best():
    ps = pieces(0.82, 0.75, 0.35, 0.66)
    voice.decide(ps, threshold=0.5)
    assert [p.kept for p in ps] == [True, True, False, True]


def test_decide_noisy_but_mine_is_kept():
    # babble drags all of my scores down together; none of them is far from my best
    ps = pieces(0.55, 0.48, 0.41)
    voice.decide(ps, threshold=0.5)
    assert all(p.kept for p in ps)


def test_decide_nothing_reaches_threshold():
    ps = pieces(0.45, 0.40)
    voice.decide(ps, threshold=0.5)
    assert not any(p.kept for p in ps)


def test_decide_unscored_pieces_untouched():
    ps = [voice.Piece(0, 1, None)]
    voice.decide(ps, 0.5)
    assert ps[0].kept


def test_split_segment_pieces():
    assert voice.split_segment(0, 8000) == [(0, 8000)]
    parts = voice.split_segment(100, 5 * SR)
    assert parts[0][0] == 100 and parts[-1][1] == 100 + 5 * SR
    assert all(abs((b - a) - 5 * SR / len(parts)) <= 1 for a, b in parts)
    assert all(1.0 * SR <= b - a <= 2.0 * SR for a, b in parts)


def test_context_widens_short_pieces():
    audio = np.arange(5 * SR, dtype=np.float32)
    assert len(voice.context(audio, 2 * SR, 2 * SR + 1000)) == SR
    assert len(voice.context(audio, 0, 1000)) == SR // 2 + 1000 // 2  # clipped at the start
    assert len(voice.context(audio, SR, 3 * SR)) == 2 * SR


def test_calibrate_threshold_bounds():
    assert voice.calibrate_threshold([]) == voice.DEFAULT_THRESHOLD
    assert voice.calibrate_threshold([0.99] * 20) == voice.THRESHOLD_MAX
    assert voice.calibrate_threshold([0.3] * 20) == voice.THRESHOLD_MIN
    mid = voice.calibrate_threshold(list(np.linspace(0.7, 0.9, 20)))
    assert voice.THRESHOLD_MIN < mid < voice.THRESHOLD_MAX


# --- enrollment & voiceprint --------------------------------------------------------------

def test_enroll_builds_profile_and_add_extends_it():
    eng = FakeEngine()
    clips = [voice.analyse_clip(eng, i16([silence(0.3), tone(ME, 3.0), silence(0.3)])) for _ in range(3)]
    assert all(c.speech_s > 2.5 for c in clips)
    prof = voice.build_profile(eng, clips)
    assert len(prof.embeddings) == 3 and prof.self_scores and min(prof.self_scores) > 0.99
    assert prof.threshold == voice.THRESHOLD_MAX
    more = voice.build_profile(eng, clips[:1], base=prof)
    assert len(more.embeddings) == 4 and len(more.self_scores) > len(prof.self_scores)


def test_enroll_clip_without_speech():
    clip = voice.analyse_clip(FakeEngine(), i16([silence(2.0)]))
    assert clip.speech_s == 0 and clip.pieces == []


def test_profile_roundtrip_is_private(isolated_home):
    p = me_profile()
    path = p.save()
    loaded = voice.Profile.load()
    assert np.allclose(loaded.centroid, p.centroid)
    if sys.platform != "win32":
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_profile_from_another_model_is_rejected(isolated_home):
    p = me_profile()
    p.model = "some_other_model"
    p.save()
    with pytest.raises(ValueError, match="re-run"):
        voice.Profile.load()


def test_resample_length():
    x = np.zeros(48000, np.int16)
    assert len(voice.resample(x, 48000)) == 16000
    assert voice.resample(x, 16000) is x


# --- config -------------------------------------------------------------------------------

def test_set_values_keeps_comments_and_tables(isolated_home):
    path = config_mod.config_path()
    config_mod.load(path)
    config_mod.set_values({"voice_lock": True, "noise_suppression": True})
    text = path.read_text()
    assert "voice_lock = true" in text and "# true = only speech" in text
    cfg = config_mod.load(path)
    assert cfg.voice_lock and cfg.noise_suppression and cfg.voice_lock_threshold == 0.0


def test_set_values_adds_missing_key_above_tables(isolated_home):
    path = config_mod.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('mode = "hold"\n\n[replacements]\n"x" = "Y"\n')
    config_mod.set_values({"voice_lock": True}, path)
    cfg = config_mod.load(path)
    assert cfg.voice_lock and cfg.replacements == {"x": "Y"}
    assert path.read_text().index("voice_lock") < path.read_text().index("[replacements]")


def test_threshold_validation():
    assert Config(voice_lock_threshold=1.5).validate()
    assert not Config(voice_lock_threshold=0.6).validate()


# --- filter & pipeline --------------------------------------------------------------------

def test_filter_off_by_default_never_imports_models():
    f = voice.VoiceFilter(Config())
    assert not f.active and f.session() is None and f.problem == ""


def test_filter_voice_lock_without_enrollment(isolated_home):
    f = voice.VoiceFilter(Config(voice_lock=True), engine=FakeEngine())
    assert not f.lock and not f.active and "enroll" in f.problem


def test_filter_threshold_override():
    prof = me_profile()
    prof.threshold = 0.52
    assert voice.VoiceFilter(Config(voice_lock=True), FakeEngine(), prof).threshold == 0.52
    assert voice.VoiceFilter(Config(voice_lock=True, voice_lock_threshold=0.61), FakeEngine(), prof).threshold == 0.61


class CapturingGroq:
    def __init__(self):
        self.audio = []

    def warm(self):
        pass

    def transcribe(self, audio, **kw):
        import soundfile as sf

        self.audio.append(sf.read(io.BytesIO(audio), dtype="int16")[0])
        return "hello there"

    def chat(self, **kw):
        return None


class FakeInjector:
    injecting = False

    def __init__(self):
        self.injected = []

    def inject(self, text, app_id=None):
        self.injected.append(text)
        return "clipboard"


def make_pipeline(tmp_path, monkeypatch, engine=None):
    monkeypatch.setattr(daemon, "active_app", lambda: "editor")
    cfg = Config(voice_lock=True, noise_suppression=True, cleanup=False)
    vf = voice.VoiceFilter(cfg, engine=engine or FakeEngine(), profile=me_profile())
    groq, inj = CapturingGroq(), FakeInjector()
    return daemon.Pipeline(cfg, groq, inj, History(tmp_path / "h.jsonl"), voice=vf), groq, inj


def test_pipeline_sends_only_my_voice(tmp_path, monkeypatch):
    p, groq, inj = make_pipeline(tmp_path, monkeypatch)
    rec = Recording(i16([tone(OTHER, 2.0), silence(0.5), tone(ME, 3.0), silence(0.5), tone(OTHER, 2.0)]))
    assert p.process(rec, released_at=0) == "hello there"
    sent = groq.audio[0]
    assert len(sent) < len(rec.samples) / 2 and abs(dominant(sent) - ME) < 5
    assert p.history.recent(1)[0]["voice"] == "ok"


def test_pipeline_streaming_session(tmp_path, monkeypatch):
    p, groq, inj = make_pipeline(tmp_path, monkeypatch)
    audio = i16([tone(ME, 2.0)])
    s = p.voice.session()
    voice.feed_all(s, audio)
    assert p.process(Recording(audio), released_at=0, session=s) == "hello there"
    assert len(groq.audio) == 1


def test_pipeline_not_me_is_never_sent(tmp_path, monkeypatch):
    p, groq, inj = make_pipeline(tmp_path, monkeypatch)
    assert p.process(Recording(i16([tone(OTHER, 3.0)])), released_at=0) is None
    assert groq.audio == [] and inj.injected == []


def test_pipeline_filter_failure_falls_back_to_unfiltered(tmp_path, monkeypatch):
    p, groq, inj = make_pipeline(tmp_path, monkeypatch, engine=FakeEngine(fail=True))
    rec = Recording(i16([tone(OTHER, 2.0)]))
    assert p.process(rec, released_at=0) == "hello there"
    assert len(groq.audio[0]) == len(rec.samples)


def test_recorder_feeds_sink_while_recording():
    got = []
    r = Recorder()
    r._open_stream = lambda: None
    r.start(sink=got.append)
    block = np.ones((320, 1), np.int16)
    r._callback(block, 320, None, None)
    rec = r.stop()
    r._callback(block, 320, None, None)  # after stop: ignored
    assert len(got) == 1 and len(rec.samples) == 320


# --- real models (only where they're downloaded; `spoke enroll --download-only`) ---------

@pytest.mark.skipif(
    not os.environ.get("SPOKE_VOICE_MODELS") or bool(voice.missing_models()),
    reason="set SPOKE_VOICE_MODELS to a folder with the downloaded models",
)
def test_real_engine_smoke():
    pytest.importorskip("sherpa_onnx")
    eng = voice.Engine()
    rng = np.random.default_rng(0)
    noise = (rng.standard_normal(SR * 3) * 200).astype(np.int16)
    r = run(noise, voice._unit(np.ones(eng.dim, np.float32)), engine=eng)
    assert r.status in ("no-speech", "not-you", "ok")
    e = eng.embed(tone(ME, 1.5))
    assert e.shape == (eng.dim,) and abs(np.linalg.norm(e) - 1) < 1e-3


def test_quiet_mic_is_not_dropped_by_rms_gate_when_filter_on(tmp_path, monkeypatch):
    p, groq, inj = make_pipeline(tmp_path, monkeypatch)
    quiet = Recording(i16([tone(ME, 2.0, amp=0.005)]))  # RMS ~116, under the default 150 gate
    assert quiet.rms < p.cfg.silence_rms_threshold
    assert p.process(quiet, released_at=0) == "hello there"
