"""预览「方框 / 圆形」标注工具的样子（临时脚本，不参与发布）。

做法：临时把 TB_ITEMS 换成 10 个按钮的版本，补上两个新图标，
然后走**真实的工具条渲染管线**输出像素；再抓一块真实桌面，
用真实的 GDI+ 画笔在它上面画方框和圆形，让你看线宽和三档的效果。

用法：python tools/preview_shape_tools.py → assets/_dbg_shape_tools.png
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

from ctypes import byref, c_void_p, c_float, c_int

import snap
from snap import user32, gdi32, GFX, P, gdip

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "_dbg_shape_tools.png")

# 补上绘制矩形路径需要的原型（正式实现时也会用到）
snap._proto(gdip.GdipAddPathRectangle, [P, c_float, c_float, c_float, c_float])

# ---------- 1. 临时扩充工具条：在箭头后面插两个按钮
snap.TB_ITEMS = (("reselect", "重选"), ("pen", "画笔"), ("mosaic", "马赛克"),
                 ("arrow", "箭头"), ("rect", "方框"), ("ellipse", "圆形"),
                 ("undo", "撤销"), ("copy", "复制"), ("save", "保存"),
                 ("cancel", "取消"))

_orig_icon = snap.App._draw_tool_icon


def _icon(self, g, kind, x, y, sz, color, bg=None):
    """方框 / 圆形图标：15×15 网格，线条语言跟现有图标一致"""
    if kind == "rect":
        lw = max(1.15, self.px(1.35))
        u = sz / 15.0

        def L(x1, y1, x2, y2):
            g.line(color, lw, x + x1 * u, y + y1 * u, x + x2 * u, y + y2 * u, 2)

        L(1.9, 3.4, 13.1, 3.4)
        L(13.1, 3.4, 13.1, 11.6)
        L(13.1, 11.6, 1.9, 11.6)
        L(1.9, 11.6, 1.9, 3.4)
        return
    if kind == "ellipse":
        lw = max(1.15, self.px(1.35))
        u = sz / 15.0
        pen = P()
        gdip.GdipCreatePen1(color, lw, 0, byref(pen))
        gdip.GdipDrawEllipse(g.g, pen, x + 1.7 * u, y + 3.4 * u,
                             11.6 * u, 8.2 * u)
        gdip.GdipDeletePen(pen)
        return
    return _orig_icon(self, g, kind, x, y, sz, color, bg)


snap.App._draw_tool_icon = _icon

# ---------- 2. 让两种新模式也展开选项排（颜色 + 线宽）
_orig_vis, _orig_color, _orig_pen = (snap.App._opt_visible, snap.App._color_opts,
                                     snap.App._pen_opts)
snap.App._opt_visible = lambda self: (self.mode in ("rect", "ellipse")
                                      or _orig_vis(self))
snap.App._color_opts = lambda self: (self.mode in ("rect", "ellipse")
                                     or _orig_color(self))
snap.App._pen_opts = lambda self: (self.mode in ("rect", "ellipse")
                                   or _orig_pen(self))


def to_rgba(raw, W, H, bg=(48, 52, 60)):
    """预乘 BGRA → 叠到深灰底上的 RGBA"""
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
app.sel = (vsx + 400, vsy + 300, 860, 420)

tiles = []
for mode in ("rect", "ellipse", None):
    app.mode = None
    app.drawing = False
    if mode:
        app._begin_mode(mode)
    else:
        app._refresh_tool()
    app._show_tool()
    if not app.hwnd_tool:
        print("%s: 工具条创建失败" % mode)
        continue
    W, H = app.tb_size
    raw = ctypes.string_at(app.tb_bits, W * H * 4)
    tiles.append((mode, W, H, to_rgba(raw, W, H)))
    print("%-8s %dx%d  rects=%d" % (mode, W, H, len(app._tool_layout()[2])))
app._close_tool()

# ---------- 3. 在真实桌面上画方框 / 圆形，看线宽效果
SW, SH = 860, 440
sdc = app._get_screen_dc()
shot = snap._new_dib(sdc, SW, SH)
gdi32.BitBlt(shot[0], 0, 0, SW, SH, sdc, vsx + 200, vsy + 160, snap.SRCCOPY)

C_RED, C_BLUE, C_YELLOW = 0xFFFF3B30, 0xFF0A84FF, 0xFFFFD60A
gg = GFX(shot[0])


def _pen(argb, w):
    pen = P()
    gdip.GdipCreatePen1(argb, float(w), 0, byref(pen))
    return pen


def rect(argb, w, x, y, ww, hh):
    pen = _pen(argb, w)
    p = P()
    gdip.GdipCreatePath(0, byref(p))
    gdip.GdipAddPathRectangle(p, float(x), float(y), float(ww), float(hh))
    gdip.GdipDrawPath(gg.g, pen, p)
    gdip.GdipDeletePath(p)
    gdip.GdipDeletePen(pen)


def ellipse(argb, w, x, y, ww, hh):
    pen = _pen(argb, w)
    gdip.GdipDrawEllipse(gg.g, pen, float(x), float(y), float(ww), float(hh))
    gdip.GdipDeletePen(pen)


rect(C_RED, 3, 40, 40, 300, 150)          # 细
ellipse(C_BLUE, 6, 400, 60, 300, 200)     # 中
rect(C_YELLOW, 10, 100, 250, 220, 140)    # 粗
gg.close()
shot_rgba = ctypes.string_at(shot[3], SW * SH * 4)
shot_rgba = bytes(shot_rgba)

# ---------- 4. 拼成一张图
tw = max(t[1] for t in tiles)
pad = 14
TW = max(tw, SW) + pad * 2
TH = pad + sum(t[2] + 10 for t in tiles if t[0]) + SH + pad * 2


def put(canvas, rgba, x0, y0, W, H, opaque=False):
    for row in range(H):
        s = row * W * 4
        d = ((y0 + row) * TW + x0) * 4
        if opaque:
            canvas[d:d + W * 4] = rgba[s:s + W * 4]
            for xx in range(W):                 # 强制不透明
                canvas[d + xx * 4 + 3] = 255
        else:
            canvas[d:d + W * 4] = rgba[s:s + W * 4]


canvas = bytearray(TW * TH * 4)
for i in range(TW * TH):
    canvas[i * 4] = 0x30
    canvas[i * 4 + 1] = 0x34
    canvas[i * 4 + 2] = 0x3C
    canvas[i * 4 + 3] = 255

y = pad
for tag, W, H, rgba in tiles:
    if tag is None:
        continue
    put(canvas, rgba, pad, y, W, H)
    y += H + 10
put(canvas, shot_rgba, pad, y, SW, SH, opaque=True)

hold = ctypes.create_string_buffer(bytes(canvas), len(canvas))
img = snap.buf_to_image(hold, TW, TH)
gdip.GdipSaveImageToFile(img, OUT, byref(snap.PNG_CLSID), None)
gdip.GdipDisposeImage(img)

snap._free_dib(shot)
user32.DestroyWindow(app.hwnd_main)
print("saved", OUT, "%dx%d" % (TW, TH))
