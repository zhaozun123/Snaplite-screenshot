"""按「合成后」的亮度测棱边均匀度（唯一可信的度量方式）。

之前直接读 BGRA 缓冲是预乘颜色：半透明像素的 RGB 已经被乘以 alpha，
读出来的亮度天然偏低，于是角上永远显得「暗一块」。必须先按不透明背景合成。

每度弧长的发光量按 1/d 加权（一个像素在半径 d 处张开 1/d 弧度）。
"""
import ctypes
import math
import sys

sys.path.insert(0, ".")

import snap
from snap import user32

BG = 96.0          # 合成背景（中灰，和真实桌面同量级）
BASE = 69.0        # 面板内部合成后的亮度


def cov_area(R):
    out = []
    for yy in range(R):
        row = []
        for xx in range(R):
            tot = 0.0
            for s in range(16):
                y = yy + (s + 0.5) / 16.0
                dy = R - y
                w = 0.0 if abs(dy) >= R else math.sqrt(R * R - dy * dy)
                lo = max(xx, R - w)
                hi = min(xx + 1, R + w)
                if hi > lo:
                    tot += (hi - lo) / 16.0
            row.append(int(round(max(0.0, min(1.0, tot)) * 255.0)))
        out.append(row)
    return out


app = snap.App()
app.create_main()
W, H = app.MW, app.MH
R = int(round(app.RADIUS))


def render(analytic, rim_in, width, argb):
    app.mn_cov = None
    app.mn_cov_key = None
    app._corner_cov = (lambda RR: cov_area(RR)) if analytic \
        else snap.App._corner_cov.__get__(app, snap.App)
    orig = snap.GFX.stroke_round

    def patched(self, a0, w0, x, y, ww, hh, r):
        if a0 == 0x59FFFFFF:
            return orig(self, argb, width, x, y, ww, hh, r - rim_in)
        return orig(self, a0, w0, x, y, ww, hh, r)

    snap.GFX.stroke_round = patched
    try:
        app.mn_glass = bytes([120, 120, 120, 255]) * (W * H)
        app._present_main()
        return ctypes.string_at(app.mn_bits, W * H * 4)
    finally:
        snap.GFX.stroke_round = orig


def comp_lum(buf, x, y):
    o = (y * W + x) * 4
    a = buf[o + 3] / 255.0
    r, g, b = buf[o + 2], buf[o + 1], buf[o]
    # 预乘 → 还原真色 → 铺到 BG 上
    if a > 0:
        r, g, b = r / a, g / a, b / a
    r = r * a + BG * (1 - a)
    g = g * a + BG * (1 - a)
    b = b * a + BG * (1 - a)
    return 0.299 * r + 0.587 * g + 0.114 * b


def per_degree(buf, lo=5, hi=85):
    n = hi - lo
    acc = [0.0] * n
    for y in range(R):
        for x in range(R):
            dx = R - (x + 0.5)
            dy = R - (y + 0.5)
            d = math.hypot(dx, dy)
            if d < R - 2.2 or d > R + 0.2 or d < 1e-6:
                continue
            deg = math.degrees(math.atan2(dy, dx))
            if deg < lo or deg >= hi:
                continue
            acc[int(deg - lo)] += max(0.0, comp_lum(buf, x, y) - BASE) / d
    return acc


print("角上「每度弧长的棱边发光量」（5°~85°，合成后测量）")
for label, an, ri, wd, argb in (
        ("现状 w1.0 / R / 35%", False, 0.0, 1.0, 0x59FFFFFF),
        ("面积 w1.0 / R / 35%", True, 0.0, 1.0, 0x59FFFFFF),
        ("面积 w1.0 / R-0.5 / 35%", True, 0.5, 1.0, 0x59FFFFFF),
        ("面积 w1.5 / R-0.75 / 30%", True, 0.75, 1.5, 0x4CFFFFFF),
        ("面积 w2.0 / R-1.0 / 22%", True, 1.0, 2.0, 0x38FFFFFF),
):
    b = render(an, ri, wd, argb)
    v = per_degree(b)
    g = [sum(v[i:i + 10]) / 10.0 for i in range(0, len(v) - 10, 10)]
    m = sum(g) / len(g)
    dev = max(abs(x - m) / m * 100 for x in g)
    print("\n%-24s 均值 %6.2f  最大偏离 %4.1f%%" % (label, m, dev))
    print("    每 10°（5°→85°）：%s" % "  ".join("%5.2f" % x for x in g))

user32.DestroyWindow(app.hwnd_main)
