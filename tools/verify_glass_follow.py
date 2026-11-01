# -*- coding: utf-8 -*-
"""验证两件事：

1. 主面板不再带 WS_EX_TOPMOST（不再是「永远置顶」）。
2. 桌面变了以后，玻璃底会自己重算 —— 而且算出来的是**真实背景**，
   不是在拿自己当背景（自我递归）、也不是停在旧画面。

做法（不建额外窗口，避免 ctypes 造窗口类踩坑）：
  面板放 A 点 → 记下玻璃底 → 挪到 B 点 → 模拟「别的窗口动了」→ 触发一次刷新
  → 把面板挪开，抓 B 点露出来的桌面当**像素级真值** → 和玻璃底比。

为什么真值要「挪开面板再抓」，而不是「先把自己透明掉再抓」：
后者要提交一帧全透明并等 DWM 合成（40~50ms，肉眼能看见闪），只能用来
建首次底；真正要验的是「面板还盖着的时候，算出来的背景对不对」，那就必须
拿同一时刻、同一块区域的真实桌面来比 —— 把面板移开正好能得到它。
"""
import sys
import os
import time
import ctypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from ctypes import byref, c_void_p, c_int

import snap
from snap import user32, w, dwmapi, grab_screen

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

ok = True


def check(name, cond, extra=""):
    global ok
    mark = "PASS" if cond else "FAIL"
    if not cond:
        ok = False
    print("[%s] %s %s" % (mark, name, extra))


def mean_rgb(buf):
    """mn_glass 是预乘 BGRA，取平均亮度做粗比对"""
    if not buf:
        return None
    n = len(buf) // 4
    step = max(1, n // 4000)          # 抽样，别把 12 万像素全走一遍
    tot = cnt = 0
    for i in range(0, n, step):
        o = i * 4
        tot += buf[o] + buf[o + 1] + buf[o + 2]
        cnt += 1
    return tot / float(cnt * 3) if cnt else None


def mean_abs(a, b):
    """两张 BGRA 图的平均逐像素差（抽样，够看差异量级）

    注意别写成 `x or -1`：完全相等时结果是 0.0，那是 falsy，会被当成「没算出来」。"""
    if not a or not b or len(a) != len(b):
        return None
    n = len(a) // 4
    step = max(1, n // 6000)
    tot = cnt = 0
    for i in range(0, n, step):
        o = i * 4
        tot += (abs(a[o] - b[o]) + abs(a[o + 1] - b[o + 1])
                + abs(a[o + 2] - b[o + 2])) / 3.0
        cnt += 1
    return tot / cnt


GWLP_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010

app = snap.App()
app.create_main()

_getlong = user32.GetWindowLongW
_getlong.argtypes = [w.HWND, c_int]
_getlong.restype = c_int
ex = _getlong(app.hwnd_main, GWLP_EXSTYLE)
check("主窗口不再置顶", (ex & WS_EX_TOPMOST) == 0, "exstyle=0x%08X" % (ex & 0xFFFFFFFF))

user32.SetWindowPos(app.hwnd_main, None, 320, 240, 0, 0,
                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
time.sleep(0.25)
app._refresh_glass(blank=False)
app.glass_dirty = False
glass_a = bytes(app.mn_glass or b"")
mean_a = mean_rgb(glass_a)
check("A 点玻璃底已生成", bool(glass_a), "mean=%.1f" % (mean_a or -1))

# ---------- 1. 没变化时不该重算
app._tick_glass()
check("桌面没变时零开销", bytes(app.mn_glass) == glass_a)

# ---------- 2. 钩子回调会置位
app._on_desktop_event(0, 0, 0, 0, 0, 0, 0)
check("钩子回调置位", app.glass_dirty is True)
check("钩子确实装上了", bool(app.glass_hook), "hook=%s" % (app.glass_hook,))

# ---------- 3. 挪到 B 点：桌面变了，自动刷新要跟得上
user32.SetWindowPos(app.hwnd_main, None, 760, 470, 0, 0,
                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
time.sleep(0.25)
app.glass_dirty = True
t0 = time.perf_counter()
app._tick_glass()
dt = (time.perf_counter() - t0) * 1000.0
glass_b = bytes(app.mn_glass or b"")
mean_b = mean_rgb(glass_b)
check("自动刷新真的换了画面", glass_b != glass_a,
      "mean %.1f → %.1f" % (mean_a or -1, mean_b or -1))
check("单次刷新耗时可接受（<60ms）", dt < 60.0, "%.1f ms" % dt)

# ---------- 4. 画的是真背景：把面板挪开，抓同一块露出来的桌面当真值
BX, BY = 760, 470                    # 上面已经挪到这里了
W, H = app.MW, app.MH
app._refresh_glass_live()            # 面板还在 B 点，算一遍玻璃底
time.sleep(0.1)
live = bytes(app.mn_glass or b"")
mine = ctypes.string_at(app.mn_bits, W * H * 4)    # 面板自身长什么样

# 先证明「自校验」本身是有效的：这会儿面板就在 B 点，不设排除属性直接抓，
# 抓回来的必然是面板自己 —— _looks_like_self 必须认出来。它要是恒返回 False，
# 后面「没抓到自己」的结论就全是假的。
raw_self = grab_screen(BX, BY, W, H).raw
check("自校验能认出「抓到的是自己」", app._looks_like_self(raw_self, W, H) is True,
      "与面板差 %.1f" % (mean_abs(raw_self, mine) or -1))

user32.SetWindowPos(app.hwnd_main, None, 60, 60, 0, 0,   # 让开 B 点
                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
try:
    dwmapi.DwmFlush()
except Exception:
    pass
time.sleep(0.35)
truth_raw = grab_screen(BX, BY, W, H).raw
truth = app._blur_bgra(truth_raw, W, H)       # 和玻璃底同一种模糊，才可比

# 再抓一次同一块，量一下「桌面本身在这段时间里变了多少」。
# 这条判据的麻烦在于它依赖真实桌面：托盘时钟跳秒、窗口重绘、光标闪，
# 都会让两次抓取天然不同 —— 固定阈值要么太松放过真问题，要么太紧天天误报。
# 所以先测出当下的噪声，阈值跟着它走。
time.sleep(0.2)
truth2 = app._blur_bgra(grab_screen(BX, BY, W, H).raw, W, H)
noise = mean_abs(truth, truth2) or 0.0

# 判据说明（都是**量级判据**，不是像素级等值）：
#   · 干净时 live 与真值差 0.2~2（同一块画面、同一种模糊）；
#   · 「抓到面板自己」是另一个量级：与面板自身差会掉到 0~5，所以下界取 20；
#   · 上限 = 噪声的 4 倍 + 8，封顶 40 —— 桌面自己在动时自动放宽，
#     但不会放宽到「和噪声一样大」从而失去意义。
limit = min(40.0, max(18.0, noise * 4.0 + 8.0))
d_truth = mean_abs(live, truth)
d_mine = mean_abs(live, mine)
check("自动刷新的玻璃底 ≈ 真实背景",
      d_truth is not None and d_truth < limit,
      "平均逐像素差 %.1f / 阈值 %.1f（桌面噪声 %.1f）"
      % (d_truth if d_truth is not None else -1, limit, noise))
check("玻璃底不是面板自己（没自我递归）", d_mine is not None and d_mine > 20.0,
      "与面板自身差 %.1f（抓到自己时会掉到 0~5）"
      % (d_mine if d_mine is not None else -1))

user32.DestroyWindow(app.hwnd_main)
print("\n结果：%s" % ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
