# -*- coding: utf-8 -*-
"""活跃档后台线程：出帧从主线程挪走之后，行为必须还跟以前一致。

为什么单独一个文件：test_glass_follow 测的是「什么时候该重算」的判断链，
要求时序确定；线程一进来时序就不确定了，混在一起两边都测不准。这里只管
线程本身 —— 起停、暂停、空闲收工、帧不堆积、DC 不跨线程。

真实节奏（每 2 个垂直消隐出一帧）没法在单测里验，那是 tools/verify_glass_pace.py
的活；这里验的是「逻辑对不对」，不是「帧率够不够」。

用法：python tests/test_glass_live_thread.py
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

ok = True


def check(name, cond, extra=""):
    global ok
    print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))
    if not cond:
        ok = False


# 线程里的 DwmFlush 换成空实现：真等一个刷新周期要 6ms，单测没这个必要
class _FakeDwm:
    @staticmethod
    def DwmFlush():
        time.sleep(0.002)


snap.dwmapi = _FakeDwm

U = ctypes.WinDLL("user32", use_last_error=True)
U.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
U.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
U.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
U.DispatchMessageW.restype = ctypes.c_ssize_t

snap.SetProcessDpiAwarenessContext(c_void_p(-4))
app = snap.App()
app.create_main()
app._build_capture_buffers()

W, H = app.MW, app.MH
SIZE = W * H * 4

state = {"raw": bytes(SIZE), "vary": True, "grab": 0, "blur": 0, "present": 0}


def fake_frame(ww, hh):
    state["grab"] += 1
    if state["vary"]:
        n = state["grab"] & 0xFF
        return bytes([n]) * (ww * hh * 4), 0, 0, ww, hh    # 每抓一次都不一样
    return state["raw"], 0, 0, ww, hh


def fake_blur(raw, ww, hh):
    state["blur"] += 1
    return b"G" * 16                            # 内容不重要，能被贴上去就行


# 线程走 _grab_live_frame（属性常设那条），定时器走 _grab_glass_block，
# 两个入口都要打桩，否则线程那边会去抓真实桌面。
# 留白取 0：这一组盯的是线程的起停与出帧节拍，几何由 test_blur_edge.py 盯
app._grab_live_frame = fake_frame
app._grab_glass_block = lambda: fake_frame(W, H)
app._blur_bgra = fake_blur
app._present_main = lambda: state.__setitem__("present", state["present"] + 1)


def pump(seconds):
    """跑一会儿消息泵：线程投的 WM_GLASS_FRAME 要靠它才会被贴上去"""
    end = time.time() + seconds
    msg = w.MSG()
    while time.time() < end:
        while U.PeekMessageW(byref(msg), None, 0, 0, 1):
            U.TranslateMessage(byref(msg))
            U.DispatchMessageW(byref(msg))
        time.sleep(0.002)


print("=" * 62)
print("活跃档后台线程：跟随 / 暂停 / 收工")
print("=" * 62)

# ---------- 1. 能起来
state["vary"] = True
started = app._start_glass_live()
check("能起线程", started is True)
check("线程真的在跑", app._glass_thread_alive())
check("线程期间定时器退回低频", app.glass_tick_ms == snap.GLASS_TICK_IDLE,
      "tick=%d" % app.glass_tick_ms)

# ---------- 2. 画面在变 → 持续出帧，且模糊是在线程里做的
before_present = state["present"]
before_blur = state["blur"]
pump(0.45)
check("画面在变时持续出帧", state["present"] > before_present + 3,
      "present +%d" % (state["present"] - before_present))
check("模糊也由线程算（不是主线程）", state["blur"] > before_blur)
check("贴上去的就是线程交来的那一帧", app.mn_glass == b"G" * 16)

# ---------- 3. 线程活着时主线程的定时器不许插手（否则两边抢着抓屏）
grab_before = state["grab"]
app._tick_glass()
check("线程在跑时 _tick_glass 直接返回、不再抓屏",
      state["grab"] == grab_before, "grab +%d" % (state["grab"] - grab_before))

# ---------- 4. 截图期间线程必须让路（否则「抓屏排除自己」会被两边来回改）
app.capturing = True
pump(0.08)                 # 先把暂停之前已经投在路上的那一帧消化掉
present_before = state["present"]
pump(0.25)
check("截图进行中线程不出帧", state["present"] == present_before,
      "present +%d" % (state["present"] - present_before))
app.capturing = False

# ---------- 5. 画面静止 → 超过保持时间自己收工，并把定时器交回低频
state["vary"] = False
state["raw"] = bytes([7]) * SIZE
parity = snap.GLASS_LIVE_HOLD
snap.GLASS_LIVE_HOLD = 0.25
app._set_glass_tick(snap.GLASS_TICK_LIVE)
pump(1.0)
snap.GLASS_LIVE_HOLD = parity
check("画面静止后线程自己收工", not app._glass_thread_alive())
check("收工后定时器回到空闲档", app.glass_tick_ms == snap.GLASS_TICK_IDLE,
      "tick=%d" % app.glass_tick_ms)

# ---------- 6. 停线程：字段必须清干净，且可以再起
state["vary"] = True
app._start_glass_live()
check("能再次起来", app._glass_thread_alive())
app._stop_glass_live()
check("停掉后不再存活", not app._glass_thread_alive())
check("停掉后状态清空",
      app.glass_thread is None and app.glass_stop is None
      and app.glass_slot is None and app.glass_posted is False)

# ---------- 7. 帧不堆积：贴一帧之后投递标记要清掉
app.glass_posted = True
app.glass_slot = b"F" * 8
app._glass_frame_ready()
check("贴完帧清掉投递标记", app.glass_posted is False)
check("贴完帧清掉 slot（不留旧帧）", app.glass_slot is None)

# ---------- 8. 屏幕 DC 不能跨线程共用
main_dc = app._get_screen_dc()
seen = {}


def worker_dc_probe():
    seen["dc"] = app._get_screen_dc()


th = __import__("threading").Thread(target=worker_dc_probe)
th.start()
th.join()
check("后台线程拿到的是自己的 DC", seen["dc"] and seen["dc"] != main_dc)

# ---------- 9. 拖动开始要先停掉线程（否则它清属性会抹掉拖动的排除设置）
state["vary"] = True
app._start_glass_live()
app._start_main_drag()
check("按下拖动时线程已让路", not app._glass_thread_alive())
app._end_main_drag()

try:
    app._stop_glass_live()
except Exception:
    pass

print("\n" + "=" * 62)
print("结果：%s" % ("全部通过" if ok else "有失败项"))
print("=" * 62)
sys.exit(0 if ok else 1)
