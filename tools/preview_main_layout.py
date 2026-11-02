"""看主面板新版排版：去掉「轻量截图」胶囊、版本号跟到标题后、右上角加图钉。

横排三态（不置顶 / 已置顶 / 鼠标悬在图钉上），2 倍放大；再单独出一张标题行
6 倍放大，专门看图钉图标画得清不清楚。

用法：python -u tools/preview_main_layout.py
产出：assets/_dbg_main_layout.png、assets/_dbg_pin_zoom.png
"""
import ctypes
import os
import sys
import time
from ctypes import c_void_p, byref

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import snap
from snap import user32

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from png_write import write_png_bgra

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT1 = os.path.join(ROOT, "assets", "_dbg_main_layout.png")
OUT2 = os.path.join(ROOT, "assets", "_dbg_pin_zoom.png")


def save_png(raw, ww, hh, path):
    write_png_bgra(path, ww, hh, raw)


def zoom(buf, w, h, z, x0=0, y0=0, cw=None, ch=None):
    """放大。可选裁一块再放大（x0/y0/cw/ch 都是源图坐标）"""
    cw = cw or w
    ch = ch or h
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


def compose_on(bits, w, h, bg=(30, 34, 42)):
    """mn_bits 是预乘 alpha，铺到不透明底色上才是眼睛看到的颜色"""
    out = bytearray(w * h * 4)
    for i in range(0, w * h * 4, 4):
        a = bits[i + 3]
        ia = 255 - a
        out[i] = min(255, bits[i] + bg[0] * ia // 255)
        out[i + 1] = min(255, bits[i + 1] + bg[1] * ia // 255)
        out[i + 2] = min(255, bits[i + 2] + bg[2] * ia // 255)
        out[i + 3] = 255
    return bytes(out)


app = snap.App()
app.create_main()
W, H = app.MW, app.MH
F = app.BLUR_F

LEFT = (user32.GetSystemMetrics(snap.SM_XVIRTUALSCREEN) + 640) // F * F
TOP = (user32.GetSystemMetrics(snap.SM_YVIRTUALSCREEN) + 360) // F * F
user32.SetWindowPos(app.hwnd_main, None, LEFT, TOP, 0, 0, 0x0001 | 0x0004)
time.sleep(0.3)

glass = app._blur_behind(LEFT, TOP)
if not glass:
    print("抓不到背景，改用纯色底")
    glass = bytes(bytearray([58, 52, 70, 255] * (W * H)))


def render(topmost, hover):
    app.topmost = topmost
    app.hover_main = hover
    app.mn_glass = glass
    app._present_main()
    return compose_on(ctypes.string_at(app.mn_bits, W * H * 4), W, H)


states = [("不置顶", render(False, None)),
          ("已置顶", render(True, None)),
          ("悬停图钉", render(False, "pin"))]

# ---------- 横排三态，2 倍
Z, GAP = 2, 12
ZW, ZH = W * Z, H * Z
CW = ZW * 3 + GAP * 2
canvas = bytearray(CW * ZH * 4)
for k, (name, img) in enumerate(states):
    xo = k * (ZW + GAP) * 4
    big = zoom(img, W, H, Z)
    for y in range(ZH):
        o = (y * CW + k * (ZW + GAP)) * 4
        canvas[o:o + ZW * 4] = big[y * ZW * 4:(y + 1) * ZW * 4]
for y in range(ZH):
    pass
save_png(bytes(canvas), CW, ZH, OUT1)
print("已写出 %s  （左→右：%s，%d 倍）" % (OUT1, " / ".join(s[0] for s in states), Z))

# ---------- 标题行 6 倍（含图钉），竖排三态
Z2 = 6
CROP_H = 38
ZW2 = W * Z2
ZH2 = CROP_H * Z2
CW2 = ZW2
CH2 = ZH2 * 3 + GAP * 2
canvas2 = bytearray(CW2 * CH2 * 4)
for k, (name, img) in enumerate(states):
    big = zoom(img, W, H, Z2, 0, 0, W, CROP_H)
    yo = k * (ZH2 + GAP)
    canvas2[yo * CW2 * 4:(yo + ZH2) * CW2 * 4] = big
save_png(bytes(canvas2), CW2, CH2, OUT2)
print("已写出 %s  （上→下：%s，%d 倍）" % (OUT2, " / ".join(s[0] for s in states), Z2))

user32.DestroyWindow(app.hwnd_main)
