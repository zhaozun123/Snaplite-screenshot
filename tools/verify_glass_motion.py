# -*- coding: utf-8 -*-
"""真机验证：主面板不动、背后画面在变时，玻璃底到底跟不跟。

单元测试（tests/test_glass_follow.py）用的是假数据，只能证明判定逻辑对。
这个工具走**真实抓屏**，在面板背后放一个原地变色的窗口（不移动、不改大小，
所以不会触发任何窗口事件），看玻璃底会不会跟着换 —— 这正是用户报的那个
「背景在动但模糊不动」的场景。

颜色变化用 SetLayeredWindowAttributes 改透明度实现：窗口位置和尺寸都不动，
纯属「画面内容变了」，能排除窗口事件路径的干扰。

用法：python tools/verify_glass_motion.py
耗时约 8 秒，期间屏幕上会出现一个蓝色方块（在面板背后），结束自动关闭。
"""
import ctypes
import hashlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from ctypes import byref, c_void_p, sizeof
from ctypes import wintypes as w

import snap
from snap import GFX, PAINTSTRUCT, WNDCLASSEXW, WNDPROC, gdi32, user32

# 这个工具一步步调 _tick_glass() 看同步判定链路，活跃档交给后台线程之后
# 一次 tick 只会「启动线程」就返回，断言全部失效。所以这里关掉线程，专门验
# 老系统那条退路（同步路径）；线程那条路由 tools/verify_glass_pace.py 验。
snap.GLASS_USE_LIVE_THREAD = False

ok = True


def check(name, cond, extra=""):
    global ok
    print("[%s] %-42s %s" % ("PASS" if cond else "FAIL", name, extra))
    if not cond:
        ok = False


COLOR = 0xFF1E6FD0
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
LWA_ALPHA = 0x00000002
PM_REMOVE = 0x0001


def anim_proc(hwnd, msg, wp, lp):
    """动画窗口：只负责刷成一块纯色。不响应鼠标键盘，纯背景板。"""
    if msg == 0x0014:                       # WM_ERASEBKGND：自己画，别擦
        return 1
    if msg == 0x000F:                       # WM_PAINT
        ps = PAINTSTRUCT()
        dc = user32.BeginPaint(hwnd, byref(ps))
        try:
            r = w.RECT()
            user32.GetClientRect(hwnd, byref(r))
            with GFX(dc) as g:
                g.fill(COLOR, 0, 0, r.right, r.bottom)
        finally:
            user32.EndPaint(hwnd, byref(ps))
        return 0
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


def settle(redraw=True):
    """等 DWM 把这一帧真的合成到屏幕上，否则抓到的还是上一帧。

    redraw=True 时才主动让探针窗口重绘。验证「画面停住后不该再刷」的时候
    必须传 False —— 否则每拍都强制重绘，会抓到 DWM 的中间混合状态，测出
    来的抖动是工具自己制造的，跟被测代码无关。"""
    if redraw:
        user32.InvalidateRect(anim, None, True)
        user32.UpdateWindow(anim)
    try:
        snap.dwmapi.DwmFlush()
    except Exception:
        pass
    time.sleep(0.09)


def digest(buf):
    if not buf:
        return "none"
    return hashlib.md5(buf).hexdigest()[:8]


snap.SetProcessDpiAwarenessContext(c_void_p(-4))
app = snap.App()
app.create_main()
app._build_capture_buffers()

# 找主面板的位置，动画窗口就摆在它背后同一块地方
rc = w.RECT()
user32.GetWindowRect(app.hwnd_main, byref(rc))
MW, MH = app.MW, app.MH
print("主面板位置 (%d,%d) %dx%d" % (rc.left, rc.top, MW, MH))

# ---------- 造一块「原地变色」的背景板
proc = WNDPROC(anim_proc)
wc = WNDCLASSEXW()
wc.cbSize = sizeof(WNDCLASSEXW)
wc.style = 0
wc.lpfnWndProc = proc
wc.hInstance = app.hinst
wc.hbrBackground = None
wc.lpszClassName = "SnapLiteMotionProbe"
atom = user32.RegisterClassExW(byref(wc))
print("注册探针窗口类: atom=%s" % atom)

anim = user32.CreateWindowExW(
    WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
    "SnapLiteMotionProbe", "motion", WS_POPUP,
    rc.left, rc.top, MW, MH, None, None, app.hinst, None)
if not anim:
    print("创建探针窗口失败，无法验证")
    sys.exit(2)

user32.SetLayeredWindowAttributes(anim, 0, 255, LWA_ALPHA)
user32.ShowWindow(anim, 4)                  # SW_SHOWNOACTIVATE
# 压到主面板下面，不然会盖住面板
user32.SetWindowPos(anim, 1, 0, 0, 0, 0, 0x0003)   # HWND_BOTTOM + NOSIZE|NOMOVE


print("\n" + "=" * 62)
print("真实抓屏验证：背景内容变化 → 玻璃底必须跟着换")
print("=" * 62)

# 先让玻璃底稳定下来（连跑几拍，确保已经抓到探针窗口的画面）
app.glass_raw = None
for _ in range(4):
    settle()
    app._tick_glass()
time.sleep(0.1)

# ---------- A. 背景不变：玻璃底必须稳定
print("\n[A] 背景不动时，玻璃底应当纹丝不动")
base = digest(app.mn_glass)
stable = []
for _ in range(3):
    settle()
    app._tick_glass()
    stable.append(digest(app.mn_glass))
check("连续 3 拍玻璃底不变", len(set(stable)) == 1,
      "→ %s" % " ".join(stable))

# ---------- B. 背景在变：玻璃底必须跟着换（核心）
print("\n[B] 背景内容变化时，玻璃底必须跟着换（用户报的 bug）")
seen = []
for i, alpha in enumerate((60, 200, 90, 240, 70)):
    user32.SetLayeredWindowAttributes(anim, 0, alpha, LWA_ALPHA)
    settle()
    app._tick_glass()
    seen.append(digest(app.mn_glass))
    print("     第 %d 次改透明度 %3d → 玻璃底 %s" % (i + 1, alpha, seen[-1]))

changed = len(set(seen) - {base})
check("5 次背景变化，玻璃底至少跟着换了 4 次", changed >= 4,
      "变化 %d 次 / 唯一值 %d 个" % (changed, len(set(seen))))
check("玻璃底不再是变化前那一帧", seen[-1] != base, "%s → %s" % (base, seen[-1]))

# ---------- C. 停手之后再确认一次：不该再乱刷
#   这里把探针改回完全不透明。半透明时底下透出来的是真实桌面，而这台机器上
#   那块区域有别的程序在持续重绘（实测：什么都不动，连抓 5 次 raw 也全不同），
#   属于环境噪声，会把「该不该刷新」这件事搅浑。不透明后画面完全由探针决定。
user32.SetLayeredWindowAttributes(anim, 0, 255, LWA_ALPHA)
settle()
app._tick_glass()                           # 先吸收掉「改回不透明」这一次变化
time.sleep(0.2)
settled = [digest(app.mn_glass)]
for _ in range(3):
    settle(redraw=False)                    # 画面不动了，也不再催它重绘
    app._tick_glass()
    settled.append(digest(app.mn_glass))
print("\n[C] 背景停住后，玻璃底应当稳定在新的那一帧")
check("停手后 4 拍保持同一帧", len(set(settled)) == 1, "→ %s" % " ".join(settled))

# ---------- 收尾
user32.DestroyWindow(anim)
print("\n" + "=" * 62)
print("结果：%s" % ("全部通过" if ok else "有失败项"))
print("=" * 62)
sys.exit(0 if ok else 1)
