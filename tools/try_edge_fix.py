"""圆角抗锯齿试改：三种棱边位置 × 两种覆盖率，只在测试进程里打补丁。

度量：把左上角 R×R 区域里每个像素按「它相对圆心的角度」分到 9 个扇区，
统计每个扇区里 (亮度 - 内部底色) 的总和。整圈等宽等亮时各扇区应当接近。
逐条射线采样会走样（同一像素被重复计入不同次数），按像素分箱不会。
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


def render(analytic, rim_shift):
    app.mn_cov = None
    app.mn_cov_key = None
    app._corner_cov = (lambda RR: cov_area(RR)) if analytic \
        else snap.App._corner_cov.__get__(app, snap.App)
    orig = snap.GFX.stroke_round

    def patched(self, argb, width, x, y, ww, hh, r):
        if argb == 0x59FFFFFF and width == 1:
            r = r - rim_shift
        return orig(self, argb, width, x, y, ww, hh, r)

    snap.GFX.stroke_round = patched
    try:
        app.mn_glass = bytes([120, 120, 120, 255]) * (W * H)
        app._present_main()
        return ctypes.string_at(app.mn_bits, W * H * 4)
    finally:
        snap.GFX.stroke_round = orig


def sectors(buf, nbin=9):
    """把角落像素按角度分箱，累加 (亮度 - 底色)。只收半径 >= R-1.8 的像素，
    也就是棱边所在的那一圈，避免把内部和受光高光算进来。"""
    acc = [0.0] * nbin
    for y in range(R):
        for x in range(R):
            dx = R - (x + 0.5)
            dy = R - (y + 0.5)
            d = math.hypot(dx, dy)
            if d < R - 1.8 or d > R + 0.6:
                continue
            o = (y * W + x) * 4
            lum = 0.299 * buf[o + 2] + 0.587 * buf[o + 1] + 0.114 * buf[o]
            a = math.degrees(math.atan2(dy, dx))
            i = int(a / (90.0 / nbin))
            i = max(0, min(nbin - 1, i))
            acc[i] += max(0.0, lum - BASE)
    return acc


rows = []
for rim in (0.0, 0.5, 1.0):
    for label, an in (("现状3x3", False), ("面积积分", True)):
        b = render(an, rim)
        s = sectors(b)
        mean = sum(s) / len(s)
        dev = max(abs(v - mean) / mean * 100 for v in s)
        rows.append((label, rim, s, dev))
        print("%-8s 棱边内缩 %.1fpx  扇区 %s  最大偏离 %.1f%%"
              % (label, rim, " ".join("%5.0f" % v for v in s), dev))

print("\n结论：最大偏离越小，整圈越均匀")
best = min(rows, key=lambda r: r[3])
print("最好：%s + 内缩 %.1fpx（最大偏离 %.1f%%）" % (best[0], best[1], best[3]))

user32.DestroyWindow(app.hwnd_main)
