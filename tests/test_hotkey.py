from spoke.hotkey import CANCEL, START, STOP, HotkeyMachine


def test_hold_basic():
    m = HotkeyMachine("hold")
    assert m.key_down(0.0) == START
    assert m.key_up(1.0) == STOP


def test_hold_ignores_key_repeat():
    m = HotkeyMachine("hold")
    assert m.key_down(0.0) == START
    assert [m.key_down(0.05 * i) for i in range(1, 20)] == [None] * 19
    assert m.key_up(2.0) == STOP


def test_hold_chord_cancels():
    m = HotkeyMachine("hold")
    assert m.key_down(0.0) == START
    assert m.other_key(0.1) == CANCEL  # Right Ctrl + C
    assert m.key_up(0.2) is None
    assert m.key_down(1.0) == START  # next hold works normally


def test_debounce_bounce_after_release():
    m = HotkeyMachine("hold", debounce_s=0.03)
    m.key_down(0.0)
    assert m.key_up(1.0) == STOP
    assert m.key_down(1.01) is None  # bounce
    assert m.key_up(1.02) is None
    assert m.key_down(1.5) == START


def test_reset_after_auto_stop():
    m = HotkeyMachine("hold")
    m.key_down(0.0)
    m.reset()  # max_seconds hit
    assert m.key_up(400.0) is None


def test_toggle_double_tap_starts_single_tap_stops():
    m = HotkeyMachine("toggle", double_tap_s=0.4)
    assert m.key_down(0.0) is None
    assert m.key_up(0.1) is None
    assert m.key_down(0.3) == START  # second tap starts immediately on key-down
    assert m.key_up(0.4) is None
    assert m.key_down(30.0) == STOP
    assert m.key_up(30.1) is None
    assert m.recording is False


def test_toggle_slow_taps_do_not_start():
    m = HotkeyMachine("toggle", double_tap_s=0.4)
    m.key_down(0.0)
    m.key_up(0.1)
    assert m.key_down(1.0) is None


def test_toggle_long_press_is_not_a_tap():
    m = HotkeyMachine("toggle", double_tap_s=0.4, tap_max_s=0.35)
    m.key_down(0.0)
    m.key_up(1.0)  # held a second: not a tap
    assert m.key_down(1.2) is None


def test_toggle_chord_is_not_a_tap():
    m = HotkeyMachine("toggle")
    m.key_down(0.0)
    m.other_key(0.05)
    m.key_up(0.1)
    assert m.key_down(0.2) is None


def test_toggle_other_keys_while_recording_do_not_cancel():
    m = HotkeyMachine("toggle")
    m.key_down(0.0); m.key_up(0.1); m.key_down(0.2); m.key_up(0.3)
    assert m.recording
    assert m.other_key(1.0) is None  # hotkey not held
    assert m.recording
