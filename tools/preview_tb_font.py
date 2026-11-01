"""对比「外框缩放」和「字号缩放」拆开前后的实际效果。

三行 1:1（屏幕上多大就是多大）+ 两行 2 倍放大（看笔画是否发虚）。

用法：python tools/preview_tb_font.py → assets/_dbg_tb_font.png
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from ctypes import byref, c_void_p

import snap
from snap import user32, GFX

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "_dbg_tb_font.png")
BG = (72, 78, 88)
PAD = 16
CAP = 20                    # 每行上方的文字标注高度
GAP = 10
ZOOM = 2                    # 第二段用 2 倍放大

# (标注, 外框缩放, 字号缩放)
ROWS = [
    ("A  外框0.86  字0.86（上一版）", 0.86, 0.86),
    ("B  外框0.86  字0.92（现在）", 0.86, 0.92),
    ("C  外框1.00  字1.00（原设计）", 1.00, 1.00),
]
ZOOM_ROWS = [0, 1]


def to_bgra(raw, W, H, bg=BG):
    """预乘 BGRA → 叠到中灰底上的 BGRA"""
    out = bytearray(W * H * 4)
    for i in range(W * H):
        o = i * 4
        a = raw[o + 3]
        inv = 255 - a
        out[o] = min(255, raw[o] + bg[2] * inv // 255)
        out[o + 1] = min(255, raw[o + 1] + bg[1] * inv // 255)
        out[o + 2] = min(255, raw[o + 2] + bg[0] * inv // 255)
        out[o + 3] = 255
    return bytes(out)


app = snap.App()
app.create_main()
user32.SetWindowPos(app.hwnd_main, None, 40, 40, 0, 0, 0x0001 | 0x0004 | 0x0010)
app._build_capture_buffers()
vsx, vsy, vsw, vsh = app.vs
app.sel = (vsx + 500, vsy + 300, 700, 420)

tiles = []
for tag, sc, fs in ROWS:
    snap.TB_SCALE, snap.TB_FONT_SCALE = sc, fs
    app._close_tool()
    app._refresh_tool()
    app._show_tool()
    W, H = app.tb_size
    raw = ctypes.string_at(app.tb_bits, W * H * 4)
    tiles.append((tag, W, H, to_bgra(raw, W, H), app.tb_font(12),
                  app.tb(snap.TB_ICON_SIZE), app.tb(snap.TB_BTN_H)))
    print("%-28s %dx%d  字号 %dpx  图标 %dpx" % (tag, W, H, app.tb_font(12),
                                              app.tb(snap.TB_ICON_SIZE)))
app._close_tool()

# 画布：上半 1:1 三行，下半 2 倍放大两行
zoom_w = max(t[1] for t in tiles) * ZOOM
col_w = max(max(t[1] for t in tiles), zoom_w)
CW = col_w + PAD * 2
CH = PAD
for t in tiles:
    CH += CAP + 4 + t[2] + GAP
CH += 14                                    # 分段留白
for i in ZOOM_ROWS:
    CH += CAP + 4 + tiles[i][2] * ZOOM + GAP
CH += PAD

sdc = app._get_screen_dc()
dc, bmp, old, bits = snap._new_dib(sdc, CW, CH)
g = GFX(dc)
g.fill(0xFF58504C, 0, 0, CW, CH)
g.close()

canvas = bytearray(CW * CH * 4)
for i in range(CW * CH):
    canvas[i * 4] = 0x4C
    canvas[i * 4 + 1] = 0x50
    canvas[i * 4 + 2] = 0x58
    canvas[i * 4 + 3] = 255

g = GFX(dc)
y = PAD
for tag, W, H, rgba, fs, isz, bh in tiles:
    g.text(tag, 14, 0xFFFFFFFF, PAD, y, 460, CAP, 0, True)
    g.text("整条 %d×%d   字号 %dpx   图标 %dpx   按钮高 %dpx"
           % (W, H, fs, isz, bh), 14, 0xFFC9D2DE, PAD + 470, y, 400, CAP, 0)
    for row in range(H):
        for xx in range(W):
            s = (row * W + xx) * 4
            d = ((y + CAP + 4 + row) * CW + PAD + xx) * 4
            canvas[d:d + 3] = rgba[s:s + 3]
    y += CAP + 4 + H + GAP

y += 14
for i in ZOOM_ROWS:
    tag, W, H, rgba, fs, isz, bh = tiles[i]
    g.text(tag + "   ×2 放大", 14, 0xFFFFFFFF, PAD, y, 460, CAP, 0, True)
    for row in range(H * ZOOM):
        sy = row // ZOOM
        for xx in range(W * ZOOM):
            s = (sy * W + xx // ZOOM) * 4
            d = ((y + CAP + 4 + row) * CW + PAD + xx) * 4
            canvas[d:d + 3] = rgba[s:s + 3]
    y += CAP + 4 + H * ZOOM + GAP
g.close()

hold = ctypes.create_string_buffer(bytes(canvas), len(canvas))
img = snap.buf_to_image(hold, CW, CH)
snap.gdip.GdipSaveImageToFile(img, OUT, byref(snap.PNG_CLSID), None)
snap.gdip.GdipDisposeImage(img)
snap._free_dib((dc, bmp, old, bits))
user32.DestroyWindow(app.hwnd_main)
print("→", OUT, CW, "x", CH)
