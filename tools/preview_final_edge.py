"""最终前后对比：左＝旧（R=20 + 3×3 覆盖率），右＝新（R=15 + 面积积分）。

两边的底都是同一块纯色，所以除了棱边和轮廓没有任何变量。
"""
import ctypes
import sys

sys.path.insert(0, ".")

import snap
from snap import user32

OUT = sys.argv[1] if len(sys.argv) > 1 else "build_final_edge"
ZOOM = 12
CROP = 30


def cov_old(R):
    out = []
    for yy in range(R):
        row = []
        for xx in range(R):
            hit = 0
            for sy in range(3):
                for sx in range(3):
                    dx = R - (xx + (sx + 0.5) / 3.0)
                    dy = R - (yy + (sy + 0.5) / 3.0)
                    if dx * dx + dy * dy <= R * R:
                        hit += 1
            row.append(int(round(hit * 255.0 / 9.0)))
        out.append(row)
    return out


app = snap.App()
app.create_main()
W, H = app.MW, app.MH
NEW_COV = snap.App._corner_cov


def render(radius, cov_fn):
    app.RADIUS = float(radius)
    app.mn_cov = None
    app.mn_cov_key = None
    app._corner_cov = cov_fn
    app.mn_glass = bytes([120, 120, 120, 255]) * (W * H)
    app._present_main()
    return ctypes.string_at(app.mn_bits, W * H * 4)


left = render(20, cov_old)
right = render(15, NEW_COV.__get__(app, snap.App))

GAP = 6
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
print("保存 %s.png %dx%d（左=旧 R20+3x3，右=新 R15+面积积分）状态 %d" % (OUT, ow, oh, st))

user32.DestroyWindow(app.hwnd_main)
