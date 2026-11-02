"""在真实桌面上量「边缘内侧那圈阴影」：旧法（只抓面板大小）vs 新法（外扩再裁）。

用法：python -u tools/audit_inner_shadow.py

静止和拖动走的是同一个 _present_main / _draw_main，唯一不同的是玻璃底的来历：

  · 旧法  _blur_bgra(raw)，raw **正好是面板大小**
          模糊的输入边界落在面板边缘上 → 四条边暗一半，往里 10px 才恢复
  · 新法  _blur_behind()：抓「面板 + 四周各一圈」再模糊，从内部裁出面板
          暗边落在面板外面，被裁掉
  · 拖动  _grab_behind()：从一张更大的快照里裁，同样碰不到边界 —— 这就是
          「一动不动有阴影、一拖动就没了」的原因

raw 那一列是没模糊过的真实背景，用来判断「暗」是不是桌面上本来就有的。
"""
import os
import sys
import time
from ctypes import c_void_p

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import snap
from snap import user32

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

app = snap.App()
app.create_main()
if not app.hwnd_main:
    print("FAIL: 没有窗口")
    sys.exit(1)

W, H = app.MW, app.MH
F = app.BLUR_F

LEFT = (user32.GetSystemMetrics(snap.SM_XVIRTUALSCREEN) + 640) // F * F
TOP = (user32.GetSystemMetrics(snap.SM_YVIRTUALSCREEN) + 360) // F * F
user32.SetWindowPos(app.hwnd_main, None, LEFT, TOP, 0, 0, 0x0001 | 0x0004)
time.sleep(0.25)

print("面板 %dx%d  模糊系数 %d  位置 (%d,%d)" % (W, H, F, LEFT, TOP))

raw = app._grab_without_self(LEFT, TOP, W, H)
if not raw:
    print("抓不到背景（没有 WDA_EXCLUDEFROMCAPTURE？），本机无法测")
    sys.exit(1)
old = app._blur_bgra(raw, W, H)
new = app._blur_behind(LEFT, TOP)
if not new:
    print("_blur_behind 没出图")
    sys.exit(1)


def rowmean(buf, x0, x1, y):
    o = (y * W + x0) * 4
    t = 0
    for x in range(x0, x1):
        t += buf[o]
        o += 4
    return t / float(x1 - x0)


def colmean(buf, x, y0, y1):
    t = 0
    for y in range(y0, y1):
        t += buf[(y * W + x) * 4]
    return t / float(y1 - y0)


D = 20
print("\n=== 顶边：第 d 行往内（中间一半宽度平均） ===")
print("  d     真背景     旧法     新法   旧-真    新-真")
for d in range(D):
    r = rowmean(raw, W // 4, W // 4 * 3, d)
    o = rowmean(old, W // 4, W // 4 * 3, d)
    n = rowmean(new, W // 4, W // 4 * 3, d)
    print("%3d  %7.1f  %7.1f  %7.1f  %+6.1f  %+6.1f" % (d, r, o, n, o - r, n - r))

print("\n=== 底边 ===")
print("  d     真背景     旧法     新法   旧-真    新-真")
for d in range(D):
    r = rowmean(raw, W // 4, W // 4 * 3, H - 1 - d)
    o = rowmean(old, W // 4, W // 4 * 3, H - 1 - d)
    n = rowmean(new, W // 4, W // 4 * 3, H - 1 - d)
    print("%3d  %7.1f  %7.1f  %7.1f  %+6.1f  %+6.1f" % (d, r, o, n, o - r, n - r))

print("\n=== 左边：第 d 列往内（中间三分之一高度平均） ===")
print("  d     真背景     旧法     新法   旧-真    新-真")
for d in range(D):
    r = colmean(raw, d, H // 3, H // 3 * 2)
    o = colmean(old, d, H // 3, H // 3 * 2)
    n = colmean(new, d, H // 3, H // 3 * 2)
    print("%3d  %7.1f  %7.1f  %7.1f  %+6.1f  %+6.1f" % (d, r, o, n, o - r, n - r))

user32.DestroyWindow(app.hwnd_main)
