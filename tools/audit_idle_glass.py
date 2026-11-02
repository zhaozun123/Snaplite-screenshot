"""审计「界面不动时黑一下」这条路径。

用法：python -u tools/audit_idle_glass.py

盯三件事：

1. 静置（桌面完全不动）时，`_grab_without_self` 到底会不会失败。
   失败两次就会掉进「左右插值伪造背景」那条退路 —— 那条路的画面是按面板
   两侧像素拉出来的渐变，不是真背景（代码注释自己写了差 ±36 亮度）。
2. 退路产出的玻璃底，和真实背景差多少（拿藏起窗口后抓的全屏当标准答案）。
3. 退路在不同位置的表现：亮处 / 暗处 / 花瓣边缘。
"""
import sys
import os
import time
from ctypes import c_void_p

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import snap
from snap import user32, w

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

VX = user32.GetSystemMetrics(snap.SM_XVIRTUALSCREEN)
VY = user32.GetSystemMetrics(snap.SM_YVIRTUALSCREEN)
VW = user32.GetSystemMetrics(snap.SM_CXVIRTUALSCREEN)
VH = user32.GetSystemMetrics(snap.SM_CYVIRTUALSCREEN)


def luma(g, W, H, step=3):
    tot = n = 0
    stride = W * 4
    for y in range(0, H, step):
        row = g[y * stride:y * stride + stride]
        for k in range(0, len(row) - 3, 4 * step):
            tot += row[k] + row[k + 1] + row[k + 2]
            n += 3
    return tot / float(n) if n else 0.0


def truth_luma(truth, VWc, left, top, W, H, step=3):
    x0, y0 = left - VX, top - VY
    tot = n = 0
    stride = VWc * 4
    for y in range(max(0, y0), min(VH, y0 + H), step):
        row = truth[y * stride:y * stride + stride]
        for x in range(max(0, x0), min(VWc, x0 + W), step):
            k = x * 4
            tot += row[k] + row[k + 1] + row[k + 2]
            n += 3
    return tot / float(n) if n else 0.0


def snap_truth(app):
    user32.ShowWindow(app.hwnd_main, 0)
    time.sleep(0.20)
    t = snap.grab_screen(VX, VY, VW, VH).raw
    user32.ShowWindow(app.hwnd_main, 5)
    time.sleep(0.12)
    return t


app = snap.App()
app.create_main()
if not app.hwnd_main:
    print("FAIL: 没有窗口")
    sys.exit(1)
W, H = app.MW, app.MH

print("屏幕 %dx%d  面板 %dx%d" % (VW, VH, W, H))
print("能抓屏排除自己 = %s（build %s）"
      % (app._can_exclude_from_capture(), snap.win_build()))

try:
    ok = bool(user32.SetWindowDisplayAffinity(app.hwnd_main,
                                              snap.WDA_EXCLUDEFROMCAPTURE))
    print("SetWindowDisplayAffinity 直接调用 = %s" % ok)
    user32.SetWindowDisplayAffinity(app.hwnd_main, snap.WDA_NONE)
except Exception as e:
    print("SetWindowDisplayAffinity 异常 %r" % (e,))

# ---------- 1. 静置时抓屏成功率 ----------
# 面板放到一个亮暗都有内容的位置
user32.SetWindowPos(app.hwnd_main, None, 700, 380, 0, 0, 0x0001 | 0x0004)
time.sleep(0.25)

fail = 0
n_try = 40
t0 = time.perf_counter()
for i in range(n_try):
    raw = app._grab_glass_raw(W, H)
    if raw is None:
        fail += 1
    time.sleep(0.09)          # 复刻 GLASS_TICK_IDLE 的节拍
dt = (time.perf_counter() - t0) * 1000
print("\n[静置抓屏] %d 次里失败 %d 次（%.0f%%，每次约 %.1fms）"
      % (n_try, fail, 100.0 * fail / n_try, dt / n_try))

# ---------- 2. 退路（插值伪造）产出 vs 真实背景 ----------
spots = [("花瓣中间（亮）", 300, 300),
         ("花瓣边缘", 620, 380),
         ("右上暗角", 2150, 120),
         ("右下暗区", 2100, 1100)]
truth = snap_truth(app)
print("\n位置              坐标        真实亮度   正常路径   伪造退路   偏差")
for name, x, y in spots:
    user32.SetWindowPos(app.hwnd_main, None, x, y, 0, 0, 0x0001 | 0x0004)
    time.sleep(0.20)
    lt = truth_luma(truth, VW, x, y, W, H)

    good = app._live_glass_excluded(W, H)
    fake = app._live_glass_patched(W, H)
    lg = luma(good, W, H) if good else float("nan")
    lf = luma(fake, W, H) if fake else float("nan")
    print("  %-14s (%4d,%4d)  %6.1f  %8.1f  %8.1f  %+7.1f"
          % (name, x, y, lt, lg, lf, (lf - lt) if fake else float("nan")))

# ---------- 3. 空闲一拍真的被触发时会怎样 ----------
print("\n[空闲一拍] 强制让抓屏失败两次，看主线程会贴出什么：")
user32.SetWindowPos(app.hwnd_main, None, 700, 380, 0, 0, 0x0001 | 0x0004)
time.sleep(0.2)
lt = truth_luma(truth, VW, 700, 380, W, H)
real_grab = app._grab_without_self
app._grab_without_self = lambda *a, **k: None     # 模拟两次都抓到自己
app._can_exclude_from_capture = lambda: True
try:
    app._refresh_glass_live()
    lg = luma(app.mn_glass, W, H) if app.mn_glass else float("nan")
    print("  真实背景 %.1f   贴上去的 %.1f   偏差 %+.1f"
          % (lt, lg, lg - lt))
finally:
    app._grab_without_self = real_grab

user32.DestroyWindow(app.hwnd_main)
