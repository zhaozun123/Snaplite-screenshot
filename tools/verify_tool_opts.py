"""检查工具条切换模式时的蒙版 / 玻璃形状是否跟着重建。

历史 bug：画笔、马赛克、箭头三种模式的选项排宽高都是 682×92，
表面重建只比尺寸 → 沿用上一种模式的蒙版和玻璃覆盖率 →
文字后面冒出不该有的毛玻璃、控件被裁掉、右侧留一块残影。
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
from snap import user32

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

FAIL = []


def check(name, ok, detail=""):
    print(("  [PASS] " if ok else "  [FAIL] ") + name + ("   " + detail if detail else ""))
    if not ok:
        FAIL.append(name)


app = snap.App()
app.create_main()
user32.SetWindowPos(app.hwnd_main, None, 40, 40, 0, 0, 0x0001 | 0x0004 | 0x0010)
app._build_capture_buffers()
vsx, vsy, vsw, vsh = app.vs
app.sel = (vsx + 400, vsy + 300, 700, 420)

order = ["pen", "mosaic", "arrow", "mosaic", "pen", None]
for mode in order:
    app.mode = None
    app.sel_shape = None
    if mode:
        app._begin_mode(mode)
    else:
        app._refresh_tool()
    app._show_tool()
    tag = mode or "none"
    W, H = app.tb_size
    key = app._tool_layout_key()
    rects = app._tool_layout()[2]
    cov = app.tb_cov
    alpha = app.tb_bits and ctypes.string_at(app.tb_bits, W * H * 4)

    print("--- %s  %dx%d  rects=%d" % (tag, W, H, len(rects)))
    check("表面按当前布局重建", app.tb_key == key)
    check("玻璃形状按当前布局重建", app.tb_cov_key == key)

    # 主排按钮中心有玻璃底，缝隙没有
    gaps_ok, btns_ok = True, True
    for (name, x, y, ww, hh) in rects:
        if name != "@btn":
            continue
        c = cov[int(y + hh / 2) * W + int(x + ww / 2)]
        if c != 255:
            btns_ok = False
    check("主排按钮位置都有玻璃底", btns_ok)

    # 选项排（第二排）完全没有玻璃底
    if H > app.px(44):
        oy = app.px(44) + 2
        opt_cols = [cov[yy * W + xx] for yy in range(oy, H) for xx in range(0, W, 3)]
        hit = sum(1 for v in opt_cols if v > 8)
        check("选项排没有玻璃底（不会有字后面的方块）", hit == 0, "命中=%d" % hit)

        # 选项排里，标签区域只能有字，不能是一整块底色
        for (name, x, y, ww, hh) in rects:
            if not name.startswith("@label"):
                continue
            xa, ya = int(x) + 2, int(y) + 2
            xb, yb = int(x + ww) - 2, int(y + hh) - 2
            total = max(1, (xb - xa) * (yb - ya))
            lit = 0
            for yy in range(ya, yb):
                for xx in range(xa, xb):
                    if alpha[(yy * W + xx) * 4 + 3] > 8:
                        lit += 1
            ratio = lit / float(total)
            check("标签 %s 只有字、没有底色块" % name, ratio < 0.25,
                  "非透明占比=%.0f%%" % (ratio * 100))

    # 未使用区域（整块里没有控件的地方）不应该有残留
    stray = 0
    for yy in range(0, H, 2):
        for xx in range(0, W, 2):
            in_rect = False
            for (name, x, y, ww, hh) in rects:
                if x - 4 <= xx <= x + ww + 4 and y - 4 <= yy <= y + hh + 4:
                    in_rect = True
                    break
            if not in_rect and alpha[(yy * W + xx) * 4 + 3] > 8:
                stray += 1
    check("控件之外的区域没有残留像素", stray == 0, "残留点=%d" % stray)

app._close_tool()
user32.DestroyWindow(app.hwnd_main)
print()
print("结论：" + ("全部通过" if not FAIL else "失败 %d 项 → %s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
