"""对照实验：只改「快照余量」这一个变量，看补抓和卡顿是不是跟着它变。

用法：python -u tools/audit_pad_sweep.py

不修改产品代码：只在测试进程里改模块常量 `snap.DRAG_PAD`（预取线程启动时读它），
其余逻辑一行不动。每个余量跑同一段 5000px/s 的全屏往返路径。

要看的三件事：
  补抓次数   主线程当场抓屏的次数 —— 这是唯一会让一帧多花 15~45ms 的地方
  单帧耗时   中位 / 90分位 / 最大 —— 「跟不跟手」的直接指标
  背景偏差   每一帧玻璃底 vs 该位置真实桌面 —— 防止「变快的同时变错」
  取快照周期 后台线程一轮多久（抓 + 整块模糊）—— 余量变大后它会不会变慢到失效
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

VX = user32.GetSystemMetrics(snap.SM_XVIRTUALSCREEN)
VY = user32.GetSystemMetrics(snap.SM_YVIRTUALSCREEN)
VW = user32.GetSystemMetrics(snap.SM_CXVIRTUALSCREEN)
VH = user32.GetSystemMetrics(snap.SM_CYVIRTUALSCREEN)

rows = []
cur = {"on": False, "i": -1}
grabs = []
rounds = []

_real_present = snap.App._present_main
_real_now = snap.App._grab_behind_now
_real_store = snap.App._store_drag_world
_last_store = [0.0]


def _wrap_present(self, dst=None):
    r = _real_present(self, dst)
    if cur["on"] and self.drag_last_pos:
        rows.append((cur["i"], self.drag_last_pos, self.mn_glass))
    return r


def _wrap_now(self, *a, **kw):
    t = time.perf_counter()
    r = _real_now(self, *a, **kw)
    if cur["on"]:
        grabs.append(((time.perf_counter() - t) * 1000, r is None))
    return r


def _wrap_store(self, *a, **kw):
    r = _real_store(self, *a, **kw)
    if cur["on"]:
        now = time.perf_counter()
        if _last_store[0]:
            rounds.append((now - _last_store[0]) * 1000)
        _last_store[0] = now
    return r


snap.App._present_main = _wrap_present
snap.App._grab_behind_now = _wrap_now
snap.App._store_drag_world = _wrap_store


def _luma(g, W, H, step=3):
    tot = n = 0
    st = W * 4
    for y in range(0, H, step):
        row = g[y * st:y * st + st]
        for k in range(0, len(row) - 3, 4 * step):
            tot += row[k] + row[k + 1] + row[k + 2]
            n += 3
    return tot / float(n) if n else 0.0


def _truth_luma(truth, left, top, W, H, step=3):
    x0, y0 = left - VX, top - VY
    tot = n = 0
    st = VW * 4
    for y in range(y0, y0 + H, step):
        if y < 0 or y >= VH:
            continue
        row = truth[y * st:y * st + st]
        for x in range(x0, x0 + W, step):
            if x < 0 or x >= VW:
                continue
            k = x * 4
            tot += row[k] + row[k + 1] + row[k + 2]
            n += 3
    return tot / float(n) if n else 0.0


def _fake_start(app, left, top):
    app._stop_glass_live()
    app.drag_live = app._excl_acquire("drag")
    if app.drag_live:
        try:
            snap.dwmapi.DwmFlush()
        except Exception:
            pass
    else:
        app._begin_drag_glass()
        if not app.drag_active:
            return False
    app.main_drag = True
    app.drag_last_pos = None
    app.drag_world = None
    app.drag_target = (left, top)
    app.main_drag_off = (0, 0)
    if app.drag_live:
        app._grab_behind_now(left, top)
        app._start_drag_prefetch()
    return True


def measure(pad, step, gap, label):
    snap.DRAG_PAD = pad
    app = snap.App()
    app.create_main()
    if not app.hwnd_main:
        return
    W, H = app.MW, app.MH

    user32.ShowWindow(app.hwnd_main, 0)
    time.sleep(0.18)
    truth = snap.grab_screen(VX, VY, VW, VH).raw
    user32.ShowWindow(app.hwnd_main, 5)
    time.sleep(0.12)

    n = (VW - W) // step
    path = [(i * step, 400) for i in range(1, n + 1)]
    path += [(i * step, 400) for i in range(n - 1, -1, -1)]

    user32.SetWindowPos(app.hwnd_main, None, 0, 400, 0, 0, 0x0001 | 0x0004)
    time.sleep(0.05)
    rows.clear()
    grabs.clear()
    rounds.clear()
    _last_store[0] = 0.0
    cur["on"] = False
    if not _fake_start(app, path[0][0], path[0][1]):
        print("FAIL 进不了拖动")
        user32.DestroyWindow(app.hwnd_main)
        return
    time.sleep(0.12)

    cur["on"] = True
    cost = []
    for i, (px, py) in enumerate(path):
        cur["i"] = i
        t0 = time.perf_counter()
        app._drag_frame(px, py)
        cost.append((time.perf_counter() - t0) * 1000)
        time.sleep(gap)
    cur["on"] = False
    app._end_main_drag()
    user32.DestroyWindow(app.hwnd_main)

    devs = []
    for i, pos, g in rows:
        if not g:
            continue
        devs.append(_luma(g, W, H) - _truth_luma(truth, pos[0], pos[1], W, H))
    cs = sorted(cost)
    devs.sort()
    ms = sorted(x[0] for x in grabs)
    rd = sorted(rounds)
    print("  %-16s 补抓 %3d/%3d  单帧 %5.1f/%5.1f/%6.1fms  偏差 %+5.1f~%+5.1f  一轮 %4.0fms"
          % (label, len(grabs), len(path),
             cs[len(cs) // 2], cs[min(len(cs) - 1, int(len(cs) * 0.9))], cs[-1],
             devs[0] if devs else 0, devs[-1] if devs else 0,
             rd[len(rd) // 2] if rd else 0))
    if grabs:
        print("  %-16s 补抓耗时 中位 %.1fms 最大 %.1fms"
              % ("", ms[len(ms) // 2], ms[-1]))


print("屏幕 %dx%d  BLUR_F=%d  路径：横向全屏往返" % (VW, VH, snap.App.BLUR_F))
print("\n5000px/s（40px/帧，8ms 一帧，模拟快速左右拖）")
for pad in (64, 96, 160, 208, 260):
    measure(pad, 40, 0.008, "pad=%d" % pad)

print("\n2500px/s 对照（20px/帧）")
measure(96, 20, 0.008, "pad=96")
measure(208, 20, 0.008, "pad=208")
