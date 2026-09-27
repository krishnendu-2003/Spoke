from spoke.recorder import block_level, smooth_level
from spoke.tray import IDLE_HEIGHTS, MIN_H, bar_heights, render

import numpy as np


def pixels(img):
    return [img.getpixel((x, y)) for y in range(img.height) for x in range(img.width)]


def test_idle_is_static():
    assert bar_heights("idle", 1.0, 0.0) == bar_heights("idle", 0.0, 5.0) == IDLE_HEIGHTS


def test_recording_swings_over_time_and_with_level():
    loud_a = bar_heights("recording", 1.0, 0.0)
    loud_b = bar_heights("recording", 1.0, 0.2)
    quiet = bar_heights("recording", 0.0, 0.0)
    assert loud_a != loud_b  # it moves
    assert sum(loud_a) > sum(quiet) * 2  # louder = taller
    assert all(MIN_H <= h <= 1.0 for h in loud_a + loud_b + quiet)
    assert loud_a[2] >= loud_a[0]  # middle bar dominant, like a voice waveform


def test_processing_wave_moves():
    assert bar_heights("processing", 0, 0.0) != bar_heights("processing", 0, 0.3)
    assert all(h <= MIN_H + 0.35 + 1e-9 for h in bar_heights("processing", 0, 1.0))


def _colors(img):
    return {px for px in pixels(img) if px[3] > 0}


def test_template_frame_is_pure_black_on_transparent():
    img = render(bar_heights("recording", 0.8, 0.1), size=36, template=True)
    assert img.size == (36, 36)
    assert {c[:3] for c in _colors(img)} <= {(0, 0, 0)}
    assert any(px[3] == 0 for px in pixels(img))  # has transparency for the tint


def test_non_template_frame_is_black_and_white_only():
    img = render(IDLE_HEIGHTS, size=44, template=False)
    colors = {c[:3] for c in _colors(img)}
    # antialiased edges aside, only black and white (greys are allowed only as blends)
    assert (0, 0, 0) in colors and (255, 255, 255) in colors
    assert all(r == g == b for r, g, b in colors)


def test_level_mapping():
    assert block_level(np.zeros(320, dtype=np.int16)) == 0.0
    speech = (np.sin(np.arange(320) / 3) * 3000).astype(np.int16)
    assert block_level(speech) > 0.7
    assert smooth_level(0.2, 0.9) == 0.9  # attack is instant
    assert 0.2 > smooth_level(0.2, 0.0) > 0.1  # release is gradual
