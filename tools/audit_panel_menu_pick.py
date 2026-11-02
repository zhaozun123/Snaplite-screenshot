# -*- coding: utf-8 -*-
"""端到端：主面板右键 → 选中菜单第一项「截图」→ 遮罩真的铺起来了。

为什么还要验这个：`tests/test_panel_menu.py` 里 TrackPopupMenu 是桩，验的是
「命令号 → 动作」的映射；`audit_panel_menu_live.py` 验的是菜单能弹能收。两边
都过，仍然可能错在**菜单里登记的 ID 和 TrackPopupMenu 返回的 ID 对不上**
（AppendMenuW 的 uIDNewItem 写错、顺序改了没同步）。那种错只有真点一次才看
得出来：点「截图」结果开了录制，或者点了没反应。

做法：弹菜单后给菜单窗口发 VK_DOWN（选第一项）+ VK_RETURN（确认），然后看
`SnapLiteOverlay` 窗口有没有出来、`app.capturing` 是不是 True，最后 Esc 收掉。

用法：python tools/audit_panel_menu_pick.py
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

ME = os.getpid()
app = snap.App()
app.create_main()
hwnd = app.hwnd_main
user32.ShowWindow(hwnd, 5)
user32.SetForegroundWindow(hwnd)
time.sleep(0.5)


def enum_by_class(cls):
    out = []
    CB = WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

    def cb(h, lp):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(h, buf, 64)
        if buf.value == cls:
            pid = w.DWORD()
            user32.GetWindowThreadProcessId(h, byref(pid))
            if pid.value == ME:
                out.append((h, bool(user32.IsWindowVisible(h))))
        return True

    user32.EnumWindows(CB(cb), 0)
    return out


def keys(h, *vks):
    for vk in vks:
        user32.PostMessageW(h, 0x0100, vk, 0)     # WM_KEYDOWN
        user32.PostMessageW(h, 0x0101, vk, 0)     # WM_KEYUP
        time.sleep(0.12)


result = {}


def watcher():
    time.sleep(0.7)
    menus = enum_by_class("#32768")
    result["menus"] = len(menus)
    if not menus:
        result["note"] = "菜单没弹出来"
        return
    keys(menus[0][0], 0x28, 0x0D)      # VK_DOWN 选第一项，VK_RETURN 确认


th = threading.Thread(target=watcher, daemon=True)
th.start()

print("主面板上右键，选第一项（截图）…")
t0 = time.time()
with snap.GFX(app.mn_dc) as _g:
    _tr = app._title_rect(_g)
TX, TY, TW, TH = _tr
app._panel_menu((int(TY + TH // 2) & 0xFFFF) << 16 | (int(TX + TW // 2) & 0xFFFF))
print("菜单收起，耗时 %.2fs" % (time.time() - t0))
print("弹出的菜单窗口：%s 个" % result.get("menus"))

time.sleep(0.8)
ov = enum_by_class("SnapLiteOverlay")
print("\n遮罩窗口 SnapLiteOverlay：%d 个 %s" % (len(ov), ov))
print("app.capturing = %s" % app.capturing)
print("选中「截图」真的开始了截图：%s" % ("是" if (ov and app.capturing) else "否"))

# 收尾：把截图取消掉，别把遮罩留在屏幕上。
# 这里不走注入 Esc —— 遮罩是按「鼠标左键按下」才抢焦点的，PostMessage 进去
# 的键盘消息它不认。直接调内部取消，测的是「有没有起来」，不是 Esc 好不好用。
if ov:
    app._abort_capture()
    time.sleep(0.5)
    print("\n取消之后遮罩窗口：%d 个" % len(enum_by_class("SnapLiteOverlay")))
    print("app.capturing = %s" % app.capturing)

user32.DestroyWindow(hwnd)
print("\n完成")
