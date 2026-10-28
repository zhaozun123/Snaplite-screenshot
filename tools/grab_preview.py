# -*- coding: utf-8 -*-
"""启动 SnapLite 并截一张主界面预览图（含真实桌面磨砂效果）

用法：python tools/grab_preview.py [输出路径]
依赖：已打包好的 SnapLite.exe
"""
import sys, os, time, ctypes, subprocess
from ctypes import byref

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import snap
from snap import user32, w, gdip, P

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "assets", "preview.png")


def save_png(raw, ww, hh, path):
    """把 BGRA 原始字节存成 PNG（走 snap 里已有的 GDI+ 编码）"""
    tok = P()
    si = snap.GdiplusStartupInput(1, None, False, False)
    gdip.GdiplusStartup(byref(tok), byref(si), None)
    sdc = user32.GetDC(None)
    d = snap._new_dib(sdc, ww, hh)
    ctypes.memmove(d[3], raw, ww * hh * 4)
    img = P()
    gdip.GdipCreateBitmapFromScan0(ww, hh, ww * 4, 0x00022009, d[3], byref(img))
    st = gdip.GdipSaveImageToFile(img, path, byref(snap.PNG_CLSID), None)
    gdip.GdipDisposeImage(img)
    snap._free_dib(d)
    user32.ReleaseDC(None, sdc)
    return st


def main():
    try:
        snap.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass

    exe = os.path.join(HERE, "SnapLite.exe")
    if not os.path.exists(exe):
        print("找不到 SnapLite.exe，先打包")
        return 1

    hwnd = user32.FindWindowW("SnapLiteMain", snap.APP_NAME)
    started = False
    if not hwnd:
        subprocess.Popen([exe], cwd=HERE)
        started = True
        for _ in range(60):
            time.sleep(0.2)
            hwnd = user32.FindWindowW("SnapLiteMain", snap.APP_NAME)
            if hwnd:
                break
    if not hwnd:
        print("窗口没起来")
        return 1
    time.sleep(1.2)          # 等玻璃底抓完、DWM 合成稳定

    rc = w.RECT()
    user32.GetWindowRect(hwnd, byref(rc))
    ww, hh = rc.right - rc.left, rc.bottom - rc.top
    # 向外扩一圈，把桌面也带进来，方便看磨砂透视
    pad = 90
    x, y = rc.left - pad, rc.top - pad
    W, H = ww + pad * 2, hh + pad * 2
    raw = snap.grab_screen(x, y, W, H).raw
    st = save_png(raw, W, H, OUT)
    print("窗口 (%d,%d) %dx%d → %s (save=%s)" % (rc.left, rc.top, ww, hh, OUT, st))
    if started:
        print("（本次是新启动的，已留在托盘，可自行退出）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
