import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the real ~/.spoke or keychain from tests."""
    monkeypatch.setenv("SPOKE_HOME", str(tmp_path / "spoke-home"))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    return tmp_path / "spoke-home"
