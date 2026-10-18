#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成应用图标：assets/icon.png（1024px，Pillow 绘制），再导出 .icns（macOS）和 .ico（Windows）。

macOS 的 .icns 用系统 iconutil 打包（仅需 Pillow 生成各尺寸 PNG）。
"""
import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent.parent / "assets"
SIZES = [16, 32, 64, 128, 256, 512, 1024]


def draw_icon(px):
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 圆角深色底
    pad = px * 0.06
    d.rounded_rectangle([pad, pad, px - pad, px - pad], radius=px * 0.22,
                        fill=(10, 14, 21, 255), outline=(40, 52, 70, 255), width=max(2, px // 128))
    cx, cy = px / 2, px * 0.54
    col = (94, 234, 212, 255)      # teal
    col2 = (122, 162, 255, 255)    # blue accent
    lw = max(2, int(px * 0.028))
    # 8 条腿
    for s in (-1, 1):
        for k in range(4):
            base = math.radians(35 + k * 30)
            x0 = cx + s * px * 0.06
            y0 = cy + (k - 1.5) * px * 0.045
            x1 = x0 + s * math.cos(base) * px * 0.20
            y1 = y0 - math.sin(base) * px * 0.13
            x2 = x1 + s * math.cos(base) * px * 0.10
            y2 = y1 + math.sin(base) * px * 0.16
            d.line([x0, y0, x1, y1, x2, y2], fill=col, width=lw, joint="curve")
    # 身体
    r = px * 0.075
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col2)                      # 胸
    d.ellipse([cx - px * 0.105, cy + px * 0.06, cx + px * 0.105, cy + px * 0.27], fill=col)  # 腹
    d.ellipse([cx - px * 0.05, cy - px * 0.16, cx + px * 0.05, cy - px * 0.06], fill=col)     # 头
    # 眼睛
    er = max(2, px * 0.014)
    for s in (-1, 1):
        d.ellipse([cx + s * px * 0.028 - er, cy - px * 0.125 - er,
                   cx + s * px * 0.028 + er, cy - px * 0.125 + er], fill=(8, 11, 17, 255))
    return img


def main():
    ASSETS.mkdir(exist_ok=True)
    draw_icon(1024).save(ASSETS / "icon.png")
    # macOS .icns
    iconset = ASSETS / "icon.iconset"
    iconset.mkdir(exist_ok=True)
    for s in SIZES:
        draw_icon(s).save(iconset / ("icon_%dx%d.png" % (s, s)))
        if s <= 512:
            draw_icon(s * 2).save(iconset / ("icon_%dx%d@2x.png" % (s, s)))
    subprocess.run(["iconutil", "-c", "icns", "-o", str(ASSETS / "icon.icns"), str(iconset)], check=True)
    # Windows .ico
    draw_icon(256).save(ASSETS / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    for f in iconset.iterdir():
        f.unlink()
    iconset.rmdir()
    print("icons written to", ASSETS)


if __name__ == "__main__":
    main()
