"""把真实工具条放大 4 倍，并在每颗按钮上画三条参考线，用来肉眼核对居中。

绿线 = 按钮格子的垂直中线（内容应该以它为中心）
红线 = 内容的墨迹中心（图标墨迹左端 → 文字墨迹右端）
蓝线 = 文字的墨迹中心（只看字，用来判断「字本身是否居中」）

用法：python tools/diag_center_zoom.py [TB_BTN_W] [TB_SCALE]
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctypes import byref, c_void_p
import snap
from snap import user32, GFX

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "_dbg_center_zoom.png")
Z = 4                      # 放大倍数（最近邻，不糊）
PAD = 12                   # 图四周留白（实际像素）
TEXT_LUMA = 185            # 文字是纯白，图标最亮 171，用这刀把文字单独切出来

if len(sys.argv) > 1:
    snap.TB_BTN_W = int(sys.argv[1])
if len(sys.argv) > 2:
    snap.TB_SCALE = float(sys.argv[2])

snap.SetProcessDpiAwarenessContext(c_void_p(-4))


def luma(raw, W, x, y):
    o = (y * W + x) * 4
    return 0.114 * raw[o] + 0.587 * raw[o + 1] + 0.299 * raw[o + 2], raw[o + 3]


def ink_box(raw, W, x0, y0, x1, y1, lo=75.0):
    """矩形内墨迹边界 (l,t,r,b)，没有内容返回 None"""
    l = t = 10 ** 6
    r = b = -1
    for y in range(y0, y1):
        for x in range(x0, x1):
            lu, a = luma(raw, W, x, y)
            if a >= 100 and lu >= lo:
                l = min(l, x)
                r = max(r, x)
                t = min(t, y)
                b = max(b, y)
    return None if r < 0 else (l, t, r, b)


app = snap.App()
app.create_main()
app._build_capture_buffers()
vsx, vsy, vsw, vsh = app.vs
app.sel = (vsx + 300, vsy + 240, min(820, vsw - 600), min(400, vsh - 480))
app.mode = None
app._refresh_tool()
app._show_tool()

W, H = app.tb_size
raw = ctypes.string_at(app.tb_bits, W * H * 4)
BW = app.tb(snap.TB_BTN_W)
GP = app.tb(snap.TB_BTN_GAP)
print("工具条 %dx%d 按钮 %dx%d 缝 %d 缩放 %.2f" % (W, H, BW, H, GP, snap.TB_SCALE))
print("%-8s %6s %6s %10s %10s" % ("标签", "左空", "右空", "块心-格心", "字心-格心"))

marks = []          # (x, color)
for i, (kind, label) in enumerate(snap.TB_ITEMS):
    x0 = i * (BW + GP)
    if x0 + BW > W:
        break
    box = ink_box(raw, W, x0, 0, x0 + BW, H)
    tbox = ink_box(raw, W, x0, 0, x0 + BW, H, TEXT_LUMA)
    if box is None or tbox is None:
        continue
    l, t, r, b = box
    cell_c = (BW - 1) / 2.0
    block_c = (l + r) / 2.0 - x0
    text_c = (tbox[0] + tbox[2]) / 2.0 - x0
    print("%-8s %6d %6d %10.1f %10.1f"
          % (label, l - x0, (x0 + BW - 1) - r, block_c - cell_c, text_c - cell_c))
    marks.append((x0 + cell_c, 0xFF00D26A))
    marks.append((x0 + block_c, 0xFFE05252))
    marks.append((x0 + text_c, 0xFF4C9AFF))

# 最近邻放大 + 灰底合成（工具条本身是半透明的，直接放大背景会发白）
CW, CH = (W + PAD * 2) * Z, (H + PAD * 2) * Z
canvas = bytearray(CW * CH * 4)
for i in range(CW * CH):
    canvas[i * 4] = 0x3C
    canvas[i * 4 + 1] = 0x34
    canvas[i * 4 + 2] = 0x30
    canvas[i * 4 + 3] = 255
for yy in range(H * Z):
    sy = yy // Z
    for xx in range(W * Z):
        sx = xx // Z
        o = (sy * W + sx) * 4
        a = raw[o + 3] / 255.0
        d = ((yy + PAD * Z) * CW + (xx + PAD * Z)) * 4
        canvas[d] = int(raw[o] * a + 0x3C * (1 - a))
        canvas[d + 1] = int(raw[o + 1] * a + 0x34 * (1 - a))
        canvas[d + 2] = int(raw[o + 2] * a + 0x30 * (1 - a))

# 参考线直接写进像素（1px 宽的彩色竖线，在 4 倍图上正好看清）
for mx, col in marks:
    px = int(round(PAD * Z + mx * Z))
    r8, g8, b8 = (col >> 16) & 0xFF, (col >> 8) & 0xFF, col & 0xFF
    for yy in range(H * Z):
        for dx in (0, 1):
            d = ((yy + PAD * Z) * CW + px + dx) * 4
            if 0 <= px + dx < CW:
                canvas[d] = b8
                canvas[d + 1] = g8
                canvas[d + 2] = r8

hold = ctypes.create_string_buffer(bytes(canvas), len(canvas))
img = snap.buf_to_image(hold, CW, CH)
snap.gdip.GdipSaveImageToFile(img, OUT, byref(snap.PNG_CLSID), None)
snap.gdip.GdipDisposeImage(img)
app._close_tool()
user32.DestroyWindow(app.hwnd_main)
print("→", OUT)
