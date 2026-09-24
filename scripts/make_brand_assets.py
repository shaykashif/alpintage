"""Generates the dashboard's favicons and social share image from the heel
mark (a 3x3 grid with the top-left cell removed -- pterna, Greek: heel).

Writes to dashboard/assets/brand/:
  favicon.svg          scalable, follows the browser's light/dark theme
  favicon.ico          16/32/48 px, for older browsers and /favicon.ico
  favicon-32.png       PNG fallback
  apple-touch-icon.png 180 px, opaque (iOS home screen)
  icon-192.png, icon-512.png   web manifest icons
  og-image.png         1200x630 link-preview card (Open Graph / Twitter)

Output is committed, so this only needs re-running when the mark or copy
changes. Needs Pillow (not a runtime dependency):

    uv run --with pillow python scripts/make_brand_assets.py --font "path/to/Aktiv_Grotesk_Light.otf"

--font is the typeface for the share card's text; without it, a system
sans-serif is used.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "dashboard" / "assets" / "brand"

PAPER = (247, 246, 245)
INK = (23, 20, 16)
ACCENT = (34, 0, 255)

# Row-major 3x3 mark; top-left dropped (matches .brand-mark in index.html).
MARK = [
    [0, 0, 1],
    [0, 1, 0],
    [1, 0, 1],
]


def draw_mark(draw: ImageDraw.ImageDraw, x0: float, y0: float, size: float, color) -> None:
    """The mark in a size x size box: 5-unit cells, 1-unit gaps (17 units)."""
    unit = size / 17
    for r, row in enumerate(MARK):
        for c, on in enumerate(row):
            if on:
                x, y = x0 + c * 6 * unit, y0 + r * 6 * unit
                draw.rectangle([round(x), round(y), round(x + 5 * unit) - 1, round(y + 5 * unit) - 1], fill=color)


def icon(px: int, pad: float = 0.16, bg=PAPER) -> Image.Image:
    """Opaque square icon: ink mark on paper, drawn at 8x then downsampled."""
    s = px * 8
    img = Image.new("RGB", (s, s), bg)
    inner = s * (1 - 2 * pad)
    draw_mark(ImageDraw.Draw(img), s * pad, s * pad, inner, INK)
    return img.resize((px, px), Image.LANCZOS)


def svg() -> str:
    cells = "".join(
        f'<rect x="{c * 6}" y="{r * 6}" width="5" height="5"/>'
        for r, row in enumerate(MARK) for c, on in enumerate(row) if on
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="-2 -2 21 21">'
        "<style>rect{fill:#171410}@media (prefers-color-scheme:dark){rect{fill:#EDEAE4}}</style>"
        f"{cells}</svg>\n"
    )


def load_font(path: str | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in [path, "C:/Windows/Fonts/segoeuil.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf"]:
        if candidate and Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def og_image(font_path: str | None) -> Image.Image:
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)

    # Pointillist halftone field (same interference field as the hero), right-weighted.
    # Kept inside the frame, and clear of the copy on the left.
    for y in range(40, H - 39, 10):
        for x in range(870, W - 39, 10):
            a = math.sin(x * 0.012) * math.cos(y * 0.018)
            b = math.sin((x + y) * 0.007 + math.sin(y * 0.01) * 1.4)
            field = max(0.0, min(1.0, 0.5 + 0.32 * a + 0.28 * b))
            shade = min(1.0, (x - 860) / 160)
            r = 2.4 * (0.18 + 0.82 * field) * shade / 2
            if r >= 0.25:
                d.rectangle([x - r, y - r, x + r, y + r], fill=(160, 158, 154))

    # Hairline frame, like the dashboard's ledger panels.
    d.rectangle([28, 28, W - 29, H - 29], outline=(216, 213, 208), width=2)

    draw_mark(d, 84, 84, 44, INK)
    d.text((146, 84), "Pternas", font=load_font(font_path, 40), fill=INK)

    headline = load_font(font_path, 78)
    d.text((80, 250), "No points for", font=headline, fill=INK)
    d.text((80, 340), "second place.", font=headline, fill=ACCENT)
    d.text((84, 480), "Arbitrage across thousands of cultural and economic events.",
           font=load_font(font_path, 28), fill=(96, 92, 86))
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--font", help="TTF/OTF for the share card text")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "favicon.svg").write_text(svg(), encoding="utf-8")
    base = icon(256, pad=0.12)
    base.save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    icon(32, pad=0.12).save(OUT / "favicon-32.png")
    icon(180).save(OUT / "apple-touch-icon.png")
    icon(192).save(OUT / "icon-192.png")
    icon(512).save(OUT / "icon-512.png")
    og_image(args.font).save(OUT / "og-image.png", optimize=True)
    for f in sorted(OUT.iterdir()):
        print(f"  {f.name:22} {f.stat().st_size:>8,} bytes")


if __name__ == "__main__":
    main()
