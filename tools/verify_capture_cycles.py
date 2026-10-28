"""连续截图 / 托盘状态下截图 的稳定性检查。

重点是 GDI 句柄不能随着次数增长（每多一份 mn_keep、dc_shot 都是一次 CreateDIBSection），
以及主窗口隐藏时的截图路径不能踩空。
"""
import sys
import ctypes

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from ctypes import byref, c_void_p

import snap
from snap import user32, w

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

kernel32 = ctypes.windll.kernel32
kernel32.GetCurrentProcess.restype = c_void_p
proc = kernel32.GetCurrentProcess()
user32.GetGuiResources.argtypes = [c_void_p, snap.c_uint]
user32.GetGuiResources.restype = snap.c_uint

FAIL = []


def check(name, ok, detail=""):
    print(("  [PASS] " if ok else "  [FAIL] ") + name + ("   " + detail if detail else ""))
    if not ok:
        FAIL.append(name)


def gdi_count():
    return user32.GetGuiResources(proc, 0)


app = snap.App()
app.create_main()
h = app.hwnd_main
user32.SetWindowPos(h, None, 240, 140, 0, 0, 0x0001 | 0x0004 | 0x0010)
n0 = gdi_count()
print("起始 GDI 句柄 =", n0)

# ---------- 连续 8 次截图 / 取消
bad = []
for i in range(8):
    app.start_capture()
    if not app.capturing:
        bad.append((i, "capturing 没置位"))
        continue
    if app.main_rect is None:
        bad.append((i, "main_rect 缺失"))
    if not app.mn_keep:
        bad.append((i, "mn_keep 缺失"))
    app._cancel_capture()
    if app.capturing:
        bad.append((i, "capturing 没归位"))
    if app.main_rect is not None:
        bad.append((i, "main_rect 没清"))
check("连续 8 次截图/取消都正常", not bad, "%r" % (bad[:3],))

n1 = gdi_count()
print("8 次之后 GDI 句柄 =", n1)
check("GDI 句柄没有增长", n1 - n0 <= 4, "增量=%d" % (n1 - n0))

# ---------- 主窗口隐藏（收进托盘）时截图
user32.ShowWindow(h, 0)      # SW_HIDE
app.start_capture()
check("隐藏状态下也能进入截图", app.capturing)
check("隐藏状态不记录 main_rect", app.main_rect is None, "got=%r" % (app.main_rect,))
check("隐藏状态遮罩照常合成", bool(app.dc_back and app.dc_shot))
app._cancel_capture()
check("隐藏状态取消后窗口仍然隐藏", not user32.IsWindowVisible(h))
user32.ShowWindow(h, 5)      # SW_SHOW
snap.dwmapi.DwmFlush()
check("重新显示后窗口可见", bool(user32.IsWindowVisible(h)))

n2 = gdi_count()
print("最终 GDI 句柄 =", n2)
check("全程 GDI 句柄稳定", n2 - n0 <= 6, "增量=%d" % (n2 - n0))

user32.DestroyWindow(h)
print()
print("结论：" + ("全部通过" if not FAIL else "失败 %d 项 → %s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
