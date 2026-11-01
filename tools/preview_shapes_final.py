"""v1.3.0 效果图：真实渲染（不是示意图）。

上面两行 = 工具条在「方框」「圆形」模式下的真实像素；
下面一张 = 遮罩里画好方框/圆形后的真实画面（含选中手柄）。

用法：python tools/preview_shapes_final.py → assets/_dbg_shapes_final.png
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
from snap import user32, gdi32, GFX

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "_dbg_shapes_final.png")
PANEL_BG = (48, 52, 60)


def to_rgba(raw, W, H, bg=PANEL_BG):
    out = bytearray(W * H * 4)
    for i in range(W * H):
        o = i * 4
        a = raw[o + 3]
        if a == 0:
            out[o], out[o + 1], out[o + 2] = bg[2], bg[1], bg[0]
            out[o + 3] = 255
            continue
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

SEL_W, SEL_H = 860, 440
app.sel = (vsx + 300, vsy + 200, SEL_W, SEL_H)
sx, sy = app.sel[0], app.sel[1]

# ---------- 1. 真实工具条（两种模式）
tiles = []
for mode in ("rect", "ellipse"):
    app.mode = None
    app.drawing = False
    app._begin_mode(mode)
    app._show_tool()
    if not app.hwnd_tool:
        print("%s: 工具条创建失败" % mode)
        continue
    W, H = app.tb_size
    raw = ctypes.string_at(app.tb_bits, W * H * 4)
    tiles.append((mode, W, H, to_rgba(raw, W, H)))
    print("%-8s %dx%d" % (mode, W, H))
app._close_tool()

# ---------- 2. 画标注（走真实 shapes + 真实绘制路径）
app.mode = "rect"
app.shapes = [
    {"t": "rect", "x1": sx + 40, "y1": sy + 40, "x2": sx + 340, "y2": sy + 170,
     "color": 0, "w": app.px(snap.PEN_WIDTHS[0])},          # 红，细
    {"t": "ellipse", "x1": sx + 380, "y1": sy + 50, "x2": sx + 700, "y2": sy + 250,
     "color": 1, "w": app.px(snap.PEN_WIDTHS[1])},          # 蓝，中
    {"t": "rect", "x1": sx + 90, "y1": sy + 250, "x2": sx + 330, "y2": sy + 390,
     "color": 3, "w": app.px(snap.PEN_WIDTHS[2])},          # 黄，粗
]
# 选中第一个方框 → 露出四角手柄（这是真实交互状态）
app.sel_shape = app.shapes[0]

app._compose_overlay(vsw, vsh)

# 从 dc_back 里抠出选区那一块
pad = 30
CW, CH = SEL_W + pad * 2, SEL_H + pad * 2
shot = snap._new_dib(app._get_screen_dc(), CW, CH)
gdi32.BitBlt(shot[0], 0, 0, CW, CH, app.dc_back, sx - pad, sy - pad, snap.SRCCOPY)
panel_px = bytes(ctypes.string_at(shot[3], CW * CH * 4))

# ---------- 3. 拼图
tw = max(t[1] for t in tiles)
P = 14
TW = max(tw, CW) + P * 2
TH = P + sum(t[2] + 10 for t in tiles) + CH + P * 2

canvas = bytearray(TW * TH * 4)
for i in range(TW * TH):
    canvas[i * 4] = PANEL_BG[2]
    canvas[i * 4 + 1] = PANEL_BG[1]
    canvas[i * 4 + 2] = PANEL_BG[0]
    canvas[i * 4 + 3] = 255


def put(rgba, x0, y0, W, H, opaque=False):
    for row in range(H):
        s = row * W * 4
        d = ((y0 + row) * TW + x0) * 4
        canvas[d:d + W * 4] = rgba[s:s + W * 4]
        if opaque:
            for xx in range(W):
                canvas[d + xx * 4 + 3] = 255


y = P
for tag, W, H, rgba in tiles:
    put(rgba, P, y, W, H)
    y += H + 10
put(panel_px, P, y, CW, CH, opaque=True)

hold = ctypes.create_string_buffer(bytes(canvas), len(canvas))
img = snap.buf_to_image(hold, TW, TH)
snap.gdip.GdipSaveImageToFile(img, OUT, byref(snap.PNG_CLSID), None)
snap.gdip.GdipDisposeImage(img)
snap._free_dib(shot)
user32.DestroyWindow(app.hwnd_main)
print("saved", OUT, "%dx%d" % (TW, TH))
