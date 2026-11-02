# -*- coding: utf-8 -*-
"""玻璃底的四条边不许比中间暗 —— 模糊不能把「图外」当黑补。

为什么单独有这个测试：真实缺陷是「主面板静止时，边缘内侧有一圈阴影，鼠标
一拖动就没了」。根因不在透明（面板本体 alpha 全是 255），而在模糊的输入
范围：

  · `_blur_bgra` 是「缩到 1/BLUR_F 再放回来」。降采样时输入位图最外一圈会
    采到位图外面，那里没有像素，GDI+ 按黑色补 → 模糊结果的四条边正好暗一半
    （纯灰 128 进去，最外一行出来是 63），往里约 10px 才渐次恢复。
  · 静止那条路抓的范围正好等于面板大小，这条暗边就压在面板边上 → 那圈阴影。
  · 拖动那条路从一张更大的快照里裁，碰不到边界 → 一拖动就没了。

所以这里盯三件事：

  1. 外扩后再模糊，边缘不许比中心暗（留白真的把暗边推出去了）；
  2. 抓的范围确实比面板大，且对齐到 BLUR_F 的整数倍（相位一致的前提）；
  3. 裁切落点是对的 —— 偏移算错一拍，整块背景会平移，比暗边更明显。

用法：python tests/test_blur_edge.py
"""
import os
import sys
from ctypes import c_void_p

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import snap
from snap import user32

ok = True


def check(name, cond, extra=""):
    global ok
    print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))
    if not cond:
        ok = False


snap.SetProcessDpiAwarenessContext(c_void_p(-4))
app = snap.App()
app.create_main()
W, H = app.MW, app.MH
F = app.BLUR_F
LEFT, TOP = 400, 300

print("=" * 62)
print("玻璃底边界：边缘不许发暗")
print("=" * 62)
print("面板 %dx%d  模糊系数 %d" % (W, H, F))


def flat_block(gx, gy, gw, gh, probe):
    """整块恒灰：模糊之后除了边界效应以外必须还是同一个数"""
    b = bytearray(gw * gh * 4)
    for c in range(3):
        b[c::4] = bytes([128]) * (gw * gh)
    b[3::4] = b"\xff" * (gw * gh)
    return bytes(b)


def step_block(gx, gy, gw, gh, probe):
    """左半 30、右半 200，分界线在「面板左边往里 70px」处。

    分界线离面板左边缘要留够距离：两段式模糊（缩 1/13 再放回来）的过渡带
    约 ±13px，取样列必须落在过渡带外面，否则量到的是斜坡不是底色。
    偏移算错一格（13px）就会把 200 带进来，所以这个位置足够灵敏。"""
    cut = (LEFT - gx) + 70
    b = bytearray(gw * gh * 4)
    for y in range(gh):
        row = bytearray(gw * 4)
        for x in range(gw):
            v = 30 if x < cut else 200
            o = x * 4
            row[o] = row[o + 1] = row[o + 2] = v
            row[o + 3] = 255
        b[y * gw * 4:(y + 1) * gw * 4] = row
    return bytes(b)


def rowmean(buf, x0, x1, y):
    o = (y * W + x0) * 4
    t = 0
    for x in range(x0, x1):
        t += buf[o]
        o += 4
    return t / float(x1 - x0)


def colmean(buf, x, y0, y1):
    t = 0
    for y in range(y0, y1):
        t += buf[(y * W + x) * 4]
    return t / float(y1 - y0)


# ---------- 1. 几何：抓的范围必须比面板大，且是 BLUR_F 的整数倍
print("\n=== 1. 抓取范围 ===")
gx, gy, gw, gh = app._glass_rect(LEFT, TOP)
check("抓的范围比面板大（四周留出了白）", gw > W and gh > H,
      "%dx%d vs 面板 %dx%d" % (gw, gh, W, H))
check("宽高都是模糊系数的整数倍", gw % F == 0 and gh % F == 0,
      "%dx%d / %d" % (gw, gh, F))
check("起点也对齐到模糊系数", gx % F == 0 and gy % F == 0, "(%d,%d)" % (gx, gy))
check("面板完整落在抓取范围内",
      gx <= LEFT and gy <= TOP and gx + gw >= LEFT + W and gy + gh >= TOP + H,
      "块 (%d,%d,%d,%d)" % (gx, gy, gw, gh))
check("面板两侧至少各留够一个模糊格",
      LEFT - gx >= F and TOP - gy >= F, "ox=%d oy=%d" % (LEFT - gx, TOP - gy))

# ---------- 2. 边缘不许比中心暗
print("\n=== 2. 纯灰图：四条边 vs 中心 ===")
out = app._blur_behind(LEFT, TOP, grab=flat_block)
check("产出尺寸等于面板", out is not None and len(out) == W * H * 4,
      "%s" % (len(out) if out else None,))

_center = rowmean(out, W // 2, W // 2 + 10, H // 2)
_edges = {
    "顶": rowmean(out, W // 4, W // 4 * 3, 0),
    "底": rowmean(out, W // 4, W // 4 * 3, H - 1),
    "左": colmean(out, 0, H // 3, H // 3 * 2),
    "右": colmean(out, W - 1, H // 3, H // 3 * 2),
}
for _k, _v in _edges.items():
    print("  %s边 %6.1f   中心 %6.1f   中心-边 %+6.1f" % (_k, _v, _center, _center - _v))
_worst = max(_center - v for v in _edges.values())
check("四条边都不比中心暗（中心-边 ≤ 2）", _worst <= 2, "最差 %+.1f" % _worst)

# ---------- 3. 反证：直接模糊面板大小的输入，边缘会暗掉一半
print("\n=== 3. 反证：不留白的话有多糟 ===")
_naive = app._blur_bgra(flat_block(LEFT, TOP, W, H, None), W, H)
_n_center = rowmean(_naive, W // 2, W // 2 + 10, H // 2)
_n_edge = rowmean(_naive, W // 4, W // 4 * 3, 0)
_gap_new = _center - _edges["顶"]
_gap_old = _n_center - _n_edge
print("  留白后 中心-边 = %+.1f      不留白 中心-边 = %+.1f" % (_gap_new, _gap_old))
check("留白把这条暗边压掉了（新差值 < 旧差值的 1/5）",
      _gap_new < _gap_old / 5.0, "旧 %+.1f → 新 %+.1f" % (_gap_old, _gap_new))

# ---------- 4. 裁切落点：偏移算错就会整块平移
print("\n=== 4. 裁切落点对不对 ===")
out2 = app._blur_behind(LEFT, TOP, grab=step_block)
_near = rowmean(out2, 2, 20, H // 2)          # 分界线左边 70px，这里一定是暗的
_far = rowmean(out2, W - 30, W - 10, H // 2)  # 右边早过了分界线，一定是亮的
check("面板左侧仍是分界线左边的内容（≈30）", abs(_near - 30) <= 4, "实际 %.1f" % _near)
check("面板右侧是分界线右边的内容（≈200）", abs(_far - 200) <= 6, "实际 %.1f" % _far)

# ---------- 5. 贴屏幕边也不能崩：留白被钳掉时仍然盖住面板
print("\n=== 5. 贴边 / 极端位置 ===")
_vx = user32.GetSystemMetrics(snap.SM_XVIRTUALSCREEN)
_vy = user32.GetSystemMetrics(snap.SM_YVIRTUALSCREEN)
for _name, _l, _t in (("左上角", _vx, _vy),
                      ("右上角", _vx + user32.GetSystemMetrics(snap.SM_CXVIRTUALSCREEN) - W, _vy),
                      ("负坐标（多屏左侧）", _vx, _vy)):
    _a, _b, _c, _d = app._glass_rect(_l, _t)
    _cover = (_a <= _l and _b <= _t and _a + _c >= _l + W and _b + _d >= _t + H)
    check("%s 的取景仍完整盖住面板" % _name, _cover,
          "块 (%d,%d,%d,%d)" % (_a, _b, _c, _d))

_out3 = app._blur_behind(_vx, _vy, grab=flat_block)
check("贴屏幕左上角也能算出完整一帧", _out3 is not None and len(_out3) == W * H * 4)

user32.DestroyWindow(app.hwnd_main)
print("\n" + "=" * 62)
print("结果：%s" % ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
