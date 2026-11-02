"""审查主面板圆角边缘的抗锯齿质量（只读，不改产品代码）。

看三件事：
1. `_corner_cov` 的覆盖率一共有多少级台阶 —— 台阶越少，弧线上越容易看出锯齿。
2. 真正上屏那一帧，四个角的 alpha 分布是否光滑（相邻像素差值超过多少）。
3. 加亮棱边（1px 白描边）在弧线处的有效亮度是否随角度摆动。
"""
import ctypes
import sys

sys.path.insert(0, ".")

import snap
from snap import user32

app = snap.App()
app.create_main()
W, H = app.MW, app.MH
R = int(round(app.RADIUS))
print("面板 %dx%d  RADIUS=%.1f(取整 %d)  scale=%.3f  屏幕 %dx%d"
      % (W, H, app.RADIUS, R, app.scale,
         user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)))

# ---------- 1. 覆盖率本身的级数 ----------
cov = app._corner_cov(R)
vals = sorted({v for row in cov for v in row})
print("\n[1] _corner_cov 覆盖率取值：共 %d 级" % len(vals))
print("    " + ", ".join(str(v) for v in vals))

# ---------- 2. 上屏帧的 alpha 台阶 ----------
app._present_main()
buf = ctypes.string_at(app.mn_bits, W * H * 4)


def alpha(x, y):
    return buf[(y * W + x) * 4 + 3]


def rgb(x, y):
    o = (y * W + x) * 4
    return buf[o], buf[o + 1], buf[o + 2]


print("\n[2] 左上角 0..%d 行的 alpha（每行从 x=0 打，遇 255 停）" % (R - 1))
for y in range(0, R):
    row = [alpha(x, y) for x in range(0, R)]
    # 只打到第一个 255 之后
    try:
        end = row.index(255) + 1
    except ValueError:
        end = R
    print("    y=%2d  %s" % (y, " ".join("%3d" % v for v in row[:end])))

# ---------- 3. 沿弧线的棱边亮度是否均匀 ----------
print("\n[3] 棱边（1px 白描边）沿弧线的峰值亮度：按角度取样")
import math

worst = []
for deg in range(0, 181, 15):
    a = math.radians(deg)
    # 从圆心 (R,R) 沿角度方向往外走，找该像素最大亮度
    best = -1
    bestr = 0
    for k in range(0, 8):
        rr = R - 1.0 + k * 0.25
        x = R - rr * math.cos(a)
        y = R - rr * math.sin(a)
        xi, yi = int(x), int(y)
        if not (0 <= xi < W and 0 <= yi < H):
            continue
        r_, g_, b_ = rgb(xi, yi)
        lum = 0.299 * r_ + 0.587 * g_ + 0.114 * b_
        if lum > best:
            best, bestr = lum, rr
    worst.append((deg, best, bestr))
    print("    %3d°  峰值亮度 %6.1f   半径位置 %.2f" % (deg, best, bestr))

sp = [v for _, v, _ in worst]
print("    弧线亮度 最小 %.1f  最大 %.1f  摆动 %.1f"
      % (min(sp), max(sp), max(sp) - min(sp)))

# 直边对照（顶边中间）
lum_top = max(0.299 * rgb(x, 0)[0] + 0.587 * rgb(x, 0)[1] + 0.114 * rgb(x, 0)[2]
              for x in range(W // 3, 2 * W // 3))
print("    顶边中部 峰值亮度 %.1f（作为对照）" % lum_top)

user32.DestroyWindow(app.hwnd_main)
