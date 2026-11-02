"""把主面板左上圆角渲染出来并放大，用于目视对比抗锯齿效果（只读）。

背景用一块固定的浅灰蓝渐变 —— 比面板亮，边缘的台阶最容易被看出来。
同一块背景跑「改前 / 改后」，两张图可以直接对着看。

用法：python -u tools/preview_corner.py 输出名
"""
import ctypes
import sys

sys.path.insert(0, ".")

import snap
from snap import user32

OUT = sys.argv[1] if len(sys.argv) > 1 else "build_edge"
ZOOM = 5
CROP = 100

# 传 fix 时套用两处候选改动（覆盖率面积积分 + 棱边半径 R-0.5），用于前后对比
FIX = "fix" in sys.argv


def cov_area(R):
    import math
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

# 合成背景：一条固定的斜向渐变（不依赖壁纸，改前改后完全同条件）
buf = bytearray(W * H * 4)
for y in range(H):
    for x in range(W):
        o = (y * W + x) * 4
        v = 118 + (x * 46) // W + (y * 30) // H
        buf[o] = v - 12        # B
        buf[o + 1] = v          # G
        buf[o + 2] = v + 10      # R
        buf[o + 3] = 255
app.mn_glass = bytes(buf)
app._present_main()

if FIX:
    app.mn_cov = None
    app.mn_cov_key = None
    app._corner_cov = lambda RR: cov_area(RR)
    _orig = snap.GFX.stroke_round

    def _patched(self, argb, width, x, y, ww, hh, r):
        if argb == 0x59FFFFFF and width == 1:
            r = r - 0.5
        return _orig(self, argb, width, x, y, ww, hh, r)

    snap.GFX.stroke_round = _patched
    app._present_main()
    snap.GFX.stroke_round = _orig

src = ctypes.string_at(app.mn_bits, W * H * 4)

# 放大左上角
cw = ch = CROP
out = bytearray(cw * ZOOM * ch * ZOOM * 4)
ow = cw * ZOOM
for y in range(ch * ZOOM):
    sy = y // ZOOM
    for x in range(ow):
        sx = x // ZOOM
        si = (sy * W + sx) * 4
        di = (y * ow + x) * 4
        out[di:di + 4] = src[si:si + 4]

# 透明处铺成棋盘格，便于判断 alpha 台阶（图上呈灰白相间）
# 第二个参数传 "check" 才铺，默认按真实合成结果输出
if "check" in sys.argv:
    for y in range(ch * ZOOM):
        for x in range(ow):
            o = (y * ow + x) * 4
            a = out[o + 3]
            if a < 255:
                cb = 200 if ((x // 8 + y // 8) & 1) else 120
                for k in range(3):
                    out[o + k] = (out[o + k] * a + cb * (255 - a)) // 255
                out[o + 3] = 255
else:
    # 按 ULW 的合成方式铺到背景色上（外面看不到 alpha，只剩亮度台阶）
    for y in range(ch * ZOOM):
        for x in range(ow):
            o = (y * ow + x) * 4
            a = out[o + 3]
            if a < 255:
                for k in range(3):
                    out[o + k] = (out[o + k] * a + 96 * (255 - a)) // 255
                out[o + 3] = 255

# buf_to_image 不拷贝像素，只接管指针 —— 缓冲必须一直在手，否则存盘时读到已释放内存
hold = ctypes.create_string_buffer(bytes(out), len(out))
img = snap.buf_to_image(hold, ow, ch * ZOOM)
st = snap.gdip.GdipSaveImageToFile(img, OUT + ".png", ctypes.byref(snap.PNG_CLSID), None)
snap.gdip.GdipDisposeImage(img)
print("保存 %s.png  %dx%d  状态 %d  （面板 %dx%d R=%d）"
      % (OUT, ow, ch * ZOOM, st, W, H, int(round(app.RADIUS))))

user32.DestroyWindow(app.hwnd_main)
