"""工具条整体缩放对比图：同一台机器上渲染 4 档，1:1 摆在一起看大小。

用法：python tools/preview_tb_scale.py → assets/_dbg_tb_scale.png

每行一个档位：工具条 1:1 原样，下面标出缩放比例、实际像素尺寸、占 1920 屏宽的比例。
看中哪档，把 snap.py 里的 TB_SCALE 改成那个值就行。
"""
import sys
import os
import ctypes

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
OUT = os.path.join(ROOT, "assets", "_dbg_tb_scale.png")
SCREEN_W = 1920
PAD = 14
CAP_H = 20                 # 每行上方的标注高度
ROW_GAP = 16

# 每档 = 一组要临时改掉的常量。前两档不动字号图标，只把按钮里多余的空白
# 压掉；后两档是整体等比缩小（连字一起变小）。
CASES = (
    ("现在",       dict(TB_SCALE=1.00)),
    ("按钮 68",    dict(TB_SCALE=1.00, TB_BTN_W=68)),
    ("按钮 64",    dict(TB_SCALE=1.00, TB_BTN_W=64)),
    ("等比 0.92",  dict(TB_SCALE=0.92)),
    ("等比 0.86",  dict(TB_SCALE=0.86)),
)
SAVED = {k: getattr(snap, k) for k in ("TB_SCALE", "TB_BTN_W")}

# ---------- 1. 按各档出图 ----------
tiles = []
for tag, override in CASES:
    for k, v in SAVED.items():          # 每档先回到基准，否则上一档的改动会带过来
        setattr(snap, k, v)
    for k, v in override.items():
        setattr(snap, k, v)
    app = snap.App()
    app.create_main()
    user32.SetWindowPos(app.hwnd_main, None, 40, 40, 0, 0, 0x0001 | 0x0004 | 0x0010)
    app._build_capture_buffers()
    vsx, vsy, vsw, vsh = app.vs
    app.sel = (vsx + 260, vsy + 220, min(820, vsw - 520), min(400, vsh - 440))
    app.mode = None
    app._refresh_tool()
    app._show_tool()
    if app.hwnd_tool:
        W, H = app.tb_size
        raw = ctypes.string_at(app.tb_bits, W * H * 4)
        tiles.append((tag, W, H, raw, app.tb(12), app.tb(snap.TB_ICON_SIZE)))
        print("%-10s %d×%d  字号 %dpx  图标 %dpx" % (tag, W, H, app.tb(12), app.tb(snap.TB_ICON_SIZE)))
    else:
        print("%s 工具条创建失败" % tag)
    app._close_tool()
    user32.DestroyWindow(app.hwnd_main)

for k, v in SAVED.items():
    setattr(snap, k, v)

# ---------- 2. 拼图 ----------
TW = max(t[1] for t in tiles) + PAD * 2
TH = PAD + len(tiles) * (CAP_H + 4 + 48 + ROW_GAP)

app0 = snap.App()
app0.create_main()
app0._build_capture_buffers()
sdc = app0._get_screen_dc()
dc, bmp, old, bits = snap._new_dib(sdc, TW, TH)
g = GFX(dc)
g.fill(0xFF34383E, 0, 0, TW, TH)

pos = []
y = PAD
for tag, W, H, raw, fs, isz in tiles:
    g.text(tag, 15, 0xFFFFFFFF, PAD, y, 80, CAP_H, 0, True)
    g.text("%d × %d    字号 %dpx    占 1920 屏宽 %.0f%%"
           % (W, H, fs, 100.0 * W / SCREEN_W),
           12, 0xFFC9D2DE, PAD + 82, y, 340, CAP_H, 0)
    pos.append((PAD, y + CAP_H + 4, W, H, raw))
    y += CAP_H + 4 + 48 + ROW_GAP
g.close()

# 读回画布，把预乘 BGRA 的工具条按 over 混合贴上
canvas = bytearray(ctypes.string_at(bits, TW * TH * 4))
for ox, oy, W, H, raw in pos:
    for row in range(H):
        base = (oy + row) * TW
        for xx in range(W):
            s = (row * W + xx) * 4
            a = raw[s + 3]
            if a == 0:
                continue
            d = (base + ox + xx) * 4
            inv = 255 - a
            canvas[d] = min(255, raw[s] + canvas[d] * inv // 255)
            canvas[d + 1] = min(255, raw[s + 1] + canvas[d + 1] * inv // 255)
            canvas[d + 2] = min(255, raw[s + 2] + canvas[d + 2] * inv // 255)
snap._free_dib((dc, bmp, old, bits))

hold = ctypes.create_string_buffer(bytes(canvas), len(canvas))
img = snap.buf_to_image(hold, TW, TH)
snap.gdip.GdipSaveImageToFile(img, OUT, byref(snap.PNG_CLSID), None)
snap.gdip.GdipDisposeImage(img)
user32.DestroyWindow(app0.hwnd_main)
print("saved", OUT)
