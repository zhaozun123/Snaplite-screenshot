# -*- coding: utf-8 -*-
"""四联对比：真桌面 / 旧路径(插值) / 新路径(排除自己) / 真桌面模糊后

用来肉眼确认「玻璃底到底反映的是不是背后的桌面」。四块都是同一块区域、
同一时刻、面板都停在同一个位置 —— 差别只在取背景的方法。

跑完会在 assets/ 下生成 _dbg_glass_cmp.png，从上到下：
  1. 真桌面（未模糊），看清背后本来长什么样
  2. 旧路径：抓一圈 + 把面板自己那块用左右像素插值补掉
  3. 新路径：SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) 后直接抓
  4. 真桌面模糊后（把面板挪开、抓同一块），这是「标准答案」
"""
import sys
import os
import time
import ctypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from ctypes import c_void_p, byref

import snap
from snap import user32, gdip, P

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "_dbg_glass_cmp.png")
SWP = 0x0001 | 0x0004 | 0x0010
GAP = 6


def save_png(raw, ww, hh, path):
    """BGRA 原始字节 → PNG（走 snap 里已有的 GDI+ 编码）"""
    sdc = user32.GetDC(None)
    d = snap._new_dib(sdc, ww, hh)
    ctypes.memmove(d[3], raw, ww * hh * 4)
    img = P()
    gdip.GdipCreateBitmapFromScan0(ww, hh, ww * 4, 0x00022009, d[3], byref(img))
    gdip.GdipSaveImageToFile(img, path, byref(snap.PNG_CLSID), None)
    gdip.GdipDisposeImage(img)
    snap._free_dib(d)
    user32.ReleaseDC(None, sdc)


app = snap.App()
app.create_main()
W, H = app.MW, app.MH
PX, PY = 700, 400

user32.SetWindowPos(app.hwnd_main, None, PX, PY, 0, 0, SWP)
time.sleep(0.3)

app._refresh_glass_live()
new = bytes(app.mn_glass or b"")             # 新路径：排除自己后直抓
old = app._live_glass_patched(W, H) or b""   # 旧路径：抓一圈 + 左右插值
time.sleep(0.1)

# 面板让开，露出来的就是这块的真相
user32.SetWindowPos(app.hwnd_main, None, 60, 60, 0, 0, SWP)
try:
    snap.dwmapi.DwmFlush()
except Exception:
    pass
time.sleep(0.4)
sharp = snap.grab_screen(PX, PY, W, H).raw
ref = app._blur_bgra(sharp, W, H)

TILES = (("真桌面", sharp), ("旧路径(插值)", old),
         ("新路径(排除自己)", new), ("真桌面模糊(答案)", ref))
CW, CH = W, H * len(TILES) + GAP * (len(TILES) - 1)
canvas = bytearray(CW * CH * 4)
for i in range(CW * CH):
    canvas[i * 4] = 0x30
    canvas[i * 4 + 1] = 0x30
    canvas[i * 4 + 2] = 0x30
    canvas[i * 4 + 3] = 255
for idx, (_, buf) in enumerate(TILES):
    if not buf:
        continue
    oy = idx * (H + GAP)
    for row in range(H):
        canvas[(oy + row) * CW * 4:(oy + row + 1) * CW * 4] = buf[row * W * 4:(row + 1) * W * 4]

save_png(bytes(canvas), CW, CH, OUT)
print("已存 %s  (%dx%d)" % (OUT, CW, CH))
for i, (name, buf) in enumerate(TILES, 1):
    print("  %d. %-16s %s" % (i, name, "空" if not buf else "%d 字节" % len(buf)))
print("\n判读：第 3 块应和第 4 块几乎一样；第 2 块会和它们明显不同（中间被抹平）。")
user32.DestroyWindow(app.hwnd_main)
