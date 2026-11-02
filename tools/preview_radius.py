"""四个圆角半径并排（上下）对比，1:1 原样呈现，用于挑尺寸。

半径直接改 app.RADIUS 再重绘：fill_round / stroke_round / 圆角遮罩三处都读
这个值，所以一改就是完整的一套。
"""
import ctypes
import sys

sys.path.insert(0, ".")

import snap
from snap import user32

OUT = sys.argv[1] if len(sys.argv) > 1 else "build_radius"
RADII = [20, 17, 15, 13]
MARGIN = 18
GAP = 10

app = snap.App()
app.create_main()
W, H = app.MW, app.MH

raw = snap.grab_screen(900, 620, W, H).raw
GLASS = app._blur_bgra(raw, W, H)

cw = W + MARGIN * 2
ch = (H + GAP) * len(RADII) - GAP + MARGIN * 2
out = bytearray(cw * ch * 4)
for i in range(cw * ch):
    o = i * 4
    out[o] = 44
    out[o + 1] = 48
    out[o + 2] = 56
    out[o + 3] = 255

for idx, rad in enumerate(RADII):
    app.RADIUS = float(rad)
    app.mn_cov = None
    app.mn_cov_key = None
    app.mn_glass = GLASS
    app._present_main()
    buf = ctypes.string_at(app.mn_bits, W * H * 4)
    y0 = MARGIN + idx * (H + GAP)
    for y in range(H):
        for x in range(W):
            si = (y * W + x) * 4
            di = ((y0 + y) * cw + MARGIN + x) * 4
            a = buf[si + 3]
            if a == 255:
                out[di:di + 3] = buf[si:si + 3]
            else:
                for k in range(3):
                    out[di + k] = (buf[si + k] * a + out[di + k] * (255 - a)) // 255
            out[di + 3] = 255

hold = ctypes.create_string_buffer(bytes(out), len(out))
img = snap.buf_to_image(hold, cw, ch)
st = snap.gdip.GdipSaveImageToFile(img, OUT + ".png", ctypes.byref(snap.PNG_CLSID), None)
snap.gdip.GdipDisposeImage(img)
print("保存 %s.png %dx%d  自上而下 R=%s  状态 %d"
      % (OUT, cw, ch, "/".join(str(r) for r in RADII), st))

user32.DestroyWindow(app.hwnd_main)
