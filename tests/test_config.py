import tomllib

import pytest

from spoke import config as c


def test_first_run_creates_commented_config_that_round_trips(isolated_home):
    cfg = c.load()
    path = isolated_home / "config.toml"
    assert path.exists()
    text = path.read_text()
    assert text.count("#") > 20  # commented
    assert not any("api" in k or "secret" in k for k in tomllib.loads(text))  # key never in config
    assert "gsk_" not in text
    # the generated file must parse back to exactly the defaults
    assert cfg == c.Config()


def test_defaults_match_spec():
    cfg = c.Config()
    assert cfg.hotkey == "auto"
    assert cfg.mode == "hold"
    assert cfg.language == "en"
    assert cfg.stt_backend == "groq"
    assert cfg.stt_model == "whisper-large-v3-turbo"
    assert cfg.cleanup is True
    assert cfg.max_seconds == 300
    assert cfg.min_seconds == 0.3
    assert cfg.history is True
    assert cfg.debug_save_audio is False
    assert cfg.trailing_space is True
    for term in ["Lumeo", "TigerBeetle", "Soroban", "Stellar", "NestJS", "Prisma", "Next.js",
                 "FastAPI", "Turborepo", "pnpm", "BullMQ", "Supabase", "Setu", "FIRA", "ITR-4",
                 "ERI", "Krishnendu", "Kolkata"]:
        assert term in cfg.vocab
    assert cfg.replacements["tiger beetle"] == "TigerBeetle"
    assert "thanks for watching" in cfg.hallucination_blocklist


def test_regex_blocklist_entries_survive_toml_escaping(isolated_home):
    c.load()
    data = tomllib.loads((isolated_home / "config.toml").read_text())
    assert data["hallucination_blocklist"] == c.SEED_BLOCKLIST


def test_user_overrides_and_unknown_keys(isolated_home):
    isolated_home.mkdir(parents=True)
    (isolated_home / "config.toml").write_text(
        'mode = "toggle"\nmax_seconds = 60\nbogus = 1\n[replacements]\n"Foo Bar" = "FooBar"\n'
    )
    cfg = c.load()
    assert cfg.mode == "toggle"
    assert cfg.max_seconds == 60.0 and isinstance(cfg.max_seconds, float)
    assert cfg.replacements == {"foo bar": "FooBar"}  # keys lowercased
    assert cfg.language == "en"  # untouched default


def test_invalid_values_rejected(isolated_home):
    isolated_home.mkdir(parents=True)
    (isolated_home / "config.toml").write_text('mode = "sometimes"\n')
    with pytest.raises(ValueError):
        c.load()
    (isolated_home / "config.toml").write_text('cleanup = "yes"\n')
    with pytest.raises(TypeError):
        c.load()


def test_local_only_forces_local_and_disables_cleanup():
    cfg = c.Config(local_only=True, stt_backend="groq", cleanup=True)
    assert cfg.effective_stt_backend == "local"
    assert cfg.effective_cleanup is False


def test_api_key_env_fallback_when_keychain_unavailable(monkeypatch):
    import keyring

    def boom(*a, **k):
        raise RuntimeError("no backend")

    monkeypatch.setattr(keyring, "get_password", boom)
    assert c.get_api_key() == (None, "missing")
    monkeypatch.setenv("GROQ_API_KEY", " gsk_test \n")
    assert c.get_api_key() == ("gsk_test", "env")


def test_api_key_prefers_keychain(monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda s, u: "from-keychain")
    monkeypatch.setenv("GROQ_API_KEY", "from-env")
    assert c.get_api_key() == ("from-keychain", "keychain")
