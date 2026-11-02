"""把「边缘发暗」从桌面内容里剥离出来：喂一张纯灰图进 _blur_bgra。

用法：python -u tools/audit_blur_edge.py

如果连纯灰图模糊之后边上都变暗，那就和壁纸内容、和透明都无关，
是模糊函数自己在位图边界上吃到了「图外」的像素（当成黑）。
"""
import os
import sys
from ctypes import c_void_p

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import snap

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

app = snap.App()
W, H = app.MW, app.MH
F = app.BLUR_F
G = 128


def make_flat(w, h, v=G):
    b = bytearray(w * h * 4)
    b[0::4] = bytes([v]) * (w * h)
    b[1::4] = bytes([v]) * (w * h)
    b[2::4] = bytes([v]) * (w * h)
    b[3::4] = b"\xff" * (w * h)
    return bytes(b)


def rowmean(buf, w, x0, x1, y):
    o = (y * w + x0) * 4
    t = 0
    for x in range(x0, x1):
        t += buf[o]
        o += 4
    return t / float(x1 - x0)


def colmean(buf, w, x, y0, y1):
    t = 0
    for y in range(y0, y1):
        o = (y * w + x) * 4
        t += buf[o]
    return t / float(y1 - y0)


print("面板 %dx%d  模糊系数 %d  输入恒为灰 %d" % (W, H, F, G))

# ---- 1. 面板大小的输入，直接模糊 ----
out = app._blur_bgra(make_flat(W, H), W, H)
print("\n[面板大小输入] 顶边逐行（中间一半宽度平均）")
for d in range(0, 16):
    print("  y=%2d  %6.2f  (%+0.2f)" % (d, rowmean(out, W, W // 4, W // 4 * 3, d),
                                        rowmean(out, W, W // 4, W // 4 * 3, d) - G))
print("[面板大小输入] 左边逐列")
for d in range(0, 16):
    print("  x=%2d  %6.2f  (%+0.2f)" % (d, colmean(out, W, d, H // 3, H // 3 * 2),
                                        colmean(out, W, d, H // 3, H // 3 * 2) - G))

# ---- 2. 四周各多留 F 像素再模糊，然后裁回面板大小 ----
print("\n[外扩 %d 像素再裁] 顶边逐行" % F)
gw, gh = W + F * 2, H + F * 2
big = app._blur_bgra(make_flat(gw, gh), gw, gh)
crop = bytearray(W * H * 4)
for y in range(H):
    so = ((y + F) * gw + F) * 4
    crop[y * W * 4:(y + 1) * W * 4] = big[so:so + W * 4]
crop = bytes(crop)
for d in range(0, 16):
    print("  y=%2d  %6.2f  (%+0.2f)" % (d, rowmean(crop, W, W // 4, W // 4 * 3, d),
                                        rowmean(crop, W, W // 4, W // 4 * 3, d) - G))
print("[外扩 %d 像素再裁] 左边逐列" % F)
for d in range(0, 16):
    print("  x=%2d  %6.2f  (%+0.2f)" % (d, colmean(crop, W, d, H // 3, H // 3 * 2),
                                        colmean(crop, W, d, H // 3, H // 3 * 2) - G))

# ---- 3. 面板正中是不是也该是 G ----
print("\n[中心] 面板大小输入 = %.2f   外扩再裁 = %.2f"
      % (rowmean(out, W, W // 2, W // 2 + 10, H // 2),
         rowmean(crop, W, W // 2, W // 2 + 10, H // 2)))
