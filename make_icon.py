"""Generate claude_widget.ico — Claude and Grok marks over two usage bars.

The starburst path is Claude Desktop's `AppLogo`. The Grok mark is the
committed PNG from the grok-usage-widget repo, sitting next to this one.
Run once after editing; the .ico and .png are what the widget loads.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw
from svgelements import Path as SvgPath

BAR_COLOR = (76, 175, 80, 255)        # green (matches widget BAR_LOW)
BG_COLOR  = (0, 0, 0, 0)              # transparent

# Claude starburst — extracted from Claude Desktop's app.asar. ViewBox is 248×248.
CLAUDE_STARBURST_PATH = (
    "M52.4285 162.873L98.7844 136.879L99.5485 134.602L98.7844 133.334H96.4921L88.7237 132.862L62.2346 132.153"
    "L39.3113 131.207L17.0249 130.026L11.4214 128.844L6.2 121.873L6.7094 118.447L11.4214 115.257L18.171 115.847"
    "L33.0711 116.911L55.485 118.447L71.6586 119.392L95.728 121.873H99.5485L100.058 120.337L98.7844 119.392"
    "L97.7656 118.447L74.5877 102.732L49.4995 86.1905L36.3823 76.62L29.3779 71.7757L25.8121 67.2858L24.2839 57.3608"
    "L30.6515 50.2716L39.3113 50.8623L41.4763 51.4531L50.2636 58.1879L68.9842 72.7209L93.4357 90.6804"
    "L97.0015 93.6343L98.4374 92.6652L98.6571 91.9801L97.0015 89.2625L83.757 65.2772L69.621 40.8192"
    "L63.2534 30.6579L61.5978 24.632C60.9565 22.1032 60.579 20.0111 60.579 17.4246L67.8381 7.49965"
    "L71.9133 6.19995L81.7193 7.49965L85.7946 11.0443L91.9074 24.9865L101.714 46.8451L116.996 76.62"
    "L121.453 85.4816L123.873 93.6343L124.764 96.1155H126.292V94.6976L127.566 77.9197L129.858 57.3608"
    "L132.15 30.8942L132.915 23.4505L136.608 14.4708L143.994 9.62643L149.725 12.344L154.437 19.0788"
    "L153.8 23.4505L150.998 41.6463L145.522 70.1215L141.957 89.2625H143.994L146.414 86.7813L156.093 74.0206"
    "L172.266 53.698L179.398 45.6635L187.803 36.802L193.152 32.5484H203.34L210.726 43.6549L207.415 55.1159"
    "L196.972 68.3492L188.312 79.5739L175.896 96.2095L168.191 109.585L168.882 110.689L170.738 110.53"
    "L198.755 104.504L213.91 101.787L231.994 98.7149L240.144 102.496L241.036 106.395L237.852 114.311"
    "L218.495 119.037L195.826 123.645L162.07 131.592L161.696 131.893L162.137 132.547L177.36 133.925"
    "L183.855 134.279H199.774L229.447 136.524L237.215 141.605L241.8 147.867L241.036 152.711L229.065 158.737"
    "L213.019 154.956L175.45 145.977L162.587 142.787H160.805V143.85L171.502 154.366L191.242 172.089"
    "L215.82 195.011L217.094 200.682L213.91 205.172L210.599 204.699L188.949 188.394L180.544 181.069"
    "L161.696 165.118H160.422V166.772L164.752 173.152L187.803 207.771L188.949 218.405L187.294 221.832"
    "L181.308 223.959L174.813 222.777L161.187 203.754L147.305 182.486L136.098 163.345L134.745 164.2"
    "L128.075 235.42L125.019 239.082L117.887 241.8L111.902 237.31L108.718 229.984L111.902 215.452"
    "L115.722 196.547L118.779 181.541L121.58 162.873L123.291 156.636L123.14 156.219L121.773 156.449"
    "L107.699 175.752L86.304 204.699L69.3663 222.777L65.291 224.431L58.2867 220.768L58.9235 214.27"
    "L62.8713 208.48L86.304 178.705L100.44 160.155L109.551 149.507L109.462 147.967L108.959 147.924"
    "L46.6977 188.512L35.6182 189.93L30.7788 185.44L31.4156 178.115L33.7079 175.752L52.4285 162.873Z"
)
CLAUDE_STARBURST_FILL = "#D97757"

OUT = Path(__file__).resolve().parent / "claude_widget.ico"
SIZES = [256, 128, 64, 48, 32, 16]


def rasterize_path(path_d: str, fill: str, size: int, viewbox: int = 248) -> Image.Image:
    """Rasterize an SVG path (M/L/H/V/C/Z commands) to a square RGBA image."""
    from svgelements import CubicBezier, Line

    path = SvgPath(path_d)
    scale = size / viewbox
    pts: list[tuple[float, float]] = []
    for seg in path:
        if isinstance(seg, Line):
            pts.append((seg.end.real * scale, seg.end.imag * scale))
        elif isinstance(seg, CubicBezier):
            # Flatten cubic with 8 steps; this path only has one curve.
            for i in range(1, 9):
                p = seg.point(i / 8.0)
                pts.append((p.real * scale, p.imag * scale))
        elif hasattr(seg, "end") and seg.end is not None:
            pts.append((seg.end.real * scale, seg.end.imag * scale))

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(img).polygon(pts, fill=fill)
    return img


def _load_grok_mark(size: int) -> Image.Image:
    """Light-gray Grok mark. Near-black pixels are treated as background."""
    src = Path(__file__).resolve().parent.parent / "grok-usage-widget" / "grok_mark.png"
    im = Image.open(src).convert("RGBA")
    px = im.load()
    for y in range(im.height):
        for x in range(im.width):
            r, g, b, a = px[x, y]
            if a < 20 or (r < 40 and g < 40 and b < 40):
                px[x, y] = (0, 0, 0, 0)
            else:
                px[x, y] = (232, 232, 232, a)
    bbox = im.split()[-1].getbbox()
    if bbox:
        im = im.crop(bbox)
    im.thumbnail((size, size), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(im, ((size - im.width) // 2, (size - im.height) // 2))
    return canvas


def draw_icon(size: int) -> Image.Image:
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), BG_COLOR)

    # Top: Claude starburst and Grok mark, side by side
    mark = int(s * 0.40)
    gap = int(s * 0.06)
    pair = mark * 2 + gap
    left = (s - pair) // 2
    top = int(s * 0.06)
    star = rasterize_path(CLAUDE_STARBURST_PATH, CLAUDE_STARBURST_FILL, mark)
    grok = _load_grok_mark(mark)
    img.alpha_composite(star, (left, top))
    img.alpha_composite(grok, (left + mark + gap, top))

    # Bottom 1/3: two full-width bars (first biggest, second halfway)
    d = ImageDraw.Draw(img)
    bar_h      = max(4 * scale, s // 9)
    gap        = max(2 * scale, s // 16)
    bar_widths = [0.50, 1.00]   # smaller on top, biggest on bottom
    total      = 2 * bar_h + gap
    start_y    = int(s * 0.86 - total / 2)

    for i, frac in enumerate(bar_widths):
        y     = start_y + i * (bar_h + gap)
        right = s * frac
        d.rounded_rectangle([0, y, right, y + bar_h], radius=bar_h // 2,
                            fill=BAR_COLOR)

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
