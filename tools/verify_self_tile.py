"""验证「这是我自己」的指纹判据：面板样张必须判 True，真实桌面必须判 False。

用法：python -u tools/verify_self_tile.py
"""
import sys
import os
import ctypes
import time
from ctypes import c_void_p

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import snap
from snap import user32

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

app = snap.App()
app.create_main()
if not app.hwnd_main:
    print("FAIL: 没有窗口")
    sys.exit(1)
time.sleep(0.15)
app._present_main()

W, H = app.mn_size
total = W * H * 4
selfbuf = ctypes.string_at(app.mn_bits, total)

x0, x1, y0, y1 = app._self_btn_rect()
print("面板 %dx%d   取样框 x%d..%d  y%d..%d  (%d 像素)"
      % (W, H, x0, x1, y0, y1, (x1 - x0) * (y1 - y0)))


def avg(buf):
    tb = tg = tr = 0
    n = 0
    stride = W * 4
    for y in range(y0, y1):
        o = y * stride + x0 * 4
        row = buf[o:o + (x1 - x0) * 4]
        for k in range(0, len(row) - 3, 4):
            tb += row[k]
            tg += row[k + 1]
            tr += row[k + 2]
            n += 1
    return (tb / float(n), tg / float(n), tr / float(n))


c = avg(selfbuf)
print("面板按钮取样区平均 BGR = (%.1f, %.1f, %.1f)" % c)
print("  期望约 (255, 132, 10) = C_ACCENT")

ok_self = app._is_self_tile(selfbuf, W, H)
print("面板样张判「是我自己」: %s   %s" % (ok_self, "OK" if ok_self else "**失败**"))

# 真实桌面：抓屏幕下方一块，内容与面板无关
bg = snap.grab_screen(200, 900, W, H).raw
ok_bg = app._is_self_tile(bg, W, H)
cb = avg(bg)
print("桌面样张平均 BGR = (%.1f, %.1f, %.1f)" % cb)
print("桌面样张判「是我自己」: %s   %s" % (ok_bg, "OK" if not ok_bg else "**误判**"))

# 拖动中被污染的样张：面板自己贴在背景上（模拟「这一帧抓到了自己」）
print("\n判据阈值 46.0，实测差值：")
for c2 in snap.SELF_BTN_COLORS:
    d = (abs(c[0] - (c2 & 0xFF)) + abs(c[1] - ((c2 >> 8) & 0xFF))
         + abs(c[2] - ((c2 >> 16) & 0xFF)))
    print("  面板 vs 0x%08X : %.1f" % (c2, d))
for c2 in snap.SELF_BTN_COLORS:
    d = (abs(cb[0] - (c2 & 0xFF)) + abs(cb[1] - ((c2 >> 8) & 0xFF))
         + abs(cb[2] - ((c2 >> 16) & 0xFF)))
    print("  桌面 vs 0x%08X : %.1f" % (c2, d))

user32.DestroyWindow(app.hwnd_main)
sys.exit(0 if (ok_self and not ok_bg) else 2)
