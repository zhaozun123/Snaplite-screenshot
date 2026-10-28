"""真抓屏验证：遮罩显示后，屏幕上主界面那块到底是“变灰”“变亮”还是“真置顶”。

内部缓冲对不代表屏幕上对 —— 真窗口如果还压在遮罩上面，抓屏抓到的就是原色，
这正是之前的 bug。所以这里直接抓屏比对：

  A：截图前（主窗口正常显示）
  B：遮罩展开、没框选 → 应该是 A 压暗（≈0.40 倍）
  C：框选盖住主窗口 → 应该回到 A 原色
  D：展开工具 / 切画笔之后 → 仍然是 C，主界面不能消失
"""
import sys
import ctypes

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from ctypes import byref, c_void_p

import snap
from snap import user32, w, dwmapi

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

FAIL = []


def check(name, ok, detail=""):
    print(("  [PASS] " if ok else "  [FAIL] ") + name + ("   " + detail if detail else ""))
    if not ok:
        FAIL.append(name)


def flush():
    try:
        dwmapi.DwmFlush()
    except Exception:
        pass


def grab(vs):
    return snap.grab_screen(vs[0], vs[1], vs[2], vs[3]).raw


def sample(raw, vsw, vsx, vsy, x, y):
    i = ((y - vsy) * vsw + (x - vsx)) * 4
    return (raw[i + 2], raw[i + 1], raw[i])


def dist(a, b):
    return max(abs(a[i] - b[i]) for i in range(3))


app = snap.App()
app.create_main()
h = app.hwnd_main
user32.SetWindowPos(h, None, 240, 140, 0, 0, 0x0001 | 0x0004 | 0x0010)

rc = w.RECT()
user32.GetWindowRect(h, byref(rc))
pts = [(rc.left + 40, rc.top + 60),            # 按钮左侧空白
       (rc.left + rc.right - rc.left - 60, rc.top + 90),
       (rc.left + 100, rc.top + rc.bottom - rc.top - 20)]
print("采样点 = %r" % (pts,))

vs = (user32.GetSystemMetrics(0x4C), user32.GetSystemMetrics(0x4D),   # XVIRTUALSCREEN / YVIRTUALSCREEN
      user32.GetSystemMetrics(0x4E), user32.GetSystemMetrics(0x4F))
vsw = vs[2]
flush()
raw_a = grab(vs)

app._begin_capture()
vs = app.vs
vsw = vs[2]
# 打开遮罩时鼠标在屏幕中央，自动识别可能已经高亮了一块（甚至正好框住主窗口），
# 那主界面当然是亮的 —— 这里先清掉，只验“什么都没框”的状态
app.auto_rect = None
app.sel = None
app._redraw_overlay()
flush()
raw_b = grab(vs)

# B = A 压暗
bad = []
for (x, y) in pts:
    a = sample(raw_a, vsw, vs[0], vs[1], x, y)
    b = sample(raw_b, vsw, vs[0], vs[1], x, y)
    ratio = [b[i] / float(a[i] or 1) for i in range(3)]
    if not all(0.30 < r < 0.50 for r in ratio):
        bad.append((x, y, a, b, ratio))
check("遮罩展开后主界面变灰（不是原来的亮块）", not bad, "%r" % (bad[:2],))

# C = 框选盖住 → 原色
mx, my, mw, mh = app.main_rect
app.sel = (mx - vs[0] - 40, my - vs[1] - 40, mw + 80, mh + 80)
app._redraw_overlay()
flush()
raw_c = grab(vs)
bad = []
for (x, y) in pts:
    a = sample(raw_a, vsw, vs[0], vs[1], x, y)
    c = sample(raw_c, vsw, vs[0], vs[1], x, y)
    if dist(a, c) > 3:
        bad.append((x, y, a, c))
check("框选盖住主界面时恢复原色（跟截图内容一致）", not bad, "%r" % (bad[:2],))

# D = 用工具之后不能消失
app._begin_mode("pen")
for _ in range(4):
    app._refresh_tool()
flush()
raw_d = grab(vs)
bad = []
for (x, y) in pts:
    c = sample(raw_c, vsw, vs[0], vs[1], x, y)
    d = sample(raw_d, vsw, vs[0], vs[1], x, y)
    if dist(c, d) > 6:
        bad.append((x, y, c, d))
check("展开工具/切画笔后主界面没消失、没变形", not bad, "%r" % (bad[:2],))

app._leave_mode()
app._close_tool()

# 取消截图后回到 A
app._close_overlay()
flush()
raw_e = grab(vs)
bad = []
for (x, y) in pts:
    a = sample(raw_a, vsw, vs[0], vs[1], x, y)
    e = sample(raw_e, vsw, vs[0], vs[1], x, y)
    if dist(a, e) > 6:
        bad.append((x, y, a, e))
check("取消截图后主界面回到原样", not bad, "%r" % (bad[:2],))

user32.DestroyWindow(h)
print()
print("结论：" + ("全部通过" if not FAIL else "失败 %d 项 → %s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
