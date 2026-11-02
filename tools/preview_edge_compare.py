"""把左上角圆角渲染两遍（现状 / 试改）并排放大，存成一张对比图。只读，不改 snap.py。

用法：python -u tools/preview_edge_compare.py 输出名 [zoom]
左边＝现状，右边＝试改（覆盖率面积积分 + 棱边半径 R-0.5），中间留 4px 分隔。
"""
import ctypes
import math
import sys

sys.path.insert(0, ".")

import snap
from snap import user32

OUT = sys.argv[1] if len(sys.argv) > 1 else "build_edge_compare"
ZOOM = int(sys.argv[2]) if len(sys.argv) > 2 else 10
CROP = 30


def cov_area(R):
    out = []
    for yy in range(R):
        row = []
        for xx in range(R):
            tot = 0.0
            for s in range(16):
                y = yy + (s + 0.5) / 16.0
                dy = R - y
                w = 0.0 if abs(dy) >= R else math.sqrt(R * R - dy * dy)
                lo = max(xx, R - w)
                hi = min(xx + 1, R + w)
                if hi > lo:
                    tot += (hi - lo) / 16.0
            row.append(int(round(max(0.0, min(1.0, tot)) * 255.0)))
        out.append(row)
    return out


app = snap.App()
app.create_main()
W, H = app.MW, app.MH

# 固定背景：比面板亮，边缘对比明显
# 传 real 时改抓真实壁纸（取一块离窗口远的区域，避免抓到面板自己）
REAL = "real" in sys.argv
if REAL:
    raw = snap.grab_screen(900, 620, W, H).raw
    app.mn_glass = app._blur_bgra(raw, W, H)
    print("使用真实壁纸作底")
else:
    bg = bytearray(W * H * 4)
    for y in range(H):
        for x in range(W):
            o = (y * W + x) * 4
            v = 118 + (x * 46) // W + (y * 30) // H
            bg[o] = v - 12
            bg[o + 1] = v
            bg[o + 2] = v + 10
            bg[o + 3] = 255
    app.mn_glass = bytes(bg)


def render(fix):
    app.mn_cov = None
    app.mn_cov_key = None
    app._corner_cov = (lambda RR: cov_area(RR)) if fix \
        else snap.App._corner_cov.__get__(app, snap.App)
    orig = snap.GFX.stroke_round

    def patched(self, argb, width, x, y, ww, hh, r):
        if fix and argb == 0x59FFFFFF and width == 1:
            r = r - 0.5
        return orig(self, argb, width, x, y, ww, hh, r)

    snap.GFX.stroke_round = patched
    try:
        app._present_main()
        return ctypes.string_at(app.mn_bits, W * H * 4)
    finally:
        snap.GFX.stroke_round = orig


left = render(False)
right = render(True)

GAP = 4
cw = CROP * ZOOM
ow = cw * 2 + GAP
oh = cw
out = bytearray(ow * oh * 4)


for half, buf in ((0, left), (1, right)):
    x0 = half * (cw + GAP)
    for y in range(oh):
        sy = y // ZOOM
        for x in range(cw):
            sx = x // ZOOM
            si = (sy * W + sx) * 4
            di = (y * ow + x0 + x) * 4
            a = buf[si + 3]
            if a == 255:
                out[di:di + 3] = buf[si:si + 3]
            else:
                for k in range(3):
                    out[di + k] = (buf[si + k] * a + 96 * (255 - a)) // 255
            out[di + 3] = 255
# 中间分隔线
for y in range(oh):
    for x in range(cw, cw + GAP):
        di = (y * ow + x) * 4
        out[di] = out[di + 1] = out[di + 2] = 255
        out[di + 3] = 255

hold = ctypes.create_string_buffer(bytes(out), len(out))
img = snap.buf_to_image(hold, ow, oh)
st = snap.gdip.GdipSaveImageToFile(img, OUT + ".png", ctypes.byref(snap.PNG_CLSID), None)
snap.gdip.GdipDisposeImage(img)
print("保存 %s.png  %dx%d（放大 %d 倍，左现状 / 右改后）状态 %d" % (OUT, ow, oh, ZOOM, st))

user32.DestroyWindow(app.hwnd_main)
