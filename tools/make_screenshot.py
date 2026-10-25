# -*- coding: utf-8 -*-
"""生成 README 用的界面截图（完全离屏，不触碰真实桌面）

思路：
  复用 snap.py 自己的绘制管线（_compose_overlay / _draw_tool / _draw_shapes），
  所以出来的图和用户实际看到的界面像素级一致；但：
    · 背景换成脚本合成的示例窗口内容，绝不抓真实桌面（隐私）
    · 全程不显示任何窗口，不遮挡用户正在使用的屏幕
    · 工具条单独离屏渲染后，按真实坐标合成到遮罩图上

用法：python tools/make_screenshot.py
输出：assets/screenshot.png（遮罩+选区+工具条）
      assets/result.png（选区裁切后的成品图，即用户复制到剪贴板的那张）
"""
import sys, os, ctypes
from ctypes import byref, c_void_p

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import snap
from snap import GFX, gdi32, user32, w, c_int, c_void_p as CVP

# 控制台强制 UTF-8：CI runner 的默认 ANSI 码页是 cp1252，打印中文会 UnicodeEncodeError
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")

# 截图用的虚拟画布（16:9，够大能放下工具条）
W, H = 1280, 720


def make_dib(dc, ww, hh):
    """建一个可读写的 DIB，返回 (dc, bits)"""
    bi = snap.BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(snap.BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = ww
    bi.bmiHeader.biHeight = -hh
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    bits = c_void_p()
    bmp = gdi32.CreateDIBSection(dc, byref(bi), snap.DIB_RGB_COLORS, byref(bits), None, 0)
    mdc = gdi32.CreateCompatibleDC(dc)
    old = gdi32.SelectObject(mdc, bmp)
    return mdc, bits, bmp, old


def dib_to_png(bits, ww, hh, path):
    """把 DIB 内存存成 PNG（复用 snap.py 的 GDI+ 通道）"""
    raw = ctypes.string_at(bits, ww * hh * 4)
    buf = ctypes.create_string_buffer(raw, len(raw))
    img = snap.buf_to_image(buf, ww, hh)
    snap.gdip.GdipSaveImageToFile(img, path, byref(snap.PNG_CLSID), None)
    return path


def draw_demo_background(g, W, H):
    """合成一张“正在截图某个应用”的背景图。

    刻意画成一个像代码编辑器 / 文档的窗口，避免 README 看起来像空白演示。
    """
    # 桌面底色
    g.fill(0xFF2B3038, 0, 0, W, H)

    # 应用窗口（居中偏左）
    wx, wy, ww, hh = int(W * 0.06), int(H * 0.10), int(W * 0.60), int(H * 0.80)
    g.fill_round(0xFF1E2229, wx, wy, ww, hh, 8)
    g.stroke_round(0xFF39404A, 1, wx + 0.5, wy + 0.5, ww - 1, hh - 1, 8)

    # 标题栏
    g.fill_round(0xFF262B33, wx, wy, ww, 30, 8)
    g.fill(0xFF262B33, wx, wy + 22, ww, 8)
    for i, c in enumerate((0xFFFF5F57, 0xFFFFBD2E, 0xFF28C840)):
        g.fill_circle(c, wx + 18 + i * 16, wy + 15, 5)
    g.text("config.py", 12, 0xFF9AA4B2, wx + 78, wy, 160, 30, 0)

    # 侧边栏
    sx = wx + 1
    g.fill(0xFF21252C, sx, wy + 30, 120, hh - 31)
    for i, t in enumerate(("main.py", "renderer.py", "hotkey.py",
                           "tray.py", "capture.py", "overlay.py")):
        col = 0xFFE6EAF0 if i == 1 else 0xFF7C8695
        g.text(t, 11, col, sx + 14, wy + 34 + i * 26, 110, 24, 0)

    # 代码区（伪代码，带行号与缩进感）
    code_x = wx + 126
    lines = [
        ("def capture_region(hwnd):", 0xFFD7DCE4, 0),
        ("    rect = win_bounds(hwnd)", 0xFF9AA4B2, 1),
        ("    if not rect:", 0xFFD7DCE4, 1),
        ("        return None", 0xFF9AA4B2, 2),
        ("", 0, 0),
        ("    buf = grab_screen(*rect)", 0xFF9AA4B2, 1),
        ("    return render(buf, rect)", 0xFFD7DCE4, 1),
    ]
    y = wy + 46
    for i, (txt, col, ind) in enumerate(lines):
        g.text(str(i + 1), 10, 0xFF4E5663, wx + 6, y, 24, 16, 2)
        if txt:
            g.text(txt, 11, col, code_x + ind * 14, y, 420, 16, 0)
        y += 22

    # 右侧“浏览器”窗口（体现智能识别）
    bx, by, bw2, bh2 = int(W * 0.70), int(H * 0.16), int(W * 0.24), int(H * 0.50)
    g.fill_round(0xFF232830, bx, by, bw2, bh2, 8)
    g.stroke_round(0xFF39404A, 1, bx + 0.5, by + 0.5, bw2 - 1, bh2 - 1, 8)
    g.fill_round(0xFF2C323B, bx, by, bw2, 26, 8)
    g.fill(0xFF2C323B, bx, by + 18, bw2, 8)
    g.fill_round(0xFF161A20, bx + 8, by + 34, bw2 - 16, 22, 10)
    g.text("https://example.com/docs", 10, 0xFF8A94A3, bx + 16, by + 34, bw2 - 30, 22, 0)
    for i in range(5):
        g.fill_round(0xFF2E343D, bx + 12, by + 66 + i * 24, bw2 - 24 - (i % 3) * 30, 12, 3)

    # 底部状态栏
    g.fill_round(0xFF20252C, bx, by + bh2 + 12, bw2, 22, 5)
    g.text("Python 3.11  ·  main", 9, 0xFF79828F, bx + 8, by + bh2 + 12, bw2 - 16, 22, 0)


def build_screenshot():
    os.makedirs(ASSETS, exist_ok=True)

    app = snap.App()
    app.scale = 1.0                      # 截图固定 1:1，避免 DPI 缩放影响排版
    app.vs = (0, 0, W, H)

    sdc = user32.GetDC(None)
    app.hdc_screen = sdc

    # 三块离屏缓冲：shot(原图) / dim(变暗) / back(合成结果)
    dc_shot, bits_shot, bmp_shot, old_shot = make_dib(sdc, W, H)
    dc_dim, bits_dim, bmp_dim, old_dim = make_dib(sdc, W, H)
    dc_back, bits_back, bmp_back, old_back = make_dib(sdc, W, H)
    app.dc_shot, app.bmp_shot, app.dc_shot_old = dc_shot, bmp_shot, old_shot
    app.dc_dim, app.bmp_dim, app.dc_dim_old = dc_dim, bmp_dim, old_dim
    app.dc_back, app.bmp_back, app.dc_back_old = dc_back, bmp_back, old_back

    # 1) 画合成背景到 shot
    g = GFX(dc_shot)
    draw_demo_background(g, W, H)
    g.close()

    # 2) dim = shot + 半透明黑（与 _build_capture_buffers 一致）
    gdi32.BitBlt(dc_dim, 0, 0, W, H, dc_shot, 0, 0, snap.SRCCOPY)
    g = GFX(dc_dim)
    g.fill(0x9A000000, 0, 0, W, H)
    g.close()

    # 3) 设置一个选区 + 画笔笔迹 + 箭头，走真实绘制管线
    app.sel = (150, 120, 620, 400)
    app.mode = "pen"
    app.anno_color = 0          # 红
    app.pen_width = 1            # 中
    app.drag_mode = None
    app.draw_start = None

    # 一条红色手写笔迹（用正弦模拟手写）
    import math
    pts = []
    for i in range(90):
        t = i / 89.0
        x = 230 + t * 300
        y = 300 + math.sin(t * 7.0) * 42 - t * 40
        pts.append((x, y))
    app.shapes.append({"t": "pen", "pts": pts, "color": 0,
                       "w": app.px(snap.PEN_WIDTHS[1])})

    # 一个箭头
    app.shapes.append({"t": "arrow", "x1": 620, "y1": 430,
                       "x2": 480, "y2": 355, "color": 1})

    # 4) 合成遮罩（真实逻辑）
    app._compose_overlay(W, H)

    # 5) 离屏渲染工具条
    app._ensure_tool_surface()
    tw, th = app.tb_size
    ctypes.memset(app.tb_bits, 0, tw * th * 4)
    g = GFX(app.tb_dc)
    app._draw_tool(g, tw, th)
    g.close()
    tool_raw = ctypes.string_at(app.tb_bits, tw * th * 4)

    # 6) 把工具条贴到合成图底部（模拟真实位置：选区下方居中）
    app.tb_pos = (150 + (620 - tw) // 2, 540)
    tx, ty = app.tb_pos
    base = ctypes.string_at(bits_back, W * H * 4)
    buf = bytearray(base)
    for y in range(th):
        oy = ty + y
        if oy < 0 or oy >= H:
            continue
        row_t = tool_raw[y * tw * 4:(y + 1) * tw * 4]
        for x in range(tw):
            ox = tx + x
            if ox < 0 or ox >= W:
                continue
            ta = row_t[x * 4 + 3]
            if ta == 0:
                continue
            o = (oy * W + ox) * 4
            # 工具条是预乘 alpha 的直通 alpha，over 合成
            ia = 255 - ta
            for ch in range(3):
                buf[o + ch] = (row_t[x * 4 + ch] + buf[o + ch] * ia // 255) & 0xFF
            buf[o + 3] = 255
    ctypes.memmove(bits_back, bytes(buf), W * H * 4)

    p1 = dib_to_png(bits_back, W, H, os.path.join(ASSETS, "screenshot.png"))

    # 7) 成品图（用户复制到剪贴板的那张）：走 _render_result
    buf2, ww2, hh2 = app._render_result()
    if buf2:
        img = snap.buf_to_image(buf2, ww2, hh2)
        p2 = os.path.join(ASSETS, "result.png")
        snap.gdip.GdipSaveImageToFile(img, p2, byref(snap.PNG_CLSID), None)
    else:
        p2 = None

    # 收尾
    for dc, bmp, old in ((dc_shot, bmp_shot, old_shot),
                         (dc_dim, bmp_dim, old_dim),
                         (dc_back, bmp_back, old_back)):
        gdi32.SelectObject(dc, old)
        gdi32.DeleteDC(dc)
        gdi32.DeleteObject(bmp)
    app._release_tool_surface()
    user32.ReleaseDC(None, sdc)

    print("生成成功:")
    print("  ", p1, os.path.getsize(p1), "bytes")
    if p2:
        print("  ", p2, os.path.getsize(p2), "bytes")
    return p1, p2


if __name__ == "__main__":
    build_screenshot()
