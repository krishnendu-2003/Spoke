import io
import json
import wave

import numpy as np

from spoke.history import History
from spoke.recorder import Recording, encode_wav, rejection_reason, rms


def test_rms():
    assert rms(np.zeros(100, dtype=np.int16)) == 0
    assert abs(rms(np.full(100, 1000, dtype=np.int16)) - 1000) < 1e-6
    assert rms(np.zeros(0, dtype=np.int16)) == 0


def test_wav_is_16k_mono_int16():
    data = encode_wav(np.arange(1600, dtype=np.int16))
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (16000, 1, 2, 1600)


def test_rejects_short_and_silent():
    short = Recording(np.full(int(0.2 * 16000), 3000, dtype=np.int16))
    silent = Recording(np.full(16000 * 2, 20, dtype=np.int16))
    speech = Recording((np.sin(np.arange(32000) / 5) * 3000).astype(np.int16))
    assert "too short" in rejection_reason(short, 0.3, 150)
    assert "silence" in rejection_reason(silent, 0.3, 150)
    assert rejection_reason(speech, 0.3, 150) is None


def test_history_append_recent_and_rotation(tmp_path):
    h = History(tmp_path / "history.jsonl", max_bytes=400)
    for i in range(20):
        h.append(text=f"line {i}", raw=f"raw {i}")
    assert (tmp_path / "history.jsonl.1").exists()
    recent = h.recent(3)
    assert [e["text"] for e in recent] == ["line 17", "line 18", "line 19"]
    for line in (tmp_path / "history.jsonl").read_text().splitlines():
        assert set(json.loads(line)) >= {"ts", "text"}


def test_history_disabled_writes_nothing(tmp_path):
    h = History(tmp_path / "h.jsonl", enabled=False)
    h.append(text="secret")
    assert not (tmp_path / "h.jsonl").exists()


def test_flac_upload_is_lossless_and_smaller():
    import soundfile as sf

    rng = np.random.default_rng(0)
    x = ((np.sin(np.arange(32000) / 7) * 2000) + rng.normal(0, 80, 32000)).astype(np.int16)
    data, name, mime = Recording(x).to_upload("flac")
    assert (name, mime) == ("audio.flac", "audio/flac")
    assert len(data) < len(Recording(x).to_wav()) * 0.8
    back, sr = sf.read(io.BytesIO(data), dtype="int16")
    assert sr == 16000 and np.array_equal(back, x)


def test_wav_upload_option():
    data, name, mime = Recording(np.zeros(1600, dtype=np.int16)).to_upload("wav")
    assert name == "audio.wav" and data[:4] == b"RIFF"
