# -*- coding: utf-8 -*-
"""「抓屏排除自己」的归属 + 拖动时不吃脏帧。

为什么单独有这个测试：真实缺陷是「界面不动的时候，面板自己突然黑一下又
好了」。根因不是模糊也不是属性本身（反复切换实测屏幕亮度一个数都没变），
而是**同一个窗口属性被三处各自 set/clear**：

  · 空闲探测  抓之前设 0x11，抓完立刻清回 0
  · 活跃线程  活着期间常设，退出时清回 0
  · 拖动      整段占用

谁清谁的时序一错，就会在别人抓屏的那一刻被清成 0，抓回来的是面板自己。
模糊之后整块发暗、下一帧又正常 —— 就是「黑一下」。

这条链路没法靠肉眼复现（概率低、一闪而过），所以用断言盯住三件事：
  1. 属性有归属，别人清不掉
  2. 抓到自己要认出来，不能拿去上屏
  3. 拿不到干净背景时，拖动**不许**只搬窗口不换背景

用法：python tests/test_glass_guard.py
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from ctypes import byref, c_void_p
from ctypes import wintypes as w

import snap
from snap import user32

ok = True


def check(name, cond, extra=""):
    global ok
    print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))
    if not cond:
        ok = False


snap.GLASS_USE_LIVE_THREAD = False
snap.SetProcessDpiAwarenessContext(c_void_p(-4))

app = snap.App()
app.create_main()
W, H = app.MW, app.MH

if not app._can_exclude_from_capture():
    print("这台机器的 Windows 版本不支持「抓屏排除自己」，本测试跳过")
    sys.exit(0)


def affinity():
    v = w.DWORD()
    user32.GetWindowDisplayAffinity(app.hwnd_main, byref(v))
    return v.value


# ---------------------------------------------------------------- 1. 归属
print("\n=== 1. 「抓屏排除自己」只认主人 ===")
app._excl_reset()
check("初始状态属性是关的", affinity() == 0, "0x%X" % affinity())

check("拖动把它设上了", app._excl_acquire("drag"))
check("设置后属性 = 0x11", affinity() == 0x11, "0x%X" % affinity())
check("主人是 drag", app._excl_owner == "drag", str(app._excl_owner))

# 空闲探测抓完会 release("tick") —— 它不该把拖动正在用的属性清掉
app._excl_release("tick")
check("别人清不动（属性仍为 0x11）", affinity() == 0x11, "0x%X" % affinity())
check("主人还是 drag", app._excl_owner == "drag", str(app._excl_owner))

# 线程起来时也不许抢归属，否则它一收工就把拖动的属性带走了
check("线程 acquire 返回可用", app._excl_acquire("live"))
check("但归属没被抢走", app._excl_owner == "drag", str(app._excl_owner))
app._excl_release("live")
check("线程收工不清属性", affinity() == 0x11, "0x%X" % affinity())

app._excl_release("drag")
check("主人松手后属性归零", affinity() == 0, "0x%X" % affinity())
check("归属也清空了", app._excl_owner is None, str(app._excl_owner))

app._excl_acquire("live")
app._excl_reset()
check("_excl_reset 无条件清掉", affinity() == 0 and app._excl_owner is None,
      "0x%X" % affinity())

# ---------------------------------------------------------------- 2. 自检
print("\n=== 2. 抓回来的画面像不像面板自己 ===")
app._present_main()                      # 让 mn_bits 有一帧真实内容
mine = ctypes.string_at(app.mn_bits, W * H * 4)
check("原样返回 → 判为抓到自己", app._looks_like_self(mine, W, H))

other = bytearray(mine)
for i in range(0, len(other), 4):
    other[i] = 255 - other[i]
    other[i + 1] = 255 - other[i + 1]
    other[i + 2] = 255 - other[i + 2]
check("反色背景 → 判为不是自己", not app._looks_like_self(bytes(other), W, H))

garbage = bytes(W * H * 4)
check("全黑背景 → 判为不是自己", not app._looks_like_self(garbage, W, H))

# ---------------------------------------------------------------- 3. 脏帧不上屏
print("\n=== 3. 抓到自己时不许拿去上屏 ===")
real_grab = snap.grab_screen
calls = {"n": 0}


class FakeBuf(object):
    def __init__(self, data):
        self.raw = data


STUB = {"ox": 0, "oy": 0}      # 面板像素在抓回来的大图里该摆哪儿


def grab_self(x, y, ww, hh):
    calls["n"] += 1
    mine = ctypes.string_at(app.mn_bits, W * H * 4)
    if ww == W and hh == H:
        return FakeBuf(mine)
    # 拖动取背景抓的是「面板外扩一圈」的大块，判据只看窗口对应的那一块。
    # 摆放位置必须用 _drag_world_rect 实际算出来的偏移：它会把左上角对齐到
    # 模糊网格，所以**不再等于** left-pad，写死 pad 会摆错地方。
    ox, oy = STUB["ox"], STUB["oy"]
    buf = bytearray(ww * hh * 4)
    for r in range(H):
        do = ((oy + r) * ww + ox) * 4
        so = r * W * 4
        buf[do:do + W * 4] = mine[so:so + W * 4]
    return FakeBuf(bytes(buf))


snap.grab_screen = grab_self
try:
    # 这一段盯的是「连续抓到自己」时的重试与放弃，测的是同步抓那条退路，
    # 所以先把预取关掉，让 _grab_behind 直接现抓（预取开着时它只裁快照）。
    snap.DRAG_PREFETCH = False
    app.drag_world = None
    calls["n"] = 0
    _pad = app.DRAG_WORLD_PAD
    _gx, _gy, _, _ = app._drag_world_rect(100, 100, _pad)
    STUB["ox"], STUB["oy"] = 100 - _gx, 100 - _gy
    app._excl_acquire("drag")
    out = app._grab_behind(100, 100)
    check("连续两次都抓到自己 → 返回 None", out is None,
          repr(out)[:60] if out is not None else "")
    check("确实重试了两次", calls["n"] == 2, "%d 次" % calls["n"])

    # ------------------------------------------------------- 4. 不许只搬窗口
    print("\n=== 4. 拿不到干净背景，窗口一帧都不许动 ===")
    # 回到真实路径：快照没建 → 补抓 → 补抓到的还是自己 → 这一帧不许动
    snap.DRAG_PREFETCH = True
    app.drag_world = None
    app.main_drag = True
    app.drag_live = True
    app.drag_last_pos = None
    rc0 = w.RECT()
    user32.GetWindowRect(app.hwnd_main, byref(rc0))
    glass_before = app.mn_glass
    _l, _t = rc0.left + 40, rc0.top + 40
    _gx, _gy, _, _ = app._drag_world_rect(_l, _t, snap.DRAG_PAD)
    STUB["ox"], STUB["oy"] = _l - _gx, _t - _gy
    drew = app._drag_frame(_l, _t)
    rc1 = w.RECT()
    user32.GetWindowRect(app.hwnd_main, byref(rc1))
    check("这一帧没画", drew is False)
    check("窗口位置没变", (rc1.left, rc1.top) == (rc0.left, rc0.top),
          "(%d,%d) → (%d,%d)" % (rc0.left, rc0.top, rc1.left, rc1.top))
    check("玻璃底没被换掉", app.mn_glass is glass_before)
    check("位置记忆没前进（下一次还会重试）", app.drag_last_pos is None,
          str(app.drag_last_pos))
finally:
    snap.grab_screen = real_grab
    snap.DRAG_PREFETCH = True
    app.drag_world = None
    app.main_drag = False
    app.drag_live = False
    app._excl_reset()

# ---------------------------------------------------------------- 5. 正常帧照旧
print("\n=== 5. 干净的背景照常上屏 ===")
# 这一段必须换成「干净背景」的桩。上面那个 grab_self 摆的是面板像素，一旦
# 窗口位置让面板那一块整块落进裁剪范围，就会被指纹判成「抓到自己」而拒收 ——
# 这条断言原来有约 1/3 概率误报，根因是桩的摆放位置和窗口位置耦合，不是产品
# 问题。换成纯背景色之后结果是确定的：干净背景就该照常上屏。


def grab_clean(x, y, ww, hh):
    calls["n"] += 1
    return FakeBuf(bytes([40, 42, 46, 255]) * (ww * hh))


snap.grab_screen = grab_clean
app.main_drag = True
app.drag_live = True
app.drag_last_pos = None
app._excl_acquire("drag")
rc0 = w.RECT()
user32.GetWindowRect(app.hwnd_main, byref(rc0))
drew = app._drag_frame(rc0.left + 20, rc0.top + 10)
rc1 = w.RECT()
user32.GetWindowRect(app.hwnd_main, byref(rc1))
check("这一帧画了", drew is True)
check("窗口搬到了目标位置", (rc1.left, rc1.top) == (rc0.left + 20, rc0.top + 10),
      "(%d,%d)" % (rc1.left, rc1.top))
check("位置记忆跟上了", app.drag_last_pos == (rc0.left + 20, rc0.top + 10),
      str(app.drag_last_pos))
app.main_drag = False
app.drag_live = False
app._excl_reset()
snap.grab_screen = grab_self      # 后面几节还要用「抓到自己」的桩

# ---------------------------------------------------------------- 6. 后台预取快照
print("\n=== 6. 拖动取背景：主线程只裁、绝不自己抓 ===")
snap.DRAG_PREFETCH = True
app.drag_world = None
calls["n"] = 0


def grab_counted(x, y, ww, hh):
    """数次数 + 给干净背景。

    这里刻意**不用** grab_self：它摆的是面板像素，摆放位置由第 3 节按 left=100
    算出来，而本节用的是 600/400，一旦两边偏移凑巧重叠就会被判成「抓到自己」
    而整段失败 —— 那和本节要验证的东西（只裁不抓、尺寸对齐）毫无关系。"""
    calls["n"] += 1
    return FakeBuf(bytes([40, 42, 46, 255]) * (ww * hh))


snap.grab_screen = grab_counted
try:
    app._excl_acquire("drag")
    # 快照还没建起来：必须直接返回 None，绝不能顺手在主线程抓一次。
    # 主线程抓一次要等 DWM 5~28ms，等待时长随机，一次就把帧节奏打乱。
    got = app._grab_behind(600, 400)
    check("没快照就返回 None（主线程不抓屏）", got is None, repr(got))
    check("确实一次都没抓", calls["n"] == 0, "%d 次" % calls["n"])

    # 首帧补抓（按下那一刻做，手还没动）：走 store 路径存一张大的当起点
    raw = app._grab_behind_now(600, 400, store=True, pad=snap.DRAG_PAD)
    check("补抓回来了正好的面板块", raw is not None and len(raw) == W * H * 4)
    check("补抓建立了快照", app.drag_world is not None)
    pad = snap.DRAG_PAD
    wx, wy, ww, wh, wb = app.drag_world
    gx0, gy0, gw0, gh0 = app._drag_world_rect(600, 400, pad)
    check("快照盖得住面板并留出 pad 余量",
          (ww, wh) == (gw0, gh0) and ww >= W + pad * 2 and wh >= H + pad * 2,
          "%dx%d" % (ww, wh))
    check("快照尺寸是模糊网格的整数倍（否则条纹相位会随尺寸漂）",
          ww % app.BLUR_F == 0 and wh % app.BLUR_F == 0,
          "%dx%d 网格 %d" % (ww, wh, app.BLUR_F))
    check("快照左上角对齐到模糊网格",
          (wx, wy) == (gx0, gy0) and wx % app.BLUR_F == 0 and wy % app.BLUR_F == 0,
          "%dx%d @ (%d,%d)" % (ww, wh, wx, wy))
    check("窗口位置变了，快照尺寸也不许变（网格才不会错开）",
          app._drag_world_rect(700, 500, pad)[2:] == (gw0, gh0),
          "%s vs %s" % (app._drag_world_rect(700, 500, pad)[2:], (gw0, gh0)))

    n0 = calls["n"]
    inside = app._grab_behind(600 + pad - 4, 400 + pad - 4)   # 仍在快照内
    check("快照范围内不再抓屏", calls["n"] == n0, "又抓了 %d 次" % (calls["n"] - n0))
    check("裁出来的大小对", inside is not None and len(inside) == W * H * 4)

    out = app._grab_behind(600 + pad * 3, 400)                # 走出右边界
    check("走出快照 → 返回 None，交给调用方补抓", out is None, repr(out))

    # 补抓只服务当前这一帧，绝不许顶替后台维护的那张大图：一旦换了，覆盖从
    # pad 掉到 DRAG_PAD_SMALL，下一帧立刻又不够，会把补抓喂成连续补抓。
    keep = app.drag_world
    calls["n"] = 0
    patched = app._grab_behind_now(600 + pad * 3, 400)
    check("补抓能拿到画面（不靠后台那张图）",
          patched is not None and len(patched) == W * H * 4)
    check("补抓不顶替后台快照", app.drag_world is keep)
    check("补抓只抓一小块，不动大图",
          calls["n"] == 1 and app.drag_world[2] == keep[2],
          "抓了 %d 次，快照宽 %d" % (calls["n"], app.drag_world[2]))

    # 新方案的核心保证：同一张快照里裁两个位置，内容必须**严格平移**。
    # 这是「拖动时条纹不抖」的根据 —— 模糊只对整块做过一次，降采样网格相对
    # 屏幕固定；而旧写法每帧裁完再模糊，网格跟着裁切点走，同一段壁纸每帧的
    # 条纹位置都在变（实测偏差 4.4~5.9/255，随偏移量以 BLUR_F 为周期摆动）。
    a1 = app._grab_behind(600, 400)
    a2 = app._grab_behind(608, 400)
    step = 8
    tot = cnt = 0
    for y in range(4, H - 4, 4):
        base = y * W
        for x in range(4, W - step - 4, 3):
            o1 = (base + x + step) * 4
            o2 = (base + x) * 4
            tot += (abs(a1[o1] - a2[o2]) + abs(a1[o1 + 1] - a2[o2 + 1])
                    + abs(a1[o1 + 2] - a2[o2 + 2]))
            cnt += 3
    shift_diff = tot / float(cnt)
    check("同一快照内裁切严格等于平移（条纹不会抖）", shift_diff < 0.5,
          "差 %.2f（旧方案约 4~6）" % shift_diff)
finally:
    snap.grab_screen = real_grab      # 回到最初的真抓屏，桩全部撤掉
    snap.DRAG_PREFETCH = True
    app.drag_world = None
    app.drag_live = False
    app._excl_reset()

# ---------------------------------------------------------------- 6b. 预取线程起停
print("\n=== 6b. 预取线程跟着拖动起停 ===")
app.main_drag = True
app.drag_live = True
app.drag_target = (500, 400)
started = app._start_drag_prefetch()
check("线程起来了", started and app.drag_thread is not None and app.drag_thread.is_alive())
deadline = time.time() + 2.0
while time.time() < deadline and app.drag_world is None:
    time.sleep(0.01)
check("线程自己拍出了快照", app.drag_world is not None)
snap_grab = app.drag_world
app._end_drag_glass()
check("收尾后线程已停", app.drag_thread is None)
check("收尾后快照已清", app.drag_world is None)
check("收尾后目标位置已清", app.drag_target is None)

# ---------------------------------------------------------------- 6c. 贴屏边不许补黑边
print("\n=== 6c. 快照必须钳在屏内（否则面板会发黑） ===")
# 真实缺陷：外扩 240 像素之后，窗口离屏幕边不到 240 就开始越界，grab_screen
# 会拿边缘像素把越界处铺满，铺出来是一大片纯色；实测贴着右边缘往里推，面板
# 平均亮度从 167 一路掉到 43（几乎全黑）——用户说的「一会黑一下」就是这个。
_VX = user32.GetSystemMetrics(76)
_VY = user32.GetSystemMetrics(77)
_VW = user32.GetSystemMetrics(78)
_VH = user32.GetSystemMetrics(79)
_SCW = user32.GetSystemMetrics(0)
_SCH = user32.GetSystemMetrics(1)
_pad = snap.DRAG_PAD
for _name, _l, _t in (("贴右下角", _VX + _VW - W, _VY + _VH - H),
                      ("贴左上角", _VX, _VY),
                      ("窗口已出屏", _VX + _VW + 50, _VY + 50)):
    _gx, _gy, _gw, _gh = app._drag_world_rect(_l, _t, _pad)
    check("%s：快照没越出屏幕" % _name,
          _gx >= _VX and _gy >= _VY and _gx + _gw <= _VX + _VW and _gy + _gh <= _VY + _VH,
          "(%d,%d) %dx%d  屏 (%d,%d) %dx%d" % (_gx, _gy, _gw, _gh, _VX, _VY, _VW, _VH))
_gx, _gy, _gw, _gh = app._drag_world_rect(_VX + _VW - W, _VY + _VH - H, _pad)
check("贴右下角：快照仍然盖得住窗口",
      _gx <= _VX + _VW - W and _gy <= _VY + _VH - H
      and _gx + _gw >= _VX + _VW and _gy + _gh >= _VY + _VH,
      "(%d,%d) %dx%d" % (_gx, _gy, _gw, _gh))
# ---------------------------------------------------------------- 7. 迟到的后台帧
print("\n=== 7. 拖动中迟到的后台帧不许贴上去 ===")
app.main_drag = True
app.drag_live = True
app.mn_glass = b"\x11" * (W * H * 4)
before = app.mn_glass
app.glass_slot = b"\x22" * (W * H * 4)      # 线程在拖动开始前算好的旧位置画面
app.glass_posted = True
app._glass_frame_ready()
check("拖动中丢弃了那条消息", app.mn_glass is before)
check("glass_slot 已清空", app.glass_slot is None)
check("glass_posted 已复位（不会把线程卡住）", app.glass_posted is False)

app.main_drag = False
app.glass_slot = b"\x33" * (W * H * 4)
app._glass_frame_ready()
check("不在拖动时照常贴上去", app.mn_glass[:4] == b"\x33" * 4)
app.drag_live = False

# ---------------------------------------------------------------- 8. 指纹判据
print("\n=== 8. 抓到面板自己必须认出来（拖动中逐像素比会漏）===")
# 为什么补这一节：拖动时 mn_bits 里的玻璃底是**上一个位置**的桌面，拿它当基准
# 逐像素比，「抓到面板自己」那张反而对不上 → 漏判 → 整块发暗。改用按钮纯色当
# 锚点，背景怎么换都认得出。
app._present_main()
_selfbuf = ctypes.string_at(app.mn_bits, W * H * 4)
_bx0, _bx1, _by0, _by1 = app._self_btn_rect()
check("取样框落在面板内",
      0 <= _bx0 < _bx1 <= W and 0 <= _by0 < _by1 <= H,
      "x%d..%d y%d..%d" % (_bx0, _bx1, _by0, _by1))
check("面板样张判为「自己」", app._is_self_tile(_selfbuf, W, H))
check("_looks_like_me 也认", app._looks_like_me(_selfbuf, W, H))

_bg = bytearray(_selfbuf)
for _y in range(_by0, _by1):
    for _x in range(_bx0, _bx1):
        _o = (_y * W + _x) * 4
        _bg[_o], _bg[_o + 1], _bg[_o + 2] = 40, 42, 46
check("换成桌面色后不再判为自己", not app._is_self_tile(bytes(_bg), W, H))

# 模拟拖动：背景整个换掉（另一个位置的桌面），只有 UI 不变
_moved = bytearray(b"\x78" * (W * H * 4))
for _y in range(_by0, _by1):
    for _x in range(_bx0, _bx1):
        _o = (_y * W + _x) * 4
        _moved[_o:_o + 4] = _selfbuf[_o:_o + 4]
check("背景整块换掉后，指纹仍认得出", app._is_self_tile(bytes(_moved), W, H))
check("同一张：老判据（逐像素比）确实漏掉了",
      not app._looks_like_self(bytes(_moved), W, H))
check("所以 _looks_like_me 靠指纹兜住了这个漏判",
      app._looks_like_me(bytes(_moved), W, H))

# ======================================================================
print("\n=== 9. 拖动余量：面积守住、按方向变长（这是「快拖抖」的唯一解）===")
# 正方形余量在运动方向只有 96px，快拖两三帧就出界，主线程只能同步补抓
# （一次 15~45ms），那就是「慢一拍 + 抖」。改法是面积不变、只改形状：
# 面积守住 → 「抓 + 模糊」耗时不变 → 主线程不变慢（加大余量那条路就死在这）。
_f = snap.App.BLUR_F
_area_lo = snap.DRAG_AREA * 0.94
_area_hi = snap.DRAG_AREA * 1.06
_bad = []
_front = {}
for _dx, _dy in [(0, 0), (5, 0), (40, 0), (-40, 0), (80, 0), (-80, 0),
                 (20, 10), (0, 40), (0, -40), (0, 80)]:
    _px, _py, _lx, _ly = app._drag_pads(_dx, _dy)
    _gx, _gy, _gw, _gh = app._drag_world_rect(
        400, 400, _px, _py, _lx, _ly, _dx < 0, _dy < 0)
    _ox, _oy = 400 - _gx, 400 - _gy
    _n = "%d,%d" % (_dx, _dy)
    if _gw % _f or _gh % _f or _gx % _f or _gy % _f:
        _bad.append(_n + " 不是 13 的倍数")
    if not (_area_lo <= _gw * _gh <= _area_hi):
        _bad.append("%s 面积 %d 跑出 %d~%d" % (_n, _gw * _gh, _area_lo, _area_hi))
    if _ox < 0 or _oy < 0 or _ox + W > _gw or _oy + H > _gh:
        _bad.append(_n + " 盖不住窗口")
    if abs(_dx) >= abs(_dy) and _dx:
        _front[_n] = (_gx + _gw - (400 + W)) if _dx > 0 else (400 - _gx)
    elif _dy:
        _front[_n] = (_gy + _gh - (400 + H)) if _dy > 0 else (400 - _gy)

check("10 个方向都是 13 倍数、面积±6%、盖得住窗口",
      not _bad, "；".join(_bad))
check("缓拖（5px/帧）横向前方留了 200 以上",
      _front.get("5,0", 0) >= 200, "实际 %d" % _front.get("5,0", 0))
check("快拖（80px/帧）横向前方留了 320 以上（原来只有 96）",
      _front.get("80,0", 0) >= 320, "实际 %d" % _front.get("80,0", 0))
check("纵向快拖也拉长了（0,80）",
      _front.get("0,80", 0) >= 240, "实际 %d" % _front.get("0,80", 0))
check("前向随速度单调变长（5 → 40 → 80）",
      _front["5,0"] < _front["40,0"] < _front["80,0"],
      "%s" % [_front["5,0"], _front["40,0"], _front["80,0"]])
# 静止时必须是老的对称余量：首帧补抓、以及贴边兜底都还按这个走
check("静止时退回对称 96（和改动前一致）",
      app._drag_pads(0, 0) == (snap.DRAG_PAD, snap.DRAG_PAD, 0, 0),
      "%s" % (app._drag_pads(0, 0),))

# ======================================================================
print("\n=== 10. 控件探测线程必须是事件驱动，不能轮询 ===")
# 这个线程从程序启动活到退出，但只有截图过程中才有请求。写成「睡 15ms 轮询」
# 就是 64 次/秒的空转唤醒，永远不停 —— 静置时那点 CPU 几乎全是它，更要紧的是
# 进程因此永远进不了深度空闲（笔记本上就是白耗电）。
_calls = [0]
_orig_msaa = snap.msaa_rect


def _count_msaa(x, y):
    _calls[0] += 1
    return None


snap.msaa_rect = _count_msaa
try:
    _p = snap.Probe()
    _p.start()
    time.sleep(1.2)
    check("空闲 1.2 秒一次查询都没发（不是轮询）", _calls[0] == 0,
          "实际 %d 次" % _calls[0])
    _seq = _p.request(600, 400)
    time.sleep(0.3)
    check("有请求时立刻醒来做一次", _calls[0] == 1, "实际 %d 次" % _calls[0])
    _r = _p.latest()
    check("结果带正确序号", bool(_r) and _r[1] == _seq,
          "%s vs %s" % (_r, _seq))
    # 连投多次只做最新那次：鼠标快速划过时不要积压一队过期的查询
    for _i in range(6):
        _seq2 = _p.request(700 + _i * 10, 400)
    time.sleep(0.4)
    _r2 = _p.latest()
    check("连投 6 次只留最新结果", bool(_r2) and _r2[1] == _seq2,
          "%s vs %s" % (_r2, _seq2))
finally:
    snap.msaa_rect = _orig_msaa

print("\n=== 11. 圆角遮罩：覆盖率必须是「面积积分」而不是几档台阶 ===")
_R = int(round(app.RADIUS))
_cov = app._corner_cov(_R)

# 3×3 超采样只能取到 9 个值，弧线上相邻像素在几档之间跳 —— 那就是角上
# 「一段亮一段暗」的来源。面积积分能取到明显更多的档位。
# 档位数随半径变，所以拿「同样半径下 3×3 会有几档」做基准，而不是写死。
_old = []
for _y in range(_R):
    _row = []
    for _x in range(_R):
        _h = 0
        for _sy in range(3):
            for _sx in range(3):
                _dx = _R - (_x + (_sx + 0.5) / 3.0)
                _dy = _R - (_y + (_sy + 0.5) / 3.0)
                if _dx * _dx + _dy * _dy <= _R * _R:
                    _h += 1
        _row.append(int(round(_h * 255.0 / 9.0)))
    _old.append(_row)
_vals = sorted({v for row in _cov for v in row})
_ovals = sorted({v for row in _old for v in row})
check("档位明显多于 3×3 超采样", len(_vals) >= len(_ovals) + 4,
      "新 %d 档 vs 旧 %d 档" % (len(_vals), len(_ovals)))

# 与 16×16 超采样真值对比：3×3 的最大偏差是 16，面积积分应当 ≤ 4
_NN = 16
_mx = 0
for _y in range(_R):
    for _x in range(_R):
        _hit = 0
        for _sy in range(_NN):
            for _sx in range(_NN):
                _dx = _R - (_x + (_sx + 0.5) / _NN)
                _dy = _R - (_y + (_sy + 0.5) / _NN)
                if _dx * _dx + _dy * _dy <= _R * _R:
                    _hit += 1
        _mx = max(_mx, abs(_cov[_y][_x] - int(round(_hit * 255.0 / (_NN * _NN)))))
check("与 16×16 超采样真值最大偏差 ≤ 4/255", _mx <= 4, "实际 %d" % _mx)

# 每行从左到右不应回退（圆的覆盖率是单调的）。3×3 会在中途掉档。
_bad = []
for _y in range(_R):
    for _x in range(1, _R):
        if _cov[_y][_x] < _cov[_y][_x - 1]:
            _bad.append((_x, _y, _cov[_y][_x - 1], _cov[_y][_x]))
check("每一行覆盖率只增不减", not _bad, "回退处 %s" % (_bad[:3],))

check("角块内侧像素完全不透明", _cov[_R - 1][_R - 1] == 255,
      "实际 %d" % _cov[_R - 1][_R - 1])
check("角块最外角完全透明", _cov[0][0] == 0, "实际 %d" % _cov[0][0])

# 半径变了要重算（缓存不能串）
_c2 = app._corner_cov(max(4, _R - 4))
check("半径变了会重算缓存", app.mn_cov_key == max(4, _R - 4) and len(_c2) == max(4, _R - 4),
      "key=%s" % (app.mn_cov_key,))
app._corner_cov(_R)

user32.DestroyWindow(app.hwnd_main)
print("\n" + "=" * 62)
print("结果：%s" % ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
