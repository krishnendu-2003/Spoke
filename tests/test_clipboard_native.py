"""Round-trips the REAL OS clipboard backend. Opt-in (it clobbers your clipboard):
    SPOKE_NATIVE_TESTS=1 pytest tests/test_clipboard_native.py
CI runs it on macOS, Windows and Linux (under Xvfb)."""
import os
import time

import pytest

from spoke.inject import make_clipboard

pytestmark = pytest.mark.skipif(os.environ.get("SPOKE_NATIVE_TESTS") != "1", reason="set SPOKE_NATIVE_TESTS=1")


def _text(snap):
    """Pull the plain-text payload out of a backend-specific snapshot."""
    items = snap.items
    if isinstance(items, tuple):  # Linux CLI backends: (target, bytes)
        return items[1].decode()
    for it in items:
        if isinstance(it, dict):  # macOS: {uti: bytes}
            if "public.utf8-plain-text" in it:
                return it["public.utf8-plain-text"].decode()
        elif it[0] == 13:  # Windows CF_UNICODETEXT
            return it[1].decode("utf-16-le").rstrip("\0")
    return None


def test_set_text_and_restore_roundtrip():
    cb = make_clipboard()
    assert cb is not None, "no clipboard backend on this platform"
    cb.set_text("original contents ✓")
    time.sleep(0.1)
    before = cb.snapshot()
    assert _text(before) == "original contents ✓"

    cb.set_text("dictated text from Spoke")
    time.sleep(0.1)
    assert _text(cb.snapshot()) == "dictated text from Spoke"

    cb.restore(before)
    time.sleep(0.1)
    assert _text(cb.snapshot()) == "original contents ✓"


def test_marker_changes_on_write():
    cb = make_clipboard()
    m1 = cb.marker()
    cb.set_text("x")
    m2 = cb.marker()
    if m1 is not None:  # macOS / Windows expose a change counter; Linux doesn't
        assert m1 != m2
