import plistlib
import sys
from pathlib import Path

from spoke import app, config, instance


def test_bundle_path_only_when_frozen(tmp_path):
    exe = tmp_path / "Spoke.app" / "Contents" / "MacOS" / "Spoke"
    exe.parent.mkdir(parents=True)
    exe.touch()
    assert app.bundle_path(str(exe), frozen=True) == (tmp_path / "Spoke.app").resolve()
    assert app.bundle_path(str(exe), frozen=False) is None
    assert app.bundle_path(sys.executable, frozen=True) is None  # a plain python isn't a bundle


def test_clean_argv_drops_finder_psn():
    assert app.clean_argv(["-psn_0_12345"]) == []
    assert app.clean_argv(["doctor", "-psn_0_1"]) == ["doctor"]


def test_login_plist_opens_the_app_through_launchservices():
    d = plistlib.loads(app.login_plist(Path("/Applications/Spoke.app")))
    assert d["Label"] == app.LOGIN_LABEL
    assert d["ProgramArguments"] == ["/usr/bin/open", "-a", "/Applications/Spoke.app"]
    assert d["RunAtLoad"] is True
    assert "KeepAlive" not in d  # quitting from the menu must stay quit


def test_set_login_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert not app.login_enabled()
    app.set_login(True, Path("/Applications/Spoke.app"))
    assert app.login_enabled()
    app.set_login(False, None)
    assert not app.login_enabled()


def test_relaunch_command():
    cmd = app.relaunch_command(Path("/Applications/Spoke.app"))
    assert cmd[-1] == "/Applications/Spoke.app" and "open -n" in cmd[2]
    assert app.relaunch_command(None)[1:] == ["-m", "spoke.app"]


def test_hotkey_label():
    assert app.hotkey_label("auto").startswith("Right Option")
    assert app.hotkey_label("f13") == "F13"


def test_spoke_cmd_in_bundle(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/Applications/Spoke.app/Contents/MacOS/Spoke")
    assert config.spoke_cmd("enroll") == "/Applications/Spoke.app/Contents/MacOS/Spoke enroll"


def test_single_instance_lock():
    first = instance.acquire()
    assert first is not None
    try:
        if sys.platform != "win32":
            assert instance.acquire() is None
            assert instance.holder_pid() is not None
    finally:
        first.close()
    again = instance.acquire()
    assert again is not None
    again.close()


def test_main_off_macos_points_at_cli(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    assert app.main([]) == 2
    assert "macOS only" in capsys.readouterr().err


def test_main_passes_commands_to_cli(monkeypatch):
    seen = []
    import spoke.cli

    monkeypatch.setattr(spoke.cli, "main", lambda argv: seen.append(argv) or 0)
    assert app.main(["history", "-n", "1"]) == 0
    assert seen == [["history", "-n", "1"]]
