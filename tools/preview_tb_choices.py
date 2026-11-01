"""工具条字号放大方案对比图。

用户反馈「工具栏里的字体有点小，看着吃力」，要挑一档放大的字号。
这里把候选方案各画一行整条工具条（1:1 屏幕上多大就多大），
再在下面把「马赛克」那颗按钮 2 倍放大，方便看文字是否折行、留白是否均匀。

按钮宽 65px 是硬的：字体放大到某个程度，「马赛克」三个字会把按钮
撑满甚至溢出。所以每行都标出实际占用，方便判断极限。

用法：python tools/preview_tb_choices.py → assets/_dbg_tb_font.png
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
CAP = 22                    # 每行上方标注高度
GAP = 12
ZOOM = 2

# (编号+说明, 外框缩放, 字号缩放, 图标基准尺寸, 逻辑按钮宽)
# 按钮宽是硬约束：65px 下「马赛克」13px 就到头了，14px 会折行。
# 想再大只能加宽按钮（TB_BTN_W 是逻辑值，乘 0.86 后取整才是实际像素）。
ROWS = [
    ("A  改前      字号0.92 → 11px　按钮65　整条677", 0.86, 0.92, 17, 76),
    ("B  微调      字号1.00 → 12px　按钮65", 0.86, 1.00, 17, 76),
    ("C  不改宽度  字号1.08 → 13px　按钮65（极限）", 0.86, 1.08, 17, 76),
    ("D  改后      字号1.17 → 14px　按钮69　整条717", 0.86, 1.17, 17, 80),
    ("E  加宽按钮  字号1.25 → 15px　按钮72（整条 +70px）", 0.86, 1.25, 17, 84),
]

# 也可以只画其中几档，例如：python tools/preview_tb_choices.py A D
if len(sys.argv) > 1:
    _pick = {a.strip().upper() for a in sys.argv[1:]}
    ROWS = [r for r in ROWS if r[0][0] in _pick]


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
for tag, sc, fs, isz, btw in ROWS:
    snap.TB_SCALE, snap.TB_FONT_SCALE = sc, fs
    snap.TB_ICON_SIZE, snap.TB_BTN_W = isz, btw
    app._close_tool()
    app._refresh_tool()
    app._show_tool()
    W, H = app.tb_size
    raw = ctypes.string_at(app.tb_bits, W * H * 4)
    # 量一下「马赛克」那颗按钮的实际占用
    _, _, rects = app._tool_layout()
    mi = [k for k, _ in snap.TB_ITEMS].index("mosaic")
    bw = rects[mi][3]
    tiles.append((tag, W, H, to_bgra(raw, W, H), app.tb_font(12),
                  app.tb(snap.TB_ICON_SIZE), bw, rects[mi][1]))
    print("%-46s 整条 %dx%d  字号 %dpx  按钮 %dpx" %
          (tag, W, H, app.tb_font(12), bw))
app._close_tool()

max_w = max(t[1] for t in tiles)
zoom_btn_w = max(t[7] for t in tiles) * ZOOM
CW = max(max_w, zoom_btn_w * len(tiles) + PAD * 2) + PAD * 2
CH = PAD
for t in tiles:
    CH += CAP + 4 + t[2] + GAP
CH += 16
CH += CAP + 4 + max(t[2] for t in tiles) * ZOOM + GAP
CH += PAD

sdc = app._get_screen_dc()
dc, bmp, old, bits = snap._new_dib(sdc, CW, CH)

canvas = bytearray(CW * CH * 4)
for i in range(CW * CH):
    canvas[i * 4] = 0x4C
    canvas[i * 4 + 1] = 0x50
    canvas[i * 4 + 2] = 0x58
    canvas[i * 4 + 3] = 255


def blit(src, sw, sh, dx, dy, zoom=1):
    for row in range(sh * zoom):
        sy = row // zoom
        base = (dy + row) * CW
        for xx in range(sw * zoom):
            s = (sy * sw + xx // zoom) * 4
            d = (base + dx + xx) * 4
            canvas[d:d + 3] = src[s:s + 3]


g = GFX(dc)
y = PAD
for tag, W, H, rgba, fs, isz, bw, bx in tiles:
    g.text(tag, 13, 0xFFFFFFFF, PAD, y, 700, CAP, 0, True)
    # 折行判断要用「文字可用宽度」，不是「按钮宽 - 内容宽」：
    # 文字框左边 = bx+iw+gap-sl，右边一直被给到按钮右缘，
    # 所以可用宽 = (bw - iw - gap + ink_w)/2 + sl，超过 char 推进宽度就折行。
    il, it, ir, ib = snap.TB_ICON_INK["mosaic"]
    iw = (ir - il + 1) * isz / 17.0
    sl, sr = app.tb(snap.TB_TEXT_SIDE_L), app.tb(snap.TB_TEXT_SIDE_R)
    gap = app.tb(snap.TB_ICON_GAP)
    adv = g.measure("马赛克", fs)[0]
    ink = adv - sl - sr
    usable = (bw - iw - gap + ink) / 2.0 + sl
    m = usable - adv
    g.text("整条 %d×%d   字号 %dpx   按钮 %dpx   「马赛克」可用 %.1f / 需要 %.1f（余 %.1f）%s"
           % (W, H, fs, bw, usable, adv, m, "  折行!" if m < 0 else ""),
           13, 0xFF9AD7A0 if m >= 1 else 0xFFF0B060, PAD + 560, y, 640, CAP, 0)
    blit(rgba, W, H, PAD, y + CAP + 4)
    y += CAP + 4 + H + GAP

y += 16
g.text("「马赛克」按钮 ×2 放大（看文字是否折行、左右留白是否均衡）",
       13, 0xFFFFFFFF, PAD, y, 700, CAP, 0, True)
y += CAP + 4
x = PAD
for tag, W, H, rgba, fs, isz, bw, bx in tiles:
    # 只裁出马赛克那颗按钮那一段
    seg_w = bw
    seg = bytearray(seg_w * H * 4)
    for row in range(H):
        for xx in range(seg_w):
            s = (row * W + bx + xx) * 4
            d = (row * seg_w + xx) * 4
            seg[d:d + 3] = rgba[s:s + 3]
    blit(bytes(seg), seg_w, H, x, y, ZOOM)
    g.text(tag.split()[0], 12, 0xFFFFFFFF, x, y + H * ZOOM + 2, seg_w * ZOOM, 16, 1)
    x += seg_w * ZOOM + PAD
g.close()

hold = ctypes.create_string_buffer(bytes(canvas), len(canvas))
img = snap.buf_to_image(hold, CW, CH)
snap.gdip.GdipSaveImageToFile(img, OUT, byref(snap.PNG_CLSID), None)
snap.gdip.GdipDisposeImage(img)
snap._free_dib((dc, bmp, old, bits))
user32.DestroyWindow(app.hwnd_main)
print("→", OUT, CW, "x", CH)
