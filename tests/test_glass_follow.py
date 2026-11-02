# -*- coding: utf-8 -*-
"""玻璃底跟随验证：桌面内容变了，背景模糊必须跟上。

为什么单独有这个测试：真实缺陷是「主面板不动、背景画面在变（视频、动画、
滚动）时，模糊效果停在旧画面」。根因是刷新判据只看窗口事件（搬家 / 切前
台），而**画面内容变化不产生任何窗口事件**。这类问题不会有崩溃、不会报错，
只会让人看着觉得卡，所以必须用测试盯住。

浏览器 / 视频播放器这两种场景在这里没法稳定复现，所以做法是：把「抓回来
的原始背景」换成可控的假数据，直接验证 _tick_glass 的判定链路 ——
「内容没变就一帧都不该算，内容变了必须重算并切高频档」。

用法：python tests/test_glass_follow.py
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

from ctypes import c_void_p

import snap

ok = True


def check(name, cond, extra=""):
    global ok
    print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))
    if not cond:
        ok = False


# ---------------------------------------------------------------- 准备
# 这一组测的是「判定链路」本身：什么时候该重算、什么时候该省。
# 活跃档交给后台线程之后，出帧时机不再确定，这里关掉线程走同步路径；
# 线程那段逻辑由 tests/test_glass_live_thread.py 单独盯。
snap.GLASS_USE_LIVE_THREAD = False

snap.SetProcessDpiAwarenessContext(c_void_p(-4))
app = snap.App()
app.create_main()
app._build_capture_buffers()

W, H = app.MW, app.MH
SIZE = W * H * 4

# 打桩：抓屏换成可控数据（真抓屏 6ms 且内容不可控），提交画面只计数
state = {"raw": bytes(SIZE), "grab": 0, "blur": 0, "present": 0}

real_blur = app._blur_bgra


def fake_block():
    # 留白取 0、整块就等于面板：这一组测的是「判定链路」，几何由
    # tests/test_blur_edge.py 单独盯，这里不让它掺进来
    state["grab"] += 1
    return state["raw"], 0, 0, W, H


def counted_blur(raw, ww, hh):
    state["blur"] += 1
    return real_blur(raw, ww, hh)


app._grab_glass_block = fake_block
app._blur_bgra = counted_blur
app._present_main = lambda: state.__setitem__("present", state["present"] + 1)


def reset():
    state["grab"] = state["blur"] = state["present"] = 0


def tick():
    app._tick_glass()


def bg_with_marker(mark):
    """造一帧「背景」：整片深灰，右上角放一个会变的亮点当地标"""
    b = bytearray(bytes(SIZE))
    for yy in range(4, 14):
        for xx in range(W - 14, W - 4):
            o = (yy * W + xx) * 4
            b[o] = b[o + 1] = b[o + 2] = mark
            b[o + 3] = 255
    return bytes(b)


print("=" * 62)
print("玻璃底跟随：桌面内容变化必须触发重算")
print("=" * 62)

# ---------- 1. 第一拍：从没有背景到有背景，必须重算
app.glass_raw = None
app.glass_dirty = False
state["raw"] = bg_with_marker(200)
reset()
tick()
check("首拍必然重算（此时还没有可比对的旧帧）",
      state["blur"] == 1 and state["present"] == 1,
      "blur=%d present=%d" % (state["blur"], state["present"]))
check("重算后记住了这一帧", app.glass_raw == state["raw"])
check("重算后切到活跃档", app.glass_tick_ms == snap.GLASS_TICK_LIVE,
      "tick=%d" % app.glass_tick_ms)

# ---------- 2. 内容没变：一帧都不该算
reset()
tick()
check("画面没变时不模糊、不重画",
      state["blur"] == 0 and state["present"] == 0,
      "blur=%d present=%d" % (state["blur"], state["present"]))
check("画面没变时仍然抓了一次（这是判据本身，6ms）", state["grab"] == 1,
      "grab=%d" % state["grab"])

# ---------- 3. 内容变了：必须重算（这就是用户报的那个 bug）
app.glass_live_until = time.time() + 5          # 处在活跃期
before = app.mn_glass
state["raw"] = bg_with_marker(40)               # 地标亮度变了 = 视频换了一帧
reset()
tick()
check("背景内容变化 → 重算并重画",
      state["blur"] == 1 and state["present"] == 1,
      "blur=%d present=%d" % (state["blur"], state["present"]))
check("玻璃底确实换成了新画面", app.mn_glass != before and app.mn_glass is not None)
check("变化后保持活跃档", app.glass_tick_ms == snap.GLASS_TICK_LIVE)

# ---------- 4. 活跃期过后没变化 → 退回空闲档省电
app.glass_live_until = time.time() - 1          # 模拟活跃期已过
reset()
tick()
check("活跃期结束后退回空闲档", app.glass_tick_ms == snap.GLASS_TICK_IDLE,
      "tick=%d" % app.glass_tick_ms)
check("退回空闲档这一拍没做重活", state["blur"] == 0 and state["present"] == 0)

# ---------- 5. 活跃期内没变化 → 不许提前降档（避免来回跳）
app.glass_live_until = time.time() + 5
app._set_glass_tick(snap.GLASS_TICK_LIVE)
reset()
tick()
check("活跃期内没变化也不降档", app.glass_tick_ms == snap.GLASS_TICK_LIVE,
      "tick=%d" % app.glass_tick_ms)

# ---------- 6. 窗口搬家事件（dirty）时，即使像素相同也要重算
#   场景：另一个窗口整体挪走，抓回来的这块恰好长得一样，但语义上必须刷新
app.glass_dirty = True
reset()
tick()
check("事件置位时即使像素相同也重算",
      state["blur"] == 1 and state["present"] == 1,
      "blur=%d present=%d" % (state["blur"], state["present"]))
check("重算后标记被清掉", app.glass_dirty is False)

# ---------- 7. 截图流程中不该跟着刷（遮罩盖着，主面板停在透明帧）
app.glass_dirty = True
app.capturing = True
state["raw"] = bg_with_marker(90)
reset()
tick()
check("截图进行中不刷新", state["blur"] == 0 and state["present"] == 0,
      "blur=%d" % state["blur"])
app.capturing = False

# ---------- 8. 拖动中不刷新（拖动自己有自己的渲染路径）
app.glass_dirty = True
app.drag_active = True
reset()
tick()
check("拖动进行中不刷新", state["blur"] == 0 and state["present"] == 0,
      "blur=%d" % state["blur"])
check("拖动中标记保留着，松手后还要用", app.glass_dirty is True)

# ---------- 9. 拖动结束：必须要求重抓，并切到活跃档
app.drag_small = b"x"
app.drag_small_size = (2, 2)
app.glass_dirty = False
app._set_glass_tick(snap.GLASS_TICK_IDLE)
app._end_drag_glass()
check("拖动结束丢掉缩略图", app.drag_small is None and app.drag_active is False)
check("拖动结束要求重抓（途中背景可能已经变了）", app.glass_dirty is True)
check("拖动结束切到活跃档", app.glass_tick_ms == snap.GLASS_TICK_LIVE,
      "tick=%d" % app.glass_tick_ms)

# ---------- 10. 老系统退路：拿不到可比对的原始像素，回到事件驱动
app._grab_glass_block = lambda: None
app.glass_dirty = False
app.glass_live_until = time.time() - 1
app._set_glass_tick(snap.GLASS_TICK_LIVE)
reset()
tick()
check("老系统无事件时不做完整刷新", state["present"] == 0)
check("老系统空闲时会退回空闲档", app.glass_tick_ms == snap.GLASS_TICK_IDLE,
      "tick=%d" % app.glass_tick_ms)

app._grab_glass_block = lambda: None
app.glass_dirty = True
app._set_glass_tick(snap.GLASS_TICK_IDLE)
lived = {"n": 0}
orig_live = app._refresh_glass_live
app._refresh_glass_live = lambda: lived.__setitem__("n", lived["n"] + 1)
tick()
app._refresh_glass_live = orig_live
check("老系统有事件时仍然走完整刷新", lived["n"] == 1, "n=%d" % lived["n"])

# ---------- 11. 真·抓屏入口：老系统必须明确返回 None，不许硬抓
app._grab_glass_block = snap.App._grab_glass_block.__get__(app, snap.App)
keep_excl = app._excl_ok
app._excl_ok = False                      # 强制「不支持排除自身」
check("不支持排除自身时 _grab_glass_block 返回 None",
      app._grab_glass_block() is None)
app._excl_ok = keep_excl

# ---------- 12. 节拍本身必须是「空闲比活跃慢」，否则省电无意义
check("空闲档比活跃档慢", snap.GLASS_TICK_IDLE > snap.GLASS_TICK_LIVE,
      "idle=%d live=%d" % (snap.GLASS_TICK_IDLE, snap.GLASS_TICK_LIVE))
check("活跃保持时间足够跨过至少 5 拍",
      snap.GLASS_LIVE_HOLD * 1000 / snap.GLASS_TICK_LIVE >= 5,
      "hold=%.1fs" % snap.GLASS_LIVE_HOLD)

print("\n" + "=" * 62)
print("结果：%s" % ("全部通过" if ok else "有失败项"))
print("=" * 62)
sys.exit(0 if ok else 1)
