"""极端甩动下还有多少余量：从 5000 一路加到 16000px/s，看补抓从哪一档开始出现。

    python -u tools/audit_speed_sweep.py

补抓 = 主线程当场抓屏（一次 15~45ms），出现就意味着这一帧「顿一下」。
目标是「用户能甩到的速度都不补抓」，所以看的是拐点在哪，不看单次抖动。
"""
import sys
import time

sys.path.insert(0, ".")
import snap
from snap import user32, w

SW = user32.GetSystemMetrics(0)
SH = user32.GetSystemMetrics(1)
VW = user32.GetSystemMetrics(snap.SM_CXVIRTUALSCREEN)
VH = user32.GetSystemMetrics(snap.SM_CYVIRTUALSCREEN)

frames = []
grabs = []
cur = {"on": False}


def _hook():
    orig_grab = snap.App._grab_behind_now

    def wrapped(self, left, top, store=None, pad=None):
        t = time.perf_counter()
        r = orig_grab(self, left, top, store, pad)
        if cur["on"]:
            grabs.append((time.perf_counter() - t) * 1000.0)
        return r

    snap.App._grab_behind_now = wrapped

    orig_frame = snap.App._drag_frame

    def frame_wrapped(self, left, top):
        t = time.perf_counter()
        ok = orig_frame(self, left, top)
        if cur["on"] and ok:
            frames.append((time.perf_counter() - t) * 1000.0)
        return ok

    snap.App._drag_frame = frame_wrapped


def run(label, step, gap=0.008):
    app = snap.App()
    app.create_main()
    if not app.hwnd_main:
        print("FAIL: 没有窗口")
        return
    W, H = app.MW, app.MH

    x0, y0 = 60, 500
    user32.ShowWindow(app.hwnd_main, 0)
    time.sleep(0.12)
    user32.ShowWindow(app.hwnd_main, 5)
    time.sleep(0.10)
    user32.SetWindowPos(app.hwnd_main, None, x0, y0, 0, 0, 0x0001 | 0x0004)
    time.sleep(0.05)

    frames.clear()
    grabs.clear()
    cur["on"] = False
    # 直接进拖动：抓屏排除自己 + 后台预取，和真实按下拖动走同一条路
    snap.App._start_main_drag(app)
    if not app.main_drag:
        print("FAIL: 进不了拖动")
        user32.DestroyWindow(app.hwnd_main)
        return
    time.sleep(0.15)

    n = (VW - W - 100) // step
    n = max(1, min(n, 40))
    path = [(x0 + i * step, y0) for i in range(1, n + 1)]
    path += [(x0 + i * step, y0) for i in range(n - 1, -1, -1)]

    cur["on"] = True
    for px, py in path:
        app._drag_frame(px, py)
        time.sleep(gap)
    cur["on"] = False
    app._end_main_drag()
    user32.DestroyWindow(app.hwnd_main)

    ms = sorted(frames)
    gs = sorted(grabs)
    print("  %-18s %5d px/s   帧 %3d   补抓 %2d  中位 %5.2fms  90分位 %5.2fms  "
          "最大 %5.2fms   补抓最贵 %s"
          % (label, step * 125, len(frames), len(grabs),
             ms[len(ms) // 2] if ms else 0,
             ms[int(len(ms) * 0.9)] if ms else 0,
             ms[-1] if ms else 0,
             "%.1fms" % gs[-1] if gs else "-"))


_hook()
print("屏幕 %dx%d  面板 340x148  DRAG_AREA=%d  LEAN_MAX=%.2f"
      % (SW, SH, snap.DRAG_AREA, snap.DRAG_LEAN_MAX))
for label, step in [("5000px/s", 40), ("8000px/s", 64), ("12000px/s", 96),
                    ("16000px/s", 128)]:
    run(label, step)
