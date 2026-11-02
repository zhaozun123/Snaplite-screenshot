"""三个候选方案并排放大，用于目视挑选。只读，不改 snap.py。

左：现状（3×3 覆盖率 + 棱边半径 R）
中：面积积分覆盖率 + 棱边半径 R-0.5
右：面积积分覆盖率 + 棱边半径 R-1.0

用法：python -u tools/preview_edge3.py 输出名 [zoom] [real]
"""
import ctypes
import math
import sys

sys.path.insert(0, ".")

import snap
from snap import user32

OUT = sys.argv[1] if len(sys.argv) > 1 else "build_edge3"
ZOOM = int(sys.argv[2]) if len(sys.argv) > 2 else 12
CROP = 26
REAL = "real" in sys.argv


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

if REAL:
    raw = snap.grab_screen(900, 620, W, H).raw
    app.mn_glass = app._blur_bgra(raw, W, H)
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


def render(analytic, rim_shift, width=1.0, argb=0x59FFFFFF):
    app.mn_cov = None
    app.mn_cov_key = None
    app._corner_cov = (lambda RR: cov_area(RR)) if analytic \
        else snap.App._corner_cov.__get__(app, snap.App)
    orig = snap.GFX.stroke_round

    def patched(self, a0, w0, x, y, ww, hh, r):
        if a0 == 0x59FFFFFF:
            return orig(self, argb, width, x, y, ww, hh, r - rim_shift)
        return orig(self, a0, w0, x, y, ww, hh, r)

    snap.GFX.stroke_round = patched
    try:
        app._present_main()
        return ctypes.string_at(app.mn_bits, W * H * 4)
    finally:
        snap.GFX.stroke_round = orig


variants = [(False, 0.0, 1.0, 0x59FFFFFF),
            (True, 0.0, 1.0, 0x59FFFFFF),
            (True, 1.0, 2.0, 0x38FFFFFF)]
imgs = [render(a, r, wd, c) for a, r, wd, c in variants]

GAP = 4
cw = CROP * ZOOM
ow = cw * len(imgs) + GAP * (len(imgs) - 1)
oh = cw
out = bytearray(ow * oh * 4)

for idx, buf in enumerate(imgs):
    x0 = idx * (cw + GAP)
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

for idx in range(1, len(imgs)):
    x0 = idx * (cw + GAP) - GAP
    for y in range(oh):
        for x in range(x0, x0 + GAP):
            di = (y * ow + x) * 4
            out[di] = out[di + 1] = out[di + 2] = 255
            out[di + 3] = 255

hold = ctypes.create_string_buffer(bytes(out), len(out))
img = snap.buf_to_image(hold, ow, oh)
st = snap.gdip.GdipSaveImageToFile(img, OUT + ".png", ctypes.byref(snap.PNG_CLSID), None)
snap.gdip.GdipDisposeImage(img)
print("保存 %s.png %dx%d 放大 %d 倍（左=现状 中=精确覆盖率 右=精确覆盖率+2px柔边）状态 %d"
      % (OUT, ow, oh, ZOOM, st))

user32.DestroyWindow(app.hwnd_main)
