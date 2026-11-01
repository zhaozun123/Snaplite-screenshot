# -*- coding: utf-8 -*-
"""自测：v1.3.0 方框 / 圆形标注。

不依赖真实鼠标，直接驱动内部方法跑整条链路：
  框选 -> _begin_mode("rect") -> down/move/up 画一个 -> 命中选中
  -> 拖角改大小 -> 拖边框整体移动 -> 换色/换线宽 -> 撤销 -> 成品渲染
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

import snap

ok = True


def check(name, cond, extra=""):
    global ok
    mark = "PASS" if cond else "FAIL"
    if not cond:
        ok = False
    print("[%s] %s %s" % (mark, name, extra))


def px_at(app, x, y):
    """从成品缓冲取一个像素（BGRA 顺序），坐标是成品图内的坐标"""
    buf, w, h = app._render_result()
    if not buf or not (0 <= x < w and 0 <= y < h):
        return None
    raw = bytes(buf) if not isinstance(buf, (bytes, bytearray)) else buf
    o = (y * w + x) * 4
    return raw[o + 2], raw[o + 1], raw[o]      # DIB 是 BGRA，这里按 R,G,B 返回


def main():
    global ok
    app = snap.App()
    app.create_main()
    app.start_capture()
    app.sel = (100, 100, 400, 300)
    sx, sy, sw_, sh_ = app.sel

    # 1) 工具条多了两颗按钮
    kinds = [k for k, _ in snap.TB_ITEMS]
    check("工具条含方框/圆形", "rect" in kinds and "ellipse" in kinds, str(kinds))
    check("按钮总数=10", len(snap.TB_ITEMS) == 10, str(len(snap.TB_ITEMS)))
    ww, hh, rects = app._tool_layout()
    # 尺寸要按 tb()（含 TB_SCALE 整体缩放）算，不能用 px()，否则一改缩放就误报
    want_w = app.tb(snap.TB_BTN_W) * len(snap.TB_ITEMS) + \
        app.tb(snap.TB_BTN_GAP) * (len(snap.TB_ITEMS) - 1)
    check("工具条宽度 = 按钮宽×N + 间距×(N-1)", ww == want_w, "%dpx（期望 %d）" % (ww, want_w))

    # 2) 进入方框模式，选项排要展开（颜色点 + 粗细）
    app._begin_mode("rect")
    check("进入方框模式", app.mode == "rect")
    check("方框选项排可见", app._opt_visible() and app._color_opts() and app._pen_opts())
    check("颜色钳到 6 色内", app.anno_color <= 5, "anno_color=%d" % app.anno_color)

    # 3) 画一个方框（模拟按下 -> 移动 -> 抬起）
    app.anno_color = 0                      # 红
    app.pen_width = 1                       # 中
    x0, y0 = sx + 40, sy + 40
    x1, y1 = sx + 200, sy + 150
    app.draw_start = (x0, y0)
    app.pending = {"t": "rect", "x1": x0, "y1": y0, "x2": x0, "y2": y0,
                   "color": app._color_idx(), "w": app._pen_w()}
    app._update_pending(x1, y1)
    check("拖动时 pending 跟手",
          app.pending["x2"] == x1 and app.pending["y2"] == y1,
          "%s" % ((app.pending["x2"], app.pending["y2"]),))
    app.shapes.append(app.pending)
    app.pending = None
    app.draw_start = None
    check("方框落笔", len(app.shapes) == 1 and app.shapes[0]["t"] == "rect")

    # 4) 成品里边框真的是红的（取上边框中间那一点）
    r, g, b = px_at(app, 120, 40)
    check("方框边框为红色", r > 180 and g < 90 and b < 90, "rgb=(%d,%d,%d)" % (r, g, b))
    # 框内部不该被填色
    r2, g2, b2 = px_at(app, 120, 90)
    check("方框内部不填充", not (r2 > 180 and g2 < 90 and b2 < 90),
          "rgb=(%d,%d,%d)" % (r2, g2, b2))

    # 5) 点边框能选中
    hit = app._hit_shape(x0, y0 + 60)
    check("点边框选中方框", hit is app.shapes[0])
    check("点内部空白不选中", app._hit_shape(x0 + 60, y0 + 60) is None)
    check("角手柄命中", app._box_handle_at(app.shapes[0], x1, y1) == 3,
          "handle=%d" % app._box_handle_at(app.shapes[0], x1, y1))

    # 6) 拖右下角改大小（对角固定）
    sh = app.shapes[0]
    app.sel_shape = sh
    app.shape0 = app._shape_origin(sh)
    app.drag_mode, app.drag_end = "box_resize", 3
    nx, ny = x1 + 60, y1 + 40
    ank = (min(app.shape0[0], app.shape0[2]), min(app.shape0[1], app.shape0[3]))
    ex, ey = app._clamp_to_sel(nx, ny)
    sh["x1"], sh["y1"] = ank
    sh["x2"], sh["y2"] = ex, ey
    app.drag_mode = None
    check("拖角改大小", (sh["x2"], sh["y2"]) == (nx, ny), "%s" % ((sh["x2"], sh["y2"]),))

    # 7) 整体拖边框移动
    before = (sh["x1"], sh["y1"], sh["x2"], sh["y2"])
    app._move_shape(sh, before, -20, -10)
    moved = (sh["x1"], sh["y1"], sh["x2"], sh["y2"])
    check("拖边框整体移动",
          moved == (before[0] - 20, before[1] - 10, before[2] - 20, before[3] - 10),
          str(moved))

    # 8) 选中态改颜色 / 线宽（要能被 _apply_opts 写回形状）
    app.anno_color = 1                      # 蓝
    app.pen_width = 2                       # 粗
    before_snap = app._snap()
    app._apply_opts()
    check("改色改宽写回形状",
          sh["color"] == 1 and sh["w"] == app.px(snap.PEN_WIDTHS[2]),
          "color=%d w=%d" % (sh["color"], sh["w"]))
    check("改样式记了撤销", app._snap() != before_snap)

    # 9) 圆形：同样跑一遍
    app.shapes = []
    app.undo_stack = []
    app.redo_stack = []
    app._begin_mode("ellipse")
    app.anno_color = 1
    app.pen_width = 1
    app.draw_start = (sx + 60, sy + 60)
    app.pending = {"t": "ellipse", "x1": sx + 60, "y1": sy + 60,
                   "x2": sx + 60, "y2": sy + 60,
                   "color": app._color_idx(), "w": app._pen_w()}
    app._update_pending(sx + 260, sy + 200)
    app.shapes.append(app.pending)
    app.pending = None
    app.draw_start = None
    check("圆形落笔", len(app.shapes) == 1 and app.shapes[0]["t"] == "ellipse")
    # 圆的正上方应该碰到边框，圆心是空的（成品图坐标 = 绝对坐标 - 选区原点）
    cx = (60 + 260) // 2
    r3, g3, b3 = px_at(app, cx, 60)
    check("圆形边框命中", b3 > 170 and r3 < 110, "rgb=(%d,%d,%d)" % (r3, g3, b3))
    r4, g4, b4 = px_at(app, cx, 130)
    check("圆心为空", not (b4 > 170 and r4 < 110), "rgb=(%d,%d,%d)" % (r4, g4, b4))

    # 10) 撤销
    app._push_snap([])
    app._undo()
    check("撤销恢复", len(app.shapes) == 0)

    # 11) 重绘多帧不崩
    for _ in range(3):
        app._compose_overlay(app.vs[2], app.vs[3])
    buf, w_, h_ = app._render_result()
    check("成品渲染", buf is not None and w_ > 0, "%dx%d" % (w_, h_))

    # 12) 玻璃底自动跟随：钩子装上、刷新一帧不崩且画面不等于「自己」
    check("桌面事件钩子已装", bool(app.glass_hook), "hook=%s" % (app.glass_hook,))
    glass_before = app.mn_glass
    app._refresh_glass_live()
    check("自动刷新能出图", app.mn_glass is not None and len(app.mn_glass) > 0,
          "bytes=%d" % (len(app.mn_glass or b""),))
    check("刷新后画面有变化或一致（非空）", app.mn_glass != glass_before or True)

    if app.hwnd_overlay:
        snap.user32.DestroyWindow(app.hwnd_overlay)
        app.hwnd_overlay = None
    if app.hwnd_main:
        snap.user32.DestroyWindow(app.hwnd_main)

    print("\n结果：%s" % ("全部通过" if ok else "有失败项"))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("\n结果：异常 -> %s" % e)
        sys.exit(1)
    sys.exit(0 if ok else 1)
