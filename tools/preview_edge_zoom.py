"""把圆角 45° 那一段放大 20 倍并排输出，用来判断 1px 棱边在弧线上的实际形态。

左＝现状，右＝试改（面积覆盖率 + 棱边半径 R-0.5）。
"""
import ctypes
import math
import sys

sys.path.insert(0, ".")

import snap
from snap import user32

OUT = sys.argv[1] if len(sys.argv) > 1 else "build_edge_zoom"
ZOOM = 20
# 45° 附近：圆心在 (R,R)，方向 (-0.707,-0.707)，取样区以 (R-14,R-14) 为起点
X0 = 2
Y0 = 2
CW = 13


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


def render(fix):
    app.mn_cov = None
    app.mn_cov_key = None
    app._corner_cov = (lambda RR: cov_area(RR)) if fix \
        else snap.App._corner_cov.__get__(app, snap.App)
    orig = snap.GFX.stroke_round

    def patched(self, argb, w0, x, y, ww, hh, r):
        if fix and argb == 0x59FFFFFF:
            r = r - 0.5
        return orig(self, argb, w0, x, y, ww, hh, r)

    snap.GFX.stroke_round = patched
    try:
        app.mn_glass = bytes([120, 120, 120, 255]) * (W * H)
        app._present_main()
        return ctypes.string_at(app.mn_bits, W * H * 4)
    finally:
        snap.GFX.stroke_round = orig


left = render(False)
right = render(True)

GAP = 6
cw = CW * ZOOM
ow = cw * 2 + GAP
oh = cw
out = bytearray(ow * oh * 4)
for half, buf in ((0, left), (1, right)):
    x0 = half * (cw + GAP)
    for y in range(oh):
        sy = Y0 + y // ZOOM
        for x in range(cw):
            sx = X0 + x // ZOOM
            si = (sy * W + sx) * 4
            di = (y * ow + x0 + x) * 4
            a = buf[si + 3]
            if a == 255:
                out[di:di + 3] = buf[si:si + 3]
            else:
                for k in range(3):
                    out[di + k] = (buf[si + k] * a + 20 * (255 - a)) // 255
            out[di + 3] = 255

for y in range(oh):
    for x in range(cw, cw + GAP):
        di = (y * ow + x) * 4
        out[di] = out[di + 1] = out[di + 2] = 255
        out[di + 3] = 255

hold = ctypes.create_string_buffer(bytes(out), len(out))
img = snap.buf_to_image(hold, ow, oh)
st = snap.gdip.GdipSaveImageToFile(img, OUT + ".png", ctypes.byref(snap.PNG_CLSID), None)
snap.gdip.GdipDisposeImage(img)
print("保存 %s.png %dx%d（左上角 45° 区域，放大 %d 倍）状态 %d" % (OUT, ow, oh, ZOOM, st))

user32.DestroyWindow(app.hwnd_main)
