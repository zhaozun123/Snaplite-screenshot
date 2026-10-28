"""验证主界面拖动的两条路径。

1) 只点一下空白处、不移动 → 窗口不能动、拖动状态要收干净
2) 真的拖动 → 每 1px 都提交，位置和背景同帧、最终落位精确
"""
import sys
import time
import threading
import statistics

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from ctypes import byref, c_void_p

import snap
from snap import user32, w

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

LEFTDOWN, LEFTUP = 0x0002, 0x0004

app = snap.App()
app.create_main()
h = app.hwnd_main
app.capturing = False

# 把抓屏计数挂上去，看「点一下」到底有没有真的抓
calls = {"n": 0}
_orig_begin = app._begin_drag_glass


def counting_begin():
    calls["n"] += 1
    _orig_begin()


app._begin_drag_glass = counting_begin

msg = snap.MSG()


def pump(ms=120):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < ms / 1000.0:
        while user32.PeekMessageW(byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(byref(msg))
            user32.DispatchMessageW(byref(msg))
        time.sleep(0.001)


ok = True

# ---------------- 1) 点一下不拖 ----------------
user32.SetWindowPos(h, None, 600, 400, 0, 0, 0x0001 | 0x0004 | 0x0010)
pump(200)
rc = w.RECT()
user32.GetWindowRect(h, byref(rc))
gx, gy = rc.left + 170, rc.top + 138     # 底部空白处

user32.SetCursorPos(gx, gy)
pump(80)
calls["n"] = 0
user32.mouse_event(LEFTDOWN, 0, 0, 0, 0)
pump(160)
print("[点一下] 按下后：main_drag=%s drag_active=%s 抓屏次数=%d"
      % (app.main_drag, app.drag_active, calls["n"]))
if not app.drag_active:
    print("  FAIL: 按下时没建好拖动缓存（拖动第一帧会顿）")
    ok = False
user32.mouse_event(LEFTUP, 0, 0, 0, 0)
pump(160)
print("[点一下] 松手后：main_drag=%s" % app.main_drag)
if app.main_drag:
    print("  FAIL: 拖动状态没收尾")
    ok = False
rc2 = w.RECT()
user32.GetWindowRect(h, byref(rc2))
if (rc2.left, rc2.top) != (600, 400):
    print("  FAIL: 窗口被点走了，(%d,%d)" % (rc2.left, rc2.top))
    ok = False

# ---------------- 2) 真的拖 ----------------
X0, Y0 = 600, 400
GRAB = (170, 138)
N, DT = 90, 0.016
user32.SetWindowPos(h, None, X0, Y0, 0, 0, 0x0001 | 0x0004 | 0x0010)
pump(200)
calls["n"] = 0
submitted = []
_orig_frame = app._drag_frame


def counting_frame(left, top):
    r = _orig_frame(left, top)
    if r:
        submitted.append(left)
    return r


app._drag_frame = counting_frame


def mover():
    time.sleep(0.06)
    user32.SetCursorPos(X0 + GRAB[0], Y0 + GRAB[1])
    time.sleep(0.04)
    user32.mouse_event(LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    for i in range(1, N + 1):
        user32.SetCursorPos(X0 + GRAB[0] + i, Y0 + GRAB[1])
        time.sleep(DT)
    time.sleep(0.10)
    user32.mouse_event(LEFTUP, 0, 0, 0, 0)


tm = threading.Thread(target=mover, daemon=True)
tm.start()
t0 = time.perf_counter()
while time.perf_counter() - t0 < 8.0:
    pump(20)
    if not tm.is_alive():
        break
pump(200)

rc3 = w.RECT()
user32.GetWindowRect(h, byref(rc3))
print("\n[拖动] 抓屏次数=%d（应为 1）" % calls["n"])
print("[拖动] 最终位置 (%d,%d)，期望 (%d,%d)"
      % (rc3.left, rc3.top, X0 + N, Y0))
print("[拖动] 提交帧数 %d / 理想 %d" % (len(submitted), N))
if calls["n"] != 1:
    print("  FAIL: 抓屏次数不对")
    ok = False
if (rc3.left, rc3.top) != (X0 + N, Y0):
    print("  FAIL: 最终位置不对")
    ok = False
gaps = [submitted[i] - submitted[i - 1] for i in range(1, len(submitted))]
if gaps:
    print("  相邻帧位移：中位 %d px，最大 %d px" % (statistics.median(gaps), max(gaps)))
    if max(gaps) > 1:
        print("  FAIL: 有跳帧（画面会一顿一顿）")
        ok = False
if app.main_drag or app.drag_active:
    print("  FAIL: 松手后状态没清干净")
    ok = False

user32.DestroyWindow(h)
print("\n结果：%s" % ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
