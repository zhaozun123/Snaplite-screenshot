"""分析用户截图里面板的「高亮棱边」到底哪里不均匀（只读）。

步骤：先按亮度找出面板的包围盒，再分别沿四条直边统计最亮那一行的亮度，
最后沿圆角弧线按角度统计。整圈应当接近一致，偏差大的地方就是眼睛看到的「不均匀」。
"""
import math
import sys

sys.path.insert(0, ".")

from png_read import read_png

path = sys.argv[1]
w, h, bpp, px = read_png(path)


def lum(x, y):
    o = (y * w + x) * bpp
    return 0.299 * px[o] + 0.587 * px[o + 1] + 0.114 * px[o + 2]


print("图 %dx%d" % (w, h))

# 面板是很暗的一块，先找它的行列范围
rows = [y for y in range(h) if sum(1 for x in range(w) if lum(x, y) < 55) > w * 0.35]
cols = [x for x in range(w) if sum(1 for y in range(h) if lum(x, y) < 55) > h * 0.35]
print("暗区行 %d..%d   列 %d..%d" % (rows[0], rows[-1], cols[0], cols[-1]))
x0, x1, y0, y1 = cols[0], cols[-1], rows[0], rows[-1]
print("面板宽高 %d x %d" % (x1 - x0 + 1, y1 - y0 + 1))

print("\n[直边] 每条边最外一行/列的最亮亮度（取中段，避开圆角）")
mid = (x0 + x1) // 2
midy = (y0 + y1) // 2
for name, fn in (
    ("顶边 y=%d" % y0, max(lum(x, y0) for x in range(x0 + 40, x1 - 40))),
    ("底边 y=%d" % y1, max(lum(x, y1) for x in range(x0 + 40, x1 - 40))),
    ("左边 x=%d" % x0, max(lum(x0, y) for y in range(y0 + 40, y1 - 40))),
    ("右边 x=%d" % x1, max(lum(x1, y) for y in range(y0 + 40, y1 - 40))),
):
    print("    %-12s %6.1f" % (name, fn))

print("\n[顶边] 沿水平方向每 12px 的最亮亮度（看是否忽明忽暗）")
vals = []
for x in range(x0 + 20, x1 - 20, 12):
    v = max(lum(x, y0 + d) for d in (0, 1))
    vals.append(v)
    print("    x=%3d  %6.1f" % (x, v))
print("    顶边摆动 %.1f（最小 %.1f 最大 %.1f）"
      % (max(vals) - min(vals), min(vals), max(vals)))

print("\n[左边] 沿垂直方向每 8px 的最亮亮度")
vals = []
for y in range(y0 + 20, y1 - 20, 8):
    v = max(lum(x0 + d, y) for d in (0, 1))
    vals.append(v)
    print("    y=%3d  %6.1f" % (y, v))
print("    左边摆动 %.1f（最小 %.1f 最大 %.1f）"
      % (max(vals) - min(vals), min(vals), max(vals)))
