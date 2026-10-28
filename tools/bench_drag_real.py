# -*- coding: utf-8 -*-
"""真实鼠标拖动实测（自绘拖动版）。

采样线程每 1.5ms 记一次（鼠标坐标、窗口坐标、已提交帧数），
拖动中「窗口左上角」应当始终等于「鼠标坐标 - 按下时的偏移」。
"""
import sys
import os
import time
import threading

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctypes import byref, c_void_p

import snap
from snap import user32, w

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

LEFTDOWN, LEFTUP = 0x0002, 0x0004
GRAB = 12                      # 按下点相对窗口左上角的偏移
X0, Y0 = 520, 360
DX, DY = 620, 330
STEPS = 70
STEP_DT = 0.008

app = snap.App()
app.create_main()
h = app.hwnd_main
user32.SetWindowPos(h, None, X0, Y0, 0, 0, 0x0001 | 0x0004 | 0x0010)
time.sleep(0.4)

stats = {"n": 0, "rt": [], "frames": []}
samples = []
stop = threading.Event()

orig = app._drag_frame


def wrapped(left, top):
    t0 = time.perf_counter()
    ok = orig(left, top)
    if ok:
        stats["n"] += 1
        stats["rt"].append((time.perf_counter() - t0) * 1000.0)
        stats["frames"].append((time.perf_counter(), left, top))
    return ok


app._drag_frame = wrapped


def cursor():
    p = w.POINT()
    user32.GetCursorPos(byref(p))
    return p.x, p.y


def sampler():
    while not stop.is_set():
        t = time.perf_counter()
        cx, cy = cursor()
        rc = w.RECT()
        user32.GetWindowRect(h, byref(rc))
        samples.append((t, cx, cy, rc.left, rc.top))
        time.sleep(0.0015)


def mover():
    time.sleep(0.08)
    user32.SetCursorPos(X0 + GRAB, Y0 + GRAB)
    time.sleep(0.03)
    user32.mouse_event(LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    for i in range(1, STEPS + 1):
        user32.SetCursorPos(X0 + GRAB + int(DX * i / float(STEPS)),
                            Y0 + GRAB + int(DY * i / float(STEPS)))
        time.sleep(STEP_DT)
    time.sleep(0.10)
    user32.mouse_event(LEFTUP, 0, 0, 0, 0)


msg = snap.MSG()


def pump():
    while user32.PeekMessageW(byref(msg), None, 0, 0, 1):
        user32.TranslateMessage(byref(msg))
        user32.DispatchMessageW(byref(msg))


th_s = threading.Thread(target=sampler, daemon=True)
th_s.start()
th_m = threading.Thread(target=mover, daemon=True)
th_m.start()

t0 = time.perf_counter()
while time.perf_counter() - t0 < 4.0:
    pump()
    if not th_m.is_alive():
        break
    time.sleep(0)
t_dur = time.perf_counter() - t0

stop.set()
th_m.join(1.0)
th_s.join(1.0)
for _ in range(300):
    pump()

rc = w.RECT()
user32.GetWindowRect(h, byref(rc))
print("模拟鼠标 %d 步 × %.0f ms，窗口最终 (%d,%d)，期望 (%d,%d)，偏差 (%d,%d)"
      % (STEPS, STEP_DT * 1000, rc.left, rc.top,
         X0 + DX, Y0 + DY, rc.left - (X0 + DX), rc.top - (Y0 + DY)))

if stats["rt"]:
    rt = sorted(stats["rt"])
    print("提交帧数 %d，单帧 中位 %.2f ms  p90 %.2f  最慢 %.2f"
          % (len(rt), rt[len(rt) // 2], rt[int(len(rt) * 0.9)], rt[-1]))

# 拖动中：窗口是否严格跟住鼠标（lag 应恒为 0）
lag = []
for (t, cx, cy, wl, wt) in samples:
    lag.append((cx - GRAB - wl, cy - GRAB - wt))
nonzero = [l for l in lag if l != (0, 0)]
print("\n采样 %d 次，窗口位置与鼠标不符的采样 %d 次（%.0f%%）"
      % (len(lag), len(nonzero), 100.0 * len(nonzero) / max(1, len(lag))))
if nonzero:
    print("  最大偏差 %d px（横）/ %d px（纵）"
          % (max(abs(l[0]) for l in nonzero), max(abs(l[1]) for l in nonzero)))
    print("  说明：窗口位置由我们在同一帧里提交，采样到偏差通常是采样线程恰好"
          "落在「鼠标已动、消息还没处理」的间隙里，属正常 1 帧内延迟")

# 帧间隔
ts = [f[0] for f in stats["frames"]]
if len(ts) > 3:
    ivs = sorted((ts[i] - ts[i - 1]) * 1000 for i in range(1, len(ts)))
    print("\n相邻两帧间隔：中位 %.1f ms  p90 %.1f  最长 %.1f"
          % (ivs[len(ivs) // 2], ivs[int(len(ivs) * 0.9)], ivs[-1]))
    print("等效帧率 ≈ %.0f fps（拖动共 %.0f ms）"
          % (len(ts) / max(0.001, t_dur), t_dur * 1000))

# 内容是否随位置变化
if stats["frames"]:
    ps = {(f[1], f[2]) for f in stats["frames"]}
    print("提交过的不同位置数 %d" % len(ps))

print("\n拖动结束后 drag_active=%s main_drag=%s" % (app.drag_active, app.main_drag))
user32.DestroyWindow(h)
