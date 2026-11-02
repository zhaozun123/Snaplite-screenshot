"""打印主面板左上角每像素的亮度与 alpha（只读）。

看三件事：
- 弧线外缘相邻像素的 alpha 差（台阶高度）
- 棱边 1px 描边在弧线上的覆盖是否连续（有没有断点 / 突然变粗）
- 顶边那层「受光高光」在角上是否戛然而止
"""
import ctypes
import sys

sys.path.insert(0, ".")

import snap
from snap import user32

app = snap.App()
app.create_main()
W, H = app.MW, app.MH

# 固定背景：浅灰，边缘对比明显
app.mn_glass = bytes([120, 120, 120, 255]) * (W * H)
app._present_main()

buf = ctypes.string_at(app.mn_bits, W * H * 4)
R = int(round(app.RADIUS))
N = R + 3

print("面板 %dx%d  R=%d" % (W, H, R))
print("\n[A] alpha 表（空格分隔，256=不透明）")
hdr = "      " + "".join("%4d" % x for x in range(N))
print(hdr)
for y in range(N):
    row = []
    for x in range(N):
        row.append(buf[(y * W + x) * 4 + 3])
    print("y=%2d  %s" % (y, "".join("%4d" % v for v in row)))

print("\n[B] 亮度表（0.299R+0.587G+0.114B，已含 alpha 合成后不可见，仅看内容）")
print(hdr)
for y in range(N):
    row = []
    for x in range(N):
        o = (y * W + x) * 4
        row.append(int(0.299 * buf[o + 2] + 0.587 * buf[o + 1] + 0.114 * buf[o]))
    print("y=%2d  %s" % (y, "".join("%4d" % v for v in row)))

# 弧线外缘：每行/每列第一个 alpha>0 的像素 alpha 值
print("\n[C] 外缘（每行最左侧非全透明像素）的 alpha：")
for y in range(N):
    for x in range(N):
        a = buf[(y * W + x) * 4 + 3]
        if a > 0:
            print("    y=%2d  x=%2d  alpha=%3d" % (y, x, a))
            break

user32.DestroyWindow(app.hwnd_main)
