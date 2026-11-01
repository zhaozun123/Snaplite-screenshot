# -*- coding: utf-8 -*-
"""主面板交互验证：hover / 点 ✕ / 点 — / 拖窗口 / 按键 / 丢捕获。

为什么单独有这个测试：_on_main 是主面板的窗口过程，原来是一个 97 行、
34 个分支的 if 链，拆成 _main_* 之后**没有任何测试覆盖**——拆错一个分支
（比如把「✕ 收进托盘」写成「退出程序」）不会有任何报警。

这里直接往窗口过程发消息，模拟真实的点击和按键，全部走 _on_main 这条
真实分派路径，不绕过去调子方法。

用法：python tests/test_main_panel.py
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from ctypes import byref, c_void_p

import snap
from snap import user32

WM_MOUSEMOVE = 0x0200
WM_MOUSELEAVE = 0x02A3
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_KEYDOWN = 0x0100
WM_CAPTURECHANGED = 0x0215

ok = True


def check(name, cond, extra=""):
    global ok
    print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))
    if not cond:
        ok = False


def pack(x, y):
    """把客户区坐标打包成 lParam（低 16 位 x，高 16 位 y）"""
    return (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)


def send(hwnd, msg, wp=0, lp=0):
    return user32.SendMessageW(hwnd, msg, wp, lp)


def center(rect):
    x, y, w, h = rect
    return int(x + w / 2), int(y + h / 2)


snap.SetProcessDpiAwarenessContext(c_void_p(-4))
app = snap.App()
app.create_main()
hwnd = app.hwnd_main
check("主窗口创建", bool(hwnd))

close, hide, btn, hot = app._main_layout()

# ---------- 1. hover 命中
for name, rect, want in (("✕", close, "close"), ("—", hide, "hide"),
                         ("截图按钮", btn, "btn"), ("热键条", hot, "hot")):
    x, y = center(rect)
    send(hwnd, WM_MOUSEMOVE, 0, pack(x, y))
    check("鼠标移到 %s 上会高亮" % name, app.hover_main == want,
          "hover_main=%r 期望 %r" % (app.hover_main, want))

# 空白处（面板左下角，按钮下方）应当不高亮任何东西
x, y = app.px(6), app.MH - app.px(6)
send(hwnd, WM_MOUSEMOVE, 0, pack(x, y))
check("空白处不高亮任何元素", app.hover_main is None, "hover_main=%r" % app.hover_main)

# ---------- 2. 鼠标离开
send(hwnd, WM_MOUSEMOVE, 0, pack(*center(btn)))
send(hwnd, WM_MOUSELEAVE)
check("鼠标离开后高亮清空", app.hover_main is None and not app.tracking_main)

# ---------- 3. 空白处按下并拖动 → 窗口自己接管拖动
x, y = app.px(6), app.MH - app.px(6)
send(hwnd, WM_LBUTTONDOWN, 1, pack(x, y))
check("空白处按下开始拖窗口", app.main_drag)
# 抬起来要收尾
send(hwnd, WM_LBUTTONUP, 0, pack(x, y))
check("抬手结束拖窗口", not app.main_drag)

# ---------- 4. 拖动中被系统抢走捕获 → 也要收尾
send(hwnd, WM_LBUTTONDOWN, 1, pack(x, y))
check("再次按下进入拖动", app.main_drag)
send(hwnd, WM_CAPTURECHANGED, 0, 0)
check("丢捕获后拖动状态收尾", not app.main_drag)

# ---------- 5. 点 — 收进托盘（窗口隐藏，但进程不退）
send(hwnd, WM_LBUTTONDOWN, 1, pack(*center(hide)))
visible_after_hide = bool(user32.IsWindowVisible(hwnd))
check("点「—」后窗口隐藏", not visible_after_hide)
user32.ShowWindow(hwnd, 5)          # SW_SHOW，恢复
check("隐藏后还能重新显示", bool(user32.IsWindowVisible(hwnd)))

# ---------- 6. 点 ✕ 只收进托盘，不退出
send(hwnd, WM_LBUTTONDOWN, 1, pack(*center(close)))
check("点「✕」后窗口隐藏而非退出", not user32.IsWindowVisible(hwnd))
check("点「✕」后主窗口句柄仍然有效", bool(user32.IsWindow(hwnd)))
user32.ShowWindow(hwnd, 5)

# ---------- 7. Esc：不录制时吃掉
r = send(hwnd, WM_KEYDOWN, 0x1B, 0)     # VK_ESCAPE
check("Esc 被吃掉（返回 0）", r == 0, "返回 %s" % r)
check("Esc 不改动任何状态", not app.recording and not app.main_drag)

# ---------- 8. 录制热键时，按键必须交给录制器
hits = []
orig_rec = app._on_record_key
app._on_record_key = lambda vk: (hits.append(vk), orig_rec(vk))[1]
app._begin_record()
check("进入录制状态", app.recording)
send(hwnd, WM_KEYDOWN, ord("B"), 0)
check("录制时按键被转给录制器", hits == [ord("B")], "收到 %s" % hits)
app._on_record_key = orig_rec
app._end_record()
send(hwnd, WM_KEYDOWN, ord("B"), 0)
check("退出录制后按键不再进录制器", hits == [ord("B")], "收到 %s" % hits)

# ---------- 9. 需要特定返回值的消息不能被分派链吞掉
r = send(hwnd, 0x0014, 0, 0)            # WM_ERASEBKGND
check("WM_ERASEBKGND 返回 1（已处理背景）", r == 1, "返回 %s" % r)
r = send(hwnd, 0x0003, 0, 0)            # WM_MOVE
check("WM_MOVE 返回 0 且不触发重画", r == 0, "返回 %s" % r)

# ---------- 10. 拆出来的命中函数和分派一致
check("_main_hit 与各区域一致",
      app._main_hit(*center(close)) == "close"
      and app._main_hit(*center(hide)) == "hide"
      and app._main_hit(*center(btn)) == "btn"
      and app._main_hit(*center(hot)) == "hot")

user32.DestroyWindow(hwnd)
app.probe.stop() if hasattr(app.probe, "stop") else None
print()
print("结果：%s" % ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
