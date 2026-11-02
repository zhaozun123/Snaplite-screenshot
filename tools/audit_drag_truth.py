"""真机审计：每帧玻璃底 vs「那个位置背后的真实桌面」，逐帧对亮度。

用法：python -u tools/audit_drag_truth.py

为什么这样测才有结论：

  前几轮只看「这一帧亮不亮」，但壁纸本身有明有暗（右下角接近全黑），
  「变黑」到底是背景本来就黑、还是程序贴错了，前者永远说不清。

  这里先把窗口藏起来抓一整屏，当成「标准答案」；之后每贴一帧，就用同一
  个矩形去标准答案里取平均亮度。模糊（缩到 1/13 再放大）几乎不改变平均
  亮度，所以两个数应该始终相等。差得多 = 这一帧贴的不是那个位置的真实
  背景 = 眼睛看到的「黑一下」。

同时记录：这一帧是否被丢弃（窗口没跟手）、是否走了主线程补抓（卡一下）、
是否误判「抓到自己」、单帧耗时、帧间隔。

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
VX = user32.GetSystemMetrics(snap.SM_XVIRTUALSCREEN)
VY = user32.GetSystemMetrics(snap.SM_YVIRTUALSCREEN)
VW = user32.GetSystemMetrics(snap.SM_CXVIRTUALSCREEN)
VH = user32.GetSystemMetrics(snap.SM_CYVIRTUALSCREEN)

rows = []            # 每帧一条记录
cur = {"on": False, "i": -1}
grabs = []           # (帧号, 毫秒, 是否失败)
selfhit = []         # 帧号：判据认为「抓到自己」

_real_present = snap.App._present_main
_real_now = snap.App._grab_behind_now
_real_me = snap.App._looks_like_me


def _wrap_present(self, dst=None):
    r = _real_present(self, dst)
    if cur["on"]:
        pos = self.drag_last_pos
        # mn_bits 就是刚刚提交给系统的像素（已叠压暗层、已削圆角）
        rows.append([cur["i"], pos, self.mn_glass, self.mn_bits.value,
                     self.mn_size])
    return r


def _wrap_now(self, *a, **kw):
    t = time.perf_counter()
    r = _real_now(self, *a, **kw)
    if cur["on"]:
        grabs.append((cur["i"], (time.perf_counter() - t) * 1000, r is None))
    return r


def _wrap_me(self, buf, W, H):
    r = _real_me(self, buf, W, H)
    if cur["on"] and r:
        selfhit.append(cur["i"])
    return r


snap.App._present_main = _wrap_present
snap.App._grab_behind_now = _wrap_now
snap.App._looks_like_me = _wrap_me


def _mean_luma_buf(g, W, H, step=3):
    """一块 W×H BGRA 的平均亮度（隔 step 取样）"""
    tot = n = 0
    stride = W * 4
    for y in range(0, H, step):
        o = y * stride
        row = g[o:o + stride]
        for k in range(0, len(row) - 3, 4 * step):
            tot += row[k] + row[k + 1] + row[k + 2]
            n += 3
    return tot / float(n) if n else 0.0


def _truth_luma(truth, VWc, left, top, W, H, step=3):
    """在整屏标准答案里取 (left, top) 处 W×H 的平均亮度（屏幕坐标）"""
    x0 = left - VX
    y0 = top - VY
    tot = n = 0
    stride = VWc * 4
    for y in range(y0, y0 + H, step):
        if y < 0 or y >= VH:
            continue
        o = y * stride
        row = truth[o:o + stride]
        if not row:
            continue
        for x in range(x0, x0 + W, step):
            if x < 0 or x >= VWc:
                continue
            k = x * 4
            tot += row[k] + row[k + 1] + row[k + 2]
            n += 3
    return tot / float(n) if n else 0.0


def _fake_start(app, left, top):
    """复刻 _start_main_drag 取背景的那一段，但不读鼠标、不 SetCapture"""
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


def run(name, path, gap):
    app = snap.App()
    app.create_main()
    if not app.hwnd_main:
        print("FAIL: 没有窗口")
        return
    W, H = app.MW, app.MH

    # ---- 标准答案：把窗口藏起来，抓一整屏桌面 ----
    user32.ShowWindow(app.hwnd_main, 0)          # SW_HIDE
    time.sleep(0.20)
    truth = snap.grab_screen(VX, VY, VW, VH).raw
    user32.ShowWindow(app.hwnd_main, 5)          # SW_SHOW
    time.sleep(0.15)

    x0, y0 = path[0]
    user32.SetWindowPos(app.hwnd_main, None, x0, y0, 0, 0, 0x0001 | 0x0004)
    time.sleep(0.06)

    rows.clear()
    grabs.clear()
    selfhit.clear()
    cur["on"] = False
    if not _fake_start(app, x0, y0):
        print("FAIL: 进不了拖动（drag_live=%s）" % app.drag_live)
        user32.DestroyWindow(app.hwnd_main)
        return
    time.sleep(0.12)

    cur["on"] = True
    dropped = []
    cost = []
    last_pos = None
    for i, (px, py) in enumerate(path):
        cur["i"] = i
        t0 = time.perf_counter()
        ok = app._drag_frame(px, py)
        cost.append((time.perf_counter() - t0) * 1000)
        if not ok and (px, py) != last_pos:
            dropped.append((i, px, py))
        if ok:
            last_pos = (px, py)
        time.sleep(gap)
    cur["on"] = False
    app._end_main_drag()
    user32.DestroyWindow(app.hwnd_main)

    # ---- 分析 ----
    devs = []
    for r in rows:
        i, pos, glass, bits, size = r
        if not glass or pos is None:
            continue
        W2, H2 = size
        lg = _mean_luma_buf(glass, W2, H2)
        lt = _truth_luma(truth, VW, pos[0], pos[1], W2, H2)
        devs.append((i, pos[0], pos[1], lg, lt, lg - lt))

    print("\n=== %s ===" % name)
    print("  面板 %dx%d  路径 %d 步  提交 %d 帧  丢弃 %d 帧  补抓 %d 次  判到自己 %d 次"
          % (W, H, len(path), len(rows), len(dropped), len(grabs), len(selfhit)))
    if not devs:
        print("  !! 一帧都没提交")
        return

    diffs = sorted(d[5] for d in devs)
    n = len(diffs)
    print("  亮度偏差（玻璃底 − 真实背景）  中位 %+.1f  90分位 %+.1f  最小 %+.1f  最大 %+.1f"
          % (diffs[n // 2], diffs[min(n - 1, int(n * 0.9))], diffs[0], diffs[-1]))

    bad = [d for d in devs if abs(d[5]) > 10.0]
    if bad:
        print("  !! 亮度对不上的帧（|偏差|>10）：")
        for d in bad[:14]:
            print("     帧%3d  (%4d,%4d)  玻璃 %5.1f  真实 %5.1f  偏差 %+.1f"
                  % (d[0], d[1], d[2], d[3], d[4], d[5]))
        print("     共 %d 帧" % len(bad))
    else:
        print("  偏差全部在 ±10 以内：没有任何一帧贴错背景")

    if dropped:
        print("  丢弃的帧（窗口没跟手）：%s" % [d[0] for d in dropped][:20])
    if selfhit:
        print("  判成「抓到自己」的帧：%s" % selfhit[:20])
    if grabs:
        ms = sorted(x[1] for x in grabs)
        fail = sum(1 for x in grabs if x[2])
        print("  补抓 中位 %.1fms 最大 %.1fms 失败 %d 次  发生在帧 %s"
              % (ms[len(ms) // 2], ms[-1], fail, [x[0] for x in grabs][:16]))
    cs = sorted(cost)
    print("  单帧耗时 中位 %.2fms 90分位 %.2fms 最大 %.2fms"
          % (cs[len(cs) // 2], cs[min(len(cs) - 1, int(len(cs) * 0.9))], cs[-1]))

    # 相邻帧的速度（检验路径本身是否匀速，程序没跟手时会体现在这）
    ts = [r[0] for r in rows]
    iv = sorted((ts[i] - ts[i - 1]) for i in range(1, len(ts)))
    if iv:
        print("  提交帧序间隔（按帧号差）中位 %d" % iv[len(iv) // 2])


print("屏幕 %dx%d  虚拟屏 %dx%d@(%d,%d)  DRAG_PAD=%d  BLUR_F=%d"
      % (SW, SH, VW, VH, VX, VY, snap.DRAG_PAD, snap.App.BLUR_F))

# 125Hz 鼠标、40px/帧 = 5000px/s：整整横扫一屏再扫回来（跨过亮/暗区域）
step = 40
n = (VW - 340) // step
path_fast = [(i * step, 400) for i in range(1, n + 1)]
path_fast += [(i * step, 400) for i in range(n - 1, -1, -1)]
run("横向 5000px/s 全屏往返", path_fast, 0.008)

# 慢速：20px/帧 = 2500px/s
step = 20
n = (VW - 340) // step
path_slow = [(i * step, 400) for i in range(1, n + 1)]
path_slow += [(i * step, 400) for i in range(n - 1, -1, -1)]
run("横向 2500px/s 全屏往返", path_slow, 0.008)

# 纵向 5000px/s（对照：用户说上下没问题）
step = 40
n = (VH - 148 - 400) // step
path_v = [(600, 400 + i * step) for i in range(1, n + 1)]
path_v += [(600, 400 + i * step) for i in range(n - 1, -1, -1)]
run("纵向 5000px/s 往返", path_v, 0.008)
