# -*- coding: utf-8 -*-
"""开始截图不再闪一下 + 马赛克取色与呈现同源。

两个真实缺陷：

1. **点「开始截图」面板闪一下**。旧流程是「先提交一帧全透明 → 等 DWM 合成
   → 抓屏 → 建缓冲 → 显示遮罩」。本机实测：0.1ms 面板就没内容了，遮罩直到
   183ms 才出现 —— 中间一百多毫秒屏幕上根本没有面板，看着就是「消失又回来」。
   现在改成抓屏那一下让 DWM 把面板当不存在（WDA_EXCLUDEFROMCAPTURE），
   面板一帧都不动。老系统没有这个属性，才退回透明帧。

2. **马赛克能「透过面板」打到背后的桌面**。旧代码抓两张图：`shot_raw` 给
   取色（抓的时候面板是透明的，里面没有面板），`dc_shot` 给呈现（把面板像素
   补了回去）。两张的差别正好是面板那一块，于是马赛克落在面板上时取到的是
   面板背后的壁纸 —— 方块跨过面板边缘颜色毫无变化。现在只抓一次屏，取色源
   从呈现图回读，天然同源。

用法：python tests/test_capture_flow.py
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from ctypes import byref, c_void_p
from ctypes import wintypes as w

import snap
from snap import user32

ok = True


def check(name, cond, extra=""):
    global ok
    if cond:
        print("[PASS] %s %s" % (name, extra))
    else:
        ok = False
        print("[FAIL] %s %s" % (name, extra))


try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

print("=" * 62)
app = snap.App()
app.create_main()
time.sleep(1.2)

W, H = app.MW, app.MH
rc = w.RECT()
user32.GetWindowRect(app.hwnd_main, byref(rc))

# ---------------------------------------------------------------- 1. 不再透明化
print("\n-- 1. 开始截图时面板不许再被藏起来")
before = ctypes.string_at(app.mn_bits, W * H * 4)
app.start_capture()
after = ctypes.string_at(app.mn_bits, W * H * 4)
check("面板像素一帧都没动（没有提交透明帧）", before == after)
check("main_blanked 为假", app.main_blanked is False, "实际 %r" % (app.main_blanked,))
check("抓屏排除归属给了 capture（抓完仍开着）", app._excl_owner == "capture",
      "实际 %r" % (app._excl_owner,))

# ---------------------------------------------------------------- 2. 取色同源
print("\n-- 2. 马赛克取色与呈现图同源（不再透到面板背后）")
vsx, vsy, vsw, vsh = app.vs
ox, oy = rc.left - vsx, rc.top - vsy

shot_px = app._shot_pixels(vsw, vsh)
check("呈现图能回读", shot_px is not None)
raw = app.shot_raw
check("取色源存在", raw is not None)

if shot_px is not None and raw is not None:
    shot_px = ctypes.string_at(shot_px, vsw * vsh * 4)
    raw = ctypes.string_at(raw, vsw * vsh * 4)

    # 面板左上角一块纯底色（避开标题、按钮、快捷键行）
    bx, by = ox + app.px(18), oy + app.px(20)
    bw, bh = app.px(24), app.px(10)
    d = 0
    for y in range(by, by + bh):
        for x in range(bx, bx + bw):
            o = (y * vsw + x) * 4
            d += abs(shot_px[o] - raw[o]) + abs(shot_px[o + 1] - raw[o + 1]) \
                + abs(shot_px[o + 2] - raw[o + 2])
    check("面板区域：取色源与呈现图逐像素一致", d == 0, "累计差 %d" % d)

    # 取色结果要和「面板自己的像素」一致，而不是和面板背后的壁纸
    blk = max(4, app._mosaic_blk())
    gx, gy = bx // blk, by // blk
    argb = app._mosaic_color(gx, gy, blk)
    check("马赛克块取到了颜色", argb is not None)
    if argb is not None:
        mr = (argb >> 16) & 0xFF
        mg = (argb >> 8) & 0xFF
        mb = argb & 0xFF
        # 块的真实覆盖范围（实现按网格取整，不一定从 bx/by 起）
        cx0, cy0 = gx * blk, gy * blk
        cx1, cy1 = min(cx0 + blk, vsw), min(cy0 + blk, vsh)
        sr = sg = sb = n = 0
        for y in range(max(cy0, oy), min(cy1, oy + H)):
            for x in range(max(cx0, ox), min(cx1, ox + W)):
                o = (y * vsw + x) * 4
                sb += raw[o]
                sg += raw[o + 1]
                sr += raw[o + 2]
                n += 1
        check("马赛克颜色 = 面板自身像素的块平均",
              n > 0 and max(abs(mr - sr / n), abs(mg - sg / n), abs(mb - sb / n)) <= 3,
              "块=(%d,%d,%d) 面板块平均=(%.0f,%.0f,%.0f)"
              % (mr, mg, mb, sr / n, sg / n, sb / n))

        # 与面板外的壁纸比：两者必须明显不同，否则这条测试证明不了什么
        py = oy - app.px(30)
        if py > 4:
            br = bg = bb = bn = 0
            for y in range(py, py + app.px(8)):
                for x in range(cx0, min(cx1, ox + W)):
                    o = (y * vsw + x) * 4
                    bb += raw[o]
                    bg += raw[o + 1]
                    br += raw[o + 2]
                    bn += 1
            if bn:
                gap = max(abs(mr - br / bn), abs(mg - bg / bn), abs(mb - bb / bn))
                check("取到的色与面板背后的壁纸明显不同", gap >= 12,
                      "差 %d（小于这个数说明可能又取到壁纸了）" % gap)

# ---------------------------------------------------------------- 3. 收工归还
print("\n-- 3. 取消截图后要还回去")
app._cancel_capture()
time.sleep(0.3)
check("排除属性已归还", app._excl_owner is None, "实际 %r" % (app._excl_owner,))
check("面板仍然可见", bool(user32.IsWindowVisible(app.hwnd_main)))
check("面板内容正常（非全零）", ctypes.string_at(app.mn_bits, 4 * 64) != bytes(64 * 4))

# ---------------------------------------------------------------- 4. 置顶
print("\n-- 4. 置顶时截图要临时摘下来，收工戴回去")
app._toggle_topmost()
check("先置顶成功", app.topmost and app._is_topmost())
app.start_capture()
check("截图期间置顶已摘掉", app.topmost_was is True and not app._is_topmost())
app._cancel_capture()
time.sleep(0.3)
check("取消后置顶恢复", app.topmost and app._is_topmost())
app._toggle_topmost()
check("收回到不置顶", not app.topmost and not app._is_topmost())

# ---------------------------------------------------------------- 5. 老系统退路
print("\n-- 5. 老系统（没有抓屏排除）仍走透明帧，且能画回来")
_real_acquire = app._excl_acquire
app._excl_acquire = lambda owner: False
try:
    app.start_capture()
    check("没有排除属性时退回透明帧", app.main_blanked is True)
    blank = ctypes.string_at(app.mn_bits, W * H * 4)
    check("透明帧确实是全零", blank == bytes(W * H * 4))
    app._cancel_capture()
    time.sleep(0.3)
    check("取消后面板内容画回来了",
          ctypes.string_at(app.mn_bits, W * H * 4) != bytes(W * H * 4))
finally:
    app._excl_acquire = _real_acquire

user32.DestroyWindow(app.hwnd_main)
print("\n" + "=" * 62)
print("结果：%s" % ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
