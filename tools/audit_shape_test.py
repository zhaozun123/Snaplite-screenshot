"""验证「余量形状」而不是「余量大小」才是关键。

用法：python -u tools/audit_shape_test.py

前一组对照实验的结论：
    pad 越大 → 补抓越少，但单帧越慢（模糊面积变大、抢主线程）
    64  → 54/110 补抓，单帧 9.9ms
    96  → 10/110，4.0ms
    160 →  0/110，9.8ms
    208 →  0/110，14.3ms

说明卡主线程的不是「余量」而是「整块模糊的面积」。面板是 340×148 的横条，
横向余量才是快拖需要的，纵向余量大部分是浪费。所以这一组比较：

    A 现状        96 × 96    面积 199k
    B 方块大余量  160×160    面积 353k
    C 横长条      192×48     面积 193k   ← 面积和现状一样，横向余量翻倍
    D 横长条+低优先级  C 再给预取线程设 BELOW_NORMAL

只看三件事：补抓次数、单帧耗时（中位/最大）、取快照一轮多久。
"""
import sys
import os
import time
import ctypes
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

THREAD_PRIORITY_BELOW_NORMAL = -1
_k32 = ctypes.windll.kernel32

grabs = []
rounds = []
cur = {"on": False, "low": False}
_last = [0.0]

_rn = snap.App._grab_behind_now
_rs = snap.App._store_drag_world
_rp = snap.App._present_main


def _wn(self, *a, **kw):
    t = time.perf_counter()
    r = _rn(self, *a, **kw)
    if cur["on"]:
        grabs.append((time.perf_counter() - t) * 1000)
    return r


def _ws(self, *a, **kw):
    r = _rs(self, *a, **kw)
    if cur["on"]:
        now = time.perf_counter()
        if _last[0]:
            rounds.append((now - _last[0]) * 1000)
        _last[0] = now
        if cur["low"]:
            # 预取线程自降优先级：主线程要画帧时先让它
            try:
                _k32.SetThreadPriority(_k32.GetCurrentThread(),
                                       THREAD_PRIORITY_BELOW_NORMAL)
            except Exception:
                pass
    return r


snap.App._grab_behind_now = _wn
snap.App._store_drag_world = _ws


def _tile(need, limit, f):
    n = (need + f - 1) // f * f
    return max(f, min(n, limit // f * f))


def make_rect(px, py):
    """造一个取景函数：水平余量 px、垂直余量 py（都取 BLUR_F 整数倍）"""
    def rect(self, left, top, pad):
        f = self.BLUR_F
        W, H = self.MW, self.MH
        vx, vy, vw, vh = (user32.GetSystemMetrics(snap.SM_XVIRTUALSCREEN),
                          user32.GetSystemMetrics(snap.SM_YVIRTUALSCREEN),
                          user32.GetSystemMetrics(snap.SM_CXVIRTUALSCREEN),
                          user32.GetSystemMetrics(snap.SM_CYVIRTUALSCREEN))
        gw = _tile(W + px * 2 + f, vw, f)
        gh = _tile(H + py * 2 + f, vh, f)
        gx = (left - px) // f * f
        gy = (top - py) // f * f
        gx = max(vx, min(gx, vx + vw - gw))
        gy = max(vy, min(gy, vy + vh - gh))
        return gx, gy, gw, gh
    return rect


def run(label, px, py, low, step=40, gap=0.008, axis="x"):
    snap.App._drag_world_rect = make_rect(px, py)
    snap.DRAG_PAD = max(px, py)      # 只在首帧/兜底补抓里用到
    app = snap.App()
    app.create_main()
    W, H = app.MW, app.MH

    user32.ShowWindow(app.hwnd_main, 0)
    time.sleep(0.15)
    user32.ShowWindow(app.hwnd_main, 5)
    time.sleep(0.10)

    if axis == "x":
        n = (VW - W) // step
        path = [(i * step, 400) for i in range(1, n + 1)]
        path += [(i * step, 400) for i in range(n - 1, -1, -1)]
        x0, y0 = 0, 400
    else:
        n = (VH - H - 400) // step
        path = [(600, 400 + i * step) for i in range(1, n + 1)]
        path += [(600, 400 + i * step) for i in range(n - 1, -1, -1)]
        x0, y0 = 600, 400

    user32.SetWindowPos(app.hwnd_main, None, x0, y0, 0, 0, 0x0001 | 0x0004)
    time.sleep(0.05)
    grabs.clear()
    rounds.clear()
    _last[0] = 0.0
    cur["on"] = False
    cur["low"] = False

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
    app.drag_target = path[0]
    app.main_drag_off = (0, 0)
    if app.drag_live:
        app._grab_behind_now(path[0][0], path[0][1])
        app._start_drag_prefetch()
    time.sleep(0.12)

    gx, gy, gw, gh = app._drag_world_rect(400, 400, 0)
    cur["on"] = True
    cur["low"] = low
    cost = []
    for i, (p, q) in enumerate(path):
        t0 = time.perf_counter()
        app._drag_frame(p, q)
        cost.append((time.perf_counter() - t0) * 1000)
        time.sleep(gap)
    cur["on"] = False
    app._end_main_drag()
    user32.DestroyWindow(app.hwnd_main)

    cs = sorted(cost)
    ms = sorted(grabs)
    rd = sorted(rounds)
    print("  %-22s 快照 %dx%d(%dk)  补抓 %3d/%3d  单帧 %5.1f/%5.1f/%6.1fms  一轮 %4.0fms%s"
          % (label, gw, gh, gw * gh // 1000, len(grabs), len(path),
             cs[len(cs) // 2], cs[min(len(cs) - 1, int(len(cs) * 0.9))], cs[-1],
             rd[len(rd) // 2] if rd else 0,
             ("  补抓中位%.0fms" % ms[len(ms) // 2]) if ms else ""))


print("屏幕 %dx%d  面板 %dx148  路径 5000px/s 全屏往返" % (VW, VH, 340))
print("基准（现状）：")
run("A 96x96 pad=96", 96, 96, False)
print("加面积：")
run("B 160x160", 160, 160, False)
print("同面积改形状（横向余量翻倍）：")
run("C 208x26", 208, 26, False)
run("C2 192x48", 192, 48, False)
run("C3 234x13", 234, 13, False)
print("同形状 + 预取线程降优先级：")
run("D 208x26 低优先", 208, 26, True)
print("纵向 5000px/s（形状必须跟着方向转）：")
run("方余量 96x96", 96, 96, True, axis="y")
run("竖长条 13x234", 13, 234, True, axis="y")
