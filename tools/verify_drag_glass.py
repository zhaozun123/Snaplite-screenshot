# -*- coding: utf-8 -*-
"""验证「拖动时磨砂玻璃是实时更新的」——纯自动化，不需要人手拖

做法：
  1. 建出真实的主窗口（和线上完全一样的分层窗口）
  2. 记录位置 A 的玻璃底
  3. 把窗口挪到位置 B，走一次拖动中的渲染
  4. 拿「真的在 B 位置抓屏 + 模糊」的结果做标准答案，跟缓存裁出来的比
     · 如果两者高度一致 → 说明拖动时贴的确实是「当前这个位置」的桌面
     · 如果和 A 一样、跟 B 的答案差很远 → 就是原来那个「拖到哪都还是旧画面」的 bug

用法：python tools/verify_drag_glass.py
"""
import sys, os, ctypes
from ctypes import byref, c_void_p

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import snap
from snap import user32, w

SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010


def center_rgb(bgra, W, H):
    """取中间一块 40x40 的平均色（避开圆角和边框，只看玻璃底色）"""
    cx, cy = W // 2, H // 2
    r = g = b = 0
    n = 0
    for y in range(cy - 20, cy + 20):
        base = y * W * 4
        for x in range(cx - 20, cx + 20):
            o = base + x * 4
            b += bgra[o]
            g += bgra[o + 1]
            r += bgra[o + 2]
            n += 1
    return (r / n, g / n, b / n)


def dist(c1, c2):
    return sum(abs(a - b) for a, b in zip(c1, c2)) / 3.0


def main():
    try:
        snap.SetProcessDpiAwarenessContext(c_void_p(-4))
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

    app = snap.App()
    app.create_main()
    if not app.hwnd_main:
        print("FAIL: 主窗口没建出来")
        return 1

    W, H = app.MW, app.MH
    rc = w.RECT()
    user32.GetWindowRect(app.hwnd_main, byref(rc))
    ax, ay = rc.left, rc.top
    bx, by = ax + 320, ay + 120
    print("窗口尺寸 %dx%d，位置 A=(%d,%d)  B=(%d,%d)" % (W, H, ax, ay, bx, by))

    # --- 位置 A
    app._begin_drag_glass()
    if not app.drag_active:
        print("FAIL: 拖动缓存没建起来（drag_active=False）")
        return 1
    app.drag_last_pos = None
    app._drag_frame(ax, ay)
    glass_a = app.mn_glass
    rgb_a = center_rgb(glass_a, W, H)
    print("A 位置玻璃底色 RGB = (%.1f, %.1f, %.1f)" % rgb_a)

    # --- 挪到 B：走拖动中的那一帧（位置和内容一起提交，窗口会真的到 B）
    app.drag_last_pos = None
    app._drag_frame(bx, by)
    glass_b = app.mn_glass
    rgb_b = center_rgb(glass_b, W, H)
    print("B 位置玻璃底色 RGB = (%.1f, %.1f, %.1f)" % rgb_b)
    user32.GetWindowRect(app.hwnd_main, byref(rc))
    print("提交后窗口实际位置 (%d,%d)，与目标 (%d,%d) 偏差 (%d,%d)"
          % (rc.left, rc.top, bx, by, rc.left - bx, rc.top - by))

    # --- 标准答案：把窗口藏掉，真的在 B 处抓屏 + 模糊
    user32.ShowWindow(app.hwnd_main, 0)
    try:
        snap.dwmapi.DwmFlush()
    except Exception:
        pass
    truth_raw = snap.grab_screen(bx, by, W, H).raw
    truth = app._blur_bgra(truth_raw, W, H)
    rgb_truth = center_rgb(truth, W, H)
    user32.ShowWindow(app.hwnd_main, 5)
    print("B 位置标准答案 RGB = (%.1f, %.1f, %.1f)" % rgb_truth)

    d_bt = dist(rgb_b, rgb_truth)     # 拖动结果 vs 真相
    d_ba = dist(rgb_b, rgb_a)         # 拖动结果 vs 旧位置（应当明显不同）
    print()
    print("拖动结果 vs 该位置真相  差 = %.2f  (越小越对，阈值 8)" % d_bt)
    print("拖动结果 vs 上一位置画面 差 = %.2f  (越大越好，说明画面真的换了)" % d_ba)

    ok = True
    if d_bt > 8:
        print("FAIL: 拖动时的玻璃底跟当前位置的桌面不一致")
        ok = False
    if d_ba < 2:
        print("WARN: A/B 两处桌面颜色几乎一样，这一项参考价值有限")

    # --- GDI 对象泄漏检查：反复渲染 200 次，看句柄数是否稳定
    try:
        import ctypes.wintypes as wt
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        u32.GetGuiResources.argtypes = [wt.HANDLE, wt.DWORD]
        u32.GetGuiResources.restype = wt.DWORD
        hproc = k32.GetCurrentProcess()
        gdi_before = u32.GetGuiResources(hproc, 0)
        for i in range(200):
            app.drag_last_pos = None
            app._drag_frame(bx + (i % 7), by + (i % 5))
        gdi_after = u32.GetGuiResources(hproc, 0)
        print("\n200 帧反复渲染：GDI 句柄 %d → %d" % (gdi_before, gdi_after))
        if gdi_after - gdi_before > 20:
            print("FAIL: GDI 句柄在漏（每帧建了对象没释放）")
            ok = False
    except Exception as e:
        print("\n(句柄检查跳过: %r)" % (e,))

    app._end_drag_glass()
    user32.DestroyWindow(app.hwnd_main)
    print("\n结果：%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
