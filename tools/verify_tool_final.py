"""检查 _present_tool 最终提交的像素：按钮形状 vs 按钮之间的缝。

之前 tools/verify_tool_glass.py 只检查了 _tool_glass_layer 的返回值，
但 _present_tool 后面还会用 tb_alpha（矩形+pad 覆盖蒙版）覆盖一遍 alpha。
如果那一步把缝的 alpha 又填回去，中间层对、上屏就是错的。
"""
import sys
import ctypes

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from ctypes import byref, c_void_p

import snap
from snap import user32, w

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

app = snap.App()
app.create_main()
h = app.hwnd_main
user32.SetWindowPos(h, None, 200, 200, 0, 0, 0x0001 | 0x0004 | 0x0010)

# 造一份截图环境（真抓屏）
app._build_capture_buffers()
vx, vy, vw, vh = app.vs
app.sel = (vx + 100, vy + 100, 620, 400)
app.hover_tool = -1
app.mode = None

# 打开工具条（走真实流程）
app._layout_tool() if hasattr(app, "_layout_tool") else None
app.tb_pos = (vx + 300, vy + 560)
app.hwnd_tool = user32.CreateWindowExW(
    snap.WS_EX_TOPMOST | snap.WS_EX_TOOLWINDOW | snap.WS_EX_LAYERED,
    "SnapLiteTool", "", snap.WS_POPUP,
    app.tb_pos[0], app.tb_pos[1], 100, 60, None, None, app.hinst, None)
app._present_tool()

W, H = app.tb_size
bits = ctypes.string_at(app.tb_bits, W * H * 4)
cov = app._tool_shape_cov(W, H)

print("工具条尺寸 %dx%d" % (W, H))

# 统计：形状内 / 形状外 的 alpha
inside = [i for i in range(W * H) if cov[i] == 255]
outside = [i for i in range(W * H) if cov[i] == 0]
print("形状内像素 %d，形状外（缝/空白）像素 %d" % (len(inside), len(outside)))


def stats(idx, label):
    if not idx:
        print("  %s: 无像素" % label)
        return
    a0 = sum(1 for i in idx if bits[i * 4 + 3] == 0)
    a255 = sum(1 for i in idx if bits[i * 4 + 3] == 255)
    amax = max(bits[i * 4 + 3] for i in idx)
    amin = min(bits[i * 4 + 3] for i in idx)
    print("  %s: alpha 范围 %d~%d，完全透明 %d 个 (%.1f%%)"
          % (label, amin, amax, a0, 100.0 * a0 / len(idx)))


stats(inside, "按钮内部 ")
stats(outside, "按钮之间/外部")

# 取一个明确的缝：两个按钮之间的中点
_, _, rects = app._tool_layout()
x0 = int(rects[0][1] + rects[0][3])
x1 = int(rects[1][1])
ymid = int(rects[0][2] + rects[0][4] / 2)
print("\n第 1、2 个按钮之间：x=%d..%d" % (x0, x1))
for x in range(x0, min(x1 + 1, W)):
    o = (ymid * W + x) * 4
    print("  x=%3d cov=%3d  BGRA=%s" % (x, cov[ymid * W + x],
                                        bits[o:o + 4].hex()))

# 结论
bad = sum(1 for i in outside if bits[i * 4 + 3] > 8)
print("\n缝里 alpha>8 的像素：%d 个" % bad)
if bad > 40:
    print("FAIL: 按钮之间的缝被填上了不透明像素 —— 整条会看成一块深色板")
else:
    print("OK: 缝是透明/接近透明的")

user32.DestroyWindow(app.hwnd_tool)
app.hwnd_tool = None
user32.DestroyWindow(h)
