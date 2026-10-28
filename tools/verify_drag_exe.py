# -*- coding: utf-8 -*-
"""对已经打包运行的 SnapLite.exe 做真实鼠标拖动测试（跨进程）。

只看两件事：
  · 拖动中窗口位置是否严格等于「鼠标 - 按下偏移」（跟不跟手）
  · 松手后位置是否精确落在鼠标处
"""
import sys
import os
import time
import threading
import subprocess

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ctypes
from ctypes import byref, wintypes

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


u32.FindWindowW.restype = wintypes.HWND
u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
u32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]

EXE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "SnapLite.exe")
subprocess.run(["taskkill", "/F", "/IM", "SnapLite.exe"],
               capture_output=True)
time.sleep(0.4)
proc = subprocess.Popen([EXE], creationflags=0x00000008)

hwnd = None
for _ in range(40):
    time.sleep(0.25)
    hwnd = u32.FindWindowW("SnapLiteMain", None)
    if hwnd:
        break
if not hwnd:
    print("FAIL: 没找到 SnapLite 主窗口")
    raise SystemExit(1)
time.sleep(0.6)

rc = RECT()
u32.GetWindowRect(hwnd, byref(rc))
print("窗口 (%d,%d) %dx%d" % (rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top))

GB, GBX, GBY = 70, 6, 70          # 抓手：窗口内 (6,70) 的左侧边距，避开按钮
X0, Y0 = rc.left, rc.top
DX, DY = -300, -140
STEPS, DT = 60, 0.008

samples = []
stop = threading.Event()


def sampler():
    while not stop.is_set():
        p = POINT()
        u32.GetCursorPos(byref(p))
        r = RECT()
        u32.GetWindowRect(hwnd, byref(r))
        samples.append((time.perf_counter(), p.x, p.y, r.left, r.top))
        time.sleep(0.0015)


def cursor(x, y):
    u32.SetCursorPos(x, y)


ts = threading.Thread(target=sampler, daemon=True)
ts.start()

cursor(X0 + GBX, Y0 + GBY)
time.sleep(0.05)
u32.mouse_event(0x0002, 0, 0, 0, 0)      # LEFTDOWN
time.sleep(0.05)
t0 = time.perf_counter()
for i in range(1, STEPS + 1):
    cursor(X0 + GBX + int(DX * i / float(STEPS)),
           Y0 + GBY + int(DY * i / float(STEPS)))
    time.sleep(DT)
time.sleep(0.15)
u32.mouse_event(0x0004, 0, 0, 0, 0)      # LEFTUP
t1 = time.perf_counter()
time.sleep(0.1)
stop.set()
ts.join(1.0)

r = RECT()
u32.GetWindowRect(hwnd, byref(r))
print("拖动 %.0f ms，最终窗口 (%d,%d)，期望 (%d,%d)，偏差 (%d,%d)"
      % ((t1 - t0) * 1000, r.left, r.top, X0 + DX, Y0 + DY,
         r.left - (X0 + DX), r.top - (Y0 + DY)))

lags = []
for (t, cx, cy, wl, wt) in samples:
    pass
# 只统计「鼠标已经离开起点」之后的采样，避免按下瞬间的噪声
lags = [(cx - GBX - wl, cy - GBY - wt) for (t, cx, cy, wl, wt) in samples
        if (cx - (X0 + GBX)) + (cy - (Y0 + GBY)) != 0]
if lags:
    nz = [l for l in lags if l != (0, 0)]
    print("拖动中采样 %d 次，窗口未跟住鼠标 %d 次（%.0f%%）"
          % (len(lags), len(nz), 100.0 * len(nz) / len(lags)))
    if nz:
        print("  最大偏差 %d px / %d px"
              % (max(abs(l[0]) for l in nz), max(abs(l[1]) for l in nz)))
