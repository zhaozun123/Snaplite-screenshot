"""验证「截图时主界面不再强行置顶，而是像普通窗口一样被遮罩处理」。

要验的四件事：
1. 主窗口这一帧被补回了 dc_shot（否则遮罩那块会露出桌面 → 看起来“消失”）
2. mn_bits（主窗口真正提交给系统的那一帧）是全透明的 → 真窗口不会盖在遮罩上
3. 没框到主界面时，遮罩里那块 = 压暗（变灰）；框到时 = 原色（变亮）
4. 走一遍“展开工具 / 用工具”的流程后，主窗口仍然没有被画回来（不会突然出现/消失）
"""
import sys
import ctypes

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from ctypes import byref, c_void_p

import snap
from snap import user32, gdi32, w

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

# 句柄是 64 位，GetPixel 不声明 argtypes 会把它们当 int 截断
gdi32.GetPixel.argtypes = [w.HDC, snap.c_int, snap.c_int]
gdi32.GetPixel.restype = w.DWORD

FAIL = []


def check(name, ok, detail=""):
    print(("  [PASS] " if ok else "  [FAIL] ") + name + ("   " + detail if detail else ""))
    if not ok:
        FAIL.append(name)


def px(dc, x, y):
    return gdi32.GetPixel(dc, int(x), int(y))


def rgb(c):
    return (c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)


def mem_bytes(ptr, n):
    return ctypes.string_at(ptr, n)


app = snap.App()
app.create_main()
h = app.hwnd_main
user32.SetWindowPos(h, None, 240, 140, 0, 0, 0x0001 | 0x0004 | 0x0010)  # NOSIZE|NOZORDER|NOACTIVATE

rc = w.RECT()
user32.GetWindowRect(h, byref(rc))
print("主窗口矩形 = (%d,%d) %dx%d" % (rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top))

app._begin_capture()

# ---------- 1. 位置记录 & 主窗口补回抓屏图
vsx, vsy = app.vs[0], app.vs[1]
expect = (rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top)
check("main_rect 与窗口矩形一致", app.main_rect == expect,
      "got=%r want=%r" % (app.main_rect, expect))

mw, mh = app.MH, app.MW
W, H = app.mn_keep_size if app.mn_keep_size else (0, 0)
check("留底表面尺寸与主窗口一致", (W, H) == (app.MW, app.MH), "got=%r" % ((W, H),))

mx, my, mwid, mhgt = app.main_rect
cx, cy = mx + mwid // 2, my + mhgt // 2          # 绝对屏幕坐标
idx = ((cy - my) * W + (cx - mx)) * 4
keep_raw = mem_bytes(app.mn_keep[3], W * H * 4)
ka = keep_raw[idx + 3]
print("主窗口中心像素 alpha=%d" % ka)

got = px(app.dc_shot, cx - vsx, cy - vsy)
# keep 里是 BGRA，GetPixel 给的是 COLORREF(0x00BBGGRR)，统一换成 (R,G,B) 比
keep_c = (keep_raw[idx + 2], keep_raw[idx + 1], keep_raw[idx])
shot_c = rgb(got)
# 预乘 alpha：alpha=255 时 AlphaBlend 结果正好等于源色
check("dc_shot 里该点是主窗口像素", ka == 255 and shot_c == keep_c,
      "shot=%r keep=%r alpha=%d" % (shot_c, keep_c, ka))

dim_c = rgb(px(app.dc_dim, cx - vsx, cy - vsy))
ratio = [dim_c[i] / float(shot_c[i] or 1) for i in range(3)]
check("dc_dim 该点是原色压暗后的值", all(0.33 < r < 0.46 for r in ratio),
      "dim=%r ratio=%.2f/%.2f/%.2f" % (dim_c, ratio[0], ratio[1], ratio[2]))

# ---------- 2. 主窗口提交的帧是全透明
blank_ok = all(b == 0 for b in mem_bytes(app.mn_bits, 64))
# 只看开头 64 字节不够，抽查整块
raw_now = mem_bytes(app.mn_bits, W * H * 4)
check("主窗口当前帧全透明（真窗口不会盖在遮罩上）",
      raw_now.count(0) == len(raw_now), "非零字节=%d" % (len(raw_now) - raw_now.count(0)))

# ---------- 3. 未框选 → 变灰；框选到 → 变亮
app.sel = None
app.auto_rect = None
app.mode = None
app._compose_overlay(app.vs[2], app.vs[3])
back_nosel = rgb(px(app.dc_back, cx - vsx, cy - vsy))
check("没框到主界面时遮罩里是压暗的（变灰）", back_nosel == dim_c,
      "back=%r dim=%r" % (back_nosel, dim_c))

app.sel = (mx - vsx - 40, my - vsy - 40, mwid + 80, mhgt + 80)
app._compose_overlay(app.vs[2], app.vs[3])
back_sel = rgb(px(app.dc_back, cx - vsx, cy - vsy))
check("框到主界面时遮罩里是原色（变亮）", back_sel == shot_c,
      "back=%r shot=%r" % (back_sel, shot_c))

# ---------- 4. 用工具 / 展开选项排之后主窗口仍然不出现
app._begin_mode("pen")          # 里面有 _redraw_overlay + _refresh_tool（SetWindowPos 重排）
for _ in range(6):
    app._refresh_tool()
raw_after = mem_bytes(app.mn_bits, W * H * 4)
check("展开工具后主窗口仍是透明帧（不会莫名冒出/消失）",
      raw_after.count(0) == len(raw_after),
      "非零字节=%d" % (len(raw_after) - raw_after.count(0)))
check("展开工具后 main_rect 没被清掉", app.main_rect == expect, "got=%r" % (app.main_rect,))
back_after = rgb(px(app.dc_back, cx - vsx, cy - vsy))
check("展开工具后遮罩该点仍是主界面原色", back_after == shot_c,
      "back=%r shot=%r" % (back_after, shot_c))

app._leave_mode()
app._close_tool()

# ---------- 5. 收尾后主窗口恢复
app._close_overlay()
raw_end = mem_bytes(app.mn_bits, W * H * 4)
check("取消截图后主窗口内容画回来了", raw_end.count(0) != len(raw_end),
      "非零字节=%d" % (len(raw_end) - raw_end.count(0)))

user32.DestroyWindow(h)
print()
print("结论：" + ("全部通过" if not FAIL else "失败 %d 项 → %s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
