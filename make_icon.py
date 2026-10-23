"""Generate claude_widget.ico — the split-bars tray icon, frozen at half usage.

Five vertical usage bars (two and a half filled) sit on a reset bar that is
80% along. Run once after editing; the .ico and .png are what the widget loads.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BAR_COLOR  = "#4caf50"   # widget BAR_LOW
TRACK      = "#333333"   # widget BAR_BG
TIME_COLOR = "#999999"   # widget TIME_BAR_FG

SEGMENTS = 5
USAGE    = 0.5
RESET    = 0.8
GAP      = 0.06           # widget TRAY_BAR_GAP
TIME_H   = 0.16           # widget TRAY_TIME_H

OUT = Path(__file__).resolve().parent / "claude_widget.ico"
SIZES = [256, 128, 64, 48, 32, 16]


def draw_icon(size: int) -> Image.Image:
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    gap = s * GAP
    w = (s - gap * (SEGMENTS - 1)) / SEGMENTS
    r = int(w * 0.25)

    th = round(s * TIME_H)
    ty = s - th
    d.rounded_rectangle((0, ty, s - 1, s - 1), radius=r, fill=TRACK,
                        corners=(False, False, True, True))
    d.rounded_rectangle((0, ty, round((s - 1) * RESET), s - 1), radius=r, fill=TIME_COLOR,
                        corners=(False, False, False, True))

    bottom = ty - 1
    corners = (True, True, False, False)
    filled = USAGE * SEGMENTS
    for i in range(SEGMENTS):
        x0 = round(i * (w + gap))
        x1 = round(x0 + w) - 1
        d.rounded_rectangle((x0, 0, x1, bottom), radius=r, fill=TRACK, corners=corners)
        part = min(max(filled - i, 0.0), 1.0)
        if part > 0:
            top = round(bottom * (1 - part))
            d.rounded_rectangle((x0, top, x1, bottom), radius=min(r, (bottom - top) // 2),
                                fill=BAR_COLOR, corners=corners)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    images = [draw_icon(s) for s in SIZES]
    images[0].save(
        OUT, format="ICO",
        sizes=[(s, s) for s in SIZES],
        append_images=images[1:],
    )
    images[0].save(OUT.with_suffix(".png"))
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
