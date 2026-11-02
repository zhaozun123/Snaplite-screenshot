# -*- coding: utf-8 -*-
"""真实窗口上验一遍：主面板右键能不能弹出菜单、能不能正常收掉。

单元测试（tests/test_panel_menu.py）把 TrackPopupMenu 换成了桩，能验菜单项
和分派，验不了「菜单真的出现在屏幕上」。这里放真跑一次：

- 主线程调 `_panel_menu()`，它会阻塞在 TrackPopupMenu 的模态循环里；
- 另一个线程在 0.6s 后枚举窗口找 `#32768`（系统菜单类），看它是否可见、
  在哪、多大；
- 然后给它发 Esc 收掉，确认主线程能正常返回（收不掉就说明少抢了前台，
  那是 TrackPopupMenu 的经典坑）。

用法：python tools/audit_panel_menu_live.py
"""
import ctypes
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from ctypes import byref, c_void_p, WINFUNCTYPE

import snap
from snap import user32, w

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

app = snap.App()
app.create_main()
hwnd = app.hwnd_main
user32.ShowWindow(hwnd, 5)          # SW_SHOW
user32.SetForegroundWindow(hwnd)
time.sleep(0.5)

rc = w.RECT()
user32.GetWindowRect(hwnd, byref(rc))
W = rc.right - rc.left
H = rc.bottom - rc.top
print("主面板 (%d,%d) %dx%d" % (rc.left, rc.top, W, H))

result = {}
ME = os.getpid()


def find_menus():
    """枚举本进程的 #32768 窗口（TrackPopupMenu 创建的那个）"""
    out = []
    CB = WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

    def cb(h, lp):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(h, buf, 64)
        if buf.value == "#32768":
            pid = w.DWORD()
            user32.GetWindowThreadProcessId(h, byref(pid))
            if pid.value == ME:
                r = w.RECT()
                user32.GetWindowRect(h, byref(r))
                out.append((h, r.left, r.top, r.right - r.left, r.bottom - r.top,
                            bool(user32.IsWindowVisible(h))))
        return True

    user32.EnumWindows(CB(cb), 0)
    return out


def pack(x, y):
    """客户区坐标 → lParam（低 16 位 x，高 16 位 y）"""
    return (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)


# ---- 先验「不该弹的地方」：面板正中间和底部空白，右键必须一点反应都没有。
# 这条靠「函数立刻返回 + 屏幕上没有 #32768」判断，不靠读代码。
for label, (px_, py_) in (("面板正中间", (W // 2, H // 2)),
                          ("底部空白", (W // 2, H - 6)),
                          ("截图按钮上", (W // 2, app._main_layout()[3][1] + 10))):
    t = time.time()
    app._panel_menu(pack(px_, py_))
    dt = time.time() - t
    n = len(find_menus())
    print("点%s：耗时 %.3fs，菜单窗口 %d 个 %s"
          % (label, dt, n, "✓ 没弹" if (n == 0 and dt < 0.1) else "✗ 弹了！"))

# ---- 再验「该弹的地方」：logo 正中
with snap.GFX(app.mn_dc) as _g:
    _tr = app._title_rect(_g)
TX, TY, TW, TH = _tr
print("\nlogo 字形框 %s，点它的正中 (%d,%d)" % (_tr, TX + TW // 2, TY + TH // 2))


def watcher():
    time.sleep(0.7)
    menus = find_menus()
    result["menus"] = menus
    if not menus:
        result["closed"] = "没找到菜单窗口"
        return
    h = menus[0][0]
    user32.PostMessageW(h, 0x0100, 0x1B, 0)      # WM_KEYDOWN  VK_ESCAPE
    user32.PostMessageW(h, 0x0101, 0x1B, 0)      # WM_KEYUP
    time.sleep(0.5)
    result["after"] = find_menus()


th = threading.Thread(target=watcher, daemon=True)
th.start()

t0 = time.time()
app._panel_menu(pack(TX + TW // 2, TY + TH // 2))     # 阻塞，直到菜单被收掉
elapsed = time.time() - t0

menus = result.get("menus") or []
print("\n右键后出现的菜单窗口：%d 个" % len(menus))
for h, x, y, ww, hh, vis in menus:
    print("  hwnd=%s  位置 (%d,%d)  尺寸 %dx%d  可见=%s" % (h, x, y, ww, hh, vis))

if menus:
    h, x, y, ww, hh, vis = menus[0]
    # 菜单永远弹在**真实光标**处，不是 lParam 那个坐标。本脚本没移动鼠标，
    # 所以这里的位置可能离面板很远，属正常，别当成 bug。
    print("\n菜单弹在真实光标处：(%d,%d)" % (x, y))
    print("菜单有实际尺寸：%s" % ("是" if ww > 40 and hh > 40 else "否"))
    print("弹出来是可见的：%s" % ("是" if vis else "否"))

print("\nEsc 之后菜单窗口：%d 个" % len(result.get("after") or []))
print("_panel_menu 返回耗时 %.2fs（说明模态循环能退出）" % elapsed)
print("menu_open 已复原：%s" % (app.menu_open is False))

user32.DestroyWindow(hwnd)
print("\n完成")
