# -*- coding: utf-8 -*-
"""生成 app.ico（全部尺寸都用带 AND 掩码的 BMP 条目）

为什么不用 PNG 条目：
- PNG 压缩的 ICO 条目只有 256×256 是官方支持的，但部分 shell 渲染路径
  （资源管理器的 256 大图标/Tiles 视图）会忽略 PNG 的 alpha 通道，
  把透明区域画成纯黑 —— 表现为图标外面套一个黑方块。
- BMP(DIB) 条目带 1bpp AND 掩码，是所有 Windows 版本都认的格式，
  掩码 + alpha 双保险，任何渲染路径都不会出现黑底。
"""
import ctypes
import os
import struct
from ctypes import byref

import snap

gdip = snap.gdip
P = snap.P
PF_ARGB = 0x0026200A   # PixelFormat32bppARGB

snap._proto(gdip.GdipCreateBitmapFromScan0, [ctypes.c_int] * 4 + [ctypes.c_void_p, ctypes.POINTER(P)])
snap._proto(gdip.GdipGetImageGraphicsContext, [P, ctypes.POINTER(P)])
snap._proto(gdip.GdipDisposeImage, [P])
snap._proto(gdip.GdipDeleteGraphics, [P])
snap._proto(gdip.GdipSetSmoothingMode, [P, ctypes.c_int])
snap._proto(gdip.GdipSetPixelOffsetMode, [P, ctypes.c_int])
snap._proto(gdip.GdipCreateSolidFill, [ctypes.c_uint, ctypes.POINTER(P)])
snap._proto(gdip.GdipFillRectangle, [P, P] + [ctypes.c_float] * 4)
snap._proto(gdip.GdipDeleteBrush, [P])
snap._proto(gdip.GdipCreatePath, [ctypes.c_int, ctypes.POINTER(P)])
snap._proto(gdip.GdipAddPathArc, [P] + [ctypes.c_float] * 6)
snap._proto(gdip.GdipClosePathFigure, [P])
snap._proto(gdip.GdipFillPath, [P, P, P])
snap._proto(gdip.GdipDeletePath, [P])
snap._proto(gdip.GdipCreatePen1, [ctypes.c_uint, ctypes.c_float, ctypes.c_int, ctypes.POINTER(P)])
snap._proto(gdip.GdipDrawLine, [P, P] + [ctypes.c_float] * 4)
snap._proto(gdip.GdipDeletePen, [P])

BLUE = 0xFF3B82F6
WHITE = 0xFFFFFFFF


def render(size):
    """渲染为 32bppARGB 位图，返回 top-down 的 BGRA 字节"""
    buf = ctypes.create_string_buffer(size * size * 4)   # 全 0 → 全透明
    img = P()
    st = gdip.GdipCreateBitmapFromScan0(size, size, size * 4, PF_ARGB, buf, byref(img))
    if st != 0 or not img:
        raise RuntimeError("CreateBitmapFromScan0 failed: %s" % st)

    g = P()
    gdip.GdipGetImageGraphicsContext(img, byref(g))
    gdip.GdipSetSmoothingMode(g, snap.SMOOTH_AA)
    gdip.GdipSetPixelOffsetMode(g, snap.PIXEL_HALF)

    k = size / 100.0
    m = 2.0 * k                 # 外边距
    side = size - 2 * m
    r = side * 0.235            # 圆角半径

    # 蓝色圆角底
    path = P()
    gdip.GdipCreatePath(0, byref(path))
    d = r * 2
    x0, y0 = m, m
    x1, y1 = m + side, m + side
    gdip.GdipAddPathArc(path, x0, y0, d, d, 180, 90)
    gdip.GdipAddPathArc(path, x1 - d, y0, d, d, 270, 90)
    gdip.GdipAddPathArc(path, x1 - d, y1 - d, d, d, 0, 90)
    gdip.GdipAddPathArc(path, x0, y1 - d, d, d, 90, 90)
    gdip.GdipClosePathFigure(path)
    b = P()
    gdip.GdipCreateSolidFill(BLUE, byref(b))
    gdip.GdipFillPath(g, b, path)
    gdip.GdipDeleteBrush(b)
    gdip.GdipDeletePath(path)

    # 白色取景框（小尺寸加大比例，避免糊成一团）
    if size <= 24:
        lw = max(1.45, size * 0.085)
        a = 0.245 * size
        b_ = 0.755 * size
        arm = 0.135 * size
    else:
        lw = max(1.35, size * 0.072)
        a = 0.265 * size
        b_ = 0.735 * size
        arm = 0.125 * size

    pen = P()
    gdip.GdipCreatePen1(WHITE, lw, 0, byref(pen))
    segs = [
        (a, a + arm, a, a), (a, a, a + arm, a),                    # 左上
        (b_ - arm, a, b_, a), (b_, a, b_, a + arm),                # 右上
        (a, b_ - arm, a, b_), (a, b_, a + arm, b_),                # 左下
        (b_ - arm, b_, b_, b_), (b_, b_, b_, b_ - arm),            # 右下
    ]
    for x1_, y1_, x2_, y2_ in segs:
        gdip.GdipDrawLine(g, pen, x1_, y1_, x2_, y2_)
    gdip.GdipDeletePen(pen)

    # 中心点
    cs = max(2.0, size * 0.105)
    cb = P()
    gdip.GdipCreateSolidFill(WHITE, byref(cb))
    gdip.GdipFillRectangle(g, cb, size / 2.0 - cs / 2.0, size / 2.0 - cs / 2.0, cs, cs)
    gdip.GdipDeleteBrush(cb)

    gdip.GdipDeleteGraphics(g)
    gdip.GdipDisposeImage(img)
    return buf.raw


def dib_entry(size, px):
    """构成 ICO 的 BMP 条目：BITMAPINFOHEADER + 自下而上 BGRA + 1bpp AND 掩码"""
    # biHeight 必须是两倍（XOR 图 + AND 掩码）
    hdr = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                      size * size * 4, 0, 0, 0, 0)
    stride = size * 4
    rows = [px[(size - 1 - r) * stride:(size - r) * stride] for r in range(size)]
    xor = b"".join(rows)

    mask_stride = ((size + 31) // 32) * 4
    mask = bytearray(mask_stride * size)
    for y in range(size):
        base = y * stride
        my = (size - 1 - y) * mask_stride
        for x in range(size):
            if px[base + x * 4 + 3] < 128:          # 半透明以下算透明
                mask[my + (x >> 3)] |= (0x80 >> (x & 7))
    return hdr + xor + bytes(mask)


def build_ico(items):
    n = len(items)
    head = struct.pack("<HHH", 0, 1, n)
    off = 6 + 16 * n
    entries, body = b"", b""
    for size, blob in items:
        v = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", v, v, 0, 0, 1, 32, len(blob), off)
        off += len(blob)
        body += blob
    return head + entries + body


def main():
    tok = P()
    si = snap.GdiplusStartupInput(1, None, False, False)
    gdip.GdipStartup = None
    gdip.GdiplusStartup(byref(tok), byref(si), None)

    sizes = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
    items = []
    for s in sizes:
        px = render(s)
        blob = dib_entry(s, px)
        # 自检：掩码里必须有透明位，否则说明 alpha 没画出来
        clear = sum(1 for i in range(3, len(px), 4) if px[i] < 128)
        print("  %3dx%-3d %6d bytes  透明像素 %5.1f%%" % (s, s, len(blob), 100.0 * clear / (s * s)))
        items.append((s, blob))

    data = build_ico(items)
    open("app.ico", "wb").write(data)
    print("app.ico: %d bytes, %.0f KB, %d sizes" % (len(data), len(data) / 1024.0, len(items)))


if __name__ == "__main__":
    main()
