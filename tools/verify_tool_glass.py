# -*- coding: utf-8 -*-
"""工具条磨砂底自检

验证三件事：
  1. 按钮形状内 alpha=255、按钮之间/四周的缝 alpha=0（版式不能被玻璃糊成一条）
  2. 玻璃底真的取自「该位置」的画面 —— 同一版式放在亮处 / 暗处，按钮内颜色要跟着变
  3. 缓存生效：位置和选区不变时不重复模糊

用法：python tools/verify_tool_glass.py
"""
import sys, os, ctypes
from ctypes import byref

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import snap
from snap import GFX, gdi32, user32

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

W, H = 1600, 300        # 够宽：左右两半各放得下整条工具条
ok = True


def make_dib(dc, ww, hh):
    bi = snap.BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(snap.BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = ww
    bi.bmiHeader.biHeight = -hh
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(dc, byref(bi), snap.DIB_RGB_COLORS, byref(bits), None, 0)
    mdc = gdi32.CreateCompatibleDC(dc)
    old = gdi32.SelectObject(mdc, bmp)
    return mdc, bits, bmp, old


def mean_rgb(data, W_, H_, x0, y0, x1, y1):
    s = [0, 0, 0]
    n = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            o = (y * W_ + x) * 4
            s[0] += data[o]
            s[1] += data[o + 1]
            s[2] += data[o + 2]
            n += 1
    return (s[2] / float(n), s[1] / float(n), s[0] / float(n))   # 按 R,G,B 返回


def main():
    global ok
    app = snap.App()
    app.scale = 1.0
    app.vs = (0, 0, W, H)
    sdc = user32.GetDC(None)
    app.hdc_screen = sdc

    dc_shot, bits_shot, bmp_shot, old_shot = make_dib(sdc, W, H)
    dc_dim, bits_dim, bmp_dim, old_dim = make_dib(sdc, W, H)
    app.dc_shot, app.bmp_shot, app.dc_shot_old = dc_shot, bmp_shot, old_shot
    app.dc_dim, app.bmp_dim, app.dc_dim_old = dc_dim, bmp_dim, old_dim
    app.sel = (0, 0, 1, 1)

    # 左半红、右半蓝 → 玻璃底取错位置立刻能看出来
    g = GFX(dc_shot)
    g.fill(0xFF203040, 0, 0, W, H)
    g.fill(0xFFB03030, 0, 0, W // 2, H)
    g.fill(0xFF3040B0, W // 2, 0, W - W // 2, H)
    g.close()
    gdi32.BitBlt(dc_dim, 0, 0, W, H, dc_shot, 0, 0, snap.SRCCOPY)
    g = GFX(dc_dim)
    g.fill(0x9A000000, 0, 0, W, H)
    g.close()

    app.use_shot_glass = True
    app._ensure_tool_surface()
    tw, th = app.tb_size
    rects = app._tool_layout()[2]

    # --- 1) 覆盖率：形状内不透明、缝隙透明
    cov = app._tool_shape_cov(tw, th)
    _, x, y, ww, hh = rects[0]
    c = cov[(y + hh // 2) * tw + (x + ww // 2)]
    gap_x = x + ww + 2                       # 两个按钮之间的缝
    gap = cov[(y + hh // 2) * tw + gap_x]
    print("按钮中心覆盖率 = %d（应 255）；按钮缝隙覆盖率 = %d（应 0）" % (c, gap))
    if c != 255 or gap != 0:
        print("FAIL: 形状覆盖率不对 —— 缝隙也会糊上玻璃")
        ok = False

    # --- 2) 玻璃底取自该位置
    lx = 20
    rx = W - tw - 20
    app.tb_pos = (lx, 120)
    gl = app._tool_glass_layer(tw, th, app.tb_pos)
    lm = mean_rgb(gl, tw, th, x + 8, y + 8, x + ww - 8, y + hh - 8)
    app.tb_glass = app.tb_glass_key = None
    app.tb_pos = (rx, 120)
    gr = app._tool_glass_layer(tw, th, app.tb_pos)
    rm = mean_rgb(gr, tw, th, x + 8, y + 8, x + ww - 8, y + hh - 8)
    print("左侧按钮内 RGB = (%.0f, %.0f, %.0f)   右侧 = (%.0f, %.0f, %.0f)"
          % (lm + rm))
    if not (lm[0] > lm[2] + 20 and rm[2] > rm[0] + 20):
        print("FAIL: 玻璃底没有跟着位置走（左偏红、右偏蓝才对）")
        ok = False

    # --- 3) 缝隙里 alpha 为 0（不糊玻璃）
    gap_a = gl[(y + hh // 2) * tw * 4 + gap_x * 4 + 3]
    print("缝隙像素 alpha = %d（应 0）" % gap_a)
    if gap_a != 0:
        print("FAIL: 缝隙被玻璃盖住了")
        ok = False

    # --- 4) 缓存：同位置再取一次不该重算
    app.tb_glass = app.tb_glass_key = None
    app.tb_pos = (lx, 120)
    a1 = app._tool_glass_layer(tw, th, app.tb_pos)
    a2 = app._tool_glass_layer(tw, th, app.tb_pos)
    print("缓存命中（同一对象）= %s" % (a1 is a2))
    if a1 is not a2:
        print("FAIL: 每次重画都在重新模糊")
        ok = False

    # --- 5) 20 次重算不泄漏 GDI
    k32 = ctypes.windll.kernel32
    user32.GetGuiResources.restype = ctypes.c_uint
    user32.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    h0 = user32.GetGuiResources(k32.GetCurrentProcess(), 0)
    for i in range(20):
        app.tb_glass = app.tb_glass_key = None
        app._tool_glass_layer(tw, th, (lx + i, 120))
    h1 = user32.GetGuiResources(k32.GetCurrentProcess(), 0)
    print("20 次重算：GDI 句柄 %d → %d" % (h0, h1))
    if h1 - h0 > 8:
        print("FAIL: GDI 句柄在漏")
        ok = False

    for dc, bmp, old in ((dc_shot, bmp_shot, old_shot), (dc_dim, bmp_dim, old_dim)):
        gdi32.SelectObject(dc, old)
        gdi32.DeleteDC(dc)
        gdi32.DeleteObject(bmp)
    app._release_tool_surface()
    print("\n结果:", "全部通过 ✓" if ok else "有问题 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
