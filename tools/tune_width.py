"""调棱边：目标是把角上「一段亮一段暗」的珠子感压掉。

判据：沿弧线逐行取峰值亮度（在预测位置 ±2px 内找最大），算最大值-最小值。
现在用户截图里这个差是 30~109（摆动 79），直边是 0。
线宽大于 1px 时，斜向也总有像素被完整覆盖，峰值就不会随着像素相位掉下去。
"""
import ctypes
import math
import sys

sys.path.insert(0, ".")

import snap
from snap import user32


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


def arc_peaks(buf):
    """沿左上角弧线逐行取峰值（在圆的解析位置附近 ±2px 找最亮）"""
    def lum(x, y):
        o = (y * W + x) * 4
        return 0.299 * buf[o + 2] + 0.587 * buf[o + 1] + 0.114 * buf[o]

    seq = []
    for y in range(1, R):
        dy = R - (y + 0.5)
        if abs(dy) > R:
            continue
        expect = R - math.sqrt(max(0.0, R * R - dy * dy))
        xs = range(max(0, int(expect) - 2), min(W, int(expect) + 3))
        seq.append(max(lum(x, y) for x in xs))
    return seq


print("%-26s %-6s %-6s %-6s %-8s %-8s" % ("方案", "最暗", "最亮", "摆动", "均值", "直边对照"))
for label, an, ri, wd, argb in (
        ("现状 w1.0 / R / 35%", False, 0.0, 1.0, 0x59FFFFFF),
        ("w1.0 / R-0.5 / 35%", True, 0.5, 1.0, 0x59FFFFFF),
        ("w1.5 / R-0.75 / 30%", True, 0.75, 1.5, 0x4CFFFFFF),
        ("w1.5 / R-0.75 / 35%", True, 0.75, 1.5, 0x59FFFFFF),
        ("w2.0 / R-1.0 / 22%", True, 1.0, 2.0, 0x38FFFFFF),
        ("w2.0 / R-1.0 / 26%", True, 1.0, 2.0, 0x42FFFFFF),
        ("w1.0 / R-1.0 / 35%", True, 1.0, 1.0, 0x59FFFFFF),
        ("w1.0 / R-1.5 / 35%", True, 1.5, 1.0, 0x59FFFFFF),
        ("w1.0 / R-2.0 / 35%", True, 2.0, 1.0, 0x59FFFFFF),
        ("w1.4 / R-0.7 / 30%", True, 0.7, 1.4, 0x4CFFFFFF),
        ("w2.2 / R-1.1 / 24%", True, 1.1, 2.2, 0x3DFFFFFF),
):
    b = render(an, ri, wd, argb)
    seq = arc_peaks(b)

    def lum(x, y):
        o = (y * W + x) * 4
        return 0.299 * b[o + 2] + 0.587 * b[o + 1] + 0.114 * b[o]
    straight = max(lum(x, 0) for x in range(R + 20, W - R - 20))
    print("%-26s %-6.0f %-6.0f %-6.0f %-8.1f %-8.0f"
          % (label, min(seq), max(seq), max(seq) - min(seq),
             sum(seq) / len(seq), straight))

user32.DestroyWindow(app.hwnd_main)
