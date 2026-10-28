"""把工具条「选项排」的最终像素渲染成图，肉眼看病在哪。

用法：python tools/preview_tool_opts.py  → assets/_dbg_tool_opts.png
四种状态横排：无模式 / 画笔 / 马赛克 / 箭头。
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
OUT = os.path.join(ROOT, "assets", "_dbg_tool_opts.png")
BG = (72, 78, 88)          # 中灰：既看得清浅色，也看得清深色


def to_rgba(raw, W, H, bg=BG):
    """预乘 BGRA → 叠到纯色底上的 RGBA，返回 bytes"""
    out = bytearray(W * H * 4)
    for i in range(W * H):
        o = i * 4
        a = raw[o + 3]
        if a == 0:
            out[o] = bg[2]
            out[o + 1] = bg[1]
            out[o + 2] = bg[0]
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
app.sel = (vsx + 400, vsy + 300, 700, 420)      # 一块足够大的选区，工具条能放下

states = [("none", None), ("pen", "pen"), ("mosaic", "mosaic"), ("arrow", "arrow")]
tiles = []
for tag, mode in states:
    app.mode = None
    app.drawing = False
    if mode:
        app._begin_mode(mode)
    else:
        app._refresh_tool()
    app._show_tool()
    if not app.hwnd_tool:
        print("%s: 工具条创建失败" % tag)
        continue
    W, H = app.tb_size
    raw = ctypes.string_at(app.tb_bits, W * H * 4)
    tiles.append((tag, W, H, to_rgba(raw, W, H)))
    print("%-6s  %dx%d   rects=%d" % (tag, W, H, len(app._tool_layout()[2])))

app._close_tool()

tw = max(t[1] for t in tiles)
th = sum(t[2] for t in tiles) + 10 * max(0, len(tiles) - 1)
pad = 8
TW, TH = tw + pad * 2, th + pad * 2

sdc = app._get_screen_dc()
dc, bmp, old, bits = snap._new_dib(sdc, TW, TH)
g = GFX(dc)
g.fill(0xFF404650, 0, 0, TW, TH)
g.close()

canvas = bytearray(TW * TH * 4)
for i in range(TW * TH):
    canvas[i * 4 + 3] = 255
    canvas[i * 4] = 0x50
    canvas[i * 4 + 1] = 0x46
    canvas[i * 4 + 2] = 0x40
y = pad
for tag, W, H, rgba in tiles:
    for row in range(H):
        src = row * W * 4
        dst = ((y + row) * TW + pad) * 4
        for xx in range(W):
            s = src + xx * 4
            d = dst + xx * 4
            canvas[d] = rgba[s + 2]
            canvas[d + 1] = rgba[s + 1]
            canvas[d + 2] = rgba[s]
    y += H + 10

hold = ctypes.create_string_buffer(bytes(canvas), len(canvas))
img = snap.buf_to_image(hold, TW, TH)
snap.gdip.GdipSaveImageToFile(img, OUT, byref(snap.PNG_CLSID), None)
snap.gdip.GdipDisposeImage(img)
snap._free_dib((dc, bmp, old, bits))
user32.DestroyWindow(app.hwnd_main)
print("saved", OUT)
