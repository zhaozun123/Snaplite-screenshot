"""图钉图标候选对比：26px 按钮里画四种「置顶」造型，8 倍放大挑一种。

A 现在的做法（描线漏斗）—— 保留作对照
B 实心帽 + 实心颈 + 针
C 向上箭头 + 顶部横线
D 描线图钉，腰更宽、针更长

画在 app.mn_dc 上再裁：自建 DIB 的 DC 句柄在 64 位下会被截断
（GdipCreateFromHDC 拿到无效句柄，画了半天一个像素都没落下），
主面板这块画布是已经验证能画的，直接借它。

用法：python -u tools/preview_pin_icons.py
产出：assets/_dbg_pin_icons.png
"""
import ctypes
import os
import sys
from ctypes import c_void_p

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import snap
from png_write import write_png_bgra

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "_dbg_pin_icons.png")
SC = 26          # 跟主面板按钮一样大
Z = 8
PAD = 8
COL = 0xFFFFFFFF
BG = 0xFF1E222B


def A(g, cx, cy):        # 现在：描线漏斗
    lw = 1.4
    cap_y, neck_y = cy - 5.8, cy + 0.6
    g.line(COL, lw, cx - 4.0, cap_y, cx + 4.0, cap_y)
    g.line(COL, lw, cx - 3.4, cap_y, cx - 1.4, neck_y)
    g.line(COL, lw, cx + 3.4, cap_y, cx + 1.4, neck_y)
    g.line(COL, lw, cx - 1.4, neck_y, cx + 1.4, neck_y)
    g.line(COL, lw, cx, neck_y, cx, cy + 6.4)


def B(g, cx, cy):        # 实心帽 + 实心颈 + 针
    g.fill_round(COL, cx - 4.2, cy - 6.4, 8.4, 2.8, 1.3)
    g.fill_round(COL, cx - 1.5, cy - 3.6, 3.0, 4.2, 1.3)
    g.line(COL, 1.5, cx, cy + 0.4, cx, cy + 6.2)


def C(g, cx, cy):        # 向上箭头 + 顶部横线
    lw = 1.4
    g.line(COL, lw, cx - 4.6, cy - 6.2, cx + 4.6, cy - 6.2)
    g.line(COL, lw, cx, cy + 6.2, cx, cy - 2.4)
    g.line(COL, lw, cx - 3.4, cy - 0.2, cx, cy - 4.0)
    g.line(COL, lw, cx + 3.4, cy - 0.2, cx, cy - 4.0)


def D(g, cx, cy):        # 描线图钉，腰更宽、针更长
    lw = 1.3
    cap_y, neck_y = cy - 6.0, cy + 0.2
    g.line(COL, lw, cx - 3.6, cap_y, cx + 3.6, cap_y)
    g.line(COL, lw, cx - 3.0, cap_y, cx - 2.2, neck_y)
    g.line(COL, lw, cx + 3.0, cap_y, cx + 2.2, neck_y)
    g.line(COL, lw, cx - 2.2, neck_y, cx + 2.2, neck_y)
    g.line(COL, lw, cx, neck_y, cx, cy + 7.0)


def zoom(buf, w, h, z, x0, y0, cw, ch):
    out = bytearray(cw * z * ch * z * 4)
    for y in range(ch):
        so = ((y + y0) * w + x0) * 4
        row = buf[so:so + cw * 4]
        wide = bytearray(cw * z * 4)
        for x in range(cw):
            wide[x * z * 4:(x + 1) * z * 4] = row[x * 4:(x + 1) * 4] * z
        for k in range(z):
            o = ((y * z + k) * cw * z) * 4
            out[o:o + cw * z * 4] = wide
    return bytes(out)


cands = [("A 现状", A), ("B 实心帽", B), ("C 上箭头", C), ("D 描线粗腰", D)]

app = snap.App()
app.create_main()
app._ensure_main_surface()
W, H = app.mn_size

# 先铺一层不透明底色，再一格一格画
ctypes.memset(app.mn_bits, 0, W * H * 4)
with snap.GFX(app.mn_dc) as g:
    g.fill_round(BG, 0, 0, W, H, 0)
    for i, (_, fn) in enumerate(cands):
        x = PAD + i * (SC + PAD)
        fn(g, x + SC / 2.0, PAD + SC / 2.0)

buf = ctypes.string_at(app.mn_bits, W * H * 4)
cells = [zoom(buf, W, H, Z, PAD + i * (SC + PAD), PAD, SC, SC)
         for i in range(len(cands))]

ZW = SC * Z
GAP = 6
CW = ZW * len(cells) + GAP * (len(cells) - 1)
CH = ZW
canvas = bytearray(CW * CH * 4)
for i, img in enumerate(cells):
    xo = i * (ZW + GAP)
    for y in range(CH):
        o = (y * CW + xo) * 4
        canvas[o:o + ZW * 4] = img[y * ZW * 4:(y + 1) * ZW * 4]

write_png_bgra(OUT, CW, CH, bytes(canvas))
print("已写出 %s  (%dx%d, 左→右：%s，%d 倍)"
      % (OUT, CW, CH, " / ".join(c[0] for c in cands), Z))
snap.user32.DestroyWindow(app.hwnd_main)
