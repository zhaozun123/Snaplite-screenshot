"""真机诊断：拖动时每帧的间隔、耗时、补抓次数（横向 vs 纵向对照）。

用法：python -u tools/diag_drag_pace.py

指标怎么读：
  帧间隔  两次上屏之间的真实时间。人眼对「忽长忽短」敏感，对「一直 8ms」不敏感。
  补抓    主线程同步抓屏 + 模糊，一次 10~20ms，是间隔尖峰的头号来源。
"""
import sys
import os
import time
from ctypes import byref, c_void_p

sys.stdout.reconfigure(line_buffering=True)
if "--fast-switch" in sys.argv:
    sys.setswitchinterval(0.001)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import snap
from snap import user32, w

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass
try:
    snap.winmm.timeBeginPeriod(1)
except Exception:
    pass

SW, SH = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
stats = {"now": 0, "blit": [], "at": [], "idx": 0, "pres": [], "store": []}
_real_now = snap.App._grab_behind_now
_real_blit = snap.App._blit_main
_real_present = snap.App._present_main
_real_store = snap.App._store_drag_world
FLUSH = {"on": False}


def _rec_store(self, *a, **kw):
    t = time.perf_counter()
    stats["store"].append(t)
    return _real_store(self, *a, **kw)


def _counted_now(self, *a, **kw):
    stats["now"] += 1
    stats["at"].append(stats["idx"])
    return _real_now(self, *a, **kw)


def _timed_blit(self, dst=None):
    if FLUSH["on"]:
        # 等一次屏幕消隐再提交：把提交时刻钉在刷新节拍上
        try:
            snap.dwmapi.DwmFlush()
        except Exception:
            pass
    t = time.perf_counter()
    try:
        return _real_blit(self, dst)
    finally:
        stats["blit"].append((time.perf_counter() - t) * 1000)


def _rec_present(self, dst=None):
    pos = self.drag_last_pos
    r = _real_present(self, dst)
    stats["pres"].append((time.perf_counter(), pos))
    return r


snap.App._grab_behind_now = _counted_now
snap.App._blit_main = _timed_blit
snap.App._present_main = _rec_present
snap.App._store_drag_world = _rec_store


def run(name, step, gap, n, direction, start=(200, 300), flush=False):
    FLUSH["on"] = flush
    app = snap.App()
    app.create_main()
    if not app.hwnd_main:
        print("FAIL: 没有窗口")
        return None
    user32.SetWindowPos(app.hwnd_main, None, 120, 260, 0, 0, 0x0001 | 0x0004)
    user32.SetCursorPos(*start)
    time.sleep(0.03)
    stats["now"] = 0
    stats["blit"] = []
    stats["at"] = []
    stats["pres"] = []
    stats["store"] = []
    app._start_main_drag()
    time.sleep(0.08)

    stamps = []
    prev = time.perf_counter()
    for i in range(n):
        stats["idx"] = i
        x = start[0] + (i * step if direction == "x" else 0)
        y = start[1] + (i * step if direction == "y" else 0)
        user32.SetCursorPos(x, y)
        t0 = time.perf_counter()
        app._move_main_drag()
        dur = (time.perf_counter() - t0) * 1000
        stamps.append((t0 - prev, dur))
        prev = t0
        time.sleep(gap)

    app._end_main_drag()
    user32.DestroyWindow(app.hwnd_main)

    iv = [s[0] * 1000 for s in stamps[1:]]
    du = [s[1] for s in stamps]
    iv_s, du_s = sorted(iv), sorted(du)
    m = len(iv_s)
    print("\n%s" % name)
    print("  帧间隔 ms   中位 %5.2f  最小 %5.2f  最大 %7.2f  90分位 %5.2f" % (
        iv_s[m // 2], iv_s[0], iv_s[-1], iv_s[int(m * 0.9)]))
    print("  处理耗时 ms 中位 %5.2f  最大 %6.2f" % (
        du_s[len(du_s) // 2], du_s[-1]))
    print("  主线程补抓 %d/%d 次  位置 %s" % (stats["now"], n, stats["at"][:14]))
    over = [i for i, v in enumerate(iv) if v > 25]
    print("  间隔>25ms 的帧 %d 个  位置 %s" % (len(over), over[:14]))
    if m > 3:
        jit = sum(abs(iv[i + 1] - iv[i]) for i in range(len(iv) - 1)) / (len(iv) - 1)
        print("  相邻间隔平均跳变 %.2f ms" % jit)
    bl = sorted(stats["blit"])
    if bl:
        k = len(bl)
        print("  上屏耗时 ms 中位 %5.2f  最小 %5.2f  最大 %6.2f  90分位 %5.2f" % (
            bl[k // 2], bl[0], bl[-1], bl[int(k * 0.9)]))
    st = stats["store"]
    if len(st) > 3:
        gaps = [(st[i] - st[i - 1]) * 1000 for i in range(1, len(st))]
        gs = sorted(gaps)
        print("  后台刷新 %d 次  周期中位 %5.2f ms  最大 %6.2f" % (
            len(st), gs[len(gs) // 2], gs[-1]))

    # 提交节奏：这才是人眼看到的「匀不匀」。位置增量 / 时间增量 = 瞬时速度，
    # 速度序列的变异系数越小，画面滑得越匀。
    uniq = []
    for t, p in stats["pres"]:
        if not p:
            continue
        if uniq and p == uniq[-1][1]:
            continue
        uniq.append((t, p))
    dts, sps = [], []
    for i in range(1, len(uniq)):
        dt = (uniq[i][0] - uniq[i - 1][0]) * 1000
        d = abs(uniq[i][1][0] - uniq[i - 1][1][0]) if direction == "x" \
            else abs(uniq[i][1][1] - uniq[i - 1][1][1])
        if dt > 0.05 and d > 0:
            dts.append(dt)
            sps.append(d / dt)
    if len(dts) > 4:
        def _cv(v):
            m = sum(v) / len(v)
            return (sum((x - m) ** 2 for x in v) / len(v)) ** 0.5 / m * 100

        print("  提交 %d 帧  提交间隔中位 %5.2f ms  CV %4.1f%%   瞬时速度 CV %4.1f%%"
              % (len(dts) + 1, sorted(dts)[len(dts) // 2], _cv(dts), _cv(sps)))
    FLUSH["on"] = False
    return iv


print("屏幕 %dx%d  DRAG_PAD=%d  BLUR_F=%d  流水线=%s" % (
    SW, SH, snap.DRAG_PAD, snap.App.BLUR_F, snap.DRAG_PIPELINE))
# 鼠标消息约 8ms 一个（125Hz 档）；每帧位移 = 速度 × 8ms
for _pad in (96, 112, 128, 144, 160):
    snap.DRAG_PAD = _pad
    run("横向 5000px/s  pad=%d" % _pad, 40, 0.008, 50, "x")
snap.DRAG_PAD = 96
run("横向 2500px/s  pad=96", 20, 0.008, 50, "x")
for _pad in (128, 160):
    snap.DRAG_PAD = _pad
    run("横向 2500px/s  pad=%d" % _pad, 20, 0.008, 50, "x")
snap.DRAG_PAD = 96

snap.App._grab_behind_now = _real_now
snap.App._blit_main = _real_blit
snap.App._present_main = _real_present
