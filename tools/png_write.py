"""把 BGRA 缓冲写成 PNG（zlib 手工编码，不依赖 GDI+ / PIL）。

为什么需要它：环境里没有 PIL，而用 GDI+ 的 GdipSaveImageToFile 存图偶尔会
返回 2（InvalidParameter）—— DC 句柄在 64 位下被截断或被提前回收时会这样，
而且失败是静默的（文件根本不生成）。写 PNG 本身没什么玄机，自己编码最稳。

用法：from png_write import write_png_bgra
      write_png_bgra("out.png", w, h, bgra_bytes)
"""
import struct
import zlib


def _chunk(tag, data):
    body = tag + data
    return (struct.pack(">I", len(data)) + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))


def write_png_bgra(path, w, h, buf, alpha=False):
    """BGRA（预乘或非预乘都行）→ PNG。

    alpha=False：丢掉 alpha 直接写成 RGB，半透明像素会保留它被预乘后的暗色，
                 适合「本来就是不透明」的画布。
    alpha=True ：写成 RGBA，保留透明度。"""
    assert len(buf) >= w * h * 4, "缓冲不够 %d×%d×4" % (w, h)
    stride = w * 4
    rows = []
    if alpha:
        line = bytearray(w * 4)
        for y in range(h):
            o = y * stride
            src = buf[o:o + stride]
            # BGRA -> RGBA
            line[0::4] = src[2::4]
            line[1::4] = src[1::4]
            line[2::4] = src[0::4]
            line[3::4] = src[3::4]
            rows.append(b"\x00" + bytes(line))
        color_type = 6
    else:
        line = bytearray(w * 3)
        for y in range(h):
            o = y * stride
            src = buf[o:o + stride]
            line[0::3] = src[2::4]
            line[1::3] = src[1::4]
            line[2::3] = src[0::4]
            rows.append(b"\x00" + bytes(line))
        color_type = 2
    raw = b"".join(rows)
    png = (b"\x89PNG\r\n\x1a\n"
           + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0))
           + _chunk(b"IDAT", zlib.compress(raw, 6))
           + _chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    return len(png)


if __name__ == "__main__":
    import sys
    print(__doc__)
    sys.exit(0)
