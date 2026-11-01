"""量出每个工具条图标的「真实墨迹边界」，直接打印成可粘贴的常量表。

为什么需要：图标是用 15 单位网格画的，但各图标的墨迹并没有占满这个盒子
（× 只占 3~13，框选图标占满 0~16）。按盒子居中时，小图标看起来就偏了。
把墨迹边界记下来，才能让每个图标的「看得见的中心」落在同一条线上。

墨迹判定：alpha >= 128（半透明以上），忽略抗锯齿的淡边。

用法：python tools/measure_ink.py
"""
import sys
import os
import ctypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from ctypes import c_void_p

import snap
from snap import user32, GFX, TB_ITEMS

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

SZ = 17          # 与 TB_ICON_SIZE 一致
BOX = 40         # 测量画布比图标大一圈，避免贴边被裁
FS = 12.0

app = snap.App()
app.create_main()
app._build_capture_buffers()
sdc = app._get_screen_dc()


def ink_of(draw):
    d, b, o, p = snap._new_dib(sdc, BOX, BOX)
    g = GFX(d)
    draw(g)
    g.close()
    raw = ctypes.string_at(p, BOX * BOX * 4)
    lx = ty = 10 ** 6
    rx = by = -1
    for y in range(BOX):
        for x in range(BOX):
            if raw[(y * BOX + x) * 4 + 3] >= 128:
                lx = min(lx, x); rx = max(rx, x)
                ty = min(ty, y); by = max(by, y)
    snap._free_dib((d, b, o, p))
    return (lx, ty, rx, by) if rx >= 0 else None


print("# 图标在 %dpx 方框内的墨迹边界（l, t, r, b），右/下为闭区间" % SZ)
print("TB_ICON_INK = {")
for kind, _ in TB_ITEMS:
    box = ink_of(lambda gg, k=kind: app._draw_tool_icon(gg, k, 0, 0, SZ, 0xFFFFFFFF, 0))
    if box is None:
        print("    # %s 没画出东西" % kind)
        continue
    l, t, r, b = box
    print('    "%s": (%d, %d, %d, %d),   # %dx%d，中心 (%.1f, %.1f)'
          % (kind, l, t, r, b, r - l + 1, b - t + 1, (l + r) / 2.0, (t + b) / 2.0))
print("}")

print()
print("# 文字墨迹：相对文字框 (0,0) 的左偏移与宽度 —— 用来核对 TB_TEXT_SIDE")
print("# %-8s %8s %8s %8s %8s" % ("标签", "推进宽", "墨迹左", "墨迹宽", "两侧留白和"))
gm = GFX(sdc)
for kind, label in TB_ITEMS:
    adv, _ = gm.measure(label, FS)
    box = ink_of(lambda gg, s=label: gg.text(s, FS, 0xFFFFFFFF, 0, 0, BOX, BOX, 0, False))
    if box is None:
        continue
    l, t, r, b = box
    print("  %-8s %8.1f %8d %8d %8.1f"
          % (label, adv, l, r - l + 1, adv - (r - l + 1)))
gm.close()

user32.DestroyWindow(app.hwnd_main)
