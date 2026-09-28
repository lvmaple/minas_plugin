#!/usr/bin/env python3
"""生成 pcbackup 插件图标（300×300 PNG，橙底白文件夹+上传箭头）。

纯标准库实现：3 倍超采样抗锯齿。重新生成：python tools/make_icon.py
"""
import os
import struct
import zlib

SIZE = 300          # 输出尺寸
SS = 3              # 超采样倍数
N = SIZE * SS       # 渲染画布尺寸


def inside_rounded_rect(x, y, x0, y0, x1, y1, r):
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def inside_triangle(px, py, ax, ay, bx, by, cx, cy):
    def sign(x1, y1, x2, y2, x3, y3):
        return (x1 - x3) * (y2 - y3) - (x2 - x3) * (y1 - y3)
    d1 = sign(px, py, ax, ay, bx, by)
    d2 = sign(px, py, bx, by, cx, cy)
    d3 = sign(px, py, cx, cy, ax, ay)
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


def coverage(x, y):
    """像素 (x, y)（画布坐标，浮点）的颜色：RGBA"""
    # 背景：橙色渐变圆角方块
    if not inside_rounded_rect(x, y, 0, 0, N, N, 60 * SS):
        return (0, 0, 0, 0)
    t = y / N
    bg = (
        int(255 - (255 - 242) * t),
        int(105 + (92 - 105) * t),
        int(0 + (10 - 0) * t),
    )
    # 白色文件夹（主体 + 顶部标签页）
    folder = (
        inside_rounded_rect(x, y, 48 * SS, 110 * SS, 252 * SS, 240 * SS, 24 * SS)
        or inside_rounded_rect(x, y, 48 * SS, 62 * SS, 148 * SS, 128 * SS, 24 * SS)
    )
    if folder:
        # 掏空：上传箭头（↑ = 三角头 + 短杆），透出橙色背景
        arrow = (
            inside_triangle(x, y, 150 * SS, 128 * SS, 110 * SS, 186 * SS, 190 * SS, 186 * SS)
            or inside_rounded_rect(x, y, 136 * SS, 178 * SS, 164 * SS, 218 * SS, 8 * SS)
        )
        if arrow:
            return bg + (255,)
        return (255, 255, 255, 255)
    return bg + (255,)


def render():
    rows = []
    step = 1.0 / SS
    half = step / 2
    for py in range(SIZE):
        row = bytearray([0])  # filter type 0
        for px in range(SIZE):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    x = px * SS + sx + half
                    y = py * SS + sy + half
                    cr, cg, cb, ca = coverage(x, y)
                    r += cr; g += cg; b += cb; a += ca
            k = SS * SS
            row += bytes((r // k, g // k, b // k, a // k))
        rows.append(bytes(row))
    return b"".join(rows)


def write_png(path, raw):
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def main():
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "ui", "icon.png")
    out = os.path.normpath(out)
    write_png(out, render())
    print("written:", out, os.path.getsize(out), "bytes")


if __name__ == "__main__":
    main()
