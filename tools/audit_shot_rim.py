"""沿用户截图里面板的外框逐点扫棱边亮度，找出「不均匀」的具体位置（只读）。

面板包围盒由调用方给出（前面已用亮线扫描测出 101..440 / 108..255）。
每条边取最外一列/行，圆角按 1° 一步沿弧线取样。
"""
import math
import sys

sys.path.insert(0, ".")

from png_read import read_png


def load(path):
    w, h, bpp, px = read_png(path)

    def lum(x, y):
        o = (y * w + x) * bpp
        return 0.299 * px[o] + 0.587 * px[o + 1] + 0.114 * px[o + 2]

    return w, h, lum


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else \
        r"C:\Users\ZUN_HOME\.workbuddy\clipboard-images\clipboard-2026-11-02T14-17-14-839Z-417cdd26.png"
    w, h, lum = load(path)
    x0, x1, y0, y1 = 101, 440, 108, 255
    R = 20
    print("面板 %dx%d @ (%d,%d)  R=%d" % (x1 - x0 + 1, y1 - y0 + 1, x0, y0, R))

    print("\n[顶边] y=%d，x 从 %d 到 %d（每 10px）" % (y0, x0 + R, x1 - R))
    tv = []
    for x in range(x0 + R, x1 - R + 1, 10):
        v = max(lum(x, y0 + d) for d in (-1, 0, 1))
        tv.append((x, v))
    print("    " + " ".join("%d:%.0f" % (x, v) for x, v in tv))
    print("    摆动 %.1f（%.1f ~ %.1f）"
          % (max(v for _, v in tv) - min(v for _, v in tv),
             min(v for _, v in tv), max(v for _, v in tv)))

    print("\n[底边] y=%d" % y1)
    bv = []
    for x in range(x0 + R, x1 - R + 1, 10):
        v = max(lum(x, y1 + d) for d in (-1, 0, 1))
        bv.append((x, v))
    print("    " + " ".join("%d:%.0f" % (x, v) for x, v in bv))
    print("    摆动 %.1f（%.1f ~ %.1f）"
          % (max(v for _, v in bv) - min(v for _, v in bv),
             min(v for _, v in bv), max(v for _, v in bv)))

    print("\n[左边] x=%d" % x0)
    lv = []
    for y in range(y0 + R, y1 - R + 1, 5):
        v = max(lum(x0 + d, y) for d in (-1, 0, 1))
        lv.append((y, v))
    print("    " + " ".join("%d:%.0f" % (y, v) for y, v in lv))
    print("    摆动 %.1f（%.1f ~ %.1f）"
          % (max(v for _, v in lv) - min(v for _, v in lv),
             min(v for _, v in lv), max(v for _, v in lv)))

    print("\n[右边] x=%d" % x1)
    rv = []
    for y in range(y0 + R, y1 - R + 1, 5):
        v = max(lum(x1 + d, y) for d in (-1, 0, 1))
        rv.append((y, v))
    print("    " + " ".join("%d:%.0f" % (y, v) for y, v in rv))
    print("    摆动 %.1f（%.1f ~ %.1f）"
          % (max(v for _, v in rv) - min(v for _, v in rv),
             min(v for _, v in rv), max(v for _, v in rv)))

    print("\n[四角弧线] 从左边切点扫到顶边切点，每 5°（亮度取半径 R±1 内的最大值）")
    for name, cx, cy, sx, sy in (("左上", x0 + R, y0 + R, -1, -1),
                                 ("右上", x1 - R, y0 + R, 1, -1),
                                 ("左下", x0 + R, y1 - R, -1, 1),
                                 ("右下", x1 - R, y1 - R, 1, 1)):
        vals = []
        for deg in range(0, 91, 5):
            a = math.radians(deg)
            best = 0
            for k in range(-12, 13):
                rr = R + k * 0.25
                xi = int(round(cx + sx * rr * math.cos(a)))
                yi = int(round(cy + sy * rr * math.sin(a)))
                if 0 <= xi < w and 0 <= yi < h:
                    best = max(best, lum(xi, yi))
            vals.append(best)
        print("    %s  %s   摆动 %.1f（%.1f ~ %.1f）"
              % (name, " ".join("%3.0f" % v for v in vals),
                 max(vals) - min(vals), min(vals), max(vals)))


main()
