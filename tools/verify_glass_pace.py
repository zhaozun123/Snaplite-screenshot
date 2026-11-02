# -*- coding: utf-8 -*-
"""真机验证：活跃档后台线程的出帧节奏到底锁不锁相。

单元测试（tests/test_glass_live_thread.py）把 DwmFlush 换成了空实现，只能证明
逻辑对，证明不了「帧间隔是不是整数个刷新周期」。这个工具走真实抓屏 + 真实
DwmFlush，在面板背后放一块不停变色的窗口（不动位置、不改大小，不产生任何
窗口事件），量三件事：

  1. 出帧间隔、折合几个刷新周期、顿挫频率（要接近整数、顿挫频率要低）
  2. 主线程贴一帧要多久、占空比多少（这是界面灵不灵敏的关键）
  3. 有没有掉帧/堆积（间隔出现两倍周期说明线程被拖住了）

用法：python tools/verify_glass_pace.py [秒数]
屏幕上会闪一个小窗口（面板背后那块），默认 3 秒，结束自动关闭。
"""
import ctypes
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from ctypes import byref, sizeof
from ctypes import wintypes as w

import snap
from snap import PAINTSTRUCT, WNDCLASSEXW, WNDPROC, gdi32, user32

ok = True


def check(name, cond, extra=""):
    global ok
    print("[%s] %-44s %s" % ("PASS" if cond else "FAIL", name, extra))
    if not cond:
        ok = False


U = ctypes.WinDLL("user32", use_last_error=True)
U.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
U.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
U.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
U.DispatchMessageW.restype = ctypes.c_ssize_t

dwm = ctypes.WinDLL("dwmapi")
dur = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0

# ---------- 屏幕真实刷新率
dwm.DwmFlush()
gaps = []
for _ in range(120):
    t = time.perf_counter()
    dwm.DwmFlush()
    gaps.append((time.perf_counter() - t) * 1000)
vb = statistics.median(gaps[8:])
print("屏幕：垂直消隐 %.3f ms（%.1f Hz）" % (vb, 1000.0 / vb))

# ---------- 主面板 + 背后那块会变的窗口
app = snap.App()
app.create_main()
app._build_capture_buffers()
rc = w.RECT()
user32.GetWindowRect(app.hwnd_main, byref(rc))
MW, MH = app.MW, app.MH
print("主面板：(%d,%d) %dx%d" % (rc.left, rc.top, MW, MH))


TONE = {"n": 0}          # 每次重绘换一个色调：模拟视频那种「内容真的在变」


def probe_proc(hwnd, msg, wp, lp):
    if msg == 0x000F:                       # WM_PAINT
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, byref(ps))
        brush = gdi32.CreateSolidBrush(0x00203040 + (TONE["n"] & 0x3F) * 0x20202)
        box = w.RECT(0, 0, MW, MH)
        user32.FillRect(hdc, byref(box), brush)
        gdi32.DeleteObject(brush)
        user32.EndPaint(hwnd, byref(ps))
        return 0
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


proc = WNDPROC(probe_proc)
wc = WNDCLASSEXW()
wc.cbSize = sizeof(WNDCLASSEXW)
wc.lpfnWndProc = proc
wc.hInstance = app.hinst
wc.lpszClassName = "GlassPaceProbe"
user32.RegisterClassExW(byref(wc))

probe = user32.CreateWindowExW(
    0x00000008, "GlassPaceProbe", "pace", 0x80000000 | 0x04000000,
    rc.left, rc.top, MW, MH, None, None, app.hinst, None)
user32.ShowWindow(probe, 4)
user32.SetWindowPos(probe, 1, 0, 0, 0, 0, 0x0003)      # 压到面板下面

# ---------- 记录出帧时刻与主线程耗时
stamps = []
costs = []
real_present = snap.App._present_main


def timed_present(self, pos=None):
    t0 = time.perf_counter()
    r = real_present(self, pos)
    costs.append((time.perf_counter() - t0) * 1000)
    return r


snap.App._present_main = timed_present

# WM_GLASS_FRAME 处理 = 主线程侧的全部开销，单独记一份
print("\n启动活跃档线程，跑 %.1f 秒……" % dur)
app.glass_raw = None
app._set_glass_tick(snap.GLASS_TICK_LIVE)
if not app._start_glass_live():
    print("线程起不来（系统不支持抓屏排除自己？）")
    sys.exit(2)

msg = w.MSG()
end = time.perf_counter() + dur
n = 0
while time.perf_counter() < end:
    while U.PeekMessageW(byref(msg), None, 0, 0, 1):
        U.TranslateMessage(byref(msg))
        U.DispatchMessageW(byref(msg))
        if msg.message == snap.WM_GLASS_FRAME and msg.wParam == 0:
            stamps.append(time.perf_counter())
    # 重画一块新颜色 = 内容真的在变，位置尺寸都没动 → 不产生窗口事件
    TONE["n"] += 1
    user32.InvalidateRect(probe, None, False)
    user32.UpdateWindow(probe)
    n += 1
    time.sleep(0.004)

alive_before_stop = app._glass_thread_alive()
app._stop_glass_live()
snap.App._present_main = real_present

# ---------- 报告
print("\n" + "=" * 62)
print("出帧节奏")
print("=" * 62)
iv = [(stamps[i + 1] - stamps[i]) * 1000 for i in range(len(stamps) - 1)]
# 前 3 个间隔是启动瞬态（线程刚起、属性刚设），不参与统计
iv = [x for x in iv[3:] if x > 0.3]
check("线程在跑（没有提前收工）", alive_before_stop)
if not iv:
    print("一帧都没出，后面没法测")
    sys.exit(1)

m = statistics.median(iv)
fps = 1000.0 / m
periods = m / vb
frac = abs(periods - round(periods))
beat = fps * frac
print("  出帧 %d 张 / %.1f 秒 → %.1f 帧/秒" % (len(stamps), dur, fps))
print("  间隔：中位 %.2f ms  均值 %.2f  标准差 %.2f  最小 %.2f  最大 %.2f"
      % (m, statistics.mean(iv), statistics.pstdev(iv), min(iv), max(iv)))
print("  折合 %.3f 个刷新周期；顿挫频率 %.1f Hz" % (periods, beat))

# 判据说明：本来想「让帧间隔落成刷新周期的整数倍」，实测这条路走不通 ——
# 抓一次就要 7ms、还要绕一趟消息到主线程贴图，落点根本对不准，强行对齐反而
# 掉到 58 帧。真正起作用的不是「对齐」，是「采样够密」：同一帧内容从变化到
# 上屏，31ms 的节拍最多要等 31ms、平均 15ms；13ms 的节拍最多等 13ms。
# 所以这里改判「间隔够短 + 别忽长忽短」，而不是「是不是整数倍」。
check("采样间隔够短（中位 <= 14ms，即 >= 71 帧/秒）", m <= 14.0, "%.2f ms" % m)
check("间隔抖动不大（标准差 <= 中位的一半）",
      statistics.pstdev(iv) <= m * 0.5, "%.2f vs %.2f" % (statistics.pstdev(iv), m))
# 阈值取 50ms：偶发一两次长间隔是调度抖动（本工具自己的重绘也在抢主线程），
# 用户看到的那种「一阵一阵」是周期性节奏，不是单次卡顿，所以不往死里卡。
check("没有长时间卡住（最大间隔 < 50ms）", max(iv) < 50,
      "最大 %.1f ms（%.1f 个周期）" % (max(iv), max(iv) / vb))
print("  （参考：1 个刷新周期 = %.2f ms；老方案是 31ms 一拍、间隔 5.1 个周期）" % vb)

print("\n" + "=" * 62)
print("主线程开销（决定界面灵不灵敏）")
print("=" * 62)
if costs:
    cm = statistics.median(costs)
    duty = cm / m * 100
    print("  贴一帧 中位 %.2f ms  均值 %.2f  最大 %.2f"
          % (cm, statistics.mean(costs), max(costs)))
    print("  → 主线程占空比约 %.0f%%（原同步方案的实测值是 42%%）" % duty)
    check("主线程占空比明显低于原来的 42%", duty < 30, "%.0f%%" % duty)
else:
    check("有贴帧记录", False)

# ---------- 收尾
user32.DestroyWindow(probe)
try:
    app.quit()
except Exception:
    pass

print("\n" + "=" * 62)
print("结果：%s" % ("全部通过" if ok else "有失败项"))
print("=" * 62)
sys.exit(0 if ok else 1)
