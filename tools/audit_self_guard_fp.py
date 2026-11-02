"""审计「是不是抓到自己」这两个判据，在真实壁纸上的误判率。

用法：python -u tools/audit_self_guard_fp.py

原理：把面板摆到某个位置、把该位置的真实桌面做成玻璃底并渲染出来（就是拖动
时的那一帧），然后拿「该位置的真实桌面」当抓回来的画面，喂给判据。

  此时**绝对不是抓到自己**（这就是背景），判据必须返回 False。
  返回 True = 误判 → 拖动时会白丢一帧（窗口不跟手），预取线程会白扔一张好图。

`_looks_like_self` 的老基准是「差 79」，但那是亮壁纸；这套壁纸实测亮度只有
20~41，面板本身是「玻璃底压暗 50%」，跟背景的差本来就不大，需要重新量。
"""
import sys
import os
import time
from ctypes import c_void_p

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import snap
from snap import user32, w

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

VX = user32.GetSystemMetrics(snap.SM_XVIRTUALSCREEN)
VY = user32.GetSystemMetrics(snap.SM_YVIRTUALSCREEN)
VW = user32.GetSystemMetrics(snap.SM_CXVIRTUALSCREEN)
VH = user32.GetSystemMetrics(snap.SM_CYVIRTUALSCREEN)

app = snap.App()
app.create_main()
if not app.hwnd_main:
    print("FAIL")
    sys.exit(1)
W, H = app.MW, app.MH

# 藏起来抓一整屏当素材
user32.ShowWindow(app.hwnd_main, 0)
time.sleep(0.2)
truth = snap.grab_screen(VX, VY, VW, VH).raw
user32.ShowWindow(app.hwnd_main, 5)
time.sleep(0.1)

stride = VW * 4


def crop(x, y):
    out = bytearray(W * H * 4)
    for r in range(H):
        so = (y + r) * stride + x * 4
        out[r * W * 4:(r + 1) * W * 4] = truth[so:so + W * 4]
    return bytes(out)


print("面板 %dx%d  壁纸素材 %dx%d" % (W, H, VW, VH))
print("判据阈值：_looks_like_self < 8.0 判自己；_is_self_tile 颜色差 < 46 判自己\n")
print("  坐标        背景亮度   _looks_like_self   _is_self_tile(最小色差)   误判")

xs = [40, 260, 500, 700, 900, 1120, 1340, 1560, 1780, 2000, 2220]
ys = [30, 300, 560, 820, 1080, 1260]
fp_pix = 0
fp_btn = 0
n = 0
worst = []
for y in ys:
    for x in xs:
        if x + W > VW or y + H > VH:
            continue
        raw = crop(x, y)
        # 把这一帧渲染出来，让 mn_bits 成为「面板在这个位置、这个背景」的样子
        try:
            app.mn_glass = app._blur_bgra(raw, W, H)
        except Exception:
            continue
        app._present_main(w.POINT(x, y))
        time.sleep(0.005)

        d = app._looks_like_self(raw, W, H)
        # 直接取判据内部的平均差，才能看出离阈值多远
        base = app.mn_bits.value
        st = W * 4
        x0, x1 = W // 5, W * 4 // 5
        y0, y1 = H // 5, H * 4 // 5
        tot = cnt = 0
        row = None
        for yy in range(y0, y1, 8):
            off = yy * st + x0 * 4
            row = snap.ctypes.string_at(base + off, (x1 - x0) * 4)
            for i in range(0, len(row) - 3, 16):
                o = off + i
                tot += (abs(raw[o] - row[i]) + abs(raw[o + 1] - row[i + 1])
                        + abs(raw[o + 2] - row[i + 2]))
                cnt += 3
        diff = tot / float(cnt) if cnt else 0.0

        lb = lr = lg = 0
        bx0, bx1, by0, by1 = app._self_btn_rect()
        m = 0
        for yy in range(by0, by1):
            o = yy * st + bx0 * 4
            rr = raw[o:o + (bx1 - bx0) * 4]
            for k in range(0, len(rr) - 3, 4):
                lb += rr[k]
                lg += rr[k + 1]
                lr += rr[k + 2]
                m += 1
        lb, lg, lr = lb / m, lg / m, lr / m
        col = min(abs(lb - (c & 0xFF)) + abs(lg - ((c >> 8) & 0xFF))
                  + abs(lr - ((c >> 16) & 0xFF)) for c in snap.SELF_BTN_COLORS)

        lm = snap._quick_luma(raw)
        flag = ""
        if d:
            flag += " 像素误判"
            fp_pix += 1
        if col < 46:
            flag += " 颜色误判"
            fp_btn += 1
        n += 1
        if d or col < 60:
            print("  (%4d,%4d)   %6.1f      %6.1f (%s)      %6.1f          %s"
                  % (x, y, lm, diff, "判自己" if d else "放行", col, flag))
        worst.append((diff, col, x, y))

print("\n共 %d 个位置：像素判据误判 %d 个，颜色判据误判 %d 个" % (n, fp_pix, fp_btn))
worst.sort()
print("像素判据最接近阈值的三处（越小越危险，<8 就是误判）：")
for d, c, x, y in worst[:3]:
    print("  (%4d,%4d) 平均差 %.1f" % (x, y, d))
user32.DestroyWindow(app.hwnd_main)
