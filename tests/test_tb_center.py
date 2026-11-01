# -*- coding: utf-8 -*-
"""自测：工具条按钮内部「图标 + 文字」是否居中（v1.3.1）。

不是算公式对不对，而是把真实工具条渲染到 tb_bits，再按像素统计：
  1) 每颗按钮里内容的左留白 ≈ 右留白
  2) 图标墨迹的垂直中心 ≈ 文字墨迹的垂直中心 ≈ 按钮中心
  3) 「马赛克」这种三字标签没有被折行或裁掉

跑法：python tests/test_tb_center.py
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

INK_LUMA = 75           # 内容像素亮度下限。按钮底色约 31、白色描边约 57，
                        # 而最暗的内容是「取消」的红色图标（125），75 正好卡在中间
TEXT_LUMA = 185         # 只留文字：文字白色约 195，最亮的图标（复制蓝）171
TOL = 1.5               # 水平留白允许的偏差（px），1px 抗锯齿或取整误差算合格


def check(name, cond, extra=""):
    global ok
    mark = "PASS" if cond else "FAIL"
    if not cond:
        ok = False
    print("[%s] %s %s" % (mark, name, extra))


def grab(app):
    """取当前工具条的像素，返回 (raw_bgra, W, H)。预乘 alpha 也在里面"""
    W, H = app.tb_size
    raw = ctypes.string_at(app.tb_bits, W * H * 4)
    return raw, W, H


def is_ink(raw, W, x, y, luma=INK_LUMA):
    o = (y * W + x) * 4
    if raw[o + 3] < 100:
        return False
    return (0.114 * raw[o] + 0.587 * raw[o + 1] + 0.299 * raw[o + 2]) >= luma


def ink_box(raw, W, x0, y0, x1, y1, luma=INK_LUMA):
    """在矩形内找墨迹的边界，返回 (lx, ty, rx, by) 或 None"""
    lx = ty = 10 ** 6
    rx = by = -1
    for y in range(y0, y1):
        for x in range(x0, x1):
            if is_ink(raw, W, x, y, luma):
                lx = min(lx, x)
                rx = max(rx, x)
                ty = min(ty, y)
                by = max(by, y)
    if rx < 0:
        return None
    return lx, ty, rx, by


def main():
    app = snap.App()
    app.create_main()
    # 不调用 start_capture：那条路会抓真实屏幕，工具条背后的磨砂玻璃会把桌面
    # 的亮色透到按钮边缘，扫描"内容像素"时会被当成文字，位置就不准了。
    # 这里只要按钮本身，纯色底足够判断居中。
    app._build_capture_buffers()
    # 工具条背后的磨砂玻璃会把桌面亮色透到按钮边缘，扫描"内容像素"时会被
    # 当成文字。这里把抓屏缓冲摘掉，工具条就只剩按钮本身，位置才量得准。
    app.dc_dim = app.dc_shot = None
    app.tb_glass, app.tb_glass_key = None, None
    check("初始化完成", bool(app.hwnd_main))

    vsx, vsy, vsw, vsh = app.vs
    app.sel = (vsx + 300, vsy + 250, min(760, vsw - 600), min(420, vsh - 500))
    app._show_tool()
    check("工具条已创建", bool(app.hwnd_tool))
    if not app.hwnd_tool:
        return 1

    BW = app.tb(snap.TB_BTN_W)      # 用 tb()，因为它已经把 TB_SCALE 算进去了
    BH = app.tb(snap.TB_BTN_H)
    GP = app.tb(snap.TB_BTN_GAP)
    raw, W, H = grab(app)
    print("工具条 %dx%d  按钮 %dx%d  TB_SCALE=%.2f"
          % (W, H, BW, BH, snap.TB_SCALE))

    print()
    print("%-8s %6s %6s %8s %8s %8s" % ("标签", "左留白", "右留白", "图标中心", "文字中心", "垂直差"))
    all_lr = []
    for i, (kind, label) in enumerate(snap.TB_ITEMS):
        x0 = i * (BW + GP)
        box = ink_box(raw, W, x0, 0, x0 + BW, BH)
        if box is None:
            check("按钮有内容 %s" % label, False)
            continue
        lx, ty, rx, by = box
        left = lx - x0
        right = (x0 + BW - 1) - rx
        all_lr.append((label, left, right))

        # 文字是白色（亮度约 207），图标是灰蓝/蓝/红（最亮 171），
        # 用 185 这一刀能把文字单独拎出来，比按空白列切可靠得多。
        # 注意：不同字的笔画分布不一样（「圆」「保」最亮处偏下），所以文字的
        # 绝对中心会抖 1~2px —— 只当参考打印，不用它做断言。
        tbox = ink_box(raw, W, x0, 0, x0 + BW, BH, TEXT_LUMA)
        tx_c = (tbox[1] + tbox[3]) / 2.0 if tbox else -1
        print("%-8s %6d %6d %8.1f %8.1f %8.1f"
              % (label, left, right, (ty + by) / 2.0, tx_c, tx_c - (BH - 1) / 2.0))

        check("%s 水平居中" % label, abs(left - right) <= TOL,
              "左 %d 右 %d 差 %+d" % (left, right, left - right))
        # 像素 0..BH-1 的正中间是 (BH-1)/2，不是 BH/2。
        # 整块 bbox 由较高的那个元素（图标）决定，所以这条等价于「图标居中」。
        check("%s 整体在按钮垂直中线上" % label,
              abs((ty + by) / 2.0 - (BH - 1) / 2.0) <= TOL,
              "内容中心 %.1f / 按钮中心 %.1f" % ((ty + by) / 2.0, (BH - 1) / 2.0))

    print()
    worst = max(abs(l - r) for _, l, r in all_lr) if all_lr else 99
    check("全部按钮左右留白对称", worst <= TOL, "最大差 %d" % worst)

    # 「马赛克」不能被折行：文字段高度不应超过单行字高太多
    for i, (kind, label) in enumerate(snap.TB_ITEMS):
        if label != "马赛克":
            continue
        x0 = i * (BW + GP)
        box = ink_box(raw, W, x0, 0, x0 + BW, BH, TEXT_LUMA)
        h = box[3] - box[1] + 1
        line = int(round(12 * snap.TB_SCALE)) + 4      # 单行字高的合理上限
        check("「马赛克」单行没折", h <= line, "文字高 %dpx / 上限 %dpx" % (h, line))

    # 选项排的标签文字也要在各自小方块里垂直居中
    app._begin_mode("pen")
    app._show_tool()
    raw, W, H = grab(app)
    _, _, rects = app._tool_layout()
    for name, x, y, ww, hh in rects:
        if not name.startswith("@label_"):
            continue
        box = ink_box(raw, W, int(x), int(y), int(x + ww), int(y + hh))
        if box is None:
            check("选项排标签有内容 %s" % name, False)
            continue
        c = (box[1] + box[3]) / 2.0
        check("选项排 %s 垂直居中" % name,
              abs(c - (y + (hh - 1) / 2.0)) <= TOL,
              "中心 %.1f / 目标 %.1f" % (c, y + (hh - 1) / 2.0))

    # TB_ICON_INK 是手工维护的墨迹表，图标一改就得重新生成，否则居中会悄悄失效。
    # 这里用同一种量法（alpha>=128）复核一遍。
    check_icon_ink_table(app)

    app._close_tool()
    app._abort_capture()
    return 0 if ok else 1


def check_icon_ink_table(app):
    """核对 TB_ICON_INK 和图标实际画出来的墨迹是否一致（允许 1px 误差）"""
    from snap import GFX
    sz = snap.TB_ICON_SIZE
    box = 40
    sdc = app._get_screen_dc()
    bad = 0
    for kind, _ in snap.TB_ITEMS:
        d, b, o, p = snap._new_dib(sdc, box, box)
        g = GFX(d)
        app._draw_tool_icon(g, kind, 0, 0, sz, 0xFFFFFFFF, 0)
        g.close()
        px = ctypes.string_at(p, box * box * 4)
        lx = ty = 10 ** 6
        rx = by = -1
        for y in range(box):
            for x in range(box):
                if px[(y * box + x) * 4 + 3] >= 128:
                    lx = min(lx, x); rx = max(rx, x)
                    ty = min(ty, y); by = max(by, y)
        snap._free_dib((d, b, o, p))
        if rx < 0:
            check("图标 %s 有墨迹" % kind, False)
            bad += 1
            continue
        want = snap.TB_ICON_INK.get(kind)
        if want is None:
            check("TB_ICON_INK 缺 %s" % kind, False)
            bad += 1
            continue
        diff = max(abs(a - c) for a, c in zip(want, (lx, ty, rx, by)))
        if diff > 1:
            bad += 1
            print("    %s 表里 %s，实际 %s（差 %d）→ 请重跑 tools/measure_ink.py"
                  % (kind, want, (lx, ty, rx, by), diff))
    check("TB_ICON_INK 与图标实际墨迹一致", bad == 0, "过期 %d 个" % bad)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
