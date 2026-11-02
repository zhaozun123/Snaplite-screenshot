"""用面积积分（每个像素只计一次）比较各方案在角上的棱边均匀度。

背景用纯色，所以 (亮度 - 内部底色) 只可能来自棱边。
只统计半径 R-1.6..R+0.3 那一圈、alpha>0 的像素，按 5° 分箱累加。
避开 60°~90°（顶边受光高光所在），只看 0°~60° 的纯弧线区。
"""
import ctypes
import math
import sys

sys.path.insert(0, ".")

import snap
from snap import user32

BASE = 69.0


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


def render(analytic, rim_in, width=None):
    app.mn_cov = None
    app.mn_cov_key = None
    app._corner_cov = (lambda RR: cov_area(RR)) if analytic \
        else snap.App._corner_cov.__get__(app, snap.App)
    orig = snap.GFX.stroke_round

    def patched(self, argb, w0, x, y, ww, hh, r):
        if argb == 0x59FFFFFF:
            r = r - rim_in
            if width:
                w0 = width
        return orig(self, argb, w0, x, y, ww, hh, r)

    snap.GFX.stroke_round = patched
    try:
        app.mn_glass = bytes([120, 120, 120, 255]) * (W * H)
        app._present_main()
        return ctypes.string_at(app.mn_bits, W * H * 4)
    finally:
        snap.GFX.stroke_round = orig


def bins(buf, lo=0, hi=60):
    n = len(range(lo, hi, 5))
    acc = [0.0] * n
    for y in range(R):
        for x in range(R):
            a = buf[(y * W + x) * 4 + 3]
            if a == 0:
                continue
            dx = R - (x + 0.5)
            dy = R - (y + 0.5)
            d = math.hypot(dx, dy)
            if d < R - 1.6 or d > R + 0.3:
                continue
            o = (y * W + x) * 4
            lum = 0.299 * buf[o + 2] + 0.587 * buf[o + 1] + 0.114 * buf[o]
            deg = math.degrees(math.atan2(dy, dx))
            if deg < lo or deg >= hi:
                continue
            acc[int((deg - lo) / 5)] += max(0.0, lum - BASE)
    return acc


print("角上 0°~60° 棱边发光量（每 5° 一箱，像素面积积分）")
print("%-20s %s  最大偏离" % ("方案", "  ".join("%5d" % (d) for d in range(0, 60, 5))))
for label, an, ri, wd in (("现状 3x3 / R", False, 0.0, None),
                          ("面积 / R", True, 0.0, None),
                          ("面积 / R-0.5", True, 0.5, None),
                          ("面积 / R-1.0", True, 1.0, None),
                          ("面积 / R-0.5 宽1.4", True, 0.5, 1.4),
                          ("面积 / R-1.0 宽2.0", True, 1.0, 2.0)):
    b = render(an, ri, wd)
    v = bins(b)
    m = sum(v) / len(v)
    dev = max(abs(x - m) for x in v) / m * 100
    print("%-20s %s   %4.1f%%"
          % (label, "  ".join("%5.0f" % x for x in v), dev))

user32.DestroyWindow(app.hwnd_main)
