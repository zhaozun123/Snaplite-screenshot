# -*- coding: utf-8 -*-
"""视觉验证：画一条红色画笔线，导出成品图，检查线上像素确为红色"""
import sys, os, ctypes
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import snap

# 控制台强制 UTF-8：CI runner 的默认 ANSI 码页是 cp1252，打印中文会 UnicodeEncodeError
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

app = snap.App()
app.create_main()
app.start_capture()
app.sel = (100, 100, 400, 300)

# 画笔模式下画一条水平红线（宽=中档6px）
app._begin_mode("pen")
app.anno_color = 0   # 红 0xFFFF3B30
app.pen_width = 1    # 中 6px
x0, y0 = 150, 150
app.draw_start = (x0, y0)
app.pending = {"t": "pen", "pts": [(x0, y0)], "color": 0, "w": app.px(snap.PEN_WIDTHS[1])}
for dx in range(1, 120):
    app._update_pending(x0 + dx, y0)
app.shapes.append(app.pending)
app.pending = None

buf, w, h = app._render_result()
raw = bytes(buf)
# 选区内坐标 (150-100, 150-100) = (50, 50) 附近应是红色像素
def px(x, y):
    o = (y * w + x) * 4
    return raw[o+2], raw[o+1], raw[o]  # B,G,R

# 检查线中段某点
r, g, b = px(80, 50)
print("线中点像素 RGB =", (r, g, b))
expect_red = (r > 180 and g < 100 and b < 100)
print("PASS 是红色" if expect_red else "FAIL 不是红色")

# 检查线外（背景，非红）
r2, g2, b2 = px(80, 120)
print("线外像素 RGB =", (r2, g2, b2))

if app.hwnd_overlay:
    snap.user32.DestroyWindow(app.hwnd_overlay)
if app.hwnd_main:
    snap.user32.DestroyWindow(app.hwnd_main)
print("OK")
