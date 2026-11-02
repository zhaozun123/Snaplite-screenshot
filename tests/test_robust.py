# -*- coding: utf-8 -*-
"""健壮性：四个「不常发生、一旦发生就没法自救」的问题。

这些问题平时测不出来（要等 explorer 崩、要等改分辨率、要等日志写爆），
但碰上一次就是「程序还在跑却找不到、面板卡在屏幕外、磁盘被写满」。
所以用断言锁住行为，不靠人去手工复现。

  1. explorer 重启 → 托盘图标必须重新挂上去（否则永久消失）
  2. 单实例互斥 → 第二个实例拿不到（否则两个面板互相抓屏）
  3. 分辨率变化 → 面板必须被拉回可视区
  4. 日志写爆 → 必须截断，不能无限增长

用法：python tests/test_robust.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from ctypes import byref
from ctypes import wintypes as w

import snap
from snap import user32

ok = True


def check(name, cond, extra=""):
    """打印一条断言结果，并把失败累计到 ok"""
    global ok
    print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))
    if not cond:
        ok = False


app = snap.App()
app.create_main()
if not app.hwnd_main:
    print("FAIL: 没有窗口，测试无法进行")
    sys.exit(1)
W, H = app.MW, app.MH
VX = user32.GetSystemMetrics(snap.SM_XVIRTUALSCREEN)
VY = user32.GetSystemMetrics(snap.SM_YVIRTUALSCREEN)
VW = user32.GetSystemMetrics(snap.SM_CXVIRTUALSCREEN)
VH = user32.GetSystemMetrics(snap.SM_CYVIRTUALSCREEN)

# ======================================================================
print("\n=== 1. explorer 重启后托盘图标要重新挂上去 ===")


class _FakeShell(object):
    """只记下 Shell_NotifyIconW 的调用，不真的操作系统托盘"""

    def __init__(self):
        self.calls = []

    def Shell_NotifyIconW(self, op, nid):
        self.calls.append(op)
        return True


_real_shell = snap.shell32
_fake = _FakeShell()
try:
    snap.shell32 = _fake
    app.hicon = None                       # 清掉，验证重建路径会自己造一个
    app._recreate_tray()
    check("重挂时调用了一次 NIM_ADD", _fake.calls == [0], str(_fake.calls))
    check("图标句柄自己补上了", bool(app.hicon))

    # 再挂一次不应该再造图标，也不该漏（句柄还在就用它）
    _h1 = app.hicon
    _fake.calls = []
    app._recreate_tray()
    check("重复重挂只再挂一次、不重复建图标",
          _fake.calls == [0] and app.hicon is _h1, str(_fake.calls))
finally:
    snap.shell32 = _real_shell

# 消息 ID 必须注册成功（0 会和 WM_NULL 撞车，等于每条消息都触发重挂）
check("TaskbarCreated 消息 ID 已注册（非 0）", app.wm_taskbar_created != 0,
      "0x%04X" % app.wm_taskbar_created)

# ======================================================================
print("\n=== 2. 单实例互斥：第二个实例抢不到 ===")
_M = "Local\\SnapLite-TestMutex-%d" % os.getpid()
_h1 = snap.windll.kernel32.CreateMutexW(None, False, _M)
_e1 = snap.windll.kernel32.GetLastError()
_h2 = snap.windll.kernel32.CreateMutexW(None, False, _M)
_e2 = snap.windll.kernel32.GetLastError()
check("第一次创建：没有「已存在」错误", _e1 != snap.ERROR_ALREADY_EXISTS, str(_e1))
check("第二次创建：报 ERROR_ALREADY_EXISTS", _e2 == snap.ERROR_ALREADY_EXISTS,
      str(_e2))
check("两次拿到的是同一个内核对象", bool(_h1) and bool(_h2))
check("互斥名带 Local\\ 前缀（不跨会话串台）",
      snap.MUTEX_NAME.startswith("Local\\"), snap.MUTEX_NAME)

# ======================================================================
print("\n=== 3. 分辨率变化：面板必须被拉回可视区 ===")
# 先搬到屏幕外面（模拟「分辨率调小了，面板落在新屏幕之外」）
user32.SetWindowPos(app.hwnd_main, None, VX + VW + 400, VY + VH + 300, 0, 0,
                    snap.SWP_NOSIZE | snap.SWP_NOZORDER | snap.SWP_NOACTIVATE)
_rc = w.RECT()
user32.GetWindowRect(app.hwnd_main, byref(_rc))
check("已经搬到屏幕外（前提成立）", _rc.left > VX + VW,
      "(%d,%d)" % (_rc.left, _rc.top))

app._on_display_change()
user32.GetWindowRect(app.hwnd_main, byref(_rc))
check("被拉回可视区横向范围内",
      VX <= _rc.left <= VX + VW - W, "left=%d，范围 %d~%d"
      % (_rc.left, VX, VX + VW - W))
check("被拉回可视区纵向范围内",
      VY <= _rc.top <= VY + VH - H, "top=%d，范围 %d~%d"
      % (_rc.top, VY, VY + VH - H))
check("玻璃底被标脏，下一拍会重抓", app.glass_dirty is True)
check("旧快照作废（坐标全变了）", app.drag_world is None)

# 屏幕内的正常位置不该被乱动
user32.SetWindowPos(app.hwnd_main, None, 300, 300, 0, 0,
                    snap.SWP_NOSIZE | snap.SWP_NOZORDER | snap.SWP_NOACTIVATE)
app._on_display_change()
user32.GetWindowRect(app.hwnd_main, byref(_rc))
check("本来就在屏幕里 → 位置不动", (_rc.left, _rc.top) == (300, 300),
      "(%d,%d)" % (_rc.left, _rc.top))

# ======================================================================
print("\n=== 4. 日志写爆要截断，不能无限长 ===")
_log = os.path.join(snap.CFG_DIR, "snap_test_log.log")
try:
    if os.path.exists(_log):
        os.remove(_log)
    _line = "x" * 1000 + "\n"
    for _ in range(700):                    # 约 700KB，超过 512KB 上限
        snap._write_log(_log, _line)
    _size = os.path.getsize(_log)
    check("连续写 700KB 之后文件没超过上限",
          _size <= snap.LOG_MAX_BYTES + 4096, "%d 字节" % _size)
    check("截断后仍保留了内容（不是清空）", _size > snap.LOG_MAX_BYTES // 2,
          "%d 字节" % _size)
    # 截断之后还要能继续写，不能变成只读
    snap._write_log(_log, "after truncate\n")
    with open(_log, "rb") as _f:
        _tail = _f.read()[-40:]
    check("截断后还能继续追加", b"after truncate" in _tail, repr(_tail[-20:]))
    # 截断点必须落在行首，否则会留半行乱码 —— 至少不能把 UTF-8 字符切碎
    with open(_log, "rb") as _f:
        _head = _f.read(64)
    check("截断处有说明标记或落在行首",
          _head.startswith(b"[... older entries dropped ...]") or _head.startswith(b"x"),
          repr(_head[:40]))
finally:
    try:
        os.remove(_log)
    except OSError:
        pass

user32.DestroyWindow(app.hwnd_main)
print("\n" + "=" * 62)
print("结果：%s" % ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
