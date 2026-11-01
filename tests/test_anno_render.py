# -*- coding: utf-8 -*-
"""标注渲染验证：箭头 / 方框 / 圆形 / 马赛克四条绘制路径的像素级检查。

为什么单独有这个测试：这四种标注的绘制函数（_draw_arrow_item /
_draw_box_item / _draw_mosaic_item / _paint_overlay 等）在加进来之后
**从来没有被任何测试执行过** —— 改坏了也不会报警。这里用一张自己造的
棋盘格底图，不依赖真实桌面，跑得稳、结果确定。

用法：python tests/test_anno_render.py
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from ctypes import byref, c_void_p

import snap
from snap import GFX, gdi32, user32

W, H = 360, 240
CELL = 8                      # 棋盘格边长
DARK = (26, 26, 30)           # 底色（BGR 存，写的时候按 RGB 理解）
LIGHT = (210, 210, 214)
RED = (255, 59, 48)           # ANNO_COLORS[0] = 0xFFFF3B30

ok = True


def check(name, cond, extra=""):
    global ok
    print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))
    if not cond:
        ok = False


def make_checker_bgra():
    """造一张棋盘格（BGRA，和 shot_raw 同格式）"""
    buf = bytearray(W * H * 4)
    for y in range(H):
        for x in range(W):
            c = LIGHT if ((x // CELL) + (y // CELL)) % 2 else DARK
            o = (y * W + x) * 4
            buf[o] = c[2]
            buf[o + 1] = c[1]
            buf[o + 2] = c[0]
            buf[o + 3] = 255
    return bytes(buf)


def near(raw, w, x, y, rgb, tol=70, rad=2):
    """(x,y) 附近 rad 像素内有没有接近 rgb 的像素（抗锯齿下取邻域最接近值）"""
    best = 10 ** 9
    for yy in range(max(0, y - rad), min(H, y + rad + 1)):
        for xx in range(max(0, x - rad), min(W, x + rad + 1)):
            o = (yy * w + xx) * 4
            d = (abs(raw[o + 2] - rgb[0]) + abs(raw[o + 1] - rgb[1])
                 + abs(raw[o] - rgb[2]))
            best = min(best, d)
    return best <= tol * 3, best


def px(raw, w, x, y):
    o = (y * w + x) * 4
    return raw[o + 2], raw[o + 1], raw[o]


app = snap.App()
app.create_main()
sdc = app._get_screen_dc()
app.hdc_screen = sdc      # _render_result 用它做 BitBlt 的源

# 自己造底图：不碰真实桌面，结果可复现
app.vs = (0, 0, W, H)
app.sel = (0, 0, W, H)
app.shot_raw = make_checker_bgra()
app._mos_cache = {}
def paint_checker(dc):
    """把棋盘格重新刷进一块 DC（测试里多次复用它，避免上一轮画的东西残留）"""
    gg = GFX(dc)
    for gy in range(0, H, CELL):
        for gx in range(0, W, CELL):
            c = LIGHT if ((gx // CELL) + (gy // CELL)) % 2 else DARK
            gg.fill(0xFF000000 | (c[2] << 16) | (c[1] << 8) | c[0], gx, gy, CELL, CELL)
    gg.close()


src_dc, src_bmp, src_old, src_bits = snap._new_dib(sdc, W, H)
paint_checker(src_dc)
app.dc_shot = src_dc

base_buf, bw, bh = app._render_result()
check("底图能渲染出来", base_buf is not None and (bw, bh) == (W, H), "%sx%s" % (bw, bh))
base = bytes(base_buf)

# ---------- 1. 箭头
app.shapes = [{"t": "arrow", "x1": 40, "y1": 40, "x2": 150, "y2": 150, "color": 0},
              {"t": "rect", "x1": 200, "y1": 40, "x2": 300, "y2": 130,
               "color": 0, "w": 3},
              {"t": "ellipse", "x1": 210, "y1": 150, "x2": 300, "y2": 230,
               "color": 0, "w": 3}]
buf, w, h = app._render_result()
raw = bytes(buf)

hit, d = near(raw, w, 95, 95, RED)
check("箭头杆是红的", hit, "色差 %d" % d)
hit, d = near(raw, w, 148, 148, RED)
check("箭头尖是红的", hit, "色差 %d" % d)
# 箭头外侧（沿法线再往外 20px）不该被染红
r0, g0, b0 = px(raw, w, 95 + 20, 95 - 20)
check("箭头外侧没画到", not (r0 > 180 and g0 < 100 and b0 < 100),
      "RGB=%s" % ((r0, g0, b0),))

# ---------- 2. 方框：只有描边，不填充
hit, d = near(raw, w, 250, 40, RED)
check("方框上边是红的", hit, "色差 %d" % d)
hit, d = near(raw, w, 200, 85, RED)
check("方框左边是红的", hit, "色差 %d" % d)
r1, g1, b1 = px(raw, w, 250, 85)
check("方框内部没被填充", abs(r1 - g1) < 12 and abs(g1 - b1) < 12,
      "内部 RGB=%s" % ((r1, g1, b1),))

# ---------- 3. 圆形：四边有描边、直角处是空的
hit, d = near(raw, w, 255, 150, RED)
check("圆形上边有描边", hit, "色差 %d" % d)
hit, d = near(raw, w, 210, 190, RED)
check("圆形左边有描边", hit, "色差 %d" % d)
r2, g2, b2 = px(raw, w, 213, 153)
check("圆形右上角是空的（不是方框）", abs(r2 - g2) < 12 and abs(g2 - b2) < 12,
      "角上 RGB=%s" % ((r2, g2, b2),))

# ---------- 4. 马赛克：块内颜色被抹平
app._mos_cache = {}
# blk 故意取 10（不整除棋盘格 8），否则每个马赛克块本来就是纯色，抹了看不出变化
app.shapes = [{"t": "mosaic", "pts": [(80, 200), (150, 200)], "blk": 10,
               "r": app._mosaic_r()}]
buf, w, h = app._render_result()
mos = bytes(buf)
# 同一块 8x8 内原本是纯色（棋盘格），改为看「跨块」是否被抹平：
# 取一个横跨黑白两块的 8px 区域，抹过之后块内方差应明显下降
def var_of(buf_, x0, y0, ww, hh):
    vals = []
    for y in range(y0, y0 + hh):
        for x in range(x0, x0 + ww):
            vals.append(px(buf_, w, x, y)[0])
    m = sum(vals) / len(vals)
    return sum((v - m) ** 2 for v in vals) / len(vals)

v_before = var_of(base, 84, 196, 40, 8)
v_after = var_of(mos, 84, 196, 40, 8)
check("马赛克把棋盘格抹平了", v_after < v_before * 0.75,
      "方差 %.0f → %.0f" % (v_before, v_after))
# 笔迹外（y=120）必须原样
r3, g3, b3 = px(mos, w, 100, 120)
r4, g4, b4 = px(base, w, 100, 120)
check("马赛克没溢到笔迹外", (r3, g3, b3) == (r4, g4, b4),
      "%s vs %s" % ((r3, g3, b3), (r4, g4, b4)))

# ---------- 5. 选中箭头时露出的端点手柄
app.shapes = [{"t": "arrow", "x1": 60, "y1": 60, "x2": 140, "y2": 140, "color": 0}]
app.sel_shape = app.shapes[0]
g2 = GFX(src_dc)
app._draw_shapes(g2, 0, 0, True)      # ui=True 才带手柄
g2.close()
mark_dc, mark_bmp, mark_old, mark_bits = snap._new_dib(sdc, W, H)
gdi32.BitBlt(mark_dc, 0, 0, W, H, src_dc, 0, 0, 0x00CC0020)
mark_raw = ctypes.string_at(mark_bits, W * H * 4)
# 端点 (60,60) 处应是白圈 + 橙心，取白判断
hit, d = near(mark_raw, W, 60, 60, (255, 255, 255), tol=60, rad=3)
check("选中箭头露出端点手柄", hit, "色差 %d" % d)
app.sel_shape = None
snap._free_dib((mark_dc, mark_bmp, mark_old, mark_bits))

paint_checker(src_dc)      # 第 5 步把箭头和手柄画进了底图，这里刷回干净的

# ---------- 6. 超出选区的标注要被裁掉
app.sel = (0, 0, 120, 120)
app.shapes = [{"t": "rect", "x1": 200, "y1": 200, "x2": 260, "y2": 260,
               "color": 0, "w": 3}]
buf, w, h = app._render_result()
clipped = bytes(buf)
red_found = False
for y in range(h):
    for x in range(w):
        r, g, b = px(clipped, w, x, y)
        if r > 180 and g < 100 and b < 100:
            red_found = True
            break
    if red_found:
        break
check("选区外的标注不出现在成品里", not red_found, "成品 %dx%d" % (w, h))

# ---------- 7. 反复重绘不漏 GDI 句柄
app.sel = (0, 0, W, H)
app.shapes = [{"t": "arrow", "x1": 10, "y1": 10, "x2": 90, "y2": 90, "color": 0},
              {"t": "rect", "x1": 20, "y1": 20, "x2": 60, "y2": 60, "color": 0, "w": 2},
              {"t": "ellipse", "x1": 30, "y1": 30, "x2": 70, "y2": 70, "color": 0, "w": 2}]
user32.GetGuiResources.restype = ctypes.c_uint
user32.GetGuiResources.argtypes = [c_void_p, ctypes.c_uint]
k32 = ctypes.windll.kernel32
k32.GetCurrentProcess.restype = c_void_p
for _ in range(5):                      # 预热，把首帧的缓存建好
    app._render_result()
h_before = user32.GetGuiResources(k32.GetCurrentProcess(), 0)
for _ in range(80):
    app._render_result()
h_after = user32.GetGuiResources(k32.GetCurrentProcess(), 0)
check("重绘 80 次不漏 GDI 句柄", abs(h_after - h_before) <= 2,
      "%d → %d" % (h_before, h_after))

# ---------- 8. 标注字典里不许残留渲染缓存
# 以前 _bb / _cells / _vc / _v 直接写在标注 dict 里，靠 7 处手工 pop 清理，
# 漏一处就会画出上一帧的包围盒 / 错位的马赛克格。现在缓存全在 App 上，
# 这条断言守住「标注字典只存数据」这个约定。
mos = {"t": "mosaic", "pts": [(60, 60), (120, 60)], "blk": 10,
       "r": app._mosaic_r()}
box = {"t": "rect", "x1": 30, "y1": 30, "x2": 80, "y2": 80, "color": 0, "w": 2}
arr = {"t": "arrow", "x1": 10, "y1": 10, "x2": 90, "y2": 90, "color": 0}
with GFX(src_dc) as g:
    app._draw_one_shape(g, src_dc, box, 0, 0)
    app._draw_one_shape(g, src_dc, arr, 0, 0)
cells1 = app._stroke_cells(mos)
dirty = sorted(set(k for sh in (mos, box, arr) for k in sh if k.startswith("_")))
check("标注字典里没有渲染缓存", not dirty, "残留 %s" % dirty)

check("同一笔迹重复取网格命中缓存", app._stroke_cells(mos) is cells1)
app._translate_shapes_one(mos, 13, 0)      # 平移 13px：网格对齐变了
cells2 = app._stroke_cells(mos)
check("平移后马赛克网格会重算", cells2 is not cells1 and cells2 != cells1,
      "平移前 %d 格 / 平移后 %d 格" % (len(cells1), len(cells2)))

snap._free_dib((src_dc, src_bmp, src_old, src_bits))
app.shapes = []
if app.hwnd_overlay:
    user32.DestroyWindow(app.hwnd_overlay)
user32.DestroyWindow(app.hwnd_main)
print()
print("结果：%s" % ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
