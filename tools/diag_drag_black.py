"""真机诊断：拖动时每帧背景的亮度 / 一致性 / 补抓次数。

用法：python -u tools/diag_drag_black.py

不看「程序跑得快不快」，只看「贴上去的那张背景图对不对」：

  亮度    整块玻璃底的平均亮度。某一帧突然掉一大截 → 就是眼睛看到的「黑一下」。
  残差    相邻两帧在**重叠的屏幕区域**上逐像素比。背景如果只是平移，残差应
          该接近 0；残差大变说明这一帧换了一张不一样的图（相位变了 / 抓到
          面板自己 / 抓到了别处）。
  补抓    这一帧是否走了主线程同步抓屏。走到就是那一下「慢一拍」。

不碰真实鼠标：直接给 _drag_frame 喂坐标，窗口由 UpdateLayeredWindow 搬。
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
try:
    snap.winmm.timeBeginPeriod(1)
except Exception:
    pass

SW, SH = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)

frames = []          # (t, left, top, glass)
grabs = []           # (frame_idx, ms)
cur = {"i": -1, "enabled": False}

_real_present = snap.App._present_main
_real_now = snap.App._grab_behind_now


def _rec_present(self, dst=None):
    pos = self.drag_last_pos
    if cur["enabled"] and pos:
        frames.append((time.perf_counter(), pos[0], pos[1], self.mn_glass))
    return _real_present(self, dst)


def _timed_now(self, *a, **kw):
    t = time.perf_counter()
    r = _real_now(self, *a, **kw)
    if cur["enabled"]:
        grabs.append((cur["i"], (time.perf_counter() - t) * 1000, r is None))
    return r


snap.App._present_main = _rec_present
snap.App._grab_behind_now = _timed_now


def _luma(g):
    """平均亮度（每 13 个像素取一个）"""
    tot = n = 0
    for i in range(0, len(g) - 3, 52):
        tot += g[i] + g[i + 1] + g[i + 2]
        n += 3
    return tot / float(n) if n else 0.0


def _diff(g1, x1, y1, g2, x2, y2, WW, HH):
    """两帧在重叠屏幕区域上的平均绝对差（每 2 像素取一个）"""
    ax0, ay0 = max(x1, x2), max(y1, y2)
    ax1, ay1 = min(x1 + WW, x2 + WW), min(y1 + HH, y2 + HH)
    if ax1 - ax0 < 30 or ay1 - ay0 < 20:
        return None
    span = (ax1 - ax0) * 4
    tot = n = 0
    for y in range(ay0, ay1, 2):
        o1 = ((y - y1) * WW + (ax0 - x1)) * 4
        o2 = ((y - y2) * WW + (ax0 - x2)) * 4
        r1 = g1[o1:o1 + span]
        r2 = g2[o2:o2 + span]
        for k in range(0, span, 8):
            tot += (abs(r1[k] - r2[k]) + abs(r1[k + 1] - r2[k + 1])
                    + abs(r1[k + 2] - r2[k + 2]))
            n += 3
    return tot / float(n) if n else None


def _fake_start_drag(app, left, top):
    """复刻 _start_main_drag 的取背景流程，但不读鼠标、不 SetCapture"""
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


def run(name, step, gap, n, back=True, axis="x", x0=300, y0=320, speed=None):
    app = snap.App()
    app.create_main()
    if not app.hwnd_main:
        print("FAIL: 没有窗口")
        return
    WW, HH = app.MW, app.MH

    user32.SetWindowPos(app.hwnd_main, None, x0, y0, 0, 0, 0x0001 | 0x0004)
    time.sleep(0.05)

    frames.clear()
    grabs.clear()
    cur["enabled"] = False
    if not _fake_start_drag(app, x0, y0):
        print("FAIL: 进不了拖动（drag_live=%s）" % app.drag_live)
        user32.DestroyWindow(app.hwnd_main)
        return
    time.sleep(0.10)

    # 先正后反，模拟真实来回拖
    path = []
    for i in range(n):
        d = (i + 1) * step
        path.append((x0 + d, y0) if axis == "x" else (x0, y0 + d))
    if back:
        for i in range(n):
            d = (n - i) * step
            path.append((x0 + d, y0) if axis == "x" else (x0, y0 + d))

    cur["enabled"] = True
    cost = []
    for i, (px, py) in enumerate(path):
        cur["i"] = i
        t0 = time.perf_counter()
        app._drag_frame(px, py)
        cost.append((time.perf_counter() - t0) * 1000)
        time.sleep(gap)
    cur["enabled"] = False
    app._end_main_drag()
    user32.DestroyWindow(app.hwnd_main)

    # ---- 分析 ----
    print("\n=== %s ===" % name)
    print("  尺寸 %dx%d  pad=%d  提交 %d 帧  补抓 %d 次  起点(%d,%d)"
          % (WW, HH, snap.DRAG_PAD, len(frames), len(grabs), x0, y0))

    lum = [_luma(f[3]) for f in frames]
    base = sorted(lum)[len(lum) // 2] if lum else 0
    print("  亮度 中位 %.1f  最小 %.1f  最大 %.1f" % (base, min(lum), max(lum)))

    prev = None
    black = []
    rough = []
    for i, (t, lx, ly, g) in enumerate(frames):
        d = None if prev is None else _diff(prev[3], prev[1], prev[2], g, lx, ly, WW, HH)
        if lum[i] < base - 12:
            black.append(i)
        if d is not None and d > 3.0:
            rough.append((i, d))
        prev = (t, lx, ly, g)

    # 帧间隔：这才是「跟不跟手」的指标
    ts = [f[0] for f in frames]
    iv = sorted((ts[i] - ts[i - 1]) * 1000 for i in range(1, len(ts)))
    if iv:
        m = len(iv)
        print("  提交间隔 中位 %.2fms  90分位 %.2fms  最大 %.2fms"
              % (iv[m // 2], iv[int(m * 0.9)], iv[-1]))
    cs = sorted(cost)
    if cs:
        print("  单帧处理 中位 %.2fms  90分位 %.2fms  最大 %.2fms"
              % (cs[len(cs) // 2], cs[int(len(cs) * 0.9)], cs[-1]))
    if black:
        print("  !! 变暗的帧：%s  亮度 %s"
              % (black[:12], ["%.0f" % lum[i] for i in range(len(lum)) if i in black][:12]))
    if rough:
        print("  !! 背景跳变的帧：%s" % [(i, round(d, 1)) for i, d in rough[:12]])
    if grabs:
        ms = sorted(x[1] for x in grabs)
        print("  补抓耗时 中位 %.1fms  最大 %.1fms  发生在帧 %s"
              % (ms[len(ms) // 2], ms[-1], [x[0] for x in grabs][:14]))


print("屏幕 %dx%d  DRAG_PAD=%d  BLUR_F=%d" % (SW, SH, snap.DRAG_PAD, snap.App.BLUR_F))
# 8ms 一个鼠标消息（125Hz 鼠标）：step=20 → 2500px/s
run("横向 125Hz 2500px/s", 20, 0.008, 26)
# 1ms 一个鼠标消息（1000Hz 游戏鼠标）：step=3 → 3000px/s
run("横向 1000Hz 3000px/s", 3, 0.001, 60)
# 贴左边缘：从 380 一路拖到窗口一半出屏
run("横向 贴左边缘", 20, 0.008, 20, back=False, x0=380)
# 纵向对照
run("纵向 125Hz 2500px/s", 20, 0.008, 10, back=True, axis="y", x0=300, y0=260)
