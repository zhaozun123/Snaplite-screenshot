"""把「玻璃底和真实桌面不一致」的那些帧逐帧揪出来。

用法：python -u tools/audit_truth_frames.py

上一轮出现 −36 / +43 / +50 的偏差帧，必须看清它们是：
  · 抓到了面板自己（会变亮 +36 左右）
  · 还是快照没覆盖、贴了别处的画面（可能变暗也可能变亮）
  · 还是只发生在路径两端（贴屏幕边）

逐帧打印：帧号、坐标、玻璃底亮度、该位置真实亮度、这一帧是否补抓、
抓屏矩形、窗口在快照里的偏移，以及预取线程最近一次换片的时刻。
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
stores = []

_rp = snap.App._present_main
_rn = snap.App._grab_behind_now
_rs = snap.App._store_drag_world


def _wp(self, dst=None):
    r = _rp(self, dst)
    if cur["on"] and self.drag_last_pos:
        wd = self.drag_world
        rows.append((cur["i"], self.drag_last_pos, self.mn_glass,
                     (wd[0], wd[1], wd[2], wd[3]) if wd else None,
                     len(stores)))
    return r


def _wn(self, *a, **kw):
    t = time.perf_counter()
    r = _rn(self, *a, **kw)
    if cur["on"]:
        grabs.append((cur["i"], (time.perf_counter() - t) * 1000, r is None))
    return r


def _ws(self, raw, gx, gy, gw, gh, ox, oy):
    r = _rs(self, raw, gx, gy, gw, gh, ox, oy)
    if cur["on"] and r is not None:
        stores.append((cur["i"], gx, gy, gw, gh, ox, oy))
    return r


snap.App._present_main = _wp
snap.App._grab_behind_now = _wn
snap.App._store_drag_world = _ws


def luma(g, W, H, step=3):
    tot = n = 0
    st = W * 4
    for y in range(0, H, step):
        row = g[y * st:y * st + st]
        for k in range(0, len(row) - 3, 4 * step):
            tot += row[k] + row[k + 1] + row[k + 2]
            n += 3
    return tot / float(n) if n else 0.0


def truth_luma(t, left, top, W, H, step=3):
    x0, y0 = left - VX, top - VY
    tot = n = 0
    st = VW * 4
    for y in range(y0, y0 + H, step):
        if y < 0 or y >= VH:
            continue
        row = t[y * st:y * st + st]
        for x in range(x0, x0 + W, step):
            if x < 0 or x >= VW:
                continue
            k = x * 4
            tot += row[k] + row[k + 1] + row[k + 2]
            n += 3
    return tot / float(n) if n else 0.0


def run(pad, step, gap, label):
    snap.DRAG_PAD = pad
    app = snap.App()
    app.create_main()
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
    stores.clear()
    cur["on"] = False

    app._stop_glass_live()
    app.drag_live = app._excl_acquire("drag")
    if app.drag_live:
        try:
            snap.dwmapi.DwmFlush()
        except Exception:
            pass
    else:
        app._begin_drag_glass()
    app.main_drag = True
    app.drag_last_pos = None
    app.drag_world = None
    app.drag_target = path[0]
    app.main_drag_off = (0, 0)
    if app.drag_live:
        app._grab_behind_now(path[0][0], path[0][1])
        app._start_drag_prefetch()
    time.sleep(0.12)

    cur["on"] = True
    for i, (px, py) in enumerate(path):
        cur["i"] = i
        app._drag_frame(px, py)
        time.sleep(gap)
    cur["on"] = False
    app._end_main_drag()
    user32.DestroyWindow(app.hwnd_main)

    print("\n=== %s  补抓 %d/%d  换片 %d 次 ===" % (label, len(grabs), len(path), len(stores)))
    bad = 0
    for i, pos, g, wd, ns in rows:
        if not g:
            continue
        lg = luma(g, W, H)
        lt = truth_luma(truth, pos[0], pos[1], W, H)
        d = lg - lt
        if abs(d) > 8.0:
            bad += 1
            gs = [x for x in grabs if x[0] == i]
            print("  帧%3d (%4d,%4d) 玻璃%6.1f 真实%6.1f 偏差%+6.1f  快照%s 偏移%s 换片%d %s"
                  % (i, pos[0], pos[1], lg, lt, d,
                     ("(%d,%d,%d,%d)" % wd) if wd else "无",
                     ("(%d,%d)" % (pos[0] - wd[0], pos[1] - wd[1])) if wd else "-",
                     ns, "补抓%.1fms" % gs[0][1] if gs else ""))
    print("  偏差超过 ±8 的帧：%d / %d" % (bad, len(rows)))


print("屏幕 %dx%d" % (VW, VH))
run(96, 20, 0.008, "2500px/s pad=96")
run(96, 40, 0.008, "5000px/s pad=96")
