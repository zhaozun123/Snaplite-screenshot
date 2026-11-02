"""把「静止时边缘内侧的暗圈」画出来，2 倍放大对比：现在 vs 外扩后再模糊。

用法：python -u tools/preview_inner_shadow.py
产出：assets/_dbg_inner_shadow.png
"""
import os
import sys
import time
from ctypes import c_void_p, byref

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import snap
from snap import user32, gdip, P

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "_dbg_inner_shadow.png")
Z = 2
GAP = 10


def save_png(raw, ww, hh, path):
    sdc = user32.GetDC(None)
    d = snap._new_dib(sdc, ww, hh)
    __import__("ctypes").memmove(d[3], raw, ww * hh * 4)
    img = P()
    gdip.GdipCreateBitmapFromScan0(ww, hh, ww * 4, 0x00022009, d[3], byref(img))
    gdip.GdipSaveImageToFile(img, path, byref(snap.PNG_CLSID), None)
    gdip.GdipDisposeImage(img)
    snap._free_dib(d)
    user32.ReleaseDC(None, sdc)


def zoom(buf, w, h, z):
    out = bytearray(w * z * h * z * 4)
    for y in range(h):
        row = buf[y * w * 4:(y + 1) * w * 4]
        wide = bytearray(w * z * 4)
        for x in range(w):
            wide[x * z * 4:(x + 1) * z * 4] = row[x * 4:(x + 1) * 4] * z
        for k in range(z):
            out[((y * z + k) * w * z) * 4:((y * z + k + 1) * w * z) * 4] = wide
    return bytes(out)


app = snap.App()
app.create_main()
W, H = app.MW, app.MH
F = app.BLUR_F

LEFT = (user32.GetSystemMetrics(snap.SM_XVIRTUALSCREEN) + 640) // F * F
TOP = (user32.GetSystemMetrics(snap.SM_YVIRTUALSCREEN) + 360) // F * F
user32.SetWindowPos(app.hwnd_main, None, LEFT, TOP, 0, 0, 0x0001 | 0x0004)
time.sleep(0.3)

# 现状：抓正好面板大小再模糊
raw = app._grab_without_self(LEFT, TOP, W, H)
if not raw:
    print("抓不到背景")
    sys.exit(1)
cur = app._blur_bgra(raw, W, H)

# 改法：四周各多留 F 再模糊，裁回面板大小
gw = ((W + F * 2) // F) * F
gh = ((H + F * 2) // F) * F
gx = (LEFT - F) // F * F
gy = (TOP - F) // F * F
big_raw = app._grab_without_self(gx, gy, gw, gh)
big = app._blur_bgra(big_raw, gw, gh)
ox, oy = LEFT - gx, TOP - gy
fix = bytearray(W * H * 4)
for y in range(H):
    so = ((y + oy) * gw + ox) * 4
    fix[y * W * 4:(y + 1) * W * 4] = big[so:so + W * 4]
fix = bytes(fix)


def compose(glass):
    app.mn_glass = glass
    app._present_main()
    return __import__("ctypes").string_at(app.mn_bits, W * H * 4)


a = zoom(compose(cur), W, H, Z)
b = zoom(compose(fix), W, H, Z)

ZW, ZH = W * Z, H * Z
canvas = bytearray(ZW * (ZH * 2 + GAP) * 4)
canvas[0:ZW * ZH * 4] = a
canvas[ZW * (ZH + GAP) * 4:(ZW * (ZH + GAP) + ZW * ZH) * 4] = b
# 中间留白
for y in range(ZH, ZH + GAP):
    o = y * ZW * 4
    canvas[o:o + ZW * 4] = b"\xff" * (ZW * 4)

save_png(bytes(canvas), ZW, ZH * 2 + GAP, OUT)
print("已写出 %s  （上=现在，下=外扩后再模糊，%d 倍放大）" % (OUT, Z))
user32.DestroyWindow(app.hwnd_main)
