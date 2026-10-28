# -*- coding: utf-8 -*-
"""拖动逐帧验证（纯背景层）：桌面上放一块彩色图案，窗口滑过去。

每帧做两件事：
  1. 记录拖动帧算出的玻璃底 mn_glass（纯背景，不含按钮文字）
  2. 把窗口藏起来，真的在目标位置抓屏 + 模糊，作为标准答案
比较两者差异，并把 6 帧的玻璃底拼成一张图，直观看背景是否跟着位置变。
"""
import sys
import os
import ctypes
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctypes import byref, c_void_p

import snap
from snap import user32, gdip, P, WNDPROC, WNDCLASSEXW, _new_dib, _free_dib

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

W, H = 340, 148
hinst = ctypes.windll.kernel32.GetModuleHandleW(None)


def wp(h, m, wp_, lp):
    if m == 0x000F:
        ps = snap.PAINTSTRUCT()
        hdc = user32.BeginPaint(h, byref(ps))
        g = snap.GFX(hdc)
        try:
            for c, i in [(0xFF2E86FF, 0), (0xFF33D6A6, 1), (0xFFFFD166, 2),
                         (0xFFFF6B6B, 3), (0xFFB07CFF, 4)]:
                g.fill(c, 0, i * 60, 300, 60)
        finally:
            g.close()
        user32.EndPaint(h, byref(ps))
        return 0
    if m == 0x0014:
        return 1
    return user32.DefWindowProcW(h, m, wp_, lp)


proc = WNDPROC(wp)
wc = WNDCLASSEXW()
wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
wc.lpfnWndProc = proc
wc.hInstance = hinst
wc.lpszClassName = "SNAPCOLORREF2"
user32.RegisterClassExW(byref(wc))
hwnd_ref = user32.CreateWindowExW(0x00000008, "SNAPCOLORREF2", "ref",
                                  0x80000000, 1120, 380, 300, 300,
                                  None, None, hinst, None)
user32.ShowWindow(hwnd_ref, 5)
user32.UpdateWindow(hwnd_ref)

app = snap.App()
app.create_main()
h = app.hwnd_main
user32.SetWindowPos(h, None, 600, 400, 0, 0, 0x0004 | 0x0010)
time.sleep(0.5)

app._begin_drag_glass()
if not app.drag_active:
    print("FAIL: 拖动缓存没建起来")
    raise SystemExit(1)

Y = 470
xs = [560, 760, 960, 1120, 1240, 1320]
frames = []
truths = []

for x in xs:
    app.drag_last_pos = None
    app._drag_frame(x, Y)
    frames.append(app.mn_glass)

    user32.ShowWindow(h, 0)
    try:
        snap.dwmapi.DwmFlush()
    except Exception:
        pass
    raw = snap.grab_screen(x, Y, W, H).raw
    truths.append(app._blur_bgra(raw, W, H))
    user32.ShowWindow(h, 5)
    time.sleep(0.03)


def mean_rgb(bgra):
    n = W * H
    b = sum(bgra[0:n * 4:4 * 97])
    g = sum(bgra[1:n * 4:4 * 97])
    r = sum(bgra[2:n * 4:4 * 97])
    c = len(bgra[0:n * 4:4 * 97])
    return (r / c, g / c, b / c)


def mad(a, b):
    s = 0
    n = 0
    for i in range(0, W * H * 4, 4 * 37):
        s += abs(a[i] - b[i]) + abs(a[i + 1] - b[i + 1]) + abs(a[i + 2] - b[i + 2])
        n += 3
    return s / float(n)


print("拖动帧的玻璃底 vs 该位置真实桌面（模糊后）")
print("  x      拖动帧 RGB            标准答案 RGB            平均差")
ok = True
prev = None
for i, x in enumerate(xs):
    fa = mean_rgb(frames[i])
    ta = mean_rgb(truths[i])
    d = mad(frames[i], truths[i])
    same = "（与上一帧相同！）" if prev is not None and frames[i] == prev else ""
    print("  %4d  (%5.1f,%5.1f,%5.1f)   (%5.1f,%5.1f,%5.1f)   %6.2f %s"
          % (x, fa[0], fa[1], fa[2], ta[0], ta[1], ta[2], d, same))
    if d > 8:
        ok = False
    if prev is not None and frames[i] == prev:
        ok = False
    prev = frames[i]

print("\n结果：%s" % ("PASS 每帧背景都与该位置的桌面一致，且随位置变化"
                    if ok else "FAIL"))

# 拼图：6 帧玻璃底横排
gap = 8
SW = W * len(xs) + gap * (len(xs) - 1)
sdc = app._get_screen_dc()
out = _new_dib(sdc, SW, H)
data = bytearray(ctypes.string_at(out[3], SW * H * 4))
for i, fr in enumerate(frames):
    ox = i * (W + gap)
    for y in range(H):
        so = y * W * 4
        do = (y * SW + ox) * 4
        data[do:do + W * 4] = fr[so:so + W * 4]
ctypes.memmove(out[3], bytes(data), SW * H * 4)

tok = P()
si = snap.GdiplusStartupInput(1, None, False, False)
gdip.GdiplusStartup(byref(tok), byref(si), None)
img = P()
gdip.GdipCreateBitmapFromScan0(SW, H, SW * 4, 0x00022009, out[3], byref(img))
path = os.path.abspath("assets/drag_realtime.png")
print("save:", gdip.GdipSaveImageToFile(img, path, byref(snap.PNG_CLSID), None), path)
gdip.GdipDisposeImage(img)
_free_dib(out)

app._end_drag_glass()
user32.DestroyWindow(h)
user32.DestroyWindow(hwnd_ref)
