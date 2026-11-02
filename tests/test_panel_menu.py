# -*- coding: utf-8 -*-
"""主面板右键菜单：弹的是哪份菜单、什么时候弹、弹着的时候不刷玻璃底。

为什么要单独有这个测试：右键菜单原来只有托盘图标有，主面板与它共用同一份
构建逻辑（`_menu_popup`）。共用之后有三个地方容易错，而且错了不会崩、只会
悄悄地不对：

1. **弹的位置**。第一版在整块面板上都能右键弹出来，想拖一下窗口或者随手误触
   右键就蹦出菜单。现在只有点在「SnapLite」logo 上才弹 —— 而 logo 那块矩形
   是按字形量出来的，不是绘制时用的那个 px(120) 布局框（后者比字形宽 30px，
   拿它当命中区会让标题右边一大片空白也能弹）。
2. 主面板上不该出现「显示主界面」—— 面板就在眼前，点了也不会有任何变化，
   看着像坏了一条。这里直接读真实建出来的菜单项，不靠断言代码里写没写。
3. `TrackPopupMenu` 是**阻塞**的，它自带的那个消息泵会继续派发 WM_TIMER。
   菜单正好压在面板上，玻璃底照常抓屏就会把菜单自己拍进去，菜单一关面板上
   留下一块菜单的影子。这条只能靠 `menu_open` 挡住，所以必须钉死。

用法：python tests/test_panel_menu.py
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

from ctypes import c_void_p

import snap
from snap import user32

WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205

ok = True


def check(name, cond, extra=""):
    global ok
    print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))
    if not cond:
        ok = False


def pack(x, y):
    """客户区坐标 → lParam（低 16 位 x，高 16 位 y）"""
    return (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)


def send(hwnd, msg, wp=0, lp=0):
    return user32.SendMessageW(hwnd, msg, wp, lp)


try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

app = snap.App()
app.create_main()
hwnd = app.hwnd_main
W, H = app.MW, app.MH

# ---------------------------------------------------------------- 拦截菜单
# TrackPopupMenu 一旦真跑起来就卡住等人点，测试没法继续。换成桩：读一遍
# 刚建好的菜单项，然后当作「点了菜单外面」返回 0。这样菜单是**真实建出来**
# 的，项数和文字都是 `_menu_popup` 的实际产物，不是猜的。
_u = ctypes.windll.user32
_u.GetMenuItemCount.argtypes = [c_void_p]
_u.GetMenuItemCount.restype = ctypes.c_int
_u.GetMenuStringW.argtypes = [c_void_p, ctypes.c_uint, ctypes.c_wchar_p,
                              ctypes.c_int, ctypes.c_uint]
_u.GetMenuStringW.restype = ctypes.c_int

seen = {}


def fake_track(hm, flags, x, y, n, hwindow, rect):
    texts = []
    for i in range(_u.GetMenuItemCount(hm)):
        buf = ctypes.create_unicode_buffer(128)
        _u.GetMenuStringW(hm, i, buf, 128, 0x400)      # MF_BYPOSITION
        if buf.value:
            texts.append(buf.value)                    # 分隔线读出来是空串
    seen["items"] = texts
    seen["menu_open"] = app.menu_open
    seen["hwnd"] = int(hwindow or 0)
    return 0


real_track = user32.TrackPopupMenu
user32.TrackPopupMenu = fake_track

# ---------------------------------------------------------- 1. 右键位置
# 标题字形框：右键只认这一块。坐标从 _title_rect 现算，不写死像素 ——
# 换字体 / 调 DPI 之后这些用例照样成立。
with snap.GFX(app.mn_dc) as _g:
    _tr = app._title_rect(_g)
TX, TY, TW, TH = _tr
TITLE = (TX + TW // 2, TY + TH // 2)          # logo 正中，肯定算「在上面」
BTN = app._main_layout()[3]
CLOSE = app._main_layout()[0]

print("\n=== 1. 只有点在「SnapLite」logo 上才弹 ===")
check("标题字形框量得出来", TW > app.px(20), "框 %s" % (_tr,))
check("字形框不含版本号（比那个 px(120) 布局框窄得多）",
      TW < app.px(110), "宽 %dpx" % TW)

places = [
    ("logo 正中", TITLE, True),
    ("logo 最左一列", (TX, TY + TH // 2), True),
    ("logo 最右一列", (TX + TW - 1, TY + TH // 2), True),
    ("面板正中间", (W // 2, H // 2), False),
    ("面板底部空白", (W // 2, H - 8), False),
    ("右边的截图按钮", (BTN[0] + BTN[2] // 2, BTN[1] + BTN[3] // 2), False),
    ("右上角 ✕", (CLOSE[0] + CLOSE[2] // 2, CLOSE[1] + CLOSE[3] // 2), False),
    ("版本号那一带", (TX + TW + app.px(20), TY + TH // 2), False),
]
for name, (px_, py_), want in places:
    seen.clear()
    send(hwnd, WM_RBUTTONUP, 0, pack(px_, py_))
    got = "items" in seen
    check("右键在%s%s弹菜单" % (name, "会" if want else "不会"), got == want,
          "(%d,%d) 实际%s" % (px_, py_, "弹了" if got else "没弹"))

# 下面几节都以「点中 logo」为前提
seen.clear()
app.hover_main = "btn"
app.glass_dirty = False
send(hwnd, WM_RBUTTONUP, 0, pack(*TITLE))

check("右键抬起到 logo 上时确实弹了菜单", "items" in seen)
n_panel = len(seen.get("items") or [])
check("菜单里没有「显示主界面」",
      not any("显示主界面" in t for t in seen.get("items") or []),
      "实际 %s" % (seen.get("items"),))
check("四项功能都在",
      all(any(k in t for t in seen["items"])
          for k in ("截图", "更改快捷键", "开机自动启动", "退出")),
      "实际 %s" % (seen["items"],))
check("弹的时候 menu_open 是 True", seen.get("menu_open") is True)
check("弹完 menu_open 复原", app.menu_open is False)
check("弹完把玻璃底标脏（菜单盖过一阵，桌面可能变了）", app.glass_dirty is True)
check("弹之前把 hover 高亮清掉了", app.hover_main is None)
check("菜单挂在主窗口上", seen.get("hwnd") == int(hwnd or 0))
check("右键没把窗口拖起来", app.main_drag is False)

# -------------------------------------------------- 2. 右键按下不该弹
print("\n=== 2. 只有抬起才弹 ===")
seen.clear()
send(hwnd, WM_RBUTTONDOWN, 2, pack(*TITLE))
check("右键按下不弹菜单", "items" not in seen)

# -------------------------------------------------- 3. 托盘那份菜单
print("\n=== 3. 托盘菜单仍然多一条「显示主界面」===")
seen.clear()
app._tray_menu()
check("托盘菜单里有「显示主界面」",
      any("显示主界面" in t for t in seen.get("items") or []),
      "实际 %s" % (seen.get("items"),))
check("正好比主面板多一项", len(seen.get("items") or []) == n_panel + 1,
      "托盘 %d 项 / 面板 %d 项" % (len(seen.get("items") or []), n_panel))

# -------------------------------------------------- 4. 选中之后的动作
print("\n=== 4. 选中菜单项之后干什么 ===")
calls = []
keep = (app.start_capture, app._show_panel, app.quit,
        app.set_autostart, app._begin_record, app.autostart_enabled)
app.start_capture = lambda: calls.append("capture")
app._show_panel = lambda: calls.append("show")
app.quit = lambda: calls.append("quit")
app.set_autostart = lambda on: calls.append("auto:%s" % on)
app._begin_record = lambda: calls.append("record")
app.autostart_enabled = lambda: False

app._menu_command(1)
check("截图 → start_capture", calls[-1] == "capture")
app._menu_command(2)
check("显示主界面 → 把面板叫回来", calls[-1] == "show")
app._menu_command(3)
check("退出 → quit", calls[-1] == "quit")
app._menu_command(4)
check("开机自动启动 → 取反后写注册表", calls[-1] == "auto:True")
app._menu_command(5)
check("更改快捷键 → 先叫回面板再录制",
      calls[-2:] == ["show", "record"], "实际 %s" % (calls[-2:],))
n_calls = len(calls)
app._menu_command(0)
check("取消（返回 0）什么都不做", len(calls) == n_calls)

(app.start_capture, app._show_panel, app.quit,
 app.set_autostart, app._begin_record, app.autostart_enabled) = keep

# -------------------------------------------------- 5. 拖动 / 录制中
print("\n=== 5. 拖窗口和录制快捷键的时候右键 ===")
seen.clear()
app.main_drag = True
send(hwnd, WM_RBUTTONUP, 0, pack(*TITLE))
check("左键拖着窗口时不弹菜单（否则拖动循环卡在中间）", "items" not in seen)
app.main_drag = False

ended = []
real_end = app._end_record
app._end_record = lambda: (ended.append(1), setattr(app, "recording", False))
app.recording = True
seen.clear()
send(hwnd, WM_RBUTTONUP, 0, pack(*TITLE))
check("录制中右键先结束录制", ended == [1])
check("然后照常弹菜单", "items" in seen)
app._end_record = real_end
app.recording = False

# -------------------------------------------------- 6. 菜单弹着不刷玻璃
print("\n=== 6. 菜单弹着的时候不抓屏 ===")
app._stop_glass_live()
cnt = [0]
real_block = app._grab_glass_block
app._grab_glass_block = lambda: (cnt.__setitem__(0, cnt[0] + 1), None)[1]

app.menu_open = True
app._tick_glass()
check("菜单弹着时一拍都不抓", cnt[0] == 0, "实际抓了 %d 次" % cnt[0])

app.menu_open = False
app.glass_dirty = False
app._tick_glass()
check("菜单收起后恢复抓屏", cnt[0] == 1, "实际抓了 %d 次" % cnt[0])
app._grab_glass_block = real_block

# -------------------------------------------------- 7. 消息不落到 DefWindowProc
print("\n=== 7. 右键消息被吃掉 ===")
seen.clear()
r = send(hwnd, WM_RBUTTONUP, 0, pack(*TITLE))
check("_on_main 返回 0（不交给 DefWindowProc 去弹系统菜单）", r == 0, "实际 %s" % r)
check("顺带又弹了一次", "items" in seen)

# ---------------------------------------------------------------- 收尾
user32.TrackPopupMenu = real_track
user32.DestroyWindow(hwnd)

print("\n%s" % ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
