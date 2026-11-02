"""检查拖动余量的形状：面积是否守住、运动方向是否真的变长、有没有盖不住窗口。

    python -u tools/verify_pad_shape.py

只读：不抓屏、不拖动，只调 _drag_pads / _drag_world_rect 看它们算出来的矩形。
这个脚本守的是三件事，改 _drag_pads 之前先看一遍：

1. 宽高和左上角必须是 BLUR_F 的整数倍 —— 否则模糊条纹相位会随位置漂，
   拖动就是一层连成片的水波（见 ARCHITECTURE 坑 17）。
2. 面积必须守住 DRAG_AREA 附近 —— 面积涨了「抓 + 模糊」就变贵，主线程会被
   拖慢，补抓是少了但整体更钝（坑 20）。
3. 矩形必须盖得住窗口 —— 盖不住就得当场补抓，那是 15~45ms 的停顿。
"""
import sys

sys.path.insert(0, ".")
import snap


def main():
    app = snap.App()
    app.create_main()
    if not app.hwnd_main:
        print("FAIL: 没有窗口")
        return 1
    W, H = app.MW, app.MH
    f = app.BLUR_F
    print("面板 %dx%d  BLUR_F=%d  目标面积 %d  DRAG_LEAN_MAX=%.2f"
          % (W, H, f, snap.DRAG_AREA, snap.DRAG_LEAN_MAX))
    print()
    print(" 位移(px)   快照尺寸    面积    偏差   前/后   侧向")
    worst = 0.0
    bad = []
    for dx, dy in [(0, 0), (5, 0), (40, 0), (-40, 0), (80, 0), (-80, 0),
                   (20, 10), (0, 5), (0, 40), (0, -40), (0, 80), (30, -60)]:
        padx, pady, leadx, leady = app._drag_pads(dx, dy)
        gx, gy, gw, gh = app._drag_world_rect(
            400, 400, padx, pady, leadx, leady, dx < 0, dy < 0)
        ox, oy = 400 - gx, 400 - gy
        left, right = ox, gw - (ox + W)
        top, bottom = oy, gh - (oy + H)
        if abs(dx) >= abs(dy) and dx != 0:
            front, rear = (left, right) if dx < 0 else (right, left)
            side = min(top, bottom)
        elif dy != 0:
            front, rear = (top, bottom) if dy < 0 else (bottom, top)
            side = min(left, right)
        else:
            front = rear = min(left, right, top, bottom)
            side = front
        area = gw * gh
        dev = (area - snap.DRAG_AREA) / float(snap.DRAG_AREA)
        worst = max(worst, abs(dev))
        print(" %4d,%4d  %4dx%-4d %7d  %+5.1f%%  %4d/%-4d %3d"
              % (dx, dy, gw, gh, area, dev * 100, front, rear, side))
        if gw % f or gh % f or gx % f or gy % f:
            bad.append("(%d,%d) 不是 13 的倍数" % (dx, dy))
        if ox < 0 or oy < 0 or ox + W > gw or oy + H > gh:
            bad.append("(%d,%d) 盖不住窗口" % (dx, dy))
        if side < 8:
            bad.append("(%d,%d) 侧向余量只有 %d" % (dx, dy, side))
        # 运动方向的前向余量必须明显大于后方，否则「前重后轻」没生效
        if abs(dx) + abs(dy) >= snap.DRAG_LEAN_FULL and front <= rear:
            bad.append("(%d,%d) 全速时前向没拉开：%d/%d" % (dx, dy, front, rear))

    user32 = snap.user32
    user32.DestroyWindow(app.hwnd_main)
    print()
    print("面积偏差最大 %.1f%%" % (worst * 100))
    if bad:
        for b in bad:
            print("  [FAIL] " + b)
        return 1
    print("全部通过：13 倍数对齐、面积守住、盖得住窗口、全速时前向拉开")
    return 0


if __name__ == "__main__":
    sys.exit(main())
