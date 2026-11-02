"""审计「面板偶尔整块黑一下」：抓回来的帧本身是不是黑的。

用法：python -u tools/audit_black_frame.py [秒数]

只读，不改任何程序状态。盯三件事：

1. `grab_screen` 里 `BitBlt` / `GetDIBits` 的返回值——现在代码里两个都不看。
   失败时 CreateCompatibleBitmap 造出来的是一整块黑色，链路上下没有任何人
   会发现，直接进 `_blur_bgra` 变成「纯黑玻璃底」贴到面板上。
2. 抓回来那一块的平均亮度分布。正常桌面在 20~200 之间抖动，全黑是 0。
3. 同一块连抓的耗时分布（偶发的长尾往往和坏帧同时出现）。

统计口径：亮度 = 每像素 (B+G+R)/3 的均值，每 7 个像素采一个。
"""
import sys
import os
import time
import ctypes
from ctypes import byref, c_void_p

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import snap
from snap import user32, gdi32, w

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

VX = user32.GetSystemMetrics(snap.SM_XVIRTUALSCREEN)
VY = user32.GetSystemMetrics(snap.SM_YVIRTUALSCREEN)
VW = user32.GetSystemMetrics(snap.SM_CXVIRTUALSCREEN)
VH = user32.GetSystemMetrics(snap.SM_CYVIRTUALSCREEN)

W, H = 340, 148                 # 主面板尺寸
GX = VX + (VW - W) // 2
GY = VY + (VH - H) // 2


def luma(buf, ww, hh, step=7):
    """采样平均亮度（0~255）"""
    if not buf:
        return -1.0
    n = ww * hh
    tot = 0
    cnt = 0
    stride = ww * 4
    for y in range(0, hh, 3):
        o = y * stride
        row = buf[o:o + stride]
        for k in range(0, len(row) - 3, 4 * step):
            tot += row[k] + row[k + 1] + row[k + 2]
            cnt += 3
    return tot / float(cnt) if cnt else -1.0


def grab_probe(x, y, ww, hh):
    """和 snap.grab_screen 同样的调用序列，但把返回值都记下来"""
    hdc = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, ww, hh)
    old = gdi32.SelectObject(mem, bmp)

    t0 = time.perf_counter()
    ok_blt = bool(gdi32.BitBlt(mem, 0, 0, ww, hh, hdc, x, y, snap.SRCCOPY))
    t1 = time.perf_counter()

    bi = snap.BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(snap.BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = ww
    bi.bmiHeader.biHeight = -hh
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    buf = ctypes.create_string_buffer(ww * hh * 4)
    rows = gdi32.GetDIBits(mem, bmp, 0, hh, buf, byref(bi), snap.DIB_RGB_COLORS)
    t2 = time.perf_counter()

    gdi32.SelectObject(mem, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(None, hdc)
    return buf, ok_blt, rows, (t1 - t0) * 1000.0, (t2 - t1) * 1000.0


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    print("区域 (%d,%d) %dx%d   时长 %.0fs" % (GX, GY, W, H, secs))
    print("%6s %8s %6s %7s %7s %7s  %s"
          % ("帧", "亮度", "BitBlt", "blt_ms", "dib_ms", "总ms", "备注"))

    n = 0
    luma_hist = []
    blt_fail = 0
    dib_fail = 0
    black = 0
    t_end = time.time() + secs
    prev = None
    while time.time() < t_end:
        n += 1
        buf, ok, rows, t_blt, t_dib = grab_probe(GX, GY, W, H)
        data = ctypes.string_at(buf, W * H * 4)
        L = luma(data, W, H)
        luma_hist.append(L)
        note = ""
        if not ok:
            blt_fail += 1
            note += "BitBlt失败 "
        if not rows:
            dib_fail += 1
            note += "GetDIBits失败 "
        if L < 1.0:
            black += 1
            note += "全黑 "
        if prev is not None and L < prev * 0.35 and (prev - L) > 12:
            note += "骤降(上帧%.1f) " % prev
        if note or n <= 3 or n % 100 == 0:
            print("%6d %8.1f %6s %7.2f %7.2f %7.2f  %s"
                  % (n, L, "ok" if ok else "FAIL", t_blt, t_dib,
                     t_blt + t_dib, note))
        prev = L

    luma_hist.sort()
    print("\n--- 汇总 %d 帧 ---" % n)
    print("BitBlt 失败      %d" % blt_fail)
    print("GetDIBits 失败   %d" % dib_fail)
    print("全黑帧 (<1)      %d" % black)
    if luma_hist:
        print("亮度 最小/中位/最大  %.1f / %.1f / %.1f"
              % (luma_hist[0], luma_hist[len(luma_hist) // 2], luma_hist[-1]))


if __name__ == "__main__":
    main()
