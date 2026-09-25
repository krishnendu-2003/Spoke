import numpy as np
import pytest

from spoke import daemon
from spoke.config import Config
from spoke.history import History
from spoke.recorder import Recording
from spoke.transcribe import TranscriptionError


class FakeGroq:
    def __init__(self, transcript="", cleaned=None, stt_exc=None):
        self.transcript, self.cleaned, self.stt_exc = transcript, cleaned, stt_exc
        self.chat_calls = 0

    def warm(self):
        pass

    def transcribe(self, wav, **kw):
        if self.stt_exc:
            raise self.stt_exc
        return self.transcript

    def chat(self, **kw):
        self.chat_calls += 1
        return self.cleaned


class FakeInjector:
    def __init__(self):
        self.injected = []
        self.injecting = False

    def inject(self, text, app_id=None):
        self.injected.append(text)
        return "clipboard"


def speech(seconds=2.0):
    n = int(seconds * 16000)
    return Recording((np.sin(np.arange(n) / 5) * 3000).astype(np.int16))


@pytest.fixture
def no_app(monkeypatch):
    monkeypatch.setattr(daemon, "active_app", lambda: "editor")


def make(tmp_path, groq, **cfg_kw):
    cfg = Config(**cfg_kw)
    inj = FakeInjector()
    p = daemon.Pipeline(cfg, groq, inj, History(tmp_path / "h.jsonl"))
    return p, inj


def test_full_path_cleanup_then_replacements(tmp_path, no_app):
    groq = FakeGroq("um we store balances in tiger beetle you know", "We store balances in tiger beetle.")
    p, inj = make(tmp_path, groq)
    out = p.process(speech(), released_at=0)
    assert inj.injected == ["We store balances in TigerBeetle."]
    assert out == "We store balances in TigerBeetle."
    entry = p.history.recent(1)[0]
    assert entry["raw"].startswith("um we store") and "latency" in entry
    assert "audio" not in entry


def test_hallucination_never_pasted(tmp_path, no_app):
    groq = FakeGroq("Thanks for watching!")
    p, inj = make(tmp_path, groq)
    assert p.process(speech(), 0) is None
    assert inj.injected == [] and groq.chat_calls == 0


def test_silence_never_sent(tmp_path, no_app):
    groq = FakeGroq("should not be called", stt_exc=AssertionError("called"))
    p, inj = make(tmp_path, groq)
    silent = Recording(np.zeros(32000, dtype=np.int16))
    assert p.process(silent, 0) is None
    assert inj.injected == []


def test_stt_error_is_never_pasted(tmp_path, no_app, monkeypatch):
    notes = []
    monkeypatch.setattr(daemon.notify, "notify", lambda *a, **k: notes.append(a))
    from spoke.groq_api import GroqError

    p, inj = make(tmp_path, FakeGroq(stt_exc=GroqError("HTTP 500 from /audio/transcriptions")))
    assert p.process(speech(), 0) is None
    assert inj.injected == []
    assert notes and "transcription failed" in notes[0][0]


def test_cleanup_failure_pastes_raw_with_replacements(tmp_path, no_app):
    class Broken(FakeGroq):
        def chat(self, **kw):
            raise TimeoutError()

    p, inj = make(tmp_path, Broken("deploy the next js app to vercel today please"), cleanup_mode="always")
    p.process(speech(), 0)
    assert inj.injected == ["deploy the Next.js app to vercel today please"]


def test_local_only_never_calls_cloud_cleanup(tmp_path, no_app, monkeypatch):
    groq = FakeGroq(cleaned="SHOULD NOT BE USED")

    class FakeLocal:
        def __init__(self, cfg):
            pass

        def warm(self):
            pass

        def transcribe(self, rec):
            return "this stays on my machine entirely"

    monkeypatch.setattr("spoke.transcribe.LocalTranscriber", FakeLocal)
    p, inj = make(tmp_path, groq, local_only=True)
    p.process(speech(), 0)
    assert inj.injected == ["this stays on my machine entirely"]
    assert groq.chat_calls == 0


def test_second_dictation_gets_separator_space(tmp_path, no_app):
    groq = FakeGroq("first sentence here", None)
    p, inj = make(tmp_path, groq)
    p.process(speech(), 0)
    groq.transcript = "second sentence here"
    p.process(speech(), 0)
    assert inj.injected == ["first sentence here", " second sentence here"]


def test_missing_key_for_groq_backend_errors_clearly():
    with pytest.raises(TranscriptionError, match="setup"):
        daemon.Pipeline(Config(), None, None, None)


def test_rejected_cleanup_model_is_replaced_for_next_utterance(tmp_path, no_app):
    from spoke.groq_api import GroqError

    class Picky(FakeGroq):
        def __init__(self):
            super().__init__("um so the deploy is at 5 no 6 tomorrow", None)
            self.models_used = []

        def list_models(self):
            return ["openai/gpt-oss-20b", "qwen/qwen3.8-27b", "whisper-large-v3-turbo"]

        def chat(self, *, model, **kw):
            self.models_used.append(model)
            if model == "llama-3.1-8b-instant":
                raise GroqError("HTTP 404 from /chat/completions: no such model", 404)
            return "So the deploy is at 6 tomorrow."

    groq = Picky()
    p, inj = make(tmp_path, groq, cleanup_model="llama-3.1-8b-instant")
    p.process(speech(), 0)
    p.process(speech(), 0)
    assert groq.models_used == ["llama-3.1-8b-instant", "openai/gpt-oss-20b"]
    assert inj.injected == ["um so the deploy is at 5 no 6 tomorrow", " So the deploy is at 6 tomorrow."]


def test_startup_resolution_picks_available_model(tmp_path, no_app):
    class G(FakeGroq):
        def list_models(self):
            return ["openai/gpt-oss-20b"]

    p, _ = make(tmp_path, G(), cleanup_model="auto")
    assert p.resolve_cleanup_model() == "openai/gpt-oss-20b"
