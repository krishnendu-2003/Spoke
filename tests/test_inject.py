import pytest

from spoke.inject import ClipSnapshot, Continuation, Injector, pick_target
from spoke.platform_info import Platform


class FakeClipboard:
    def __init__(self, content="original", fail_snapshot=False, fail_restore=False, complete=True):
        self.content = content
        self.seq = 0
        self.log = []
        self.fail_snapshot, self.fail_restore, self.complete = fail_snapshot, fail_restore, complete

    def marker(self):
        return self.seq

    def snapshot(self):
        self.log.append("snapshot")
        if self.fail_snapshot:
            raise RuntimeError("weird clipboard")
        return ClipSnapshot(items=self.content, empty=self.content is None, complete=self.complete)

    def set_text(self, text):
        self.log.append(("set", text))
        self.content = text
        self.seq += 1

    def restore(self, snap):
        self.log.append("restore")
        if self.fail_restore:
            raise RuntimeError("can't")
        self.content = snap.items
        self.seq += 1


class FakeKeys:
    def __init__(self, cb=None, fail=False, user_copies=None):
        self.cb, self.fail, self.user_copies = cb, fail, user_copies
        self.pastes, self.typed = [], []

    def paste(self, app_id=None):
        self.pastes.append((app_id, self.cb.content if self.cb else None))
        if self.user_copies is not None:
            self.cb.content = self.user_copies
            self.cb.seq += 1
        if self.fail:
            raise RuntimeError("keystroke failed")

    def type_text(self, text):
        self.typed.append(text)


MAC = Platform("macos", "")


def make(cb, keys=None, **kw):
    keys = keys or FakeKeys(cb)
    sleeps = []
    inj = Injector(cb, keys, sleep=sleeps.append, plat=MAC, **kw)
    return inj, keys, sleeps


def test_save_set_paste_wait_restore_order():
    cb = FakeClipboard("original")
    inj, keys, sleeps = make(cb, restore_delay=0.15)
    assert inj.inject("hello world", "app") == "clipboard"
    assert keys.pastes == [("app", "hello world")]  # clipboard held our text at paste time
    assert cb.log == ["snapshot", ("set", "hello world"), "restore"]
    assert 0.15 in sleeps
    assert cb.content == "original"


def test_restores_empty_clipboard():
    cb = FakeClipboard(None)
    inj, _, _ = make(cb)
    inj.inject("x")
    assert cb.content is None


def test_snapshot_failure_still_pastes_and_does_not_crash():
    cb = FakeClipboard("img", fail_snapshot=True)
    inj, keys, _ = make(cb)
    inj.inject("hello")
    assert keys.pastes == [(None, "hello")]
    assert "restore" not in cb.log


def test_restore_failure_is_swallowed():
    cb = FakeClipboard("orig", fail_restore=True)
    inj, keys, _ = make(cb)
    inj.inject("hello")  # no exception
    assert keys.pastes


def test_partial_snapshot_still_restored():
    cb = FakeClipboard("text-part", complete=False)
    inj, _, _ = make(cb)
    inj.inject("hi")
    assert cb.content == "text-part"


def test_clipboard_restored_even_if_paste_keystroke_fails():
    cb = FakeClipboard("orig")
    inj, _, _ = make(cb, keys=FakeKeys(cb, fail=True))
    with pytest.raises(RuntimeError):
        inj.inject("hello")
    assert cb.content == "orig"
    assert inj.injecting is False


def test_does_not_clobber_something_user_copied_during_paste():
    cb = FakeClipboard("orig")
    inj, _, _ = make(cb, keys=FakeKeys(cb, user_copies="new copy"))
    inj.inject("hello")
    assert cb.content == "new copy"
    assert "restore" not in cb.log


def test_type_method_never_touches_clipboard():
    cb = FakeClipboard("orig")
    inj, keys, _ = make(cb, method="type")
    assert inj.inject("hello") == "type"
    assert keys.typed == ["hello"] and cb.log == []


def test_no_clipboard_backend_falls_back_to_typing():
    keys = FakeKeys()
    inj = Injector(None, keys, sleep=lambda s: None, plat=MAC)
    assert inj.inject("hello") == "type"
    assert keys.typed == ["hello"]


def test_empty_text_is_noop():
    cb = FakeClipboard("orig")
    inj, keys, _ = make(cb)
    assert inj.inject("") == "none"
    assert cb.log == [] and keys.pastes == []


def test_injecting_flag_set_during_paste():
    cb = FakeClipboard("orig")
    seen = []

    class K(FakeKeys):
        def paste(self, app_id=None):
            seen.append(inj.injecting)

    inj = Injector(cb, K(cb), sleep=lambda s: None, plat=MAC)
    inj.inject("x")
    assert seen == [True] and inj.injecting is False


def test_linux_settle_delay_default():
    inj = Injector(FakeClipboard(), FakeKeys(), plat=Platform("linux", "x11"), sleep=lambda s: None)
    assert inj.settle_delay > 0
    inj = Injector(FakeClipboard(), FakeKeys(), plat=MAC, sleep=lambda s: None)
    assert inj.settle_delay == 0


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_continuation_space_same_app_within_window():
    clk = Clock()
    c = Continuation(True, 30, clock=clk)
    assert c.prefix("First.", "code") == ""
    c.record("code")
    clk.t += 10
    assert c.prefix("Second.", "code") == " "
    assert c.prefix("Second.", "slack") == ""
    assert c.prefix(", and more", "code") == ""
    clk.t += 31
    assert c.prefix("Third.", "code") == ""


def test_continuation_disabled():
    c = Continuation(False, 30)
    c.record("a")
    assert c.prefix("x", "a") == ""


def test_pick_target_prefers_rich_then_text():
    assert pick_target(["TARGETS", "UTF8_STRING", "image/png"]) == "image/png"
    assert pick_target(["TARGETS", "STRING", "UTF8_STRING"]) == "UTF8_STRING"
    assert pick_target(["text/plain", "text/html"]) == "text/html"
    assert pick_target(["TARGETS", "TIMESTAMP"]) is None
    assert pick_target(["application/x-custom"]) == "application/x-custom"
