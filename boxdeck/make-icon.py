#!/usr/bin/env python3
"""Generate BoxDeck.icns — a cat "server keeper" mark.

Design: a rounded-square with a deep gradient, a stylised cat head silhouette,
glowing eyes, whiskers, and a live-chart line running through the face — tying the
cat to what the app actually does (watch the box). Drawn at 1024 and downsampled,
so every size stays crisp.
"""
import math
import os
import subprocess
import tempfile
from PIL import Image, ImageDraw, ImageFilter

S = 1024                      # master size
OUT = os.path.dirname(os.path.abspath(__file__))

BG_TOP = (36, 44, 78)         # deep indigo
BG_BOT = (14, 18, 34)
ACCENT = (94, 234, 212)       # teal — matches the app's VRAM series
ACCENT2 = (129, 140, 248)     # indigo-400
GREEN = (74, 222, 128)        # GPU series green


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], radius, fill=255)
    return m


def vertical_gradient(size, top, bottom):
    g = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / (size - 1)
        g.putpixel((0, y), tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3)))
    return g.resize((size, size))


def cat_head(d, cx, cy, w, h, fill, outline=None, width=0):
    """Head + ears as one silhouette."""
    # ears
    ear = w * 0.30
    d.polygon([(cx - w * 0.52, cy - h * 0.28), (cx - w * 0.44, cy - h * 0.92),
               (cx - w * 0.05, cy - h * 0.46)], fill=fill)
    d.polygon([(cx + w * 0.52, cy - h * 0.28), (cx + w * 0.44, cy - h * 0.92),
               (cx + w * 0.05, cy - h * 0.46)], fill=fill)
    # head
    d.ellipse([cx - w * 0.58, cy - h * 0.55, cx + w * 0.58, cy + h * 0.62], fill=fill)
    return ear


def build():
    img = Image.new("RGB", (S, S), BG_BOT)
    img.paste(vertical_gradient(S, BG_TOP, BG_BOT), (0, 0))
    d = ImageDraw.Draw(img, "RGBA")

    # soft radial glow behind the cat
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([S * 0.16, S * 0.14, S * 0.84, S * 0.82], fill=ACCENT + (60,))
    glow = glow.filter(ImageFilter.GaussianBlur(S * 0.09))
    img = Image.alpha_composite(img.convert("RGBA"), glow)
    d = ImageDraw.Draw(img, "RGBA")

    cx, cy = S * 0.5, S * 0.50
    w, h = S * 0.50, S * 0.48

    # subtle outer ring
    d.ellipse([cx - w * 0.92, cy - h * 1.02, cx + w * 0.92, cy + h * 1.06],
              outline=ACCENT2 + (70,), width=int(S * 0.008))

    # cat silhouette
    cat_head(d, cx, cy, w, h, fill=(245, 247, 255, 255))

    # inner ears
    d.polygon([(cx - w * 0.44, cy - h * 0.36), (cx - w * 0.39, cy - h * 0.76),
               (cx - w * 0.15, cy - h * 0.46)], fill=(255, 150, 180, 230))
    d.polygon([(cx + w * 0.44, cy - h * 0.36), (cx + w * 0.39, cy - h * 0.76),
               (cx + w * 0.15, cy - h * 0.46)], fill=(255, 150, 180, 230))

    # eyes — glowing, like status LEDs
    eye_y = cy - h * 0.10
    for sx in (-1, 1):
        ex = cx + sx * w * 0.24
        d.ellipse([ex - w * 0.115, eye_y - h * 0.135, ex + w * 0.115, eye_y + h * 0.135],
                  fill=(20, 26, 46, 255))
        d.ellipse([ex - w * 0.075, eye_y - h * 0.105, ex + w * 0.075, eye_y + h * 0.105],
                  fill=GREEN + (255,))
        d.ellipse([ex - w * 0.028, eye_y - h * 0.10, ex + w * 0.028, eye_y + h * 0.10],
                  fill=(16, 22, 38, 255))                       # slit pupil
        d.ellipse([ex - w * 0.055, eye_y - h * 0.085, ex - w * 0.018, eye_y - h * 0.03],
                  fill=(255, 255, 255, 190))                    # highlight

    # nose + mouth
    ny = cy + h * 0.14
    d.polygon([(cx - w * 0.055, ny), (cx + w * 0.055, ny), (cx, ny + h * 0.075)],
              fill=(255, 140, 170, 255))
    d.arc([cx - w * 0.20, ny + h * 0.03, cx + w * 0.005, ny + h * 0.26], 270, 20,
          fill=(90, 100, 130, 210), width=int(S * 0.011))
    d.arc([cx - w * 0.005, ny + h * 0.03, cx + w * 0.20, ny + h * 0.26], 160, 270,
          fill=(90, 100, 130, 210), width=int(S * 0.011))

    # whiskers
    for sx in (-1, 1):
        for k, dy in enumerate((-0.03, 0.03, 0.09)):
            x1 = cx + sx * w * 0.20
            x2 = cx + sx * w * 0.60
            y1 = ny + h * dy
            y2 = ny + h * (dy - 0.03 + k * 0.012)
            d.line([(x1, y1), (x2, y2)], fill=(230, 236, 255, 170),
                   width=int(S * 0.009))

    # live chart line across the lower face — the "watching" idea
    pts = [(0.14, 0.55), (0.24, 0.18), (0.33, 0.72), (0.44, 0.10),
           (0.55, 0.60), (0.66, 0.28), (0.78, 0.66), (0.88, 0.34)]
    base = cy + h * 1.02
    line = [(S * px, base + S * 0.085 * (py - 0.4)) for px, py in pts]
    d.line(line, fill=ACCENT + (235,), width=int(S * 0.020), joint="curve")
    lx, ly = line[-1]
    d.ellipse([lx - S * 0.022, ly - S * 0.022, lx + S * 0.022, ly + S * 0.022],
              fill=ACCENT + (255,))
    d.ellipse([lx - S * 0.040, ly - S * 0.040, lx + S * 0.040, ly + S * 0.040],
              fill=ACCENT + (70,))

    # round the corners like a macOS app icon
    icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    icon.paste(img, (0, 0), rounded_mask(S, int(S * 0.225)))
    return icon


def menubar_glyph(px):
    """Simplified cat silhouette for the menu bar. A detailed face turns to mush at
    ~18px, so this is a bold head + ears with knocked-out eyes. Drawn in black and
    marked as a TEMPLATE image so macOS tints it for light/dark menu bars."""
    S2 = px * 8                                   # supersample, then downscale
    img = Image.new("RGBA", (S2, S2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = S2 * 0.5, S2 * 0.56
    w, h = S2 * 0.78, S2 * 0.70
    black = (0, 0, 0, 255)
    # ears
    d.polygon([(cx - w * 0.46, cy - h * 0.18), (cx - w * 0.40, cy - h * 0.74),
               (cx - w * 0.04, cy - h * 0.36)], fill=black)
    d.polygon([(cx + w * 0.46, cy - h * 0.18), (cx + w * 0.40, cy - h * 0.74),
               (cx + w * 0.04, cy - h * 0.36)], fill=black)
    # head
    d.ellipse([cx - w * 0.50, cy - h * 0.42, cx + w * 0.50, cy + h * 0.46], fill=black)
    # eyes knocked out so the silhouette still reads as a cat
    for sx in (-1, 1):
        ex = cx + sx * w * 0.20
        d.ellipse([ex - w * 0.085, cy - h * 0.17, ex + w * 0.085, cy + h * 0.05],
                  fill=(0, 0, 0, 0))
    return img.resize((px, px), Image.LANCZOS)


def main():
    icon = build()
    for px in (18, 36):                           # 1x and 2x for the menu bar
        menubar_glyph(px).save(os.path.join(OUT, f"menubar{'' if px == 18 else '@2x'}.png"))
    png = os.path.join(OUT, "icon.png")
    icon.save(png)

    with tempfile.TemporaryDirectory() as tmp:
        iconset = os.path.join(tmp, "BoxDeck.iconset")
        os.makedirs(iconset)
        for size in (16, 32, 64, 128, 256, 512):
            icon.resize((size, size), Image.LANCZOS).save(
                os.path.join(iconset, f"icon_{size}x{size}.png"))
            icon.resize((size * 2, size * 2), Image.LANCZOS).save(
                os.path.join(iconset, f"icon_{size}x{size}@2x.png"))
        subprocess.run(["iconutil", "-c", "icns", iconset,
                        "-o", os.path.join(OUT, "BoxDeck.icns")], check=True)
    print("wrote icon.png and BoxDeck.icns")


if __name__ == "__main__":
    main()
