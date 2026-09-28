#!/usr/bin/env python3
"""Icon tool for dockerctl plugin.

Two modes:

    python3 scripts/make_icon.py                  # draw the original whale icon
    python3 scripts/make_icon.py path/to/whale.png # frame YOUR image into the icon
                                                   # (resize + warm bg + accent
                                                   # circle; the artwork itself is
                                                   # never color-modified)

Output: src/ui/icon.png (300x300).
"""
import os
import sys
from PIL import Image, ImageDraw

S = 4     # supersample factor
K = 1.02  # scale about the whale's bbox center

BG_TOP = (255, 252, 250)
BG_BOT = (255, 242, 231)
ACCENT = (255, 240, 227)
ORANGE = (255, 105, 0)      # #ff6900
WHITE = (255, 255, 255)

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src", "ui", "icon.png")


def sc(x, y):
    return (((x - 152) * K + 150) * S, ((y - 122) * K + 150) * S)


def bez(p0, p1, p2, p3, n=44):
    pts = []
    for i in range(1, n + 1):
        t = i / n
        mt = 1 - t
        x = mt**3*p0[0] + 3*mt*mt*t*p1[0] + 3*mt*t*t*p2[0] + t**3*p3[0]
        y = mt**3*p0[1] + 3*mt*mt*t*p1[1] + 3*mt*t*t*p2[1] + t**3*p3[1]
        pts.append(sc(x, y))
    return pts


def background():
    """Warm gradient + accent circle at supersampled size, returned at 300x300."""
    img = Image.new("RGB", (W := 300 * S, W))
    d = ImageDraw.Draw(img)
    for y in range(W):
        t = y / (W - 1)
        d.line([(0, y), (W, y)], fill=tuple(
            int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3)))
    d.ellipse([32 * S, 32 * S, 268 * S, 268 * S], fill=ACCENT)
    return img.resize((300, 300), Image.LANCZOS)


def draw_whale():
    img = background()
    big = Image.new("RGBA", (300 * S, 300 * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)

    # ---- whale silhouette (clockwise from top of nose) ----
    outline = []
    outline += bez((52, 102), (100, 92), (165, 94), (208, 108))    # back
    outline += bez((208, 108), (224, 96), (234, 64), (244, 36))   # tail inner edge
    outline += [sc(258, 68), sc(290, 42)]                          # fluke notch (V opens up)
    outline += bez((290, 42), (294, 88), (262, 140), (218, 172))  # tail outer edge
    outline += bez((218, 172), (180, 204), (110, 208), (58, 192)) # belly
    outline += bez((58, 192), (28, 188), (16, 172), (15, 150))    # nose (flat face)
    outline += bez((15, 150), (15, 122), (28, 106), (52, 102))    # head top
    d.polygon(outline, fill=ORANGE + (255,))

    d.ellipse(sc(62, 126) + sc(80, 146), fill=WHITE + (255,))      # eye

    img = Image.alpha_composite(img.convert("RGBA"),
                                big.resize((300, 300), Image.LANCZOS))
    return img.convert("RGB")


def frame_image(src):
    """Fit a user-provided image onto the icon canvas. No color changes."""
    mark = Image.open(src).convert("RGBA")
    mark.thumbnail((236, 236), Image.LANCZOS)
    img = background().convert("RGBA")
    img.paste(mark, ((300 - mark.width) // 2, (300 - mark.height) // 2), mark)
    return img.convert("RGB")


def main():
    if len(sys.argv) > 1:
        src = sys.argv[1]
        if not os.path.isfile(src):
            print("input not found:", src)
            sys.exit(1)
        img = frame_image(src)
        print("framed", src)
    else:
        img = draw_whale()
        print("drew original whale")
    img.save(OUT, "PNG")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
