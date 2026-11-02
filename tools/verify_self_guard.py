"""验证「抓到面板自己」时新判据能不能拦住。

做法：拖动到一半，故意把「抓屏排除自己」强制关掉，让后台线程必定抓到面板
本身。分成两组对照：

  对照  把 _looks_like_me 打成恒 False（= 改动前的行为）
  实验  用真实判据

量的是「面板显示出来的平均亮度」：正常背景约 35，被自己的画面污染后会掉到
20 以下。实验组不往下掉，就说明拦住了。

用法：python -u tools/verify_self_guard.py
"""
import sys
import os
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
try:
    snap.winmm.timeBeginPeriod(1)
except Exception:
    pass

WDA_NONE = 0

lum_log = []
cur = {"on": False}
_real_present = snap.App._present_main


def _luma(g):
    tot = n = 0
    for i in range(0, len(g) - 3, 52):
        tot += g[i] + g[i + 1] + g[i + 2]
        n += 3
    return tot / float(n) if n else 0.0


def _rec(self, dst=None):
    if cur["on"] and self.mn_glass:
        lum_log.append(_luma(self.mn_glass))
    return _real_present(self, dst)


snap.App._present_main = _rec

_real_me = snap.App._looks_like_me
_off_me = lambda self, buf, ww, hh: False       # noqa: E731  —— 模拟改动前


def _fake_start_drag(app, left, top):
    app._stop_glass_live()
    app.drag_live = app._excl_acquire("drag")
    if app.drag_live:
        try:
            snap.dwmapi.DwmFlush()
        except Exception:
            pass
    app.main_drag = True
    app.drag_last_pos = None
    app.drag_world = None
    app.drag_target = (left, top)
    app.main_drag_off = (0, 0)
    if app.drag_live:
        app._grab_behind_now(left, top)
        app._start_drag_prefetch()


def run(name, guard):
    snap.App._looks_like_me = guard
    app = snap.App()
    app.create_main()
    if not app.hwnd_main:
        print("FAIL: 没有窗口")
        return None
    x0, y0 = 300, 320
    user32.SetWindowPos(app.hwnd_main, None, x0, y0, 0, 0, 0x0001 | 0x0004)
    time.sleep(0.06)
    _fake_start_drag(app, x0, y0)
    time.sleep(0.10)

    # 先正常拖 6 帧，拿一个「干净背景」的基准
    cur["on"] = True
    lum_log.clear()
    for i in range(6):
        app._drag_frame(x0 + (i + 1) * 20, y0)
        time.sleep(0.012)
    clean = list(lum_log)
    lum_log.clear()

    # 强行关掉排除属性：这一下之后后台抓到的必然是面板自己
    try:
        user32.SetWindowDisplayAffinity(app.hwnd_main, WDA_NONE)
        app._excl_owner = None
    except Exception as e:
        print("  关属性失败: %r" % (e,))
    for i in range(40):
        app._drag_frame(x0 + 140 + (i + 1) * 15, y0)
        time.sleep(0.012)
    dirty = list(lum_log)
    cur["on"] = False
    app._end_main_drag()
    user32.DestroyWindow(app.hwnd_main)

    if not clean or not dirty:
        print("  %s: 没采到数据" % name)
        return None
    c_min, d_min = min(clean), min(dirty)
    d_mean = sum(dirty) / len(dirty)
    print("\n=== %s ===" % name)
    print("  正常背景亮度   中位 %.1f  最小 %.1f  （%d 帧）"
          % (sorted(clean)[len(clean) // 2], c_min, len(clean)))
    print("  属性关掉后     中位 %.1f  最小 %.1f  （%d 帧）"
          % (sorted(dirty)[len(dirty) // 2], d_min, len(dirty)))
    print("  最低亮度差 %.1f  %s" % (c_min - d_min,
                                   "掉下去了 → 被自己的画面污染" if c_min - d_min > 8
                                   else "没掉 → 拦住了"))
    return c_min - d_min


print("屏 %dx%d" % (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)))
gap_off = run("对照：改动前的判据", _off_me)
gap_on = run("实验：新指纹判据", _real_me)

print("\n结论：改动前最低亮度掉 %.1f，改动后掉 %.1f" % (gap_off or 0, gap_on or 0))
