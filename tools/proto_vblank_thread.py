# -*- coding: utf-8 -*-
"""A 方案原型：抓屏 + 模糊放后台线程，主线程只提交，节拍跟垂直消隐（VBlank）。

目的只有一个 —— 验证「后台线程按 DwmFlush 节拍出帧，帧间隔能不能稳定成
刷新周期的整数倍」。稳定就说明锁相可行，改 snap.py 才有意义；不稳定就退回
主线程方案，不白改。

不复用 snap.py 的窗口/绘制链路，只借它的 _blur_bgra / _new_dib / grab_screen，
避免为了验证去动产品代码。

用法：
    python tools/proto_vblank_thread.py [秒数]      默认 5 秒
    python tools/proto_vblank_thread.py 5 --noflush  对照组：不等垂直消隐
"""
import ctypes
import os
import statistics
import sys
import threading
import time
from ctypes import byref, c_int, c_void_p, sizeof

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import snap
from snap import user32, gdi32, gdip, grab_screen, _proto

W, H = 340, 148                       # 与主面板同尺寸
WDA_EXCLUDEFROMCAPTURE = 0x11
WM_FRAME = snap.WM_APP + 2
WM_CLOSE = 0x0010
CLK = time.perf_counter

dwmapi = ctypes.WinDLL("dwmapi")
kernel32 = ctypes.windll.kernel32
kernel32.GetModuleHandleW.restype = ctypes.wintypes.HINSTANCE
_proto(dwmapi.DwmFlush, [], c_int)
_proto(user32.SetWindowDisplayAffinity, [ctypes.wintypes.HWND, ctypes.wintypes.DWORD],
       ctypes.wintypes.BOOL)
_proto(user32.PostMessageW, [ctypes.wintypes.HWND, ctypes.c_uint,
                             ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM],
       ctypes.wintypes.BOOL)
_proto(gdi32.SetDIBitsToDevice,
       [ctypes.wintypes.HDC, c_int, c_int, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
        c_int, c_int, ctypes.wintypes.UINT, ctypes.wintypes.UINT, c_void_p, c_void_p,
        ctypes.wintypes.UINT], c_int)
_proto(gdi32.GetDeviceCaps, [ctypes.wintypes.HDC, c_int], c_int)
_proto(user32.GetDC, [ctypes.wintypes.HWND], ctypes.wintypes.HDC)
_proto(user32.ReleaseDC, [ctypes.wintypes.HWND, ctypes.wintypes.HDC], c_int)
_proto(user32.RegisterClassExW, [ctypes.POINTER(snap.WNDCLASSEXW)], ctypes.wintypes.WORD)
_proto(user32.GetMessageW, [ctypes.POINTER(snap.MSG), ctypes.wintypes.HWND,
                            ctypes.wintypes.UINT, ctypes.wintypes.UINT], c_int)


class Host(object):
    """借 _blur_bgra 用的最小宿主。每个线程各自一份屏幕 DC —— GDI 句柄不跨线程共享。"""

    BLUR_F = 13

    def __init__(self):
        self._dc = {}

    def _get_screen_dc(self):
        tid = threading.get_ident()
        dc = self._dc.get(tid)
        if not dc:
            dc = user32.GetDC(None)
            self._dc[tid] = dc
        return dc


host = Host()
state = {
    "hwnd": None,
    "running": True,
    "slot": None,          # 最新一帧（bytearray），覆盖式
    "submits": [],         # 主线程每次贴图的时间戳
    "grab": [],            # worker 抓屏耗时
    "blur": [],            # worker 模糊耗时
    "flush": [],           # worker 等消隐耗时
    "t0": 0.0,
    "dur": 5.0,
    "flush_on": True,
    "err": 0,
    "produced": 0,
    "step": 1,
}


def _present(buf):
    hdc = user32.GetDC(state["hwnd"])
    try:
        bi = snap.BITMAPINFO()
        bi.bmiHeader.biSize = sizeof(snap.BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = W
        bi.bmiHeader.biHeight = -H
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0
        arr = ctypes.c_char_p(bytes(buf))
        gdi32.SetDIBitsToDevice(hdc, 0, 0, W, H, 0, 0, 0, H, arr, byref(bi), 0)
    finally:
        user32.ReleaseDC(state["hwnd"], hdc)


def worker():
    """抓 + 模糊，节奏跟垂直消隐走。只在出帧后通知主线程一次。"""
    hwnd = state["hwnd"]
    rc = ctypes.wintypes.RECT()
    while state["running"]:
        if state["flush_on"]:
            t = CLK()
            dwmapi.DwmFlush()
            state["flush"].append((CLK() - t) * 1000)

        user32.GetWindowRect(hwnd, byref(rc))
        t = CLK()
        try:
            user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
            try:
                raw = grab_screen(rc.left, rc.top, W, H).raw
            finally:
                user32.SetWindowDisplayAffinity(hwnd, 0)
        except Exception:
            raw = None
        state["grab"].append((CLK() - t) * 1000)
        if not raw or len(raw) != W * H * 4:
            state["err"] += 1
            continue

        t = CLK()
        try:
            buf = snap.App._blur_bgra(host, raw, W, H)
        except Exception:
            buf = None
        state["blur"].append((CLK() - t) * 1000)
        if not buf:
            state["err"] += 1
            continue

        state["produced"] += 1
        if state["produced"] % state["step"] == 0:
            state["slot"] = buf
            user32.PostMessageW(hwnd, WM_FRAME, 0, 0)


def wndproc(hwnd, msg, wp, lp):
    if msg == WM_FRAME:
        buf = state["slot"]
        if buf:
            _present(buf)
            state["submits"].append(CLK())
        return 0
    if msg == WM_CLOSE:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


def run(dur, flush_on, step=1):
    state.update(running=True, slot=None, submits=[], grab=[], blur=[], flush=[],
                 dur=dur, flush_on=flush_on, err=0, produced=0, step=step)

    proc = snap.WNDPROC(wndproc)
    cls = snap.WNDCLASSEXW()
    cls.cbSize = sizeof(snap.WNDCLASSEXW)
    cls.lpfnWndProc = proc
    cls.hInstance = kernel32.GetModuleHandleW(None)
    cls.lpszClassName = "SnapLiteProtoVBlank"
    cls.hCursor = user32.LoadCursorW(None, 32512)
    user32.RegisterClassExW(byref(cls))

    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    x, y = (sw - W) // 2, (sh - H) // 2
    hwnd = user32.CreateWindowExW(0x00000008, "SnapLiteProtoVBlank", "SnapLite 原型",
                                  0x80000000, x, y, W, H, None, None, cls.hInstance, None)
    if not hwnd:
        print("建窗失败"); return None
    state["hwnd"] = hwnd
    user32.ShowWindow(hwnd, 5)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    state["t0"] = CLK()
    cpu0 = time.process_time()

    msg = snap.MSG()
    while True:
        r = user32.GetMessageW(byref(msg), None, 0, 0)
        if r <= 0:
            break
        user32.TranslateMessage(byref(msg))
        user32.DispatchMessageW(byref(msg))
        if CLK() - state["t0"] >= dur:
            break

    wall = CLK() - state["t0"]
    cpu = time.process_time() - cpu0
    state["running"] = False
    th.join(timeout=2.0)
    user32.DestroyWindow(hwnd)
    state["hwnd"] = None

    sub = state["submits"]
    gaps = [(sub[i + 1] - sub[i]) * 1000 for i in range(len(sub) - 1)]
    return {"wall": wall, "cpu": cpu, "n": len(sub), "gaps": gaps}


def report(title, r, vblank):
    if not r or not r["gaps"]:
        print("%s：没出帧" % title); return
    g = r["gaps"]
    body = g[2:] if len(g) > 6 else g
    m = statistics.mean(body)
    sd = statistics.pstdev(body)
    print("┌ %s" % title)
    print("│ 出帧 %d 张 / %.2f 秒 = %.1f 帧/秒" % (r["n"], r["wall"], r["n"] / r["wall"]))
    print("│ 帧间隔 中位 %.2fms  均值 %.2f  标准差 %.2f  最小 %.2f  最大 %.2f"
          % (statistics.median(body), m, sd, min(body), max(body)))
    print("│ 均值 ÷ 消隐周期(%.2fms) = %.3f 倍   ← 越接近整数越锁相" % (vblank, m / vblank))
    print("│ 抖动幅度 标准差/均值 = %.2f%%" % (sd / m * 100))
    b = {}
    for v in body:
        k = round(v)
        b[k] = b.get(k, 0) + 1
    top = sorted(b.items(), key=lambda kv: -kv[1])[:6]
    print("│ 间隔分布 " + ", ".join("%dms×%d" % kv for kv in top))
    print("│ 主线程真 CPU %.1f%% 单核（%d 次提交）"
          % (r["cpu"] / r["wall"] * 100, r["n"]))
    if state["grab"]:
        print("│ worker 抓屏 中位 %.2fms  模糊 中位 %.2fms  等消隐 中位 %.2fms  每 %d 帧交 1 张"
              % (statistics.median(state["grab"]),
                 statistics.median(state["blur"]) if state["blur"] else -1,
                 statistics.median(state["flush"]) if state["flush"] else -1,
                 state["step"]))
    if state.get("err"):
        print("│ 跳过 %d 次（抓屏或模糊失败）" % state["err"])
    print("└")


def main():
    dur = 5.0
    flush_on = "--noflush" not in sys.argv
    for a in sys.argv[1:]:
        try:
            dur = float(a)
        except ValueError:
            pass

    tok = snap.P()
    si = snap.GdiplusStartupInput(1, None, False, False)
    gdip.GdiplusStartup(byref(tok), byref(si), None)

    hdc = user32.GetDC(None)
    vblank = 1000.0 / max(1, gdi32.GetDeviceCaps(hdc, 116))
    user32.ReleaseDC(None, hdc)
    print("屏幕刷新率 %.0fHz → 消隐周期 %.3fms；跑 %.0f 秒\n" % (1000.0 / vblank, vblank, dur))

    print("四组对比：抓屏节奏 × 抽帧提交\n")
    for step, flush_on, title in (
            (1, True, "A 组：跟 DwmFlush 消隐，每帧都交"),
            (1, False, "B 组：不等消隐，每帧都交（跑满）"),
            (2, False, "C 组：不等消隐，每 2 帧交 1 张（目标 ≈ 半速）"),
            (3, False, "D 组：不等消隐，每 3 帧交 1 张（目标 ≈ 1/3 速）")):
        r = run(dur, flush_on, step)
        report(title, r, vblank)
        print()


if __name__ == "__main__":
    main()
