"""验证：模糊的条纹相位是不是只由「整块左上角」决定，与尺寸无关。

背景：拖动时玻璃底是「整块桌面模糊一次，再按位置裁」。要让补抓能只抓一小块
（快），就必须保证「小块模糊」和「大块模糊」在重叠区逐像素一致。

理论：`_blur_bgra` 把 W 缩到 W//13，实际比例是 W/(W//13)，**只有 W 是 13 的
整数倍时才精确等于 13**。比例不等于 13 时，网格间隔跟着变，相位就错开。

用法：python -u tools/verify_blur_tile.py
"""
import sys
import os
import ctypes
from ctypes import byref

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import snap
from snap import user32, grab_screen

tok = snap.P()
si = snap.GdiplusStartupInput(1, None, False, False)
snap.gdip.GdiplusStartup(byref(tok), byref(si), None)


class Fake:
    BLUR_F = snap.App.BLUR_F
    _sdc = None

    def _get_screen_dc(self):
        if Fake._sdc is None:
            Fake._sdc = user32.GetDC(None)
        return Fake._sdc


fake = Fake()
F = snap.App.BLUR_F

# 抓一大块有纹理的桌面（避开窗口区），左上角先钳到 F 的整数倍
gx, gy = (300 // F) * F, (200 // F) * F
BW = (800 // F) * F
BH = (600 // F) * F
big = bytes(grab_screen(gx, gy, BW, BH))
print("大块 (%d,%d) %dx%d  都是 %d 的倍数" % (gx, gy, BW, BH, F))


def crop(d, sw, x, y, w, h):
    out = bytearray(w * h * 4)
    for r in range(h):
        so = ((y + r) * sw + x) * 4
        out[r * w * 4:(r + 1) * w * 4] = d[so:so + w * 4]
    return bytes(out)


def diff_mid(a, b, w, h, margin):
    """只比中间部分（避开缩放核在边缘的外推误差）"""
    tot = n = 0
    for y in range(margin, h - margin):
        base = y * w
        for x in range(margin, w - margin):
            o = (base + x) * 4
            tot += abs(a[o] - b[o]) + abs(a[o + 1] - b[o + 1]) + abs(a[o + 2] - b[o + 2])
            n += 3
    return tot / float(n) if n else -1


blur_big = snap.App._blur_bgra(fake, big, BW, BH)

print("\n【A】子块尺寸取 %d 的整数倍，偏移也取 %d 的倍数" % (F, F))
worst = 0
for off, (cw, ch) in ((0, (13 * 39, 13 * 20)), (13 * 3, (13 * 30, 13 * 15)),
                      (13 * 7, (13 * 41, 13 * 24)), (13, (13 * 44, 13 * 30))):
    sub = crop(big, BW, off, off, cw, ch)
    bs = snap.App._blur_bgra(fake, sub, cw, ch)
    ref = crop(blur_big, BW, off, off, cw, ch)
    d = diff_mid(bs, ref, cw, ch, F * 2)
    worst = max(worst, d)
    print("  偏移 %+4d 尺寸 %3dx%-3d  与「大块模糊后裁」的差 = %.3f" % (off, cw, ch, d))
print("  最差 %.3f （理想 0，判据 < 0.5）" % worst)

print("\n【B】子块尺寸不是 %d 的倍数（当前实现的实际情况）" % F)
for off, (cw, ch) in ((0, (340, 148)), (0, (404, 212)), (13, (532, 340))):
    sub = crop(big, BW, off, off, cw, ch)
    bs = snap.App._blur_bgra(fake, sub, cw, ch)
    ref = crop(blur_big, BW, off, off, cw, ch)
    d = diff_mid(bs, ref, cw, ch, F * 2)
    print("  偏移 %+4d 尺寸 %3dx%-3d  差 = %.3f   （比例 %d/%d=%.3f）" % (
        off, cw, ch, d, cw, cw // F, cw / float(cw // F)))

print("\n结论：如果【A】全 < 0.5 而【B】明显更大，说明「尺寸取 %d 的倍数」就能让" % F)
print("      补抓只抓一小块，与大块结果一致。")
