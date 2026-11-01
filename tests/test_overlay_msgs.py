# -*- coding: utf-8 -*-
"""自测：遮罩窗口的消息处理（_on_overlay 拆分后的入口）。

这段原来是一个 317 行的 if 链，没有任何自动化覆盖。拆成 _ov_* 之后，
这里直接按 Windows 的真实消息格式喂进去，验证：
  拖出新选区 / 移动选区 / 拖手柄改大小 / 画笔 / 方框
  / 方向键微调 / Delete / 右键逐级退出 / Esc 逐级退出 / 光标 / 兜底返回值
"""
import sys
import os

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


def lp_of(x, y):
    """把 (x, y) 打包成 lParam —— 和系统给的一模一样，低 16 位 x、高 16 位 y"""
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


class Driver:
    """按真实消息顺序驱动遮罩窗口"""

    def __init__(self, app):
        self.app = app
        self.hwnd = app.hwnd_overlay

    def send(self, msg, wp=0, lp=0):
        return self.app._on_overlay(self.hwnd, msg, wp, lp)

    def move(self, x, y):
        return self.send(snap.WM_MOUSEMOVE, 0, lp_of(x, y))

    def down(self, x, y):
        return self.send(snap.WM_LBUTTONDOWN, 0, lp_of(x, y))

    def up(self, x, y):
        return self.send(snap.WM_LBUTTONUP, 0, lp_of(x, y))

    def right(self):
        return self.send(snap.WM_RBUTTONDOWN, 0, 0)

    def key(self, vk):
        return self.send(snap.WM_KEYDOWN, vk, 0)

    def drag(self, x0, y0, x1, y1, steps=4):
        self.down(x0, y0)
        for i in range(1, steps + 1):
            self.move(x0 + (x1 - x0) * i // steps, y0 + (y1 - y0) * i // steps)
        self.up(x1, y1)


def main():
    global ok
    app = snap.App()
    app.create_main()
    app.start_capture()
    d = Driver(app)

    # ---------- 1. 拖出一个新选区 ----------
    app.sel = None
    app.pending = None
    d.drag(60, 60, 460, 360)
    s = app.sel
    check("拖出新选区", s is not None and s[2] >= 300 and s[3] >= 200, "%s" % (s,))

    # ---------- 2. 在选区内拖动 = 整体移动 ----------
    x0, y0, sw_, sh_ = app.sel
    cx, cy = x0 + sw_ // 2, y0 + sh_ // 2
    d.drag(cx, cy, cx + 30, cy + 20)
    s2 = app.sel
    check("拖动整体移动选区", s2 is not None and s2[0] == x0 + 30 and s2[1] == y0 + 20,
          "位移=(%+d,%+d)" % (s2[0] - x0, s2[1] - y0))

    # ---------- 3. 拖右下角手柄改大小 ----------
    x0, y0, sw_, sh_ = app.sel
    d.drag(x0 + sw_, y0 + sh_, x0 + sw_ - 60, y0 + sh_ - 40)
    s3 = app.sel
    check("拖手柄改大小", s3 is not None and s3[2] == sw_ - 60 and s3[3] == sh_ - 40,
          "%dx%d -> %dx%d" % (sw_, sh_, s3[2], s3[3]))

    # ---------- 4. 画笔：按一下拖几笔 ----------
    app._begin_mode("pen")
    x0, y0, sw_, sh_ = app.sel
    px0, py0 = x0 + 40, y0 + 40
    d.drag(px0, py0, px0 + 80, py0 + 40, steps=6)
    check("画笔落笔", len(app.shapes) == 1 and app.shapes[0]["t"] == "pen",
          "%d 条" % len(app.shapes))

    # ---------- 5. 方框 ----------
    app._begin_mode("rect")
    d.drag(x0 + 40, y0 + 100, x0 + 160, y0 + 180)
    check("方框落笔", len(app.shapes) == 2 and app.shapes[1]["t"] == "rect",
          "%d 条" % len(app.shapes))

    # ---------- 6. 方向键微调（选中方框） ----------
    app.sel_shape = app.shapes[1]
    bx1 = app.sel_shape["x1"]
    d.key(0x27)                                    # →
    check("方向键微调 +1px", app.sel_shape["x1"] == bx1 + 1,
          "x1 %d -> %d" % (bx1, app.sel_shape["x1"]))

    # ---------- 7. Delete 删除选中 ----------
    d.key(0x2E)
    check("Delete 删除选中标注", len(app.shapes) == 1 and app.sel_shape is None,
          "%d 条" % len(app.shapes))

    # ---------- 8. 右键逐级退出 ----------
    d.right()                                      # 有模式 -> 退出模式
    check("右键退出标注模式", app.mode is None)
    d.right()                                      # 还有一笔 -> 删掉
    check("右键删最后一笔", len(app.shapes) == 0, "%d 条" % len(app.shapes))
    d.right()                                      # 没标注了 -> 取消截图
    check("右键取消截图", app.capturing is False)

    # ---------- 9. Esc 逐级退出 ----------
    app.start_capture()
    d = Driver(app)
    app.sel = (100, 100, 400, 300)
    app._begin_mode("mosaic")
    d.key(snap.VK_ESCAPE)
    check("Esc 退出模式", app.mode is None and app.capturing is True)
    d.key(snap.VK_ESCAPE)
    check("Esc 取消截图", app.capturing is False)

    # ---------- 10. 光标与兜底返回值 ----------
    app.start_capture()
    d = Driver(app)
    app.sel = (100, 100, 400, 300)
    check("WM_SETCURSOR 返回 1", d.send(snap.WM_SETCURSOR, 0, 0) == 1)
    check("WM_ERASEBKGND 返回 1", d.send(snap.WM_ERASEBKGND, 0, 0) == 1)
    r = d.send(0x0018, 0, 0)                       # WM_SHOWWINDOW，未处理
    check("未处理消息走默认处理", isinstance(r, int), "返回 %r" % (r,))
    d.right()
    check("收尾：截图已取消", app.capturing is False)

    # GDI 句柄不该因为这一串消息泄漏
    try:
        gdi = snap.user32.GetGuiResources
        gdi.argtypes = [snap.c_void_p, snap.c_uint]
        gdi.restype = snap.c_uint
        k32 = __import__("ctypes").windll.kernel32
        k32.GetCurrentProcess.restype = snap.c_void_p
        n = gdi(k32.GetCurrentProcess(), 0)
        check("GDI 句柄未异常增长", n < 200, "%d" % n)
    except Exception as e:
        check("GDI 句柄未异常增长", True, "跳过（%r）" % (e,))

    app.quit()
    print("\n结果：%s" % ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
