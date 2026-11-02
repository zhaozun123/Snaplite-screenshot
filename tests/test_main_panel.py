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

close, pin, hide, btn, hot = app._main_layout()

# ---------- 1. hover 命中
for name, rect, want in (("✕", close, "close"), ("图钉", pin, "pin"),
                         ("—", hide, "hide"),
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

# 拖动期间必须让系统抓屏时把自己摘掉，否则「抓目标位置」会抓到自己的拖影 ——
# 那正是旧版拖动时「画面像叠了一层」的来源
_aff = snap.w.DWORD()
user32.GetWindowDisplayAffinity(hwnd, byref(_aff))
if app.drag_live:
    check("新系统拖动时设为抓屏排除自己", _aff.value == 0x11, "0x%X" % _aff.value)
else:
    print("[SKIP] 本机不支持「抓屏排除自己」，走的是缩略图退路")

# 抬起来要收尾
send(hwnd, WM_LBUTTONUP, 0, pack(x, y))
check("抬手结束拖窗口", not app.main_drag)
user32.GetWindowDisplayAffinity(hwnd, byref(_aff))
check("抬手后抓屏属性已恢复", _aff.value == 0, "0x%X" % _aff.value)
check("抬手后不再走实时抓屏路径", not app.drag_live)

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
      and app._main_hit(*center(pin)) == "pin"
      and app._main_hit(*center(hide)) == "hide"
      and app._main_hit(*center(btn)) == "btn"
      and app._main_hit(*center(hot)) == "hot")

# ---------- 11. 图钉：置顶开关
# 不能只看 self.topmost 这个书签，要真去问窗口的 ex-style —— SetWindowPos 失败
# 时（比如参数传错）状态位照样翻转，界面上的图钉会亮，实际却没置顶。
_gwl = ctypes.windll.user32.GetWindowLongW
_gwl.restype = ctypes.c_long
_gwl.argtypes = [ctypes.c_void_p, ctypes.c_int]
GWL_EXSTYLE = -20


def ex_topmost():
    return bool(_gwl(hwnd, GWL_EXSTYLE) & snap.WS_EX_TOPMOST)


check("初始不置顶", (not app.topmost) and (not ex_topmost()))
send(hwnd, WM_LBUTTONDOWN, 1, pack(*center(pin)))
check("点图钉后置顶真的生效", app.topmost and ex_topmost(),
      "topmost=%r ex=%r" % (app.topmost, ex_topmost()))
check("置顶不改变窗口可见性", bool(user32.IsWindowVisible(hwnd)))
check("置顶不触发拖窗口", not app.main_drag)
send(hwnd, WM_LBUTTONDOWN, 1, pack(*center(pin)))
check("再点一次取消置顶", (not app.topmost) and (not ex_topmost()),
      "topmost=%r ex=%r" % (app.topmost, ex_topmost()))

# 置顶是「面板自己」的属性，不能顺手把别的窗口也顶上去：
# 按钮矩形必须跟 — ✕ 一样都在标题行里，且互不重叠（重叠会点一个触发两个）
check("图钉与 — ✕ 不重叠",
      pin[0] + pin[2] <= hide[0] and hide[0] + hide[2] <= close[0],
      "pin=%s hide=%s close=%s" % (pin, hide, close))
check("三个标题行按钮都在面板内",
      all(r[0] >= 0 and r[0] + r[2] <= app.MW for r in (pin, hide, close)))

# ---------- 12. SetWindowPos 失败时不能假装成功
# 这条锁的是 `_toggle_topmost` 的「回读真实状态」：以前是先翻书签再调 API，
# API 失败时书签照样翻，图钉亮着但窗口没置顶，用户点第二下才「正好」生效。
print("\n=== 12. 置顶失败不能假装成功 ===")
_real_swp = snap.user32.SetWindowPos
snap.user32.SetWindowPos = lambda *a: 0          # 桩：什么都不做，模拟失败
try:
    send(hwnd, WM_LBUTTONDOWN, 1, pack(*center(pin)))
    check("SetWindowPos 没生效时 topmost 不翻转", not app.topmost,
          "topmost=%r" % (app.topmost,))
    check("SetWindowPos 没生效时窗口确实没置顶", not ex_topmost())
finally:
    snap.user32.SetWindowPos = _real_swp
send(hwnd, WM_LBUTTONDOWN, 1, pack(*center(pin)))
check("恢复后置顶仍然可用", app.topmost and ex_topmost(),
      "topmost=%r ex=%r" % (app.topmost, ex_topmost()))
send(hwnd, WM_LBUTTONDOWN, 1, pack(*center(pin)))
check("收回到不置顶，不留状态给后面的检查",
      (not app.topmost) and (not ex_topmost()))

# ---------- 13. 黑帧不能上屏
# 抓屏是整条链路上唯一「失败了也不作声」的环节：BitBlt 对没画上的区域不报错，
# 而 grab_screen 每次都新建位图（新建出来就是全黑）。模糊链路同样不看 GDI+
# 的返回码，黑进黑出，末尾还把 alpha 强制成 255。不拦的话，屏幕上就是面板
# 整块 (8,9,12)、250ms 后又自己变回来 —— 就是用户报的「直接黑了又变回来」。
print("\n=== 13. 黑帧闸门 ===")
import time as _time

_logged = []
_real_write_log = snap._write_log
snap._write_log = lambda path, text: _logged.append((path, text))


def blank_luma():
    """离屏表面里「面板空白处」的平均亮度。这块区域只有玻璃底和压暗层，
    所以它能直接反映这一帧用的是哪张玻璃底。"""
    W, H = app.MW, app.MH
    raw = ctypes.string_at(app.mn_bits, W * H * 4)
    tot = cnt = 0
    for yy in range(136, H - 8):
        for xx in range(26, W - 26):
            o = (yy * W + xx) * 4
            tot += raw[o] + raw[o + 1] + raw[o + 2]
            cnt += 3
    return tot / float(cnt) if cnt else 0.0


W, H = app.MW, app.MH
total_bytes = W * H * 4

# 三通道平均，不能只采一个通道：深红/深黄背景的 B 通道本来就接近 0，
# 只看 B 会把正常画面判成黑帧 —— 那样闸门就成了「随机冻住面板」。
red = bytearray(total_bytes)
for _i in range(0, total_bytes, 4):
    red[_i + 2] = 200          # R
    red[_i + 3] = 255
check("深红背景不会被当成黑帧", snap._frame_luma(bytes(red), W, H) > 60,
      "luma=%.1f" % snap._frame_luma(bytes(red), W, H))

app._refresh_glass(blank=False)          # 先拿到一帧真实背景当基准
app._present_main()
base = blank_luma()
check("基准帧正常（有背景可看）", base > 5, "luma=%.1f" % base)

app.mn_glass = bytes(total_bytes)        # 注入一整块纯黑（等价于抓屏抓黑）
app._present_main()
check("黑帧被拦下，画面保持上一帧", abs(blank_luma() - base) < 4,
      "before=%.1f after=%.1f" % (base, blank_luma()))
check("mn_glass 同时回退，不给下一次重绘留黑底",
      snap._frame_luma(app.mn_glass, W, H) > 5,
      "mn_glass luma=%.1f" % snap._frame_luma(app.mn_glass, W, H))

app.mn_glass = bytes(total_bytes)
app._present_main()
check("连续第二帧黑照样拦", abs(blank_luma() - base) < 4,
      "luma=%.1f" % blank_luma())

check("坏帧写了一条现场日志",
      any(p == snap.GLASS_LOG for p, _ in _logged),
      "共 %d 条" % len(_logged))
check("日志里同时记了原始帧和玻璃底的亮度（用来区分是抓屏黑还是模糊黑）",
      any("raw=" in t and "glass=" in t for _, t in _logged))

# 背景是真的变黑了（用户切到全黑窗口）：不能永远冻着，一秒后认下来。
# 这里直接把「第一次判黑的时刻」往前挪，不真等一秒。
app.glass_black_since = _time.time() - snap.GLASS_BLACK_HOLD - 0.1
app.mn_glass = bytes(total_bytes)
app._present_main()
# 注意阈值不是 0：黑玻璃底上屏还要压一层 0x80101217，出来是 (8,9,12)、
# 平均 9.7 —— 这正是用户截图里量到的那个值，也是「黑一下」的指纹。
check("真黑超过一秒后放行（不会永久冻住画面）", blank_luma() < base - 8,
      "luma=%.1f（黑底上屏应为 ~9.7）" % blank_luma())
check("放行后基准也变成黑，不再反复触发", app.glass_good_luma < 3,
      "good=%.1f" % app.glass_good_luma)

# 恢复：背景回到亮的，闸门不能挡 —— 只拦「由亮变黑」，不拦「由黑变亮」。
app._refresh_glass(blank=False)
app._present_main()
check("背景变回正常后立刻跟上", blank_luma() > 5,
      "luma=%.1f" % blank_luma())

snap._write_log = _real_write_log

user32.DestroyWindow(hwnd)
app.probe.stop() if hasattr(app.probe, "stop") else None
print()
print("结果：%s" % ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
