# -*- coding: utf-8 -*-
"""工具条磨砂预览：同一块带纹理的桌面上，渲染几种不同浓度的玻璃底，放大对比。

用途：调 C_TB_BTN 的 alpha（60% / 50% / 68%）时肉眼看差别。
用法：python tools/preview_tool_glass.py
输出：assets/tool_glass.png
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")

W, H = 900, 260


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


def draw_bg(g, W, H):
    """放一张「有明确内容」的桌面：色块 + 大字，模糊后才看得出磨砂"""
    g.fill(0xFF2E3440, 0, 0, W, H)
    cols = [0xFFBF616A, 0xFFD08770, 0xFFEBCB8B, 0xFFA3BE8C,
            0xFF88C0D0, 0xFF81A1C1, 0xFFB48EAD, 0xFF5E81AC]
    for i, c in enumerate(cols):
        g.fill_round(c, 20 + i * 108, 20, 92, 92, 14)
    g.text("SnapLite", 64, 0xFFECEFF4, 20, 130, 400, 90, 0, True)
    g.text("磨砂预览 FROSTED", 22, 0xFFD8DEE9, 24, 216, 500, 40, 0)


def blur_scale2x(raw, ww, hh):
    """最近邻放大 2 倍（只为看清像素，不做插值）"""
    out = bytearray(ww * 2 * hh * 2 * 4)
    for y in range(hh * 2):
        sy = y // 2
        row = raw[sy * ww * 4:(sy + 1) * ww * 4]
        dst = (y * ww * 2) * 4
        for x in range(ww):
            o = dst + x * 8
            out[o:o + 4] = row[x * 4:x * 4 + 4]
            out[o + 4:o + 8] = row[x * 4:x * 4 + 4]
    return bytes(out)


def main():
    app = snap.App()
    app.scale = 1.0
    app.vs = (0, 0, W, H)
    sdc = user32.GetDC(None)
    app.hdc_screen = sdc

    dc_shot, bits_shot, bmp_shot, old_shot = make_dib(sdc, W, H)
    dc_dim, bits_dim, bmp_dim, old_dim = make_dib(sdc, W, H)
    app.dc_shot, app.bmp_shot, app.dc_shot_old = dc_shot, bmp_shot, old_shot
    app.dc_dim, app.bmp_dim, app.dc_dim_old = dc_dim, bmp_dim, old_dim
    app.sel = (0, 0, 1, 1)        # 工具条不与选区相交

    g = GFX(dc_shot)
    draw_bg(g, W, H)
    g.close()
    gdi32.BitBlt(dc_dim, 0, 0, W, H, dc_shot, 0, 0, snap.SRCCOPY)
    g = GFX(dc_dim)
    g.fill(0x9A000000, 0, 0, W, H)
    g.close()

    app._ensure_tool_surface()
    tw, th = app.tb_size
    app.tb_pos = ((W - tw) // 2, 30)      # 压在彩色色块上，磨砂才看得出来

    base_raw = ctypes.string_at(bits_dim, W * H * 4)

    variants = [("压暗底色 + 60%", 0x99, False),
                ("原图底 + 60%", 0x99, True),
                ("原图底 + 50%", 0x80, True),
                ("原图底 + 42%", 0x6B, True)]
    pads = 16
    cw, ch = tw + pads * 2, th + pads * 2
    SW, SH = cw * 2, ch * 2 * len(variants)

    out_dc, out_bits, out_bmp, out_old = make_dib(sdc, SW, SH)
    frames = []
    for label, a, use_shot in variants:
        # 按浓度替换静态底色（悬停/选项排的 alpha 同比缩放）
        snap.C_TB_BTN = (a << 24) | 0x1D2229
        snap.C_TB_BTN_HI = (min(255, int(a * 208 / 153)) << 24) | 0x363E49
        snap.C_TB_COPY = snap.C_TB_BTN
        snap.C_TB_COPY_HI = snap.C_TB_BTN_HI
        snap.C_TB_CANCEL_HI = (min(255, int(a * 208 / 153)) << 24) | 0x5A2A33
        app.tb_glass = None
        app.tb_glass_key = None
        app.use_shot_glass = use_shot
        glass = app._tool_glass_layer(tw, th, app.tb_pos)
        ctypes.memmove(app.tb_bits, glass, tw * th * 4)
        g = GFX(app.tb_dc)
        app._draw_tool(g, tw, th)
        g.close()
        tool_raw = ctypes.string_at(app.tb_bits, tw * th * 4)

        # 拼到一块「桌面底 + 工具条」的画布上
        canvas = bytearray(cw * ch * 4)
        x0 = (app.tb_pos[0] - pads)
        y0 = (app.tb_pos[1] - pads)
        for y in range(ch):
            sy = y0 + y
            canvas[y * cw * 4:(y + 1) * cw * 4] = base_raw[(sy * W + x0) * 4:(sy * W + x0 + cw) * 4]
        px0, py0 = app.tb_pos[0] - x0, app.tb_pos[1] - y0
        for y in range(th):
            for x in range(tw):
                ta = tool_raw[(y * tw + x) * 4 + 3]
                if ta == 0:
                    continue
                o = ((py0 + y) * cw + (px0 + x)) * 4
                ia = 255 - ta
                for c in range(3):
                    canvas[o + c] = (tool_raw[(y * tw + x) * 4 + c]
                                     + canvas[o + c] * ia // 255) & 0xFF
                canvas[o + 3] = 255
        frames.append((label, blur_scale2x(bytes(canvas), cw, ch)))

    final = bytearray(SW * SH * 4)
    for i, (label, up) in enumerate(frames):
        for y in range(ch * 2):
            so = (y * cw * 2) * 4
            do = ((i * ch * 2 + y) * SW) * 4
            final[do:do + cw * 2 * 4] = up[so:so + cw * 2 * 4]
    ctypes.memmove(out_bits, bytes(final), SW * SH * 4)

    buf = ctypes.create_string_buffer(bytes(final), SW * SH * 4)
    img = snap.buf_to_image(buf, SW, SH)
    path = os.path.join(ASSETS, "tool_glass.png")
    snap.gdip.GdipSaveImageToFile(img, path, byref(snap.PNG_CLSID), None)

    for i, (label, _u) in enumerate(frames):
        print("%d) %s  — 从上往下第 %d 行" % (i + 1, label, i + 1))

    for dc, bmp, old in ((dc_shot, bmp_shot, old_shot), (dc_dim, bmp_dim, old_dim),
                         (out_dc, out_bmp, out_old)):
        gdi32.SelectObject(dc, old)
        gdi32.DeleteDC(dc)
        gdi32.DeleteObject(bmp)
    app._release_tool_surface()
    print("saved:", path, os.path.getsize(path))


if __name__ == "__main__":
    main()
