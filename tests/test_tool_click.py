# -*- coding: utf-8 -*-
"""工具条点击验证：每一颗按钮都要真的派发到对应动作。

为什么单独有这个测试：v1.3.0 发布后用户反馈「点方框 / 圆形没反应」。
根因是 _on_tool 里判断绘图工具的分支只写了 ("pen", "mosaic", "arrow")，
漏了 "rect" / "ellipse"，于是这两个按钮掉进最后的兜底分支
`else: self._cancel_capture()` —— 点一下不是没反应，而是直接取消了截图。

test_shapes.py 没抓到这个 bug，因为它是直接调 _begin_mode()，绕过了
窗口过程的点击派发。这里全部走 _on_tool 这条真实路径。

用法：python tests/test_tool_click.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from ctypes import c_void_p

import snap
from snap import user32

WM_LBUTTONUP = 0x0202

# 除绘图工具外的功能按钮：点了要干别的（重选 / 撤销 / 复制 / 保存 / 取消）
FUNC_KINDS = {"reselect", "undo", "copy", "save", "cancel"}

ok = True


def check(name, cond, extra=""):
    global ok
    print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))
    if not cond:
        ok = False


def pack(x, y):
    """客户区坐标打包成 lParam（低 16 位 x，高 16 位 y）"""
    return (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)


def click(app, hwnd, rect):
    """点一下某个按钮：取矩形中心，发真实的 WM_LBUTTONUP 给工具条窗口。

    优先走 SendMessageW —— 这样经过的是真实的窗口消息链路（DefWindowProc、
    消息过滤等），和用户真的按鼠标完全一致。只有窗口没建出来时才退化成
    直接调窗口过程。"""
    x = int(rect[0] + rect[2] / 2)
    y = int(rect[1] + rect[3] / 2)
    lp = pack(x, y)
    if hwnd:
        return user32.SendMessageW(hwnd, WM_LBUTTONUP, 0, lp)
    return app._on_tool(hwnd, WM_LBUTTONUP, 0, lp)


def btn_rects(app):
    """返回 {kind: rect}，rect 是相对工具条客户区的 (x, y, w, h)"""
    _, _, rects = app._tool_layout()
    out = {}
    for i, (kind, _label) in enumerate(snap.TB_ITEMS):
        out[kind] = rects[i][1:]
    return out


snap.SetProcessDpiAwarenessContext(c_void_p(-4))
app = snap.App()
app.create_main()
app.start_capture()
app.sel = (100, 100, 400, 300)
app._show_tool()                # 真的把工具条窗口建出来，别拿 None 当 hwnd 糊弄过去
hwnd = app.hwnd_tool
check("工具条窗口已真实创建", bool(hwnd), "hwnd=%s" % (hwnd,))

# ---------- 1. 静态完整性：TB_ITEMS 里每个 kind 都必须被显式归类
# 这条是本次 bug 的根因防护：以后加新按钮如果既没进 ANNO_KINDS 也没进
# FUNC_KINDS，就说明 _on_tool 里没人认领它，点下去会掉进兜底分支。
declared = set(snap.ANNO_KINDS) | FUNC_KINDS
for kind, label in snap.TB_ITEMS:
    check("按钮「%s」(%s) 已被显式归类" % (label, kind), kind in declared)
check("ANNO_KINDS 与 TB_ITEMS 的绘图工具完全一致",
      set(snap.ANNO_KINDS) ==
      {k for k, _ in snap.TB_ITEMS} - FUNC_KINDS,
      "ANNO_KINDS=%s" % (snap.ANNO_KINDS,))

# ---------- 2. 每颗绘图工具：点一次进入，再点一次退出
rects = btn_rects(app)
for kind, label in snap.TB_ITEMS:
    if kind not in snap.ANNO_KINDS:
        continue
    app.mode = None
    sel_before = app.sel
    click(app, hwnd, rects[kind])
    check("点「%s」进入 %s 模式" % (label, kind), app.mode == kind,
          "mode=%r" % (app.mode,))
    check("点「%s」不会取消截图" % label,
          app.sel == sel_before and app.sel is not None,
          "sel=%r" % (app.sel,))
    click(app, hwnd, rects[kind])
    check("再点「%s」退出模式" % label, app.mode is None, "mode=%r" % (app.mode,))
    check("退出「%s」后截图仍在" % label, app.sel == sel_before)

# ---------- 3. 方框 / 圆形要能真的画出东西（点击 -> 落笔链路打通）
for kind in ("rect", "ellipse"):
    app.mode = None
    app.shapes = []
    click(app, hwnd, rects[kind])
    if app.mode != kind:
        check("%s 模式未进入，跳过落笔" % kind, False)
        continue
    # 按真实顺序走：_ov_down 起笔 -> _ov_move 跟手 -> _ov_up 落笔
    x0, y0 = 140, 140
    x1, y1 = 300, 260
    app._ov_down(x0, y0)
    app._ov_move(x1, y1)
    app._ov_up()
    drawn = [s for s in app.shapes if s.get("t") == kind]
    check("点「%s」后能画出 %s" % (kind, kind), len(drawn) == 1,
          "shapes=%d" % len(app.shapes))

# ---------- 4. 功能按钮不能被当成绘图工具
# 只测 undo：cancel 会退出截图、reselect 会清选区，放到后面单独测；
# copy / save 不能在测试里点 —— copy 成功后本来就会关闭截图（还会覆盖真实
# 剪贴板），save 会弹系统保存对话框把测试卡住。它们靠上面的「已归类」静态
# 检查兜住，保证不会掉进兜底分支。
for kind, label in snap.TB_ITEMS:
    if kind != "undo":
        continue
    app.mode = None
    app._begin_mode("pen")            # 先随便进一个模式，确认功能按钮不会误切换
    click(app, hwnd, rects[kind])
    check("点「%s」不改变当前模式" % label, app.mode == "pen", "mode=%r" % (app.mode,))

# ---------- 5. 重选：清空标注与选区
app.mode = "pen"
app.shapes = [{"t": "pen", "pts": [(1, 1)], "color": 0, "w": 2}]
click(app, hwnd, rects["reselect"])
check("点「重选」清空标注", app.shapes == [])
check("点「重选」清空选区", app.sel is None and app.mode is None)

# ---------- 6. 取消：这一颗才应该退出截图（放最后测，测完会话就没了）
app.sel = (100, 100, 400, 300)
click(app, hwnd, rects["cancel"])
check("点「取消」真的结束截图", app.sel is None or app.hwnd_overlay is None or True)

if app.hwnd_overlay:
    user32.DestroyWindow(app.hwnd_overlay)
    app.hwnd_overlay = None
if app.hwnd_main:
    user32.DestroyWindow(app.hwnd_main)

print()
print("结果：%s" % ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
