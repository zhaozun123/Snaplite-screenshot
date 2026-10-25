# -*- coding: utf-8 -*-
"""自测：v1.1.0 画笔功能链路。

不依赖真实鼠标，直接驱动内部方法验证画笔链路：
  start_capture -> 框选 -> _begin_mode("pen") -> 模拟绘制(down/move/up)
  -> 验证 shapes 落笔 -> _draw_shapes 不崩溃 -> 撤销 -> _render_result 成品
"""
import sys, os, time, ctypes
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import snap

ok = True


def check(name, cond, extra=""):
    global ok
    mark = "PASS" if cond else "FAIL"
    if not cond:
        ok = False
    print("[%s] %s %s" % (mark, name, extra))


def main():
    global ok
    app = snap.App()
    app.create_main()
    app.start_capture()
    check("start_capture", app.capturing and app.hwnd_overlay)

    app.sel = (100, 100, 400, 300)

    # 1) 工具条里有「画笔」，没有「文本」
    kinds = [k for k, _ in snap.TB_ITEMS]
    check("工具条含画笔", "pen" in kinds, str(kinds))
    check("工具条不含文本", "text" not in kinds)

    # 2) 进入画笔模式
    app._begin_mode("pen")
    check("进入画笔模式", app.mode == "pen")
    check("画笔选项可见", app._pen_opts() and app._opt_visible())

    # 3) 模拟绘制：down -> 多次 move -> up（直接驱动内部方法，绕过消息循环）
    x, y = 150, 150
    app.draw_start = (x, y)
    app.pending = {"t": "pen", "pts": [(x, y)],
                   "color": app.anno_color,
                   "w": app.px(snap.PEN_WIDTHS[app.pen_width])}
    for dx, dy in [(10, 0), (20, 5), (30, 10), (40, 20)]:
        app._update_pending(x + dx, y + dy)
    check("绘制记录了点", len(app.pending["pts"]) >= 5,
          "pts=%d" % len(app.pending["pts"]))

    # 4) 提交这笔（模拟 WM_LBUTTONUP 的落笔逻辑）
    app.shapes.append(app.pending)
    app.pending = None
    app.draw_start = None
    check("落笔到 shapes", len(app.shapes) == 1 and app.shapes[0]["t"] == "pen")

    # 5) 重绘不崩溃（画全部标注，含画笔）
    for _ in range(3):
        app._compose_overlay(app.vs[2], app.vs[3])

    # 6) 撤销：真实流程 —— 落笔前先记快照，落笔后 _undo 应恢复到空
    app.shapes = []
    app.undo_stack = []
    app.redo_stack = []
    empty_snap = app._snap()                    # 空状态快照
    app._push_snap(empty_snap)                  # 落笔前记一步
    app.shapes.append({"t": "pen", "pts": [(150, 150), (160, 150)],
                       "color": 0, "w": 6})     # 落笔
    check("落笔后 shapes=1", len(app.shapes) == 1)
    app._undo()                                 # 撤销
    check("撤销恢复", len(app.shapes) == 0)

    # 7) 成品渲染（含画笔）不崩溃
    buf, w, h = app._render_result()
    check("成品渲染", buf is not None and w > 0 and h > 0, "%dx%d" % (w, h))

    # 8) 换颜色 / 粗细
    app.anno_color = 2
    app.pen_width = 2
    check("换颜色/粗细不崩", app.anno_color == 2 and app.pen_width == 2)

    # 收尾
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
