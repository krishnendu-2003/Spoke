"""Draw the app icon (the menu-bar waveform, white on a black rounded square) as .icns."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PIL import Image, ImageDraw  # noqa: E402

from spoke.tray import IDLE_HEIGHTS  # noqa: E402

SIZE = 1024
# macOS icon grid: the rounded square is ~824 px of the 1024 canvas, corner radius ~185.
BOX, RADIUS = 824, 185
HEIGHTS = [h * 1.45 for h in IDLE_HEIGHTS]  # a touch taller than the idle menu-bar frame


def draw() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    o = (SIZE - BOX) // 2
    d.rounded_rectangle((o, o, o + BOX, o + BOX), radius=RADIUS, fill=(18, 18, 20, 255))
    n = len(HEIGHTS)
    bar_w, gap = 74, 58
    x = (SIZE - (n * bar_w + (n - 1) * gap)) // 2
    mid = SIZE // 2
    for h in HEIGHTS:
        half = h * BOX * 0.62 / 2
        d.rounded_rectangle((x, mid - half, x + bar_w, mid + half), radius=bar_w // 2, fill=(255, 255, 255, 255))
        x += bar_w + gap
    return img


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "Spoke.icns")
    draw().save(out)
    print(f"wrote {out}")
