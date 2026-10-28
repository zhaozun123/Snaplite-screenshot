# -*- coding: utf-8 -*-
"""拖动玻璃的逐段耗时基准：把 _drag_glass_bytes / _present_main 拆开计时，
看清楚每帧几毫秒到底花在哪一段。"""
import sys
import os
import time
import ctypes

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctypes import byref, c_void_p

import snap
from snap import user32, gdi32, w, gdip, P, _new_dib, _free_dib, GFX

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception as e:
    print("dpi:", e)

app = snap.App()
app.create_main()
h = app.hwnd_main
user32.SetWindowPos(h, None, 600, 400, 0, 0, 0x0001 | 0x0004 | 0x0010)

W, H = app.MW, app.MH
print("窗口 %dx%d，虚拟屏幕 %s" % (W, H, app.vs))
app._begin_drag_glass()
print("drag_active=%s 缩略图=%s scale=%.2f" % (
    app.drag_active, app.drag_small_size, app.drag_scale[0]))


def bench(label, fn, n=60):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000.0)
    ts.sort()
    print("  %-22s 中位 %6.2f ms   p90 %6.2f   最慢 %6.2f"
          % (label, ts[len(ts) // 2], ts[int(len(ts) * 0.9)], ts[-1]))
    return ts[len(ts) // 2]


print("\n=== 整帧 ===")
app.drag_last_pos = None
_rc = w.RECT()
user32.GetWindowRect(h, byref(_rc))
_bx, _by = _rc.left, _rc.top
bench("_drag_frame", lambda: (setattr(app, "drag_last_pos", None),
                              app._drag_frame(_bx, _by)))

print("\n=== 拆 _drag_glass_bytes ===")
sw, sh = app.drag_small_size
vx, vy, _, _ = app.vs
kx, ky = app.drag_scale
rc = w.RECT()
user32.GetWindowRect(h, byref(rc))
ix = int(round((rc.left - vx) / kx))
iy = int(round((rc.top - vy) / ky))
iw = max(2, int(round(W / kx)))
ih = max(2, int(round(H / ky)))
print("  裁剪源 %dx%d -> 目标 %dx%d" % (iw, ih, W, H))


def crop_only():
    x0, y0 = max(0, ix), max(0, iy)
    x1, y1 = min(sw, ix + iw), min(sh, iy + ih)
    crop = bytearray(iw * ih * 4)
    if x1 > x0 and y1 > y0:
        for row in range(y0, y1):
            so = (row * sw + x0) * 4
            do = ((row - iy) * iw + (x0 - ix)) * 4
            crop[do:do + (x1 - x0) * 4] = app.drag_small[so:so + (x1 - x0) * 4]
    return crop


crop = crop_only()
bench("python 行拼裁剪", crop_only)
crop = crop_only()

sdc = app._get_screen_dc()
bench("_new_dib(W,H)", lambda: _free_dib(_new_dib(sdc, W, H)))
bench("_new_dib(iw,ih)", lambda: _free_dib(_new_dib(sdc, iw, ih)))

out = _new_dib(sdc, W, H)
src = _new_dib(sdc, iw, ih)
g = GFX(out[0])


def fill_only():
    g.fill(0xFF14161A, 0, 0, W, H)


bench("g.fill(W,H)", fill_only)

img = P()
ctypes.memmove(src[3], bytes(crop), iw * ih * 4)


def upscale_only():
    g.fill(0xFF14161A, 0, 0, W, H)
    gdip.GdipCreateBitmapFromScan0(iw, ih, iw * 4, 0x00022009, src[3], byref(img))
    gdip.GdipDrawImageRectI(g.g, img, 0, 0, W, H)
    gdip.GdipDisposeImage(img)


bench("GDI+ 放大", upscale_only)
g.close()


def readback_only():
    data = bytearray(ctypes.string_at(out[3], W * H * 4))
    data[3::4] = b"\xff" * (W * H)
    return bytes(data)


bench("string_at + alpha 置255", readback_only)

print("\n=== 拆 _present_main ===")
glass = readback_only()
app.mn_glass = glass


def memset_memmove_only():
    total = W * H * 4
    ctypes.memset(app.mn_bits, 0, total)
    ctypes.memmove(app.mn_bits, glass, total)


bench("memset + memmove", memset_memmove_only)


def draw_main_only():
    gg = GFX(app.mn_dc)
    try:
        app._draw_main(gg, W, H)
    finally:
        gg.close()


bench("_draw_main(全部文字按钮)", draw_main_only)
bench("_apply_round_mask", lambda: app._apply_round_mask(W, H))
bench("_blit_main(UpdateLayeredWindow)", app._blit_main)
app._present_main()
bench("_present_main 整体", app._present_main)

print("\n=== 拆分法合计 vs 整帧 ===")
app._end_drag_glass()
user32.DestroyWindow(h)
