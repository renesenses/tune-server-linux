"""Generate the DMG background with Mozaik Labs branding.

600x400 PNG with a teal→cyan gradient, brand stripe, and a soft drop-target
indicator pointing from the .app to the Applications symlink.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


W, H = 600, 400
BRAND_TEAL = (38, 166, 154)    # #26a69a
BRAND_CYAN = (0, 188, 212)     # #00bcd4
BRAND_GREEN = (124, 179, 66)   # #7cb342
DARK_BG = (15, 22, 28)


def lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def main() -> None:
    out = Path(__file__).resolve().parent / "dmg_background.png"

    # Solid-ish dark background
    img = Image.new("RGB", (W, H), DARK_BG)
    px = img.load()

    # Soft diagonal gradient from top-left teal glow to bottom-right cyan glow
    for y in range(H):
        for x in range(W):
            tx = x / W
            ty = y / H
            d_tl = ((tx - 0.0) ** 2 + (ty - 0.0) ** 2) ** 0.5  # 0..~1.4
            d_br = ((tx - 1.0) ** 2 + (ty - 1.0) ** 2) ** 0.5
            tl_intensity = max(0.0, 1.0 - d_tl * 1.4)
            br_intensity = max(0.0, 1.0 - d_br * 1.4)
            r = DARK_BG[0] + int(BRAND_TEAL[0] * 0.18 * tl_intensity + BRAND_CYAN[0] * 0.18 * br_intensity)
            g = DARK_BG[1] + int(BRAND_TEAL[1] * 0.18 * tl_intensity + BRAND_CYAN[1] * 0.18 * br_intensity)
            b = DARK_BG[2] + int(BRAND_TEAL[2] * 0.18 * tl_intensity + BRAND_CYAN[2] * 0.18 * br_intensity)
            px[x, y] = (min(r, 255), min(g, 255), min(b, 255))

    draw = ImageDraw.Draw(img, "RGBA")

    # Brand stripe at the top — teal to cyan to green
    stripe_h = 4
    for x in range(W):
        t = x / W
        if t < 0.5:
            c = lerp(BRAND_TEAL, BRAND_CYAN, t * 2)
        else:
            c = lerp(BRAND_CYAN, BRAND_GREEN, (t - 0.5) * 2)
        draw.line([(x, 0), (x, stripe_h)], fill=c)

    # Title
    try:
        title_font = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 28)
        sub_font = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 14)
    except Exception:
        title_font = ImageFont.load_default()
        sub_font = ImageFont.load_default()

    draw.text((30, 30), "Tune Server", fill=(255, 255, 255), font=title_font)
    draw.text((30, 65), "by Mozaik Labs · Multi-room music server", fill=(180, 200, 210), font=sub_font)

    # Soft arrow from the .app icon position (≈ x=150, y=215) to the
    # Applications symlink position (≈ x=450, y=215). Icons are 128px,
    # arrow drawn between them with rounded ends. Positions match the
    # AppleScript layout in the workflow.
    arrow_y = 215
    start_x, end_x = 230, 370
    # Glow halo behind the arrow
    halo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hd = ImageDraw.Draw(halo)
    hd.rectangle([(start_x, arrow_y - 14), (end_x, arrow_y + 14)], fill=(38, 166, 154, 60))
    halo = halo.filter(ImageFilter.GaussianBlur(8))
    img.paste(halo, (0, 0), halo)
    # Arrow shaft
    draw.line([(start_x, arrow_y), (end_x - 14, arrow_y)], fill=(38, 166, 154, 220), width=4)
    # Arrowhead
    draw.polygon(
        [(end_x, arrow_y), (end_x - 16, arrow_y - 9), (end_x - 16, arrow_y + 9)],
        fill=(38, 166, 154, 240),
    )

    # Footer
    draw.text(
        (30, H - 38),
        "Glissez Tune Server vers Applications pour installer.",
        fill=(160, 180, 195), font=sub_font,
    )

    img.save(out, "PNG")
    print(f"wrote {out} {img.size}")


if __name__ == "__main__":
    main()
