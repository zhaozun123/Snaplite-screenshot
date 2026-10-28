# -*- coding: utf-8 -*-
"""
SnapLite - 轻量截图工具
纯 Windows 原生接口实现（GDI / GDI+），无任何第三方依赖。

功能：
  · 全局热键唤起（可在面板或托盘菜单里改成任意组合）
  · 自动识别鼠标下的窗口/控件区域，单击直接捕获；也可自由拖拽框选
  · 框选后可用 8 个手柄调整范围、拖动整体移动
  · 画笔 / 马赛克 / 箭头标注
  · 复制到剪贴板 / 保存为 PNG
  · 常驻托盘；✕ 与 — 均为收进托盘，退出在托盘右键菜单
"""
import ctypes
import os
import sys
import threading
import time
import traceback
import winreg
from math import hypot, radians, cos, sin, floor, ceil
from ctypes import (windll, byref, sizeof, POINTER, WINFUNCTYPE, Structure,
                    c_int, c_uint, c_long, c_ulong, c_ushort, c_ulonglong,
                    c_void_p, c_float, c_ubyte, c_short, c_wchar)

import ctypes.wintypes as w

# ============================================================ 基础
user32 = windll.user32
gdi32 = windll.gdi32
gdip = windll.gdiplus
comdlg32 = windll.comdlg32
shell32 = windll.shell32
ole32 = windll.ole32
oleacc = windll.oleacc
dwmapi = windll.dwmapi
# AlphaBlend 只有它一个函数用得上；拿不到就退化成 BitBlt（见 _blend_main_into_shot）
try:
    msimg32 = windll.msimg32
except OSError:
    msimg32 = None

P = c_void_p
LRESULT = ctypes.c_ssize_t
WNDPROC = WINFUNCTYPE(LRESULT, w.HWND, c_uint, w.WPARAM, w.LPARAM)

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "SnapLite"
APP_VERSION = "1.2.0"
APP_TITLE = "%s %s" % (APP_NAME, APP_VERSION)

DEBUG = bool(os.environ.get("SNAPLITE_DEBUG"))

# 配置与日志统一放 %APPDATA%\SnapLite\：
# 绿色版 exe 可能被放在 C:\Program Files\ 这类只读目录，写不进去。
CFG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "SnapLite")
CFG_FILE = os.path.join(CFG_DIR, "config.ini")
LOG_FILE = os.path.join(CFG_DIR, "snap_error.log")
DEBUG_LOG = os.path.join(CFG_DIR, "snap_debug.log")


def _write_log(path, text):
    """写日志失败也不能影响主流程（磁盘满、权限不足都可能）"""
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


def dbg(msg):
    if not DEBUG:
        return
    _write_log(DEBUG_LOG, "%s  %s\n" % (time.strftime("%H:%M:%S"), msg))


def _safe_proc(fn):
    """窗口过程兜底：ctypes 回调里抛出的异常会被 ctypes 静默吞掉
    （只在 stderr 打一行 "Exception ignored"），用户和开发者都看不到现场，
    窗口还会停在半损坏状态。这里统一接住 → 写错误日志 → 返回 0。"""
    def wrapper(hwnd, msg, wp, lp):
        try:
            return fn(hwnd, msg, wp, lp)
        except Exception:
            _write_log(LOG_FILE,
                       "\n===== %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S")
                       + traceback.format_exc())
            return 0
    wrapper.__name__ = getattr(fn, "__name__", "proc")
    return wrapper


# ============================================================ 配置（快捷键持久化）
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008
DEFAULT_MOD = MOD_CONTROL | MOD_ALT
DEFAULT_VK = 0x41                      # A


def load_config():
    mod, vk = DEFAULT_MOD, DEFAULT_VK
    try:
        with open(CFG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k == "hotkey_mod":
                    mod = int(v, 0)
                elif k == "hotkey_vk":
                    vk = int(v, 0)
    except Exception:
        pass
    if not (1 <= mod <= 0xF) or not (0x01 <= vk <= 0xFE):
        return DEFAULT_MOD, DEFAULT_VK
    return mod, vk


def save_config(mod, vk):
    try:
        if not os.path.isdir(CFG_DIR):
            os.makedirs(CFG_DIR)
        with open(CFG_FILE, "w", encoding="utf-8") as f:
            f.write("# SnapLite 配置\nhotkey_mod=%d\nhotkey_vk=%d\n" % (mod, vk))
        return True
    except Exception:
        return False


HOTKEY_ID = 0xA1

WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_DESTROY = 0x0002
WM_NULL = 0x0000
WM_MOVE = 0x0003
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_ACTIVATE = 0x0006
WM_SETCURSOR = 0x0020
WM_MOUSELEAVE = 0x02A3
WM_HOTKEY = 0x0312
WM_CAPTURECHANGED = 0x0215
WM_APP = 0x8000
WM_TRAY = WM_APP + 1

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
SWP_NOACTIVATE = 0x0010
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
HWND_TOPMOST = -1

SW_HIDE = 0
SW_SHOW = 5
SW_SHOWNA = 8

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0

GW_HWNDNEXT = 2

VK_ESCAPE = 0x1B
VK_CONTROL, VK_MENU, VK_SHIFT = 0x11, 0x12, 0x10
VK_LWIN, VK_RWIN = 0x5B, 0x5C
MODIFIER_VKS = {0x10, 0x11, 0x12, 0x5B, 0x5C,
                0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}
IDC_ARROW = 32512
IDC_CROSS = 32515
IDC_SIZENWSE = 32642
IDC_SIZENESW = 32643
IDC_SIZEWE = 32644
IDC_SIZENS = 32645
IDC_SIZEALL = 32646

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


# ============================================================ 结构体
class BITMAPINFOHEADER(Structure):
    _fields_ = [("biSize", w.DWORD), ("biWidth", c_int), ("biHeight", c_int),
                ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                ("biSizeImage", w.DWORD), ("biXPelsPerMeter", c_int),
                ("biYPelsPerMeter", c_int), ("biClrUsed", w.DWORD),
                ("biClrImportant", w.DWORD)]


class BITMAPINFO(Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", w.DWORD * 3)]


class WNDCLASSEXW(Structure):
    _fields_ = [("cbSize", c_uint), ("style", c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", c_int), ("cbWndExtra", c_int), ("hInstance", w.HINSTANCE),
                ("hIcon", w.HICON), ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR),
                ("hIconSm", w.HICON)]


class MSG(Structure):
    _fields_ = [("hwnd", w.HWND), ("message", c_uint), ("wParam", w.WPARAM),
                ("lParam", w.LPARAM), ("time", w.DWORD), ("pt", w.POINT)]


class PAINTSTRUCT(Structure):
    _fields_ = [("hdc", w.HDC), ("fErase", w.BOOL), ("rcPaint", w.RECT),
                ("fRestore", w.BOOL), ("fIncUpdate", w.BOOL), ("rgbReserved", c_ubyte * 32)]


class GdiplusStartupInput(Structure):
    _fields_ = [("GdiplusVersion", c_uint), ("DebugEventCallback", c_void_p),
                ("SuppressBackgroundThread", w.BOOL), ("SuppressExternalCodecs", w.BOOL)]


class RectF(Structure):
    _fields_ = [("X", c_float), ("Y", c_float), ("Width", c_float), ("Height", c_float)]


class PointF(Structure):
    _fields_ = [("X", c_float), ("Y", c_float)]


class ICONINFO(Structure):
    _fields_ = [("fIcon", w.BOOL), ("xHotspot", w.DWORD), ("yHotspot", w.DWORD),
                ("hbmMask", w.HBITMAP), ("hbmColor", w.HBITMAP)]


class GUID(Structure):
    _fields_ = [("Data1", c_uint), ("Data2", w.WORD), ("Data3", w.WORD),
                ("Data4", c_ubyte * 8)]


class TRACKMOUSEEVENT(Structure):
    _fields_ = [("cbSize", w.DWORD), ("dwFlags", w.DWORD), ("hwndTrack", w.HWND),
                ("dwHoverTime", w.DWORD)]


class BLENDFUNCTION(Structure):
    _fields_ = [("BlendOp", c_ubyte), ("BlendFlags", c_ubyte),
                ("SourceConstantAlpha", c_ubyte), ("AlphaFormat", c_ubyte)]


class VARIANT(Structure):
    """x64 下大小 24 字节；这里只用 VT_I4 分支（低 4 字节即值）"""
    _fields_ = [("vt", c_ushort), ("w1", c_ushort), ("w2", c_ushort),
                ("w3", c_ushort), ("v", c_ulonglong), ("v2", c_ulonglong)]


class NOTIFYICONDATAW(Structure):
    _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", c_uint),
                ("uFlags", c_uint), ("uCallbackMessage", c_uint), ("hIcon", w.HICON),
                ("szTip", c_wchar * 128), ("dwState", w.DWORD), ("dwStateMask", w.DWORD),
                ("szInfo", c_wchar * 256), ("uVersion", c_uint),
                ("szInfoTitle", c_wchar * 64), ("dwInfoFlags", w.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", w.HICON)]


class OPENFILENAMEW(Structure):
    _fields_ = [("lStructSize", w.DWORD), ("hwndOwner", w.HWND), ("hInstance", w.HINSTANCE),
                ("lpstrFilter", w.LPCWSTR), ("lpstrCustomFilter", w.LPWSTR),
                ("nMaxCustFilter", w.DWORD), ("nFilterIndex", w.DWORD),
                ("lpstrFile", w.LPWSTR), ("nMaxFile", w.DWORD),
                ("lpstrFileTitle", w.LPWSTR), ("nMaxFileTitle", w.DWORD),
                ("lpstrInitialDir", w.LPCWSTR), ("lpstrTitle", w.LPCWSTR),
                ("Flags", w.DWORD), ("nFileOffset", w.WORD), ("nFileExtension", w.WORD),
                ("lpstrDefExt", w.LPCWSTR), ("lCustData", w.LPARAM),
                ("lpfnHook", c_void_p), ("lpTemplateName", w.LPCWSTR),
                ("pvReserved", c_void_p), ("dwReserved", w.DWORD), ("FlagsEx", w.DWORD)]


def _guid(d1, d2, d3, tail):
    return GUID(d1, d2, d3, (c_ubyte * 8)(*tail))


PNG_CLSID = _guid(0x557CF406, 0x1A04, 0x11D3, (0x9A, 0x73, 0x00, 0x00, 0xF8, 0x1E, 0xF3, 0x2E))


# ============================================================ 函数原型
def _proto(fn, args, ret=c_int):
    fn.argtypes = args
    fn.restype = ret
    return fn


_proto(user32.CreateWindowExW, [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, c_int, c_int,
                               c_int, c_int, w.HWND, w.HMENU, w.HINSTANCE, c_void_p], w.HWND)
_proto(user32.DefWindowProcW, [w.HWND, c_uint, w.WPARAM, w.LPARAM], LRESULT)
_proto(user32.RegisterClassExW, [POINTER(WNDCLASSEXW)], w.WORD)
_proto(user32.GetMessageW, [POINTER(MSG), w.HWND, c_uint, c_uint], c_int)
_proto(user32.TranslateMessage, [POINTER(MSG)], w.BOOL)
_proto(user32.DispatchMessageW, [POINTER(MSG)], LRESULT)
_proto(user32.SendMessageW, [w.HWND, c_uint, w.WPARAM, w.LPARAM], LRESULT)
_proto(user32.PostMessageW, [w.HWND, c_uint, w.WPARAM, w.LPARAM], w.BOOL)
_proto(user32.PostQuitMessage, [c_int], None)
_proto(user32.ShowWindow, [w.HWND, c_int], w.BOOL)
_proto(user32.IsWindowVisible, [w.HWND], w.BOOL)
_proto(user32.DestroyWindow, [w.HWND], w.BOOL)
_proto(user32.SetForegroundWindow, [w.HWND], w.BOOL)
_proto(user32.SetWindowPos, [w.HWND, w.HWND, c_int, c_int, c_int, c_int, c_uint], w.BOOL)
_proto(user32.SetFocus, [w.HWND], w.HWND)
_proto(user32.GetFocus, [], w.HWND)
_proto(user32.GetClientRect, [w.HWND, POINTER(w.RECT)], w.BOOL)
_proto(user32.FillRect, [w.HDC, POINTER(w.RECT), w.HANDLE], c_int)
_proto(user32.InvalidateRect, [w.HWND, POINTER(w.RECT), w.BOOL], w.BOOL)
_proto(user32.UpdateWindow, [w.HWND], w.BOOL)
_proto(user32.BeginPaint, [w.HWND, POINTER(PAINTSTRUCT)], w.HDC)
_proto(user32.EndPaint, [w.HWND, POINTER(PAINTSTRUCT)], w.BOOL)
_proto(user32.LoadCursorW, [w.HINSTANCE, c_void_p], w.HANDLE)
_proto(user32.SetCursor, [w.HANDLE], w.HANDLE)
_proto(user32.CreateIconIndirect, [POINTER(ICONINFO)], w.HICON)
_proto(user32.DestroyIcon, [w.HICON], w.BOOL)
_proto(gdi32.CreateBitmap, [c_int, c_int, c_uint, c_uint, c_void_p], w.HBITMAP)
_proto(user32.GetDC, [w.HWND], w.HDC)
_proto(user32.ReleaseDC, [w.HWND, w.HDC], c_int)
_proto(user32.GetSystemMetrics, [c_int], c_int)
# GetDpiForSystem / SetProcessDpiAwarenessContext 是 Win10 1703+ 才有的 API，
# 老系统上直接取属性会抛 AttributeError。若用模块级 _proto 声明，异常会发生在
# import 阶段 → 整个程序起不来（连降级逻辑都执行不到）。所以必须惰性获取。
def _lazy_proto(dll, name, argtypes, restype):
    """API 不存在 → 返回 None；存在 → 声明原型并返回函数"""
    try:
        fn = getattr(dll, name)
    except AttributeError:
        return None
    _proto(fn, argtypes, restype)
    return fn


GetDpiForSystem = _lazy_proto(user32, "GetDpiForSystem", [], c_uint)
SetProcessDpiAwarenessContext = _lazy_proto(
    user32, "SetProcessDpiAwarenessContext", [c_void_p], w.BOOL)
_proto(user32.SetProcessDPIAware, [], w.BOOL)
_proto(user32.GetCursorPos, [POINTER(w.POINT)], w.BOOL)
_proto(user32.SetCapture, [w.HWND], w.HWND)
_proto(user32.WindowFromPoint, [w.POINT], w.HWND)
_proto(user32.SetWindowRgn, [w.HWND, w.HANDLE, w.BOOL], c_int)
_proto(user32.SetLayeredWindowAttributes, [w.HWND, w.DWORD, c_ubyte, w.DWORD], w.BOOL)
_proto(user32.UpdateLayeredWindow, [w.HWND, w.HDC, POINTER(w.POINT), POINTER(w.SIZE),
                                   w.HDC, POINTER(w.POINT), w.DWORD, c_void_p, w.DWORD],
       w.BOOL)
_proto(user32.RegisterHotKey, [w.HWND, c_int, c_uint, c_uint], w.BOOL)
_proto(user32.UnregisterHotKey, [w.HWND, c_int], w.BOOL)
_proto(user32.GetKeyState, [c_int], c_short)
_proto(user32.GetAsyncKeyState, [c_int], c_short)
_proto(user32.MapVirtualKeyW, [c_uint, c_uint], c_uint)
_proto(user32.ReleaseCapture, [], w.BOOL)
_proto(user32.TrackMouseEvent, [POINTER(TRACKMOUSEEVENT)], w.BOOL)
_proto(user32.OpenClipboard, [w.HWND], w.BOOL)
_proto(user32.EmptyClipboard, [], w.BOOL)
_proto(user32.CloseClipboard, [], w.BOOL)
_proto(user32.SetClipboardData, [c_uint, w.HANDLE], w.HANDLE)
_proto(user32.CreatePopupMenu, [], w.HMENU)
_proto(user32.AppendMenuW, [w.HMENU, c_uint, w.WPARAM, w.LPCWSTR], w.BOOL)
_proto(user32.TrackPopupMenu, [w.HMENU, c_uint, c_int, c_int, c_int, w.HWND, c_void_p], c_int)
_proto(user32.DestroyMenu, [w.HMENU], w.BOOL)
_proto(user32.SetProcessDpiAwarenessContext, [c_void_p], w.BOOL)
_proto(user32.FindWindowW, [w.LPCWSTR, w.LPCWSTR], w.HWND)
_proto(shell32.Shell_NotifyIconW, [w.DWORD, POINTER(NOTIFYICONDATAW)], w.BOOL)

_proto(gdi32.CreateCompatibleDC, [w.HDC], w.HDC)
_proto(gdi32.CreateCompatibleBitmap, [w.HDC, c_int, c_int], w.HANDLE)
_proto(gdi32.SelectObject, [w.HDC, w.HANDLE], w.HANDLE)
_proto(gdi32.DeleteObject, [w.HANDLE], w.BOOL)
_proto(gdi32.DeleteDC, [w.HDC], w.BOOL)
_proto(gdi32.BitBlt, [w.HDC, c_int, c_int, c_int, c_int, w.HDC, c_int, c_int, w.DWORD], w.BOOL)
_proto(gdi32.GetDIBits, [w.HDC, w.HANDLE, c_uint, c_uint, c_void_p, POINTER(BITMAPINFO), c_uint], c_int)
_proto(gdi32.CreateRoundRectRgn, [c_int, c_int, c_int, c_int, c_int, c_int], w.HANDLE)
_proto(gdi32.CreateRectRgn, [c_int, c_int, c_int, c_int], w.HANDLE)
_proto(gdi32.CombineRgn, [w.HANDLE, w.HANDLE, w.HANDLE, c_int], c_int)
_proto(gdi32.CreateDIBSection, [w.HDC, POINTER(BITMAPINFO), c_uint, POINTER(c_void_p),
                                w.HANDLE, w.DWORD], w.HANDLE)

if msimg32:
    # 源位图是预乘 alpha（和 UpdateLayeredWindow 同一套语义），所以直接用
    # AC_SRC_ALPHA 就能得到和屏幕上一模一样的合成结果。
    _proto(msimg32.AlphaBlend, [w.HDC, c_int, c_int, c_int, c_int,
                                w.HDC, c_int, c_int, c_int, c_int, BLENDFUNCTION], w.BOOL)

_proto(gdip.GdiplusStartup, [POINTER(P), POINTER(GdiplusStartupInput), c_void_p])
_proto(gdip.GdipCreateFromHDC, [w.HDC, POINTER(P)])
_proto(gdip.GdipCreateBitmapFromScan0, [c_int, c_int, c_int, c_int, c_void_p, POINTER(P)])
_proto(gdip.GdipSaveImageToFile, [P, w.LPCWSTR, POINTER(GUID), c_void_p])
_proto(gdip.GdipGetImageGraphicsContext, [P, POINTER(P)])
_proto(gdip.GdipSetSmoothingMode, [P, c_int])
_proto(gdip.GdipSetTextRenderingHint, [P, c_int])
_proto(gdip.GdipSetPixelOffsetMode, [P, c_int])
_proto(gdip.GdipSetInterpolationMode, [P, c_int])
_proto(gdip.GdipDrawImageRectI, [P, P, c_int, c_int, c_int, c_int])
_proto(gdip.GdipCreateSolidFill, [c_uint, POINTER(P)])
_proto(gdip.GdipFillRectangle, [P, P, c_float, c_float, c_float, c_float])
_proto(gdip.GdipDeleteBrush, [P])
_proto(gdip.GdipCreatePath, [c_int, POINTER(P)])
_proto(gdip.GdipAddPathArc, [P, c_float, c_float, c_float, c_float, c_float, c_float])
_proto(gdip.GdipClosePathFigure, [P])
_proto(gdip.GdipFillPath, [P, P, P])
_proto(gdip.GdipFillPolygon, [P, P, POINTER(PointF), c_int, c_int])
_proto(gdip.GdipFillEllipse, [P, P, c_float, c_float, c_float, c_float])
_proto(gdip.GdipDrawEllipse, [P, P, c_float, c_float, c_float, c_float])
_proto(gdip.GdipSetClipRect, [P, c_float, c_float, c_float, c_float, c_int])
_proto(gdip.GdipResetClip, [P])
_proto(gdip.GdipSaveGraphics, [P, POINTER(c_int)])
_proto(gdip.GdipRestoreGraphics, [P, c_int])
_proto(gdip.GdipDrawPath, [P, P, P])
_proto(gdip.GdipDeletePath, [P])
_proto(gdip.GdipDeleteGraphics, [P])
_proto(gdip.GdipDisposeImage, [P])
_proto(gdip.GdipCreateFontFamilyFromName, [w.LPCWSTR, c_void_p, POINTER(P)])
_proto(gdip.GdipCreateFont, [P, c_float, c_int, c_int, POINTER(P)])
_proto(gdip.GdipDeleteFont, [P])
_proto(gdip.GdipDeleteFontFamily, [P])
_proto(gdip.GdipCreateStringFormat, [c_int, w.WORD, POINTER(P)])
_proto(gdip.GdipSetStringFormatAlign, [P, c_int])
_proto(gdip.GdipSetStringFormatLineAlign, [P, c_int])
_proto(gdip.GdipSetStringFormatFlags, [P, c_int])
_proto(gdip.GdipDrawString, [P, w.LPCWSTR, c_int, P, POINTER(RectF), P, P])
_proto(gdip.GdipMeasureString, [P, w.LPCWSTR, c_int, P, POINTER(RectF), P,
                                POINTER(RectF), POINTER(c_int), POINTER(c_int)])
_proto(gdip.GdipDeleteStringFormat, [P])
_proto(gdip.GdipCreatePen1, [c_uint, c_float, c_int, POINTER(P)])
_proto(gdip.GdipSetPenStartCap, [P, c_int])
_proto(gdip.GdipSetPenEndCap, [P, c_int])
_proto(gdip.GdipDeletePen, [P])
_proto(gdip.GdipDrawLine, [P, P, c_float, c_float, c_float, c_float])
_proto(gdip.GdipCreateHICONFromBitmap, [P, POINTER(w.HICON), c_uint])

_proto(comdlg32.GetSaveFileNameW, [POINTER(OPENFILENAMEW)], w.BOOL)

# 自动识别区域：窗口枚举 / DWM 真实边框 / 辅助功能取控件矩形
_proto(user32.GetTopWindow, [w.HWND], w.HWND)
_proto(user32.GetWindow, [w.HWND, c_uint], w.HWND)
_proto(user32.GetWindowThreadProcessId, [w.HWND, POINTER(w.DWORD)], w.DWORD)
_proto(user32.IsIconic, [w.HWND], w.BOOL)
_proto(user32.GetClassNameW, [w.HWND, w.LPWSTR, c_int], c_int)
_proto(user32.GetWindowRect, [w.HWND, POINTER(w.RECT)], w.BOOL)
_proto(dwmapi.DwmGetWindowAttribute, [w.HWND, w.DWORD, c_void_p, w.DWORD], c_long)
_proto(ole32.CoInitializeEx, [c_void_p, c_ulong], c_long)
_proto(oleacc.AccessibleObjectFromPoint, [w.POINT, POINTER(c_void_p), POINTER(VARIANT)], c_long)

# 文字输入框 / 控件子类化
_proto(gdi32.SaveDC, [w.HDC], c_int)
_proto(gdi32.RestoreDC, [w.HDC, c_int], w.BOOL)
_proto(gdi32.IntersectClipRect, [w.HDC, c_int, c_int, c_int, c_int], c_int)
_proto(gdi32.SelectClipRgn, [w.HDC, w.HANDLE], c_int)
# 直接从屏幕 DC 缩到小图：一步完成，比「先 BitBlt 整屏再 GDI+ 缩小」快一倍
_proto(gdi32.StretchBlt, [w.HDC, c_int, c_int, c_int, c_int,
                          w.HDC, c_int, c_int, c_int, c_int, w.DWORD], w.BOOL)
_proto(gdi32.SetStretchBltMode, [w.HDC, c_int], c_int)

# 等 DWM 把当前帧真正合成到屏幕（抓背景前要确保自己的窗口已经“消失”）
_proto(dwmapi.DwmFlush, [], c_long)

_proto(windll.kernel32.GlobalAlloc, [c_uint, ctypes.c_size_t], c_void_p)
_proto(windll.kernel32.GlobalLock, [c_void_p], c_void_p)
_proto(windll.kernel32.GlobalUnlock, [c_void_p], w.BOOL)
_proto(windll.kernel32.GetModuleHandleW, [w.LPCWSTR], w.HINSTANCE)


# ============================================================ 配色
C_TAG_BG = 0x24FFFFFF          # 玻璃上的“胶囊”：半透明白，不再是一块实心灰
C_TITLE = 0xFFF5F7FA
C_SUB = 0xFFB7C0CE
C_HINT = 0xFF97A1B1
C_ACCENT = 0xFF0A84FF          # iOS 系统蓝
C_ACCENT_HI = 0xFF3D9BFF
C_HOVER_SOFT = 0x2EFFFFFF

# 独立按钮组（截图后的浮动工具条）—— 版式保持 v1.1.0：每个按钮各自一块，之间留缝。
# 底色改成半透明：底下垫的是「该位置的桌面模糊图」（_tool_glass_layer），
# 于是按钮看起来是磨砂玻璃而不是一块实心灰。alpha 60% 时压暗程度与原来的
# 实心 #1F232A 几乎一致，只是背后能看见被模糊掉的桌面轮廓。
C_TB_BTN = 0x991D2229
C_TB_BTN_HI = 0xCC363E49
C_TB_COPY = C_TB_BTN                 # 跟同一排按钮完全一致，避免看起来"透明度不一样"
C_TB_COPY_HI = C_TB_BTN_HI
C_TB_CANCEL_HI = 0xCC5A2A33          # 取消：悬停时才泛红，静止态和其他按钮一致
C_TB_TXT = 0xFFD3DAE5
C_TB_ICO = 0xFF97A2B1
C_TB_COPY_TXT = C_TB_TXT
C_TB_COPY_ICO = 0xFF7FB2FF           # 只有图标带蓝，暗示这是主操作
C_TB_CANCEL_TXT = 0xFFFFDCE1
C_TB_CANCEL_ICO = 0xFFE8697A
TB_ALPHA = 242          # 整组半透明（0-255），略透出背后内容
TB_ITEMS = (("reselect", "重选"), ("pen", "画笔"), ("mosaic", "马赛克"),
            ("arrow", "箭头"), ("undo", "撤销"), ("copy", "复制"),
            ("save", "保存"), ("cancel", "取消"))
C_WHITE = 0xFFFFFFFF
C_WARN = 0xFFF08A8A

# 标注颜色（ARGB）：红 蓝 绿 黄 白 黑 灰
ANNO_COLORS = (0xFFFF3B30, 0xFF0A84FF, 0xFF34C759, 0xFFFFD60A,
               0xFFFFFFFF, 0xFF101014, 0xFF8E8E93)

# 马赛克：笔头半径 / 块大小（三档，随时可切）
MOSAIC_BRUSHES = (12, 20, 32)
MOSAIC_BRUSH_NAMES = ("细", "中", "粗")
MOSAIC_BLOCKS = (8, 12, 18)
MOSAIC_BLOCK_NAMES = ("小", "中", "大")

# 画笔：笔迹粗细（逻辑像素，三档，随时可切）。颜色复用 ANNO_COLORS 前六色。
# 单笔最多记多少个采样点：防止长距离涂抹时点数爆炸
# （点数越多，每帧要画的线段越多；撤销栈里还会存几十份快照）
MAX_PEN_POINTS = 4000
PEN_WIDTHS = (3, 6, 10)
PEN_WIDTH_NAMES = ("细", "中", "粗")

# 箭头交互：两端手柄半径 / 线条命中容差
ARROW_HANDLE_R = 5
ARROW_HIT_TOL = 7

TEXT_HINT_GRIDFIT = 3
SMOOTH_AA = 4
PIXEL_HALF = 2


def lo(v):
    return c_short(v & 0xFFFF).value


def hi(v):
    return c_short((v >> 16) & 0xFFFF).value


_VK_SPECIAL = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x13: "Pause",
    0x14: "CapsLock", 0x20: "Space", 0x21: "PgUp", 0x22: "PgDn",
    0x23: "End", 0x24: "Home", 0x25: "Left", 0x26: "Up", 0x27: "Right",
    0x28: "Down", 0x2C: "PrtSc", 0x2D: "Insert", 0x2E: "Delete",
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
    0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
}


def vk_name(vk):
    if 0x70 <= vk <= 0x87:
        return "F%d" % (vk - 0x70 + 1)
    if vk in _VK_SPECIAL:
        return _VK_SPECIAL[vk]
    ch = user32.MapVirtualKeyW(vk, 2)
    if ch and 0x20 <= ch < 0x7F:
        return chr(ch).upper()
    return "VK%02X" % vk


def hotkey_label(mod, vk):
    parts = []
    if mod & MOD_CONTROL:
        parts.append("Ctrl")
    if mod & MOD_ALT:
        parts.append("Alt")
    if mod & MOD_SHIFT:
        parts.append("Shift")
    if mod & MOD_WIN:
        parts.append("Win")
    parts.append(vk_name(vk))
    return "+".join(parts)


# ============================================================ 多行文字拆分


# ============================================================ 绘图封装
class GFX:
    """GDI+ 轻量封装"""

    def __init__(self, hdc=None, ptr=None):
        self.hdc = hdc                       # 记下 GDI DC，供 GDI 文字渲染/度量用
        self.g = ptr if ptr else P()
        if not ptr:
            gdip.GdipCreateFromHDC(hdc, byref(self.g))
        gdip.GdipSetSmoothingMode(self.g, SMOOTH_AA)
        gdip.GdipSetTextRenderingHint(self.g, TEXT_HINT_GRIDFIT)
        gdip.GdipSetPixelOffsetMode(self.g, PIXEL_HALF)
        gdip.GdipSetInterpolationMode(self.g, 7)
        self._fonts = {}

    def close(self):
        """释放 GDI+ 资源。必须幂等：异常路径上可能被调用两次。"""
        if getattr(self, "_closed", False):
            return
        self._closed = True
        for f in self._fonts.values():
            gdip.GdipDeleteFont(f)
        self._fonts.clear()
        gdip.GdipDeleteGraphics(self.g)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # 异常时也必须释放：否则每帧泄漏一个 GDI+ Graphics + 字体
        self.close()
        return False

    def __del__(self):
        # 兜底：绘制中途抛异常时，局部变量 g 离开作用域 → 引用计数归零 →
        # 这里兜住 close()，避免 GDI+ 对象无限累积
        try:
            self.close()
        except Exception:
            pass

    # ---------- 裁剪：标注只能落在选区里（画布外的一律裁掉）
    def clip_rect(self, x, y, ww, hh):
        gdip.GdipSetClipRect(self.g, float(x), float(y),
                             float(max(0.0, ww)), float(max(0.0, hh)), 1)
        # GDI+ 的 clip 只对 GDI+ 绘制生效，这里同步设 GDI 裁剪区，
        # 否则 GDI 画的文字会漏到选区外
        if self.hdc:
            gdi32.IntersectClipRect(self.hdc, int(x), int(y),
                                    int(x + ww), int(y + hh))

    def clip_save(self):
        """保存当前裁剪状态。clip_rect 是「相交」语义，但重置 clip 会把**外层**
        的裁剪一起清掉 —— 嵌套裁剪必须用 save/restore，否则里层画完外面就漏了。
        GDI 侧用 SaveDC 同步保存（含 clip region），restore 时一起恢复。"""
        st = c_int()
        gdip.GdipSaveGraphics(self.g, byref(st))
        if self.hdc:
            self._gdi_stack = getattr(self, "_gdi_stack", [])
            self._gdi_stack.append(gdi32.SaveDC(self.hdc))
        return st.value

    def clip_restore(self, st):
        gdip.GdipRestoreGraphics(self.g, st)
        stack = getattr(self, "_gdi_stack", [])
        if self.hdc and stack:
            gdi32.RestoreDC(self.hdc, stack.pop())

    # ---------- 基础图形
    def _brush(self, argb):
        b = P()
        gdip.GdipCreateSolidFill(argb, byref(b))
        return b

    def fill(self, argb, x, y, ww, hh):
        if ww <= 0 or hh <= 0:
            return
        b = self._brush(argb)
        gdip.GdipFillRectangle(self.g, b, x, y, ww, hh)
        gdip.GdipDeleteBrush(b)

    def fill_poly(self, argb, pts):
        """填充多边形（箭头实心三角头用）"""
        if len(pts) < 3:
            return
        arr = (PointF * len(pts))()
        for i, (px_, py_) in enumerate(pts):
            arr[i].X = px_
            arr[i].Y = py_
        b = self._brush(argb)
        gdip.GdipFillPolygon(self.g, b, arr, len(pts), 0)    # FillModeAlternate
        gdip.GdipDeleteBrush(b)

    def fill_circle(self, argb, cx, cy, r):
        if r <= 0:
            return
        b = self._brush(argb)
        gdip.GdipFillEllipse(self.g, b, cx - r, cy - r, r * 2.0, r * 2.0)
        gdip.GdipDeleteBrush(b)

    def _path_round(self, x, y, ww, hh, r):
        r = max(0.1, min(r, min(ww, hh) / 2.0))
        p = P()
        gdip.GdipCreatePath(0, byref(p))
        d = r * 2
        gdip.GdipAddPathArc(p, x, y, d, d, 180, 90)
        gdip.GdipAddPathArc(p, x + ww - d, y, d, d, 270, 90)
        gdip.GdipAddPathArc(p, x + ww - d, y + hh - d, d, d, 0, 90)
        gdip.GdipAddPathArc(p, x, y + hh - d, d, d, 90, 90)
        gdip.GdipClosePathFigure(p)
        return p

    def fill_round(self, argb, x, y, ww, hh, r):
        b = self._brush(argb)
        p = self._path_round(x, y, ww, hh, r)
        gdip.GdipFillPath(self.g, b, p)
        gdip.GdipDeletePath(p)
        gdip.GdipDeleteBrush(b)

    def stroke_round(self, argb, width, x, y, ww, hh, r):
        pen = P()
        gdip.GdipCreatePen1(argb, width, 0, byref(pen))
        p = self._path_round(x, y, ww, hh, r)
        gdip.GdipDrawPath(self.g, pen, p)
        gdip.GdipDeletePath(p)
        gdip.GdipDeletePen(pen)

    def line(self, argb, width, x1, y1, x2, y2, cap=0):
        pen = P()
        gdip.GdipCreatePen1(argb, width, 0, byref(pen))
        if cap:
            gdip.GdipSetPenStartCap(pen, cap)
            gdip.GdipSetPenEndCap(pen, cap)
        gdip.GdipDrawLine(self.g, pen, x1, y1, x2, y2)
        gdip.GdipDeletePen(pen)

    # ---------- 文字
    def _font(self, size_px, bold, fam=None):
        key = (round(size_px, 2), bool(bold), fam)
        f = self._fonts.get(key)
        if f is not None:
            return f
        names = [fam] if fam else []
        names += ["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"]
        fam_h = None
        for name in names:
            if not name:
                continue
            fh = P()
            if gdip.GdipCreateFontFamilyFromName(name, None, byref(fh)) == 0 and fh:
                fam_h = fh
                break
        fnt = P()
        gdip.GdipCreateFont(fam_h, float(size_px), 1 if bold else 0, 2, byref(fnt))
        if fam_h:
            gdip.GdipDeleteFontFamily(fam_h)
        self._fonts[key] = fnt
        return fnt

    def text(self, s, size_px, argb, x, y, ww, hh, align=0, bold=False,
             valign=1, fam=None, nowrap=False):
        fnt = self._font(size_px, bold, fam)
        br = self._brush(argb)
        fmt = P()
        gdip.GdipCreateStringFormat(0, 0, byref(fmt))
        gdip.GdipSetStringFormatAlign(fmt, align)
        gdip.GdipSetStringFormatLineAlign(fmt, valign)
        if nowrap:
            # NoWrap|NoClip：一行画完，绝不因为矩形不够宽而折到第二行
            gdip.GdipSetStringFormatFlags(fmt, 0x5000)
        rc = RectF(float(x), float(y), float(ww), float(hh))
        gdip.GdipDrawString(self.g, s, -1, fnt, byref(rc), fmt, br)
        gdip.GdipDeleteStringFormat(fmt)
        gdip.GdipDeleteBrush(br)

    def measure(self, s, size_px, bold=False, fam=None):
        fnt = self._font(size_px, bold, fam)
        fmt = P()
        gdip.GdipCreateStringFormat(0, 0, byref(fmt))
        gdip.GdipSetStringFormatFlags(fmt, 0x5000)          # NoWrap|NoClip
        rc = RectF(0.0, 0.0, 100000.0, 100000.0)
        box = RectF()
        gdip.GdipMeasureString(self.g, s, -1, fnt, byref(rc), fmt,
                               byref(box), None, None)
        gdip.GdipDeleteStringFormat(fmt)
        return box.Width, box.Height



# ============================================================ 屏幕抓取
def _new_dib(sdc, ww, hh):
    """建一块 32bpp top-down DIB，返回 (dc, bmp, old, bits)"""
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = ww
    bi.bmiHeader.biHeight = -hh
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    bits = c_void_p()
    bmp = gdi32.CreateDIBSection(sdc, byref(bi), DIB_RGB_COLORS, byref(bits), None, 0)
    dc = gdi32.CreateCompatibleDC(sdc)
    old = gdi32.SelectObject(dc, bmp)
    return dc, bmp, old, bits


def _free_dib(d):
    dc, bmp, old, bits = d
    try:
        if dc and old:
            gdi32.SelectObject(dc, old)
        if dc:
            gdi32.DeleteDC(dc)
        if bmp:
            gdi32.DeleteObject(bmp)
    except Exception:
        pass


def grab_screen(x, y, ww, hh):
    """返回 (buffer, w, h)，32bpp top-down，BGRA"""
    hdc = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, ww, hh)
    old = gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, ww, hh, hdc, x, y, SRCCOPY)

    bi = BITMAPINFO()
    bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = ww
    bi.bmiHeader.biHeight = -hh
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    buf = ctypes.create_string_buffer(ww * hh * 4)
    gdi32.GetDIBits(mem, bmp, 0, hh, buf, byref(bi), DIB_RGB_COLORS)

    gdi32.SelectObject(mem, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(None, hdc)
    return buf


def buf_to_image(buf, ww, hh):
    img = P()
    # 0x00022009 = PixelFormat32bppRGB，忽略 alpha 通道
    gdip.GdipCreateBitmapFromScan0(ww, hh, ww * 4, 0x00022009, buf, byref(img))
    return img




def make_dib(bgra, ww, hh):
    """构造 CF_DIB 需要的 bottom-up DIB 数据"""
    stride = ww * 4
    src = bgra.raw if hasattr(bgra, "raw") else bgra
    rows = [src[(hh - 1 - r) * stride:(hh - r) * stride] for r in range(hh)]
    pixel = b"".join(rows)
    header = struct_pack(ww, hh, len(pixel))
    return header + pixel


def struct_pack(ww, hh, size):
    import struct
    return struct.pack("<IiiHHIIiiII", 40, ww, hh, 1, 32, 0, size, 0, 0, 0, 0)


# ============================================================ 自动识别区域
_SKIP_CLASSES = ("Progman", "WorkerW", "SnapLiteOverlay",
                 "SnapLiteTool")

_ACC_LOC = WINFUNCTYPE(c_long, c_void_p, POINTER(c_long), POINTER(c_long),
                       POINTER(c_long), POINTER(c_long), VARIANT)
_ACC_REL = WINFUNCTYPE(c_ulong, c_void_p)


def _cloaked(hwnd):
    v = w.DWORD(0)
    hr = dwmapi.DwmGetWindowAttribute(hwnd, 14, byref(v), sizeof(v))
    return hr == 0 and v.value != 0


def win_bounds(hwnd):
    """窗口的真实可见边界（去掉 Win10 那圈看不见的拖拽边框）"""
    rc = w.RECT()
    hr = dwmapi.DwmGetWindowAttribute(hwnd, 9, byref(rc), sizeof(rc))
    if hr != 0:
        user32.GetWindowRect(hwnd, byref(rc))
    return (rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top)


def window_at(sx, sy, skip_pid):
    """按 z 序找鼠标下最上层的顶层窗口（跳过本程序的窗口）"""
    h = user32.GetTopWindow(None)
    guard = 0
    while h and guard < 3000:
        guard += 1
        nxt = user32.GetWindow(h, GW_HWNDNEXT)
        if user32.IsWindowVisible(h) and not user32.IsIconic(h):
            pid = w.DWORD(0)
            user32.GetWindowThreadProcessId(h, byref(pid))
            if pid.value != skip_pid and not _cloaked(h):
                buf = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(h, buf, 64)
                if buf.value not in _SKIP_CLASSES:
                    r = win_bounds(h)
                    if (r[2] > 0 and r[3] > 0 and r[0] <= sx < r[0] + r[2]
                            and r[1] <= sy < r[1] + r[3]):
                        return h, r
        h = nxt
    return None, None


def msaa_rect(sx, sy):
    """鼠标下的 UI 元素矩形（图像、按钮、面板等会返回各自的区域）"""
    acc = c_void_p()
    var = VARIANT()
    hr = oleacc.AccessibleObjectFromPoint(w.POINT(sx, sy), byref(acc), byref(var))
    if hr != 0 or not acc:
        return None
    try:
        vt = ctypes.cast(acc, POINTER(POINTER(c_void_p)))[0]
        fn = _ACC_LOC(ctypes.cast(vt[22], c_void_p))
        l, t, ww, hh = c_long(), c_long(), c_long(), c_long()
        hr2 = fn(acc, byref(l), byref(t), byref(ww), byref(hh), var)
        if hr2 != 0 or ww.value <= 0 or hh.value <= 0:
            return None
        return (l.value, t.value, ww.value, hh.value)
    finally:
        try:
            _ACC_REL(ctypes.cast(ctypes.cast(acc, POINTER(POINTER(c_void_p)))[0][2],
                                 c_void_p))(acc)
        except Exception:
            pass


class Probe(threading.Thread):
    """后台线程查控件矩形。

    为什么要单独开线程：某些程序的辅助功能接口会卡上好几秒，放在界面线程里
    会把整个截图工具冻住。查不到就退回"按窗口"识别，功能不受影响。
    """

    def __init__(self):
        threading.Thread.__init__(self, daemon=True)
        self._lock = threading.Lock()
        self._req = None
        self._res = None
        self._seq = 0
        self.ready = False

    def request(self, sx, sy):
        with self._lock:
            self._seq += 1
            self._req = (sx, sy, self._seq)
        return self._seq

    def latest(self):
        with self._lock:
            return self._res

    def run(self):
        try:
            ole32.CoInitializeEx(None, 2)      # STA
            self.ready = True
        except Exception:
            return
        while True:
            with self._lock:
                item = self._req
                self._req = None
            if item is None:
                time.sleep(0.015)
                continue
            rect = None
            try:
                rect = msaa_rect(item[0], item[1])
            except Exception:
                rect = None
            with self._lock:
                self._res = (rect, item[2])



# ============================================================ 主程序
class App:
    def __init__(self):
        self.hinst = windll.kernel32.GetModuleHandleW(None)
        self.scale = 1.0
        try:
            if GetDpiForSystem is not None:
                dpi = GetDpiForSystem()
                if dpi:
                    self.scale = max(1.0, dpi / 96.0)
        except Exception:
            pass

        self.vs = (user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
                   user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
                   user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
                   user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))

        # 主面板尺寸
        self.MW = self.px(340)
        self.MH = self.px(148)
        self.RADIUS = self.px(20)

        self.hwnd_main = None
        self.hwnd_overlay = None
        self.hwnd_tool = None
        self.tray = NOTIFYICONDATAW()

        # 快捷键
        self.hotkey_mod, self.hotkey_vk = load_config()
        self.hotkey_ok = False
        self.recording = False
        self.rec_msg = ""
        self.rec_err = False

        # 截图状态
        self.shot_buf = None
        self.shot_raw = None
        self.sel = None
        self.dragging = False
        self.drag_mode = None          # None / "new" / "move" / "resize"
        self.drag_handle = None
        self.sel0 = None               # 拖动开始时的选区
        self.press_pt = None
        self.press_moved = False
        self.mouse = (0, 0)
        self.capturing = False
        self.main_was_visible = False

        # 自动识别（窗口 / 控件）
        self.auto_rect = None
        self.auto_src = ""
        self.win_hwnd = None
        self.elem_rect = None
        self.probe = Probe()
        self.probe.start()
        self.probe_applied = 0
        self.probe_t = 0.0
        self.probe_pt = (0, 0)
        self.mypid = windll.kernel32.GetCurrentProcessId()

        # 标注（画笔 / 马赛克 / 箭头）
        self.shapes = []               # [{t:"pen"|"mosaic"|"arrow", ...}]
        self.mode = None               # None / "pen" / "mosaic" / "arrow"
        self.pending = None            # 拖动中的临时标注
        self.draw_start = None
        self.anno_color = 0            # ANNO_COLORS 下标：红蓝绿黄白黑灰（画笔用前六色）
        self.pen_width = 1             # PEN_WIDTHS 下标：细/中/粗
        self.mosaic_brush = 1          # MOSAIC_BRUSHES 下标：细/中/粗
        self.mosaic_block = 1          # MOSAIC_BLOCKS 下标：小/中/大
        self.undo_stack = []           # 标注快照栈（多级撤销 / 重做）
        self.redo_stack = []
        self.drag_snap = None          # 本次拖动按下时的快照
        self.nudge_t = 0.0             # 方向键微调的时间戳（连续微调算一步撤销）
        self.sel_shape = None          # 当前选中的标注（箭头，可拖动 / 改样式）
        self.shape0 = None
        self.drag_end = 0              # 箭头端点拖动：1=起点 2=终点

        # 离屏缓冲（消除闪烁的关键）
        self.dc_shot = self.dc_dim = self.dc_back = None
        self.bmp_shot = self.bmp_dim = self.bmp_back = None
        self.hdc_screen = None
        self._sdc = None               # 常驻的屏幕 DC（见 _get_screen_dc）
        self._mos_cache = {}           # 马赛克块的平均色缓存（每次截图重置）

        # 交互 hover 状态
        self.hover_main = None
        self.hover_tool = -1
        self.tracking_main = False
        self.tracking_tool = False

        # 工具条逐像素呈现表面
        self.tb_dc = self.tb_bmp = self.tb_old = self.tb_bits = None
        self.tb_size = None
        self.tb_key = None             # 布局签名（模式一换就变，见 _tool_layout_key）
        self.tb_mask = self.tb_edge = None
        self.tb_scale = None
        self.tb_alpha = None
        self.tb_pos = (0, 0)
        # 毛玻璃底：缓存「该位置的桌面模糊图 + 按钮形状覆盖率」
        self.tb_glass = None           # 玻璃底 bytes（W×H×4）
        self.tb_glass_key = None       # (x, y, W, H, sel) —— 位置/尺寸/选区变了才重算
        self.tb_cov = None             # 按钮形状覆盖率（只跟版式有关）
        self.tb_cov_key = None
        # True：玻璃底取「原图」（磨砂看得出来，跟主界面一致）；
        # False：取遮罩里压暗后的画面（几乎看不出磨砂）。见 tools/preview_tool_glass.py
        self.use_shot_glass = True

        # 主窗口逐像素呈现表面（液态玻璃）
        self.mn_dc = self.mn_bmp = self.mn_old = self.mn_bits = None
        self.mn_size = None
        # 截图瞬间主窗口那一帧的拷贝 + 窗口在屏幕上的位置：
        # 抓屏前主窗口要提交全透明帧（不然会抓到自己），那块像素就没了，
        # 靠这两样在遮罩里把“桌面上的主界面”还原出来。
        self.mn_keep = None
        self.mn_keep_size = None
        self.main_rect = None
        self.mn_glass = None           # 模糊后的桌面底（BGRA 原始字节）
        self.mn_cov = None             # 圆角覆盖率缓存
        self.mn_cov_key = None
        # 拖动中的玻璃底来源：一张全屏缩略图。
        # 拖窗时每帧都去抓屏是不行的（抓屏前必须先把自己变透明 → 会闪），
        # 所以改成「拖动开始时抓一整屏、缩到 1/13 存着，之后按新位置从里面裁」。
        self.drag_small = None         # bytes：全屏缩略图
        self.drag_small_size = None    # (sw, sh)
        self.drag_scale = (1.0, 1.0)   # 屏幕像素 / 缩略图像素
        self.drag_active = False
        self.drag_last_pos = None      # 上次提交时的窗口位置（位置没变就不重画）

        # 自己接管的窗口拖动（不走系统拖动循环，位置和背景才能同帧提交）
        self.main_drag = False
        self.main_drag_off = (0, 0)    # 按下时鼠标相对窗口左上角的偏移

        # 窗口过程统一兜一层异常：ctypes 回调里抛出的异常会被 ctypes 静默吞掉
        # （只在 stderr 打一行 "Exception ignored"），用户和开发者都看不到现场，
        # 窗口还会留在半损坏状态。统一接住 → 写日志 → 返回 0。
        self._proc_main = WNDPROC(_safe_proc(self._on_main))
        self._proc_overlay = WNDPROC(_safe_proc(self._on_overlay))
        self._proc_tool = WNDPROC(_safe_proc(self._on_tool))
        self._brush_cursors = {}       # 笔头档位 → 自绘光标句柄

        self._init_gdiplus()
        self._register_classes()

    # ---------- 工具
    def _pen_w(self):
        """当前画笔粗细（像素）。索引一律夹在合法范围内，避免 IndexError"""
        i = max(0, min(self.pen_width, len(PEN_WIDTHS) - 1))
        return self.px(PEN_WIDTHS[i])

    def _mosaic_r(self):
        """当前马赛克笔头半径（像素）"""
        i = max(0, min(self.mosaic_brush, len(MOSAIC_BRUSHES) - 1))
        return self.px(MOSAIC_BRUSHES[i])

    def _mosaic_blk(self):
        """当前马赛克块大小（像素）"""
        i = max(0, min(self.mosaic_block, len(MOSAIC_BLOCKS) - 1))
        return self.px(MOSAIC_BLOCKS[i])

    def _color_idx(self):
        """当前标注颜色下标（红蓝绿黄白黑灰）"""
        return max(0, min(self.anno_color, len(ANNO_COLORS) - 1))

    def px(self, v):
        return int(round(v * self.scale))

    def _init_gdiplus(self):
        tok = P()
        si = GdiplusStartupInput(1, None, False, False)
        gdip.GdiplusStartup(byref(tok), byref(si), None)

    def _load_cursor(self, cid):
        return user32.LoadCursorW(None, c_void_p(cid))

    def _brush_cursor(self):
        """按当前笔头档位取圆环光标（每个档位各建一次，之后复用）"""
        i = max(0, min(self.mosaic_brush, len(MOSAIC_BRUSHES) - 1))
        h = self._brush_cursors.get(i)
        if h is None:
            h = self._make_brush_cursor(self.px(MOSAIC_BRUSHES[i]))
            self._brush_cursors[i] = h
        return h

    def _pen_cursor(self):
        """画笔：圆形笔头光标。半径 = 笔迹实际半径（笔宽的一半），
        不然光标圈会比画出来的线粗一圈，看着对不上。
        复用马赛克的光标生成，键带前缀避免冲突。"""
        i = max(0, min(self.pen_width, len(PEN_WIDTHS) - 1))
        key = ("pen", i)
        h = self._brush_cursors.get(key)
        if h is None:
            h = self._make_brush_cursor(self.px(PEN_WIDTHS[i]) / 2.0, min_r=2.0)
            self._brush_cursors[key] = h
        return h

    def _make_brush_cursor(self, r_px, min_r=5.0):
        """自绘圆形马赛克笔头光标。

        走 GDI+ 的 ARGB 离屏位图 + AntiAlias 描边，边缘是真正抗锯齿的柔和过渡
        （老版本按像素距离硬判定 alpha 0/255，所以一圈都是台阶锯齿）。
        画完把“直通 alpha”预乘成系统光标位图要的形式，再 CreateIconIndirect。"""
        r = max(float(min_r), float(r_px))
        pad = 3.0
        C = int(r * 2 + pad * 2) + 2
        if C % 2 == 0:
            C += 1
        c0 = C / 2.0

        stride = C * 4
        buf = ctypes.create_string_buffer(stride * C)
        bmp = P()
        rc = gdip.GdipCreateBitmapFromScan0(C, C, stride, 0x0026200A,     # 32bppARGB
                                            ctypes.cast(buf, c_void_p), byref(bmp))
        if rc != 0 or not bmp:
            return None
        gimg = P()
        gdip.GdipGetImageGraphicsContext(bmp, byref(gimg))
        g = GFX(ptr=gimg)

        # 黑白双环：外圈黑 + 紧贴其内的白环。白环保证深色底上看得见，
        # 黑边保证浅色底上看得见；两环都不重叠，避免 AA 混成灰糊一片
        w = max(1.8, r * 0.09)
        pen = P()
        gdip.GdipCreatePen1(0xFF0A0A0E, w, 0, byref(pen))
        gdip.GdipDrawEllipse(g.g, pen, c0 - r, c0 - r, r * 2.0, r * 2.0)
        gdip.GdipDeletePen(pen)
        rr = max(1.0, r - w)
        pen = P()
        gdip.GdipCreatePen1(0xFFFFFFFF, w, 0, byref(pen))
        gdip.GdipDrawEllipse(g.g, pen, c0 - rr, c0 - rr, rr * 2.0, rr * 2.0)
        gdip.GdipDeletePen(pen)

        # 中心点：笔头正中对齐哪里一目了然
        g.fill_circle(0xFF0A0A0E, c0, c0, 1.9)
        g.fill_circle(0xFFFFFFFF, c0, c0, 1.0)

        g.close()
        gdip.GdipDisposeImage(bmp)

        # 直通 alpha -> 预乘 alpha（系统按预乘解释 32bpp 光标）
        raw = bytearray(buf.raw)
        for i in range(3, len(raw), 4):
            a = raw[i]
            if a != 255:
                if a == 0:
                    raw[i - 3] = raw[i - 2] = raw[i - 1] = 0
                else:
                    raw[i - 3] = raw[i - 3] * a // 255
                    raw[i - 2] = raw[i - 2] * a // 255
                    raw[i - 1] = raw[i - 1] * a // 255

        hdc = user32.GetDC(None)
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = C
        bi.bmiHeader.biHeight = -C
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0
        bits = c_void_p()
        hbm = gdi32.CreateDIBSection(hdc, byref(bi), DIB_RGB_COLORS, byref(bits), None, 0)
        if not hbm:
            user32.ReleaseDC(None, hdc)
            return None
        ctypes.memmove(bits, bytes(raw), len(raw))

        # AND 掩码全 0（使用颜色位图的 alpha），1bpp 每行按 2 字节对齐
        mstride = ((C + 15) // 16) * 2
        mask_bits = ctypes.create_string_buffer(mstride * C)
        hmsk = gdi32.CreateBitmap(C, C, 1, 1, ctypes.cast(mask_bits, c_void_p))

        ii = ICONINFO(False, C // 2, C // 2, hmsk, hbm)
        hcur = user32.CreateIconIndirect(byref(ii))
        gdi32.DeleteObject(hmsk)
        gdi32.DeleteObject(hbm)
        user32.ReleaseDC(None, hdc)
        return hcur

    def _register_classes(self):
        self.hcur_arrow = self._load_cursor(IDC_ARROW)
        self.hcur_cross = self._load_cursor(IDC_CROSS)
        self.hcur_move = self._load_cursor(IDC_SIZEALL)
        self.hcur_nwse = self._load_cursor(IDC_SIZENWSE)
        self.hcur_nesw = self._load_cursor(IDC_SIZENESW)
        self.hcur_we = self._load_cursor(IDC_SIZEWE)
        self.hcur_ns = self._load_cursor(IDC_SIZENS)
        specs = [
            ("SnapLiteMain", self._proc_main, self.hcur_arrow),
            ("SnapLiteOverlay", self._proc_overlay, self.hcur_cross, 0x0008),
            ("SnapLiteTool", self._proc_tool, self.hcur_arrow),
        ]
        for spec in specs:
            name, proc, cur = spec[0], spec[1], spec[2]
            style = spec[3] if len(spec) > 3 else 0
            wc = WNDCLASSEXW()
            wc.cbSize = sizeof(WNDCLASSEXW)
            wc.style = style                  # 不设 HREDRAW/VREDRAW，避免额外整窗失效
            wc.lpfnWndProc = proc
            wc.hInstance = self.hinst
            wc.hCursor = cur
            wc.hbrBackground = None
            wc.lpszClassName = name
            user32.RegisterClassExW(byref(wc))

    # ================================================== 快捷键
    def _hotkey_label(self):
        return hotkey_label(self.hotkey_mod, self.hotkey_vk)

    def _current_mod(self):
        mod = 0
        if user32.GetKeyState(VK_CONTROL) & 0x8000:
            mod |= MOD_CONTROL
        if user32.GetKeyState(VK_MENU) & 0x8000:
            mod |= MOD_ALT
        if user32.GetKeyState(VK_SHIFT) & 0x8000:
            mod |= MOD_SHIFT
        if user32.GetKeyState(VK_LWIN) & 0x8000 or user32.GetKeyState(VK_RWIN) & 0x8000:
            mod |= MOD_WIN
        return mod

    def _register_hotkey(self):
        self.hotkey_ok = bool(user32.RegisterHotKey(None, HOTKEY_ID,
                                                    self.hotkey_mod, self.hotkey_vk))
        return self.hotkey_ok

    def _begin_record(self):
        if self.recording:
            return
        self.recording = True
        self.rec_msg = ""
        self.rec_err = False
        user32.UnregisterHotKey(None, HOTKEY_ID)   # 录制期间先释放，避免自己触发
        user32.SetForegroundWindow(self.hwnd_main)
        user32.SetFocus(self.hwnd_main)
        user32.InvalidateRect(self.hwnd_main, None, False)

    def _end_record(self):
        if not self.recording:
            return
        self.recording = False
        self.rec_msg = ""
        self.rec_err = False
        self._register_hotkey()
        if self.hwnd_main:
            user32.InvalidateRect(self.hwnd_main, None, False)

    def _on_record_key(self, vk):
        if vk == VK_ESCAPE:
            self._end_record()
            return
        if vk in MODIFIER_VKS:
            return
        mod = self._current_mod()
        if mod == 0 and not (0x70 <= vk <= 0x87):
            self.rec_msg = "需配合 Ctrl / Alt"
            self.rec_err = True
            user32.InvalidateRect(self.hwnd_main, None, False)
            return
        if not self._try_hotkey(mod, vk):
            self.rec_msg = "该组合已被占用"
            self.rec_err = True
            user32.InvalidateRect(self.hwnd_main, None, False)
            return
        # 成功
        self.hotkey_mod, self.hotkey_vk = mod, vk
        self.recording = False
        self.rec_msg = ""
        self.rec_err = False
        self.hotkey_ok = True
        save_config(mod, vk)
        self._update_tray_tip()
        user32.InvalidateRect(self.hwnd_main, None, False)
        self._balloon("快捷键已更新", self._hotkey_label())

    def _try_hotkey(self, mod, vk):
        user32.UnregisterHotKey(None, HOTKEY_ID)
        if user32.RegisterHotKey(None, HOTKEY_ID, mod, vk):
            return True
        # 失败的组合要放弃，恢复原来的
        user32.RegisterHotKey(None, HOTKEY_ID, self.hotkey_mod, self.hotkey_vk)
        return False

    # ================================================== 主面板
    def create_main(self):
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        x = (sw - self.MW) // 2
        y = max(0, (sh - self.MH) // 2 - self.px(60))
        self.hwnd_main = user32.CreateWindowExW(
            WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_LAYERED, "SnapLiteMain", APP_NAME,
            WS_POPUP, x, y, self.MW, self.MH, None, None, self.hinst, None)
        if not self.hwnd_main:
            return
        # 先把背后的真实桌面抓下来做成玻璃底，再上屏 —— 避免先黑一下再变玻璃
        self._refresh_glass(blank=False)
        user32.ShowWindow(self.hwnd_main, SW_SHOW)
        dbg("create_main hwnd=%s visible=%s" % (self.hwnd_main,
                                                user32.IsWindowVisible(self.hwnd_main)))

    def _main_layout(self):
        W, H = self.MW, self.MH
        sz = self.px(26)
        gap = self.px(4)
        bx = W - self.px(16) - sz
        by = self.px(11)
        close = (bx, by, sz, sz)
        hide = (bx - gap - sz, by, sz, sz)
        btn = (self.px(18), self.px(46), W - self.px(36), self.px(46))
        hot = (self.px(18), self.px(100), W - self.px(36), self.px(30))
        return close, hide, btn, hot

    # ---------- 液态玻璃：主窗口用逐像素 alpha 分层表面
    def _paint_main(self, hwnd):
        ps = PAINTSTRUCT()
        user32.BeginPaint(hwnd, byref(ps))
        user32.EndPaint(hwnd, byref(ps))
        if self.capturing:
            # 截图期间主窗口必须停在全透明那一帧：这里一旦画回来，真窗口就会
            # 重新出现在遮罩里（透明副本 + 真窗口叠在一起，边缘和模糊全对不上）
            return
        self._present_main()

    def _get_screen_dc(self):
        """屏幕 DC：惰性创建一次，之后一直复用。

        原来各处写的是 `self.hdc_screen or user32.GetDC(None)` —— 截图之前
        hdc_screen 还是 None，于是每画一帧就 GetDC 一次却从不 ReleaseDC，
        拖动几十秒就能把进程的 GDI 句柄耗光。"""
        if self.hdc_screen:
            return self.hdc_screen
        if not self._sdc:
            self._sdc = user32.GetDC(None)
        return self._sdc

    def _release_screen_dc(self):
        if self._sdc:
            try:
                user32.ReleaseDC(None, self._sdc)
            except Exception:
                pass
            self._sdc = None

    def _ensure_main_surface(self):
        W, H = self.MW, self.MH
        if self.mn_dc and self.mn_size == (W, H):
            return
        self._release_main_surface()
        sdc = self._get_screen_dc()
        self.mn_dc, self.mn_bmp, self.mn_old, self.mn_bits = _new_dib(sdc, W, H)
        self.mn_size = (W, H)

    def _release_main_surface(self):
        if self.mn_dc:
            _free_dib((self.mn_dc, self.mn_bmp, self.mn_old, self.mn_bits))
            self.mn_dc = None
        self.mn_bmp = self.mn_old = self.mn_bits = None
        self.mn_size = None
        if self.mn_keep:
            _free_dib(self.mn_keep)
            self.mn_keep = None
        self.mn_keep_size = None

    def _blit_main(self, dst=None):
        """提交一帧。

        dst 传 None：只换内容，绝不动窗口位置。
        dst 传 POINT：**位置和内容在同一次提交里一起给系统** —— 拖动时用这个，
        DWM 会在同一个合成帧里同时移动窗口和换掉背景，两者不会错开一帧。

        注意 pptDst 一旦传值就意味着「把窗口搬到该坐标」，所以平时重绘必须
        传 None。上一版就是在这里固定传了 POINT(0,0)，才让窗口每次重绘都跑到
        屏幕左上角。"""
        if not (self.hwnd_main and self.mn_dc):
            return
        W, H = self.mn_size
        size = w.SIZE(W, H)
        pt_src = w.POINT(0, 0)
        bf = BLENDFUNCTION(0, 0, 255, 1)      # AC_SRC_OVER + AC_SRC_ALPHA
        sdc = self._get_screen_dc()
        user32.UpdateLayeredWindow(self.hwnd_main, sdc,
                                   byref(dst) if dst is not None else None,
                                   byref(size),
                                   self.mn_dc, byref(pt_src), 0, byref(bf), 2)

    def _present_blank(self):
        """提交一帧全透明：让 DWM 先把窗口从屏幕上抹掉，这样紧接着的 BitBlt
        抓到的是桌面本身，而不是我们自己的窗口（否则会抓到自己 → 套娃）"""
        if not self.hwnd_main:
            return
        self._ensure_main_surface()
        W, H = self.mn_size
        ctypes.memset(self.mn_bits, 0, W * H * 4)
        self._blit_main()

    def _present_main(self, dst=None):
        if not self.hwnd_main:
            return
        self._ensure_main_surface()
        W, H = self.mn_size
        total = W * H * 4
        ctypes.memset(self.mn_bits, 0, total)
        g = GFX(self.mn_dc)
        try:
            glass = self.mn_glass
            if glass and len(glass) == total:
                ctypes.memmove(self.mn_bits, glass, total)
            else:
                # 抓不到桌面时退化成实底，保证文字一定可读
                g.fill_round(0xFF14161A, 0, 0, W, H, self.RADIUS)
            self._draw_main(g, W, H)
        finally:
            g.close()
        self._apply_round_mask(W, H)
        self._blit_main(dst)

    # ---------- 截图时把主窗口“画回”桌面
    def _keep_main_surface(self):
        """把主窗口当前这一帧拷一份留底。

        _present_blank() 会把 mn_bits 清零（否则抓屏会抓到自己），所以这份
        拷贝是遮罩里还原主界面的唯一来源。"""
        if not (self.mn_dc and self.mn_bits):
            self.mn_keep = None
            return
        W, H = self.mn_size
        if not (self.mn_keep and self.mn_keep_size == (W, H)):
            if self.mn_keep:
                _free_dib(self.mn_keep)
                self.mn_keep = None
            dib = _new_dib(self._get_screen_dc(), W, H)
            if not (dib[0] and dib[3]):
                _free_dib(dib)                # GDI 句柄耗尽 → 不补了，遮罩那块留桌面
                self.mn_keep_size = None
                return
            self.mn_keep = dib
            self.mn_keep_size = (W, H)
        ctypes.memmove(self.mn_keep[3], self.mn_bits, W * H * 4)

    def _blend_main_into_shot(self):
        """把留底的主窗口像素合进 dc_shot。

        抓屏时主窗口是全透明的，所以 dc_shot 那块本来是「桌面本身」。不补回来，
        遮罩一盖上去那块就变回桌面 —— 看起来就是主界面“消失”。补进去之后它
        就是桌面上一个普通窗口：没框到时跟着压暗变灰，框到时随选区一起变亮，
        跟眼睛看到的一致。"""
        if not (self.main_rect and self.mn_keep and self.dc_shot):
            return
        if not (self.mn_keep[0] and self.mn_keep[3]):
            return
        mx, my, mw, mh = self.main_rect
        vsx, vsy, vsw, vsh = self.vs
        x0, y0 = max(mx, vsx), max(my, vsy)
        x1, y1 = min(mx + mw, vsx + vsw), min(my + mh, vsy + vsh)
        if x1 <= x0 or y1 <= y0:
            return                        # 窗口完全在虚拟屏外（多屏拔掉等情况）
        ww, hh = x1 - x0, y1 - y0
        dx, dy = x0 - vsx, y0 - vsy       # dc_shot 内的落点
        sx, sy = x0 - mx, y0 - my         # mn_keep 内的取点
        ok = 0
        if msimg32:
            bf = BLENDFUNCTION(0, 0, 255, 1)      # AC_SRC_OVER + AC_SRC_ALPHA
            ok = msimg32.AlphaBlend(self.dc_shot, dx, dy, ww, hh,
                                    self.mn_keep[0], sx, sy, ww, hh, bf)
        if not ok:
            # AlphaBlend 不可用（极老的系统 / 句柄耗尽）→ 直接贴，至少不是空洞；
            # 代价只是圆角边缘多出一圈黑点，肉眼几乎看不出
            gdi32.BitBlt(self.dc_shot, dx, dy, ww, hh, self.mn_keep[0], sx, sy, SRCCOPY)

    def _refresh_glass(self, blank=True):
        """抓主窗口背后的真实桌面 → 模糊 → 当玻璃底。"""
        if not self.hwnd_main:
            return
        W, H = self.MW, self.MH
        if blank:
            self._present_blank()
            try:
                dwmapi.DwmFlush()          # 等这一帧真的合成到屏幕上
            except Exception:
                pass
        rc = w.RECT()
        user32.GetWindowRect(self.hwnd_main, byref(rc))
        raw = None
        try:
            raw = grab_screen(rc.left, rc.top, W, H).raw
        except Exception as e:
            dbg("glass grab failed: %r" % (e,))
        if raw and len(raw) == W * H * 4:
            try:
                self.mn_glass = self._blur_bgra(raw, W, H)
            except Exception as e:
                dbg("glass blur failed: %r" % (e,))
                self.mn_glass = None
        else:
            self.mn_glass = None
        self._present_main()

    def _blur_bgra(self, raw, W, H):
        """缩到 1/f 再用 GDI+ 双三次插值放回来 → 平滑模糊（零第三方依赖）

        注意：不能用 GDI 的 StretchBlt + HALFTONE，32bpp 目标下会退化成
        最近邻，放大会出现明显马赛克方块。"""
        sdc = self._get_screen_dc()
        f = 13
        sw, sh = max(2, W // f), max(2, H // f)
        made = []
        imgs = []
        try:
            src = _new_dib(sdc, W, H)
            made.append(src)
            ctypes.memmove(src[3], raw, W * H * 4)

            small = _new_dib(sdc, sw, sh)
            made.append(small)
            g1 = GFX(small[0])
            img = P()
            gdip.GdipCreateBitmapFromScan0(W, H, W * 4, 0x00022009, src[3], byref(img))
            imgs.append(img)
            gdip.GdipDrawImageRectI(g1.g, img, 0, 0, sw, sh)
            g1.close()

            raw_small = ctypes.string_at(small[3], sw * sh * 4)
            out = _new_dib(sdc, W, H)
            made.append(out)
            g2 = GFX(out[0])
            img2 = P()
            # 注意：GdipCreateBitmapFromScan0 不拷贝像素，只接管指针。
            # 这里必须用变量持有这块缓冲，否则它被回收后 GDI+ 仍然去读 → 随机崩溃。
            hold = ctypes.create_string_buffer(raw_small, len(raw_small))
            gdip.GdipCreateBitmapFromScan0(sw, sh, sw * 4, 0x00022009,
                                           hold, byref(img2))
            imgs.append(img2)
            gdip.GdipDrawImageRectI(g2.g, img2, 0, 0, W, H)
            g2.close()
            data = bytearray(ctypes.string_at(out[3], W * H * 4))
        finally:
            for im in imgs:
                try:
                    gdip.GdipDisposeImage(im)
                except Exception:
                    pass
            for d in reversed(made):
                _free_dib(d)
        data[3::4] = b"\xff" * (W * H)     # 玻璃底完全不透明（压暗在绘制时叠）
        return bytes(data)

    # ---------- 拖动中的实时玻璃：全屏缩略图 + 按位置裁剪
    BLUR_F = 13          # 模糊强度：缩到 1/13 再放回来

    def _begin_drag_glass(self):
        """开始拖动：趁窗口还在原位，抓一整屏桌面存成缩略图。

        之后拖动过程中不再抓屏，只从这张缩略图里裁 —— 既跟手，又不会闪。

        这里不再「先提交一帧全透明再等 DWM 合成」：那一趟要 40~50ms，全花在
        按下的瞬间，手上一按就发粘。现在直接抓，抓到的画面里会带上我们自己
        的窗口，抓完用 _patch_own_window 把那块补掉。"""
        if not self.hwnd_main or self.drag_active:
            return

        # 重新读一次虚拟屏幕：拖动中插拔显示器的情况也要照顾到
        self.vs = (user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
                   user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
                   user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
                   user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        vx, vy, vw, vh = self.vs
        if vw < 16 or vh < 16:
            self._present_main()
            return

        f = self.BLUR_F
        sw, sh = max(2, vw // f), max(2, vh // f)
        small = None
        try:
            d = _new_dib(self._get_screen_dc(), sw, sh)
            try:
                gdi32.SetStretchBltMode(d[0], 4)       # HALFTONE：缩小要平均采样
                ok = gdi32.StretchBlt(d[0], 0, 0, sw, sh,
                                      self._get_screen_dc(), vx, vy, vw, vh, SRCCOPY)
                if ok:
                    small = ctypes.string_at(d[3], sw * sh * 4)
            finally:
                _free_dib(d)
        except Exception as e:
            dbg("drag glass grab failed: %r" % (e,))
        if not small or len(small) != sw * sh * 4:
            self._end_drag_glass()
            self._present_main()
            return

        small = self._patch_own_window(small, sw, sh, vx, vy, vw, vh)
        self.drag_small = small
        self.drag_small_size = (sw, sh)
        self.drag_scale = (vw / float(sw), vh / float(sh))
        self.drag_active = True
        self.drag_last_pos = None
        self._present_main()

    def _patch_own_window(self, small, sw, sh, vx, vy, vw, vh):
        """把缩略图里「我们自己窗口」占的那块补掉。

        抓屏时窗口还在原位，不先把它藏起来（那一趟要 40ms 且会闪），抓到的
        画面里就带着自己；等窗口被拖走、原位置重新露出来时，那块背景会变成
        自己的残影。缩略图只有 200×110 左右，窗口占的那一小块用该行左右两侧
        的外部像素做水平插值补上，放大模糊后看不出接缝。"""
        try:
            rc = w.RECT()
            user32.GetWindowRect(self.hwnd_main, byref(rc))
            kx = vw / float(sw)
            ky = vh / float(sh)
            x0 = max(0, min(sw, int(round((rc.left - vx) / kx))))
            x1 = max(0, min(sw, int(round((rc.right - vx) / kx))))
            y0 = max(0, min(sh, int(round((rc.top - vy) / ky))))
            y1 = max(0, min(sh, int(round((rc.bottom - vy) / ky))))
            if x1 - x0 < 1 or y1 - y0 < 1:
                return small

            buf = bytearray(small)
            span = float(x1 - x0)
            for y in range(y0, y1):
                lx = x0 - 1 if x0 > 0 else min(sw - 1, x1)
                rx = x1 if x1 < sw else lx
                lo = (y * sw + lx) * 4
                hi = (y * sw + rx) * 4
                b0, g0, r0 = buf[lo], buf[lo + 1], buf[lo + 2]
                b1, g1, r1 = buf[hi], buf[hi + 1], buf[hi + 2]
                for x in range(x0, x1):
                    t = (x - x0 + 0.5) / span
                    o = (y * sw + x) * 4
                    buf[o] = int(b0 + (b1 - b0) * t)
                    buf[o + 1] = int(g0 + (g1 - g0) * t)
                    buf[o + 2] = int(r0 + (r1 - r0) * t)
                    buf[o + 3] = 255
            return bytes(buf)
        except Exception as e:
            dbg("patch own window failed: %r" % (e,))
            return small

    def _end_drag_glass(self):
        self.drag_active = False
        self.drag_small = None
        self.drag_small_size = None

    def _drag_glass_bytes(self, left, top):
        """按窗口将要到达的屏幕位置，从全屏缩略图里裁出对应区域，放大回 W×H

        left/top 是**目标位置**，不是窗口当前所在位置 —— 拖动时窗口还没搬
        过去，位置由我们自己算出来，先按目标位置取背景，再和新位置一起提交。

        裁剪在 Python 里按行拼接完成，只用 GdipDrawImageRectI 这一个
        已验证可用的放大接口 —— GdipDrawImageRectRectI 在这套 ctypes
        绑定下会「返回成功但一像素都不画」，不能用。"""
        W, H = self.MW, self.MH
        sw, sh = self.drag_small_size
        vx, vy, _, _ = self.vs
        kx, ky = self.drag_scale

        # 窗口左上角在缩略图坐标系里的**浮点**位置
        fx = (left - vx) / kx
        fy = (top - vy) / ky
        ix, iy = int(floor(fx)), int(floor(fy))

        # 多取 2 个缩略图像素：放大出来要比窗口略大一圈，才留得下亚像素偏移量
        mx = 2
        ceil_w = int(ceil(W / kx)) + mx
        ceil_h = int(ceil(H / ky)) + mx
        x0, y0 = max(0, ix), max(0, iy)
        x1, y1 = min(sw, ix + ceil_w), min(sh, iy + ceil_h)

        sdc = self._get_screen_dc()
        out = _new_dib(sdc, W, H)
        g = GFX(out[0])
        try:
            # 先铺实底：越界区域和裁剪偏差都靠它兜住，不会透出桌面
            g.fill(0xFF14161A, 0, 0, W, H)
        finally:
            g.close()

        if x1 > x0 and y1 > y0:
            ew, eh = x1 - x0, y1 - y0
            # 缩略图 1 像素 = 屏幕 kx 像素，所以放大成 ew*kx × eh*ky 之后，
            # 它和屏幕是 1:1 的 —— 之后按整数屏幕像素平移，画面就能逐像素跟手。
            bw = max(1, int(round(ew * kx)))
            bh = max(1, int(round(eh * ky)))

            crop = bytearray(ew * eh * 4)
            for row in range(y0, y1):
                so = (row * sw + x0) * 4
                do = (row - y0) * ew * 4
                crop[do:do + ew * 4] = self.drag_small[so:so + ew * 4]

            src = _new_dib(sdc, ew, eh)
            big = _new_dib(sdc, bw, bh)
            img = P()
            gb = GFX(big[0])
            try:
                ctypes.memmove(src[3], bytes(crop), ew * eh * 4)
                gdip.GdipCreateBitmapFromScan0(ew, eh, ew * 4, 0x00022009,
                                               src[3], byref(img))
                gdip.GdipDrawImageRectI(gb.g, img, 0, 0, bw, bh)
            finally:
                gb.close()
                if img:
                    try:
                        gdip.GdipDisposeImage(img)
                    except Exception:
                        pass
                _free_dib(src)

            # 亚像素补偿：窗口相对这块放大图偏了多少整数屏幕像素，就往回贴多少。
            # 以前这里把位置 round 到缩略图整格，导致「每走 13px 背景才跳一次」，
            # 慢速拖动时就是一顿一顿的。
            off_x = int(round((fx - x0) * kx))
            off_y = int(round((fy - y0) * ky))
            gdi32.BitBlt(out[0], -off_x, -off_y, bw, bh, big[0], 0, 0, SRCCOPY)
            _free_dib(big)

        data = bytearray(ctypes.string_at(out[3], W * H * 4))
        _free_dib(out)
        data[3::4] = b"\xff" * (W * H)
        return bytes(data)

    def _drag_frame(self, left, top):
        """画拖动中的一帧：先按目标位置算好背景，再把「位置 + 内容」一次提交。

        关键就在这个「一次提交」：如果用系统自带的窗口拖动，窗口位置是系统
        先改的、背景是我们后补的，两者不在同一个合成帧里，DWM 合成时背景
        永远落后窗口一帧 —— 拖得越快错位越明显，看着就是背景追不上手。
        自己接管拖动、位置和内容一起交给 UpdateLayeredWindow，就不存在错位。

        返回 True 表示这一帧画了。"""
        if not (self.drag_active and self.drag_small and self.hwnd_main):
            return False
        pos = (left, top)
        if pos == self.drag_last_pos:
            return False
        self.drag_last_pos = pos
        try:
            self.mn_glass = self._drag_glass_bytes(left, top)
        except Exception as e:
            dbg("drag glass render failed: %r" % (e,))
            return False
        self._present_main(w.POINT(left, top))
        return True

    # ---------- 自己接管的窗口拖动 ----------
    def _start_main_drag(self):
        """按下空白处：记下抓手，趁窗口还在原位把整屏缩略出来。

        抓屏放在「按下」这一下、而不是等第一次移动之后：这时窗口还没动过，
        缩略图里自己的位置是准的；而且用户按下后一般要过一会儿才开始移动，
        这 40ms 就落在空档里，拖起来不会顿在第一帧。
        （试过推迟到移动时再抓，点一下确实省了 40ms，但拖动第一帧要等抓屏，
        会丢掉一个像素的位移 —— 拖动手感比「点一下多花 40ms」重要。）

        不用 ReleaseCapture + WM_NCLBUTTONDOWN 交给系统拖，是因为那样窗口
        位置由系统改、背景由我们补，两件事不在同一帧里 —— 见 _drag_frame。"""
        if self.main_drag or self.capturing:
            return
        rc = w.RECT()
        user32.GetWindowRect(self.hwnd_main, byref(rc))
        p = w.POINT()
        user32.GetCursorPos(byref(p))
        self._begin_drag_glass()
        if not self.drag_active:
            # 抓不到整屏就别拖了：硬拖会得到一个没有背景的空白窗口
            return
        self.main_drag = True
        self.main_drag_off = (p.x - rc.left, p.y - rc.top)
        user32.SetCapture(self.hwnd_main)

    def _move_main_drag(self):
        if not self.main_drag:
            return
        p = w.POINT()
        user32.GetCursorPos(byref(p))
        self._drag_frame(p.x - self.main_drag_off[0],
                         p.y - self.main_drag_off[1])

    def _end_main_drag(self):
        if not self.main_drag:
            return
        self.main_drag = False
        user32.ReleaseCapture()
        self._end_drag_glass()

    def _corner_cov(self, R):
        """圆角像素覆盖率（3×3 超采样），结果缓存，只算一次"""
        if self.mn_cov and self.mn_cov_key == R:
            return self.mn_cov
        cov = []
        for yy in range(R):
            row = []
            for xx in range(R):
                hit = 0
                for sy in range(3):
                    for sx in range(3):
                        dx = R - (xx + (sx + 0.5) / 3.0)
                        dy = R - (yy + (sy + 0.5) / 3.0)
                        if dx * dx + dy * dy <= R * R:
                            hit += 1
                row.append(int(round(hit * 255.0 / 9.0)))
            cov.append(row)
        self.mn_cov, self.mn_cov_key = cov, R
        return cov

    def _apply_round_mask(self, W, H):
        """把四角按圆角覆盖率削掉（含抗锯齿）。只处理 4 个角，
        直边本来就是不透明的，没必要每帧扫全图。"""
        R = int(round(self.RADIUS))
        R = max(2, min(R, min(W, H) // 2))
        cov = self._corner_cov(R)
        buf = bytearray(ctypes.string_at(self.mn_bits, W * H * 4))
        for x0, y0, flip_x, flip_y in ((0, 0, False, False),
                                       (W - R, 0, True, False),
                                       (0, H - R, False, True),
                                       (W - R, H - R, True, True)):
            for j in range(R):
                ly = (R - 1 - j) if flip_y else j
                rowcov = cov[ly]
                base = (y0 + j) * W
                for i in range(R):
                    a = rowcov[(R - 1 - i) if flip_x else i]
                    if a == 255:
                        continue
                    o = (base + x0 + i) * 4
                    if a <= 0:
                        buf[o] = buf[o + 1] = buf[o + 2] = buf[o + 3] = 0
                    else:
                        buf[o] = buf[o] * a // 255          # ULW 要预乘 alpha
                        buf[o + 1] = buf[o + 1] * a // 255
                        buf[o + 2] = buf[o + 2] * a // 255
                        buf[o + 3] = a
        ctypes.memmove(self.mn_bits, bytes(buf), W * H * 4)

    def _draw_main(self, g, W, H):
        R = self.RADIUS
        # 液态玻璃：模糊过的桌面已经垫在下面，这里只叠「压暗层 + 受光高光 + 棱边」
        g.fill_round(0x80101217, 0, 0, W, H, R)
        g.fill_round(0x1FFFFFFF, self.px(1), self.px(1),
                     W - self.px(2), self.px(1.6), max(1.0, R - 1))
        g.stroke_round(0x59FFFFFF, 1, 0.5, 0.5, W - 1, H - 1, R)

        close, hide, btn, hot = self._main_layout()

        # 标题
        g.text(APP_NAME, self.px(19), C_TITLE, self.px(19), self.px(10),
               self.px(120), self.px(28), 0, True)
        tag_x = self.px(126)
        g.fill_round(C_TAG_BG, tag_x, self.px(15), self.px(68), self.px(20), self.px(10))
        g.text("轻量截图", self.px(11), C_SUB, tag_x, self.px(15),
               self.px(68), self.px(20), 1)
        # 版本号
        g.text("v" + APP_VERSION, self.px(10.5), C_HINT, tag_x + self.px(76),
               self.px(15), self.px(72), self.px(20), 0)

        # — 收进托盘
        hot_hide = (self.hover_main == "hide")
        if hot_hide:
            g.fill_round(C_HOVER_SOFT, hide[0], hide[1], hide[2], hide[3], self.px(7))
        hx, hy = hide[0] + hide[2] / 2, hide[1] + hide[3] / 2
        g.line(C_WHITE if hot_hide else C_SUB, max(1.2, self.px(1.4)),
               hx - self.px(5), hy, hx + self.px(5), hy)

        # ✕ 收进托盘（不退出）
        hot_close = (self.hover_main == "close")
        if hot_close:
            g.fill_round(C_HOVER_SOFT, close[0], close[1], close[2], close[3], self.px(7))
        cx, cy = close[0] + close[2] / 2, close[1] + close[3] / 2
        arm = self.px(4.5)
        pen_c = C_WHITE if hot_close else C_SUB
        g.line(pen_c, max(1.2, self.px(1.4)), cx - arm, cy - arm, cx + arm, cy + arm)
        g.line(pen_c, max(1.2, self.px(1.4)), cx - arm, cy + arm, cx + arm, cy - arm)

        # 主按钮
        hot_btn = (self.hover_main == "btn")
        g.fill_round(C_ACCENT_HI if hot_btn else C_ACCENT, btn[0], btn[1], btn[2], btn[3],
                     self.px(12))
        g.text("开始截图", self.px(16), C_WHITE, btn[0] + self.px(20), btn[1],
               self.px(160), btn[3], 0, True)
        # 右侧取景角标（c14 跟着 DPI 走，高分屏不会缩成一小点）
        ic = 0x66FFFFFF
        ix = btn[0] + btn[2] - self.px(30)
        iy = btn[1] + btn[3] / 2 - self.px(7)
        l = self.px(6)
        c14 = self.px(14)
        g.line(ic, max(1.2, self.px(1.4)), ix, iy, ix + l, iy)
        g.line(ic, max(1.2, self.px(1.4)), ix, iy, ix, iy + l)
        g.line(ic, max(1.2, self.px(1.4)), ix + c14, iy, ix + c14 - l, iy)
        g.line(ic, max(1.2, self.px(1.4)), ix + c14, iy, ix + c14, iy + l)
        g.line(ic, max(1.2, self.px(1.4)), ix, iy + c14, ix + l, iy + c14)
        g.line(ic, max(1.2, self.px(1.4)), ix, iy + c14, ix, iy + c14 - l)
        g.line(ic, max(1.2, self.px(1.4)), ix + c14, iy + c14, ix + c14 - l, iy + c14)
        g.line(ic, max(1.2, self.px(1.4)), ix + c14, iy + c14, ix + c14, iy + c14 - l)

        # 快捷键行
        hot_hover = (self.hover_main == "hot")
        if self.recording:
            g.fill_round(0x330A84FF, hot[0], hot[1], hot[2], hot[3], self.px(9))
            g.stroke_round(C_ACCENT, 1, hot[0] + 0.5, hot[1] + 0.5,
                           hot[2] - 1, hot[3] - 1, self.px(9))
            g.text("请按下新的快捷键…", self.px(11.5), C_TITLE,
                   hot[0] + self.px(12), hot[1], self.px(180), hot[3], 0)
            msg = self.rec_msg or "Esc 取消"
            g.text(msg, self.px(11), C_WARN if self.rec_err else C_HINT,
                   hot[0] + hot[2] - self.px(126), hot[1], self.px(114), hot[3], 2)
        else:
            g.fill_round(0x3DFFFFFF if hot_hover else C_TAG_BG,
                         hot[0], hot[1], hot[2], hot[3], self.px(9))
            g.text("快捷键", self.px(11), C_HINT, hot[0] + self.px(12), hot[1],
                   self.px(52), hot[3], 0)
            g.text(self._hotkey_label(), self.px(11.5), C_TITLE,
                   hot[0] + self.px(60), hot[1], self.px(150), hot[3], 0, True)
            if self.hotkey_ok:
                right, rc_ = "修改", (C_ACCENT if hot_hover else C_SUB)
            else:
                right, rc_ = "热键被占用", C_WARN
            g.text(right, self.px(11), rc_,
                   hot[0] + hot[2] - self.px(84), hot[1], self.px(72), hot[3], 2)

    def _on_main(self, hwnd, msg, wp, lp):
        if msg == WM_PAINT:
            self._paint_main(hwnd)
            return 0
        if msg == WM_MOVE:
            # 拖动时位置由 _drag_frame 随内容一次提交，这里不需要再补画。
            # 系统拖动循环（WM_ENTERSIZEMOVE/EXITSIZEMOVE）自接管后不会再来。
            return 0
        if msg == WM_ERASEBKGND:
            return 1
        if msg == WM_TRAY:
            self._on_tray_msg(lp)
            return 0
        if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
            if self.recording:
                self._on_record_key(wp)
                return 0
            if wp == VK_ESCAPE:
                return 0
        if msg == WM_ACTIVATE:
            if (wp & 0xFFFF) == 0 and self.recording:
                self._end_record()
            return 0
        if msg == WM_MOUSEMOVE:
            if self.main_drag:
                self._move_main_drag()
                return 0
            x, y = lo(lp), hi(lp)
            close, hide, btn, hot = self._main_layout()
            hot_id = None
            if self._in(x, y, close):
                hot_id = "close"
            elif self._in(x, y, hide):
                hot_id = "hide"
            elif self._in(x, y, btn):
                hot_id = "btn"
            elif self._in(x, y, hot):
                hot_id = "hot"
            if hot_id != self.hover_main:
                self.hover_main = hot_id
                user32.InvalidateRect(hwnd, None, False)
            if not self.tracking_main:
                self.tracking_main = True
                tme = TRACKMOUSEEVENT(sizeof(TRACKMOUSEEVENT), 2, hwnd, 0)
                user32.TrackMouseEvent(byref(tme))
            return 0
        if msg == WM_MOUSELEAVE:
            self.tracking_main = False
            if self.hover_main is not None:
                self.hover_main = None
                user32.InvalidateRect(hwnd, None, False)
            return 0
        if msg == WM_LBUTTONDOWN:
            x, y = lo(lp), hi(lp)
            close, hide, btn, hot = self._main_layout()
            if self._in(x, y, close):
                # ✕ 只收进托盘，不退出；退出走托盘右键菜单
                user32.ShowWindow(hwnd, SW_HIDE)
                return 0
            if self._in(x, y, hide):
                user32.ShowWindow(hwnd, SW_HIDE)
                return 0
            if self._in(x, y, hot):
                if self.recording:
                    self._end_record()
                else:
                    self._begin_record()
                return 0
            if self._in(x, y, btn):
                self.start_capture()
                return 0
            if self.recording:
                self._end_record()
                return 0
            # 空白处可拖动窗口（自己接管，位置与背景同帧提交，见 _drag_frame）
            self._start_main_drag()
            return 0
        if msg == WM_LBUTTONUP:
            if self.main_drag:
                self._move_main_drag()     # 抬手那一下先对齐到最终位置
                self._end_main_drag()
                return 0
        if msg == WM_CAPTURECHANGED:
            # 捕获被系统或别的窗口抢走（Alt+Tab、弹窗…）→ 立刻收尾，
            # 否则窗口会一直黏在鼠标上
            if self.main_drag:
                self._end_main_drag()
                return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _in(self, x, y, rect):
        return rect[0] <= x < rect[0] + rect[2] and rect[1] <= y < rect[1] + rect[3]

    def _show_panel(self):
        if not self.hwnd_main:
            return
        # 从托盘唤回时重抓玻璃底：藏在托盘期间桌面可能已经变了
        self._refresh_glass()
        user32.ShowWindow(self.hwnd_main, SW_SHOW)
        user32.SetForegroundWindow(self.hwnd_main)
        user32.InvalidateRect(self.hwnd_main, None, False)

    # ================================================== 截图流程
    def start_capture(self):
        if self.capturing:
            return
        # 拖动中途被热键打断时，鼠标捕获还挂在主窗口上 —— 不先收尾的话，
        # 遮罩收不到鼠标（框选失效），主界面还会继续跟着光标跑。
        self._end_main_drag()
        self.capturing = True
        try:
            self._begin_capture()
        except Exception as e:
            # 这里一旦漏出去，异常会被窗口过程吞掉，而 capturing 停在 True、
            # 主窗口还停在“已提交全透明帧”的状态 —— 界面直接消失，热键也
            # 再也唤不出来，只能重启。所以必须自己兜住并复原。
            dbg("start_capture failed: %r" % (e,))
            self._abort_capture()

    def _abort_capture(self):
        """截图流程中途失败时复原，让程序回到「可以直接再截一次」的状态。"""
        self.main_rect = None
        try:
            if self.hwnd_overlay:
                user32.DestroyWindow(self.hwnd_overlay)
                self.hwnd_overlay = None
            self._close_tool()
            self._release_capture_buffers()
        finally:
            self.capturing = False
            if self.hwnd_main:
                self._present_main()      # 开头提交过一帧全透明，这里必须画回来

    def _begin_capture(self):
        if self.recording:
            self._end_record()
        self.main_was_visible = bool(self.hwnd_main and user32.IsWindowVisible(self.hwnd_main))
        self.main_rect = None
        if self.hwnd_main:
            # 不 SW_HIDE：窗口保持“可见”状态，只是这一瞬间提交一帧全透明，
            # 这样抓屏拿不到我们自己的窗口（截图里不会出现 SnapLite），
            # 但也不会出现「隐藏 → 弹出」那种闪烁和位置变化。
            #
            # 代价是屏幕上那块像素也没了，而遮罩要画“桌面上的主界面”，所以：
            # 提交透明帧之前先把这一帧像素和窗口位置留底，抓完屏再补回抓屏图里。
            # 之后主窗口一直保持全透明，由遮罩里的副本顶替显示 —— 这样它就跟
            # 桌面上一个普通窗口一样：没框到时跟着变灰，框到时随选区变亮。
            if self.main_was_visible:
                self._present_main()          # 确保留底的是屏幕上真正那一帧
                rc = w.RECT()
                user32.GetWindowRect(self.hwnd_main, byref(rc))
                self.main_rect = (rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top)
                self._keep_main_surface()
            self._present_blank()
            try:
                dwmapi.DwmFlush()
            except Exception:
                time.sleep(0.05)

        # 每次截图前重新读虚拟屏幕尺寸：运行中插拔显示器、改分辨率或改缩放后，
        # 用启动时的旧尺寸会让遮罩盖不满（露白边），抓屏和裁剪也跟着错位。
        self.vs = (user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
                   user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
                   user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
                   user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))

        self._build_capture_buffers()
        self.sel = None
        self.dragging = False
        self.drag_mode = None
        self.drag_handle = None
        self.sel0 = None
        self.press_pt = None
        self.press_moved = False
        self.shapes = []
        self.sel_shape = None
        self.mode = None
        self.drag_end = 0
        self.undo_stack = []
        self.redo_stack = []
        self.drag_snap = None
        self.auto_rect = None
        self.auto_src = ""
        self.win_hwnd = None
        self.elem_rect = None
        self.probe_applied = 0
        self.probe_t = 0.0

        vsx, vsy, vsw, vsh = self.vs
        self.hwnd_overlay = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED, "SnapLiteOverlay", "",
            WS_POPUP, vsx, vsy, vsw, vsh, None, None, self.hinst, None)
        if not self.hwnd_overlay:
            raise RuntimeError("overlay window create failed")
        # 先把整帧合成好，再用 UpdateLayeredWindow 一次性呈现 → 无白屏/黑屏过渡帧
        cx, cy = vsw // 2, vsh // 2
        self.mouse = (cx, cy)
        self._update_auto(cx, cy)
        self._compose_overlay(vsw, vsh)
        self._present_overlay()
        user32.ShowWindow(self.hwnd_overlay, SW_SHOWNA)
        user32.SetForegroundWindow(self.hwnd_overlay)
        user32.SetFocus(self.hwnd_overlay)
        # 主界面这里**不再**提 Z 序：它现在是全透明的一帧，画面由遮罩里的
        # 副本负责。以前为了让主界面一直亮着把它顶到遮罩之上，结果不管有没有
        # 框到它都是置顶的（不会变灰），而且之后任何一次 Z 序重排（展开工具条
        # 选项排、点遮罩…）都可能把它压到整屏遮罩下面 —— 看起来就是“主界面
        # 莫名消失”，取消截图才回来。

    def _present_overlay(self):
        """把 dc_back 的内容原子提交到分层窗口（ULW_OPAQUE，不涉及 alpha 通道）"""
        if not (self.hwnd_overlay and self.dc_back):
            return
        pt_dst = w.POINT(self.vs[0], self.vs[1])
        size = w.SIZE(self.vs[2], self.vs[3])
        pt_src = w.POINT(0, 0)
        user32.UpdateLayeredWindow(self.hwnd_overlay, self.hdc_screen,
                                   byref(pt_dst), byref(size), self.dc_back,
                                   byref(pt_src), 0, None, 0x00000001)  # ULW_OPAQUE

    # ---------- 离屏缓冲
    def _build_capture_buffers(self):
        self._release_capture_buffers()
        vsx, vsy, vsw, vsh = self.vs
        self.shot_buf = grab_screen(vsx, vsy, vsw, vsh)
        self.shot_raw = self.shot_buf.raw        # 马赛克取色用（只读一次，避免每块都复制）
        self._mos_cache = {}

        sdc = user32.GetDC(None)
        self.hdc_screen = sdc
        for name in ("shot", "dim"):
            dc = gdi32.CreateCompatibleDC(sdc)
            bmp = gdi32.CreateCompatibleBitmap(sdc, vsw, vsh)
            old = gdi32.SelectObject(dc, bmp)
            setattr(self, "dc_" + name, dc)
            setattr(self, "bmp_" + name, bmp)
            setattr(self, "dc_" + name + "_old", old)
        gdi32.BitBlt(self.dc_shot, 0, 0, vsw, vsh, sdc, vsx, vsy, SRCCOPY)
        # 抓屏时主窗口是全透明的，先把它的像素补回原位再生成压暗层
        self._blend_main_into_shot()

        # 呈现用的缓冲必须是 DIB（分层窗口只能吃 DIB 段）
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = vsw
        bi.bmiHeader.biHeight = -vsh
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0
        bits = c_void_p()
        bmp_back = gdi32.CreateDIBSection(sdc, byref(bi), DIB_RGB_COLORS, byref(bits), None, 0)
        dc_back = gdi32.CreateCompatibleDC(sdc)
        old_back = gdi32.SelectObject(dc_back, bmp_back)
        self.dc_back, self.bmp_back, self.dc_back_old = dc_back, bmp_back, old_back

        # 预先合成“原图 + 变暗”，每帧只需一次 BitBlt
        gdi32.BitBlt(self.dc_dim, 0, 0, vsw, vsh, self.dc_shot, 0, 0, SRCCOPY)
        g = GFX(self.dc_dim)
        g.fill(0x9A000000, 0, 0, vsw, vsh)
        g.close()

    def _release_capture_buffers(self):
        for name in ("shot", "dim", "back"):
            dc = getattr(self, "dc_" + name, None)
            if dc:
                old = getattr(self, "dc_" + name + "_old", None)
                if old:
                    gdi32.SelectObject(dc, old)
                gdi32.DeleteDC(dc)
                setattr(self, "dc_" + name, None)
            bmp = getattr(self, "bmp_" + name, None)
            if bmp:
                gdi32.DeleteObject(bmp)
                setattr(self, "bmp_" + name, None)
            setattr(self, "dc_" + name + "_old", None)
        if self.hdc_screen:
            user32.ReleaseDC(None, self.hdc_screen)
            self.hdc_screen = None
        self._release_screen_dc()
        self.shot_raw = None
        self._mos_cache = {}
        # 玻璃底取自这套缓冲，缓冲一换就必须作废
        self.tb_glass = None
        self.tb_glass_key = None

    # ---------- 自动识别
    def _request_probe(self, sx, sy, force=False):
        now = time.time()
        dx = abs(sx - self.probe_pt[0])
        dy = abs(sy - self.probe_pt[1])
        if force or now - self.probe_t > 0.18 or dx + dy > self.px(70):
            self.probe_t = now
            self.probe_pt = (sx, sy)
            self.probe.request(sx, sy)

    def _update_auto(self, x, y):
        """算出鼠标下要自动高亮的矩形（窗口 / 控件区域），坐标是遮罩内坐标"""
        vsx, vsy = self.vs[0], self.vs[1]
        sx, sy = vsx + x, vsy + y
        hwnd, wr = window_at(sx, sy, self.mypid)
        if hwnd != self.win_hwnd:
            self.win_hwnd = hwnd
            self.elem_rect = None
            self._request_probe(sx, sy, force=True)
        else:
            self._request_probe(sx, sy)

        res = self.probe.latest()
        if res and res[1] > self.probe_applied:
            self.probe_applied = res[1]
            self.elem_rect = res[0]

        best, src = None, ""
        if wr:
            best, src = wr, "窗口"
            er = self.elem_rect
            if er:
                inside = (er[0] >= wr[0] - 2 and er[1] >= wr[1] - 2
                          and er[0] + er[2] <= wr[0] + wr[2] + 2
                          and er[1] + er[3] <= wr[1] + wr[3] + 2)
                ea = er[2] * er[3]
                wa = float(wr[2] * wr[3]) or 1.0
                contains = (er[0] <= sx < er[0] + er[2] and er[1] <= sy < er[1] + er[3])
                # 只采纳"明显比窗口小的子区域"（图像、面板、按钮），
                # 否则整块客户区会把窗口边框吃掉，反而不准
                if (inside and contains and ea < wa * 0.62
                        and er[2] >= self.px(24) and er[3] >= self.px(24)):
                    best, src = er, "区域"

        self.auto_src = src
        if best:
            self.auto_rect = (best[0] - vsx, best[1] - vsy, best[2], best[3])
        else:
            self.auto_rect = None

    # ---------- 合成
    def _compose_overlay(self, W, H):
        if not self.dc_back:
            return
        # 兜底：dc_back 是长期复用的离屏 DC。GDI 侧绘制依赖的
        # 裁剪区靠 IntersectClipRect 收窄，正常路径每帧都由 clip_restore 恢复；
        # 万一某帧异常中断（没走到 restore），残留的裁剪区会污染下一帧 ——
        # 这里每帧开画前强制清空裁剪区（SelectClipRgn NULL = 全屏无裁剪），
        # 彻底杜绝「文字被裁切 / 放射状拖影」这类累积性污染。
        gdi32.SelectClipRgn(self.dc_back, None)
        gdi32.BitBlt(self.dc_back, 0, 0, W, H, self.dc_dim, 0, 0, SRCCOPY)

        sel = self.sel
        if sel and (sel[2] <= 0 or sel[3] <= 0):
            sel = None
        bright = sel
        if (bright is None and self.auto_rect and not self.dragging
                and not self.mode):
            bright = self.auto_rect

        if bright:
            sx = max(0, min(bright[0], W - 1))
            sy = max(0, min(bright[1], H - 1))
            sw_ = max(1, min(bright[2], W - sx))
            sh_ = max(1, min(bright[3], H - sy))
            gdi32.BitBlt(self.dc_back, sx, sy, sw_, sh_, self.dc_shot, sx, sy, SRCCOPY)

        g = GFX(self.dc_back)
        if bright:
            bx, by, bw_, bh_ = bright
            g.stroke_round(C_ACCENT, max(1.0, self.px(1.6)),
                           bx - 0.5, by - 0.5, bw_ + 1, bh_ + 1, 0.5)
            if sel:
                if self.drag_mode != "new" and not self.draw_start:
                    self._draw_handles(g, sel)
                self._draw_shapes(g)
                # 正在拖的画笔 / 箭头也要裁进选区，不能让笔画露到框外。
                st = g.clip_save()
                g.clip_rect(sel[0], sel[1], sel[2], sel[3])
                if self.pending:
                    self._draw_one_shape(g, self.dc_shot, self.pending, 0, 0)
                g.clip_restore(st)

            # 尺寸标签（+ 自动识别的来源提示）
            label = "%d × %d" % (bw_, bh_)
            if not sel and self.auto_src:
                label += "   单击捕获此" + self.auto_src
            lw = self.px(96) + (self.px(132) if not sel else 0)
            lh = self.px(25)
            lx = bx
            ly = by - lh - self.px(6)
            if ly < self.px(4):
                ly = by + self.px(6)
                if ly + lh > H - self.px(4):
                    ly = max(self.px(4), H - lh - self.px(4))
            lx = max(self.px(4), min(lx, W - lw - self.px(4)))
            g.fill_round(0xF01A1D24, lx, ly, lw, lh, self.px(6))
            g.text(label, self.px(12), C_WHITE, lx, ly, lw, lh, 1, True)

            if self.mode or self.sel_shape is not None:
                self._draw_tip(g, W, self._mode_tip())
        else:
            self._draw_tip(g, W, self._mode_tip() if self.mode else
                           "移动鼠标自动识别窗口/区域   ·   拖动自由框选   ·   Esc 取消")
            mx, my = self.mouse
            g.line(0x55FFFFFF, 1, mx, 0, mx, H)
            g.line(0x55FFFFFF, 1, 0, my, W, my)
        g.close()

    def _mode_tip(self):
        if self.mode == "pen":
            return "按住左键自由写画   ·   上排可切颜色 / 粗细   ·   Esc 退出"
        if self.mode == "mosaic":
            return "按住左键涂抹马赛克   ·   上排可切笔头粗细 / 块大小   ·   Esc 退出"
        if self.mode == "arrow":
            return "拖动画箭头   ·   选中后拖两端可改方向 / 长度   ·   Esc 退出"
        if self.sel_shape is not None:
            if self.sel_shape.get("t") == "arrow":
                return "拖端点改方向 · 拖线身整体移动 · 方向键微调 · Delete 删除"
            return "拖动标注移动位置 · 方向键微调 · Delete 删除"
        return "拖动空白重新框选   ·   单击标注可选中   ·   Esc 取消"

    def _draw_tip(self, g, W, tip):
        fs = self.px(13)
        tw_text = g.measure(tip, fs)[0] if tip else 0.0
        tw = min(W - self.px(24), max(self.px(300), tw_text + self.px(40)))
        th = self.px(38)
        tx = (W - tw) / 2.0
        ty = self.px(26)
        g.fill_round(0xF016181D, tx, ty, tw, th, self.px(10))
        g.stroke_round(0x1FFFFFFF, 1, tx + 0.5, ty + 0.5, tw - 1, th - 1, self.px(10))
        g.text(tip, fs, 0xFFDCE2EC, tx, ty, tw, th, 1)

    # ---------- 选区手柄
    _HANDLES = (("nw", 0, 0), ("n", 1, 0), ("ne", 2, 0), ("e", 2, 1),
                ("se", 2, 2), ("s", 1, 2), ("sw", 0, 2), ("w", 0, 1))

    def _handle_rects(self, sel):
        x, y, ww, hh = sel
        out = []
        for name, cx, cy in self._HANDLES:
            s = self.px(8) if name in ("n", "s", "e", "w") else self.px(10)
            hx = x + ww * cx / 2.0 - s / 2.0
            hy = y + hh * cy / 2.0 - s / 2.0
            out.append((name, hx, hy, s, s))
        return out

    def _draw_handles(self, g, sel):
        for name, hx, hy, hw_, hh_ in self._handle_rects(sel):
            g.fill_round(0xFFFFFFFF, hx, hy, hw_, hh_, self.px(2))
            g.stroke_round(C_ACCENT, max(1.0, self.px(1.3)), hx, hy, hw_, hh_, self.px(2))

    # ---------- 标注绘制（文字 / 马赛克 / 箭头）
    def _draw_arrow_item(self, g, x1, y1, x2, y2, color_idx=0, scale=1.0):
        """参考图样式：一根细直线 + 实心长三角头（不是两撇箭翼）"""
        color = ANNO_COLORS[max(0, min(color_idx, len(ANNO_COLORS) - 1))]
        dx, dy = x2 - x1, y2 - y1
        ln = hypot(dx, dy)
        if ln < 1.0:
            return
        ux, uy = dx / ln, dy / ln
        wdt = max(1.2, self.px(2.0) * scale)
        # 头长随线长变化，但有上下限；短线时再收一次，别让头把杆吃光
        hlen = min(max(self.px(14) * scale, ln * 0.22), self.px(40) * scale)
        hlen = min(hlen, ln * 0.6)
        hw = max(self.px(3.0) * scale, hlen * 0.20)
        bx, by = x2 - hlen * ux, y2 - hlen * uy     # 三角底边中点
        nx, ny = -uy, ux                            # 法线
        # 杆子稍微伸进三角里一点，避免两者之间留细缝
        g.line(color, wdt, x1, y1,
               bx + ux * max(1.0, wdt * 0.6), by + uy * max(1.0, wdt * 0.6), 2)
        g.fill_poly(color, [(x2, y2),
                            (bx + nx * hw, by + ny * hw),
                            (bx - nx * hw, by - ny * hw)])

    def _draw_pen_item(self, g, sh, ox=0, oy=0):
        """自由画笔：把笔迹点串成圆头折线（单点画圆），颜色/粗细可切"""
        pts = sh.get("pts") or []
        if not pts:
            return
        color = ANNO_COLORS[max(0, min(sh.get("color", 0), len(ANNO_COLORS) - 1))]
        w = max(1.5, float(sh.get("w", self._pen_w())))
        r = w / 2.0
        if len(pts) == 1:
            g.fill_circle(color, pts[0][0] + ox, pts[0][1] + oy, r)
            return
        # 圆头折线（cap=2 = 圆头），相邻点串起来就是连续平滑的笔迹
        for i in range(1, len(pts)):
            x1, y1 = pts[i - 1]
            x2, y2 = pts[i]
            g.line(color, w, x1 + ox, y1 + oy, x2 + ox, y2 + oy, 2)

    def _stroke_cells(self, sh):
        """笔刷马赛克覆盖到的网格单元（屏幕坐标对齐网格），按点数缓存"""
        blk = max(4, sh.get("blk") or self._mosaic_blk())
        pts = sh.get("pts") or []
        ver = (len(pts), blk, sh.get("_v", 0))
        if sh.get("_vc") == ver:
            return sh["_cells"]
        r = max(2.0, sh.get("r") or self._mosaic_r())
        rr = r * r
        cells = set()
        step = max(1.0, blk * 0.45)

        def add(cx, cy):
            # 网格下标不能为负：负数会让后面的取色读到 buffer 尾部
            # （Python 负索引回绕），取到屏幕底部的像素当平均色
            gx0 = max(0, int((cx - r) // blk))
            gx1 = max(0, int((cx + r) // blk))
            gy0 = max(0, int((cy - r) // blk))
            gy1 = max(0, int((cy + r) // blk))
            for gy in range(gy0, gy1 + 1):
                for gx in range(gx0, gx1 + 1):
                    ddx = gx * blk + blk * 0.5 - cx
                    ddy = gy * blk + blk * 0.5 - cy
                    if ddx * ddx + ddy * ddy <= rr:
                        cells.add((gx, gy))

        for i, (px_, py_) in enumerate(pts):
            add(px_, py_)
            if i:
                qx, qy = pts[i - 1]
                seg = hypot(px_ - qx, py_ - qy)
                k = int(seg / step)
                for j in range(1, k):
                    t = j / float(k)
                    add(qx + (px_ - qx) * t, qy + (py_ - qy) * t)
        sh["_vc"] = ver
        sh["_cells"] = cells
        return cells

    def _mosaic_color(self, gx, gy, blk):
        """一个马赛克块的颜色 = 该块内所有像素的平均色（真正的马赛克，而不是单点取色）。
        结果按块缓存，笔迹延长时只算新增的块。"""
        key = (gx, gy, blk)
        c = self._mos_cache.get(key)
        if c is not None:
            return c
        raw = self.shot_raw
        if not raw:
            return None
        vsw, vsh = self.vs[2], self.vs[3]
        x0, y0 = gx * blk, gy * blk
        x1, y1 = min(x0 + blk, vsw), min(y0 + blk, vsh)
        if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
            return None
        stride = vsw * 4
        n = x1 - x0
        sb = sg = sr = 0
        for yy in range(y0, y1):
            o = yy * stride + x0 * 4
            seg = raw[o:o + n * 4]
            sb += sum(seg[0::4])
            sg += sum(seg[1::4])
            sr += sum(seg[2::4])
        cnt = n * (y1 - y0)
        argb = 0xFF000000 | ((sr // cnt) << 16) | ((sg // cnt) << 8) | (sb // cnt)
        self._mos_cache[key] = argb
        return argb

    def _draw_mosaic_item(self, g, sh, ox=0, oy=0):
        """画笔式马赛克：把笔迹覆盖到的网格块铺成「块平均色」的色块"""
        if not self.shot_raw:
            return
        blk = max(4, sh.get("blk") or self._mosaic_blk())
        for gx, gy in self._stroke_cells(sh):
            argb = self._mosaic_color(gx, gy, blk)
            if argb is None:
                continue
            g.fill(argb, gx * blk + ox, gy * blk + oy, blk, blk)

    def _draw_shapes(self, g, ox=0, oy=0, ui=True):
        """画所有标注。**永远裁到选区里**：画布外的一律不出现（预览和成品一致）"""
        src = self.dc_shot
        sel = self.sel
        st = None
        if sel:
            st = g.clip_save()
            g.clip_rect(sel[0] + ox, sel[1] + oy, sel[2], sel[3])
        for sh in self.shapes:
            self._draw_one_shape(g, src, sh, ox, oy)
        if ui and self.sel_shape is not None:
            sh = self.sel_shape
            if sh.get("t") == "arrow":
                self._draw_arrow_ends(g, sh, ox, oy)
        if st is not None:
            g.clip_restore(st)

    def _draw_arrow_ends(self, g, sh, ox=0, oy=0):
        """选中箭头时露出两个端点手柄：拖哪头就改哪头"""
        r = self.px(ARROW_HANDLE_R)
        wd = max(1.2, self.px(1.6))
        for ex, ey in ((sh["x1"], sh["y1"]), (sh["x2"], sh["y2"])):
            g.fill_circle(0xFFFFFFFF, ex + ox, ey + oy, r)
            g.fill_circle(C_ACCENT, ex + ox, ey + oy, max(1.0, r - wd))

    def _arrow_dist(self, x, y, x1, y1, x2, y2):
        """点到线段的距离"""
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        if L2 < 0.0001:
            return hypot(x - x1, y - y1)
        t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / L2))
        return hypot(x - (x1 + dx * t), y - (y1 + dy * t))

    def _arrow_near(self, sh, x, y):
        tol = float(self.px(ARROW_HIT_TOL))
        r = float(self.px(ARROW_HANDLE_R + 3))
        if hypot(x - sh["x1"], y - sh["y1"]) <= r or hypot(x - sh["x2"], y - sh["y2"]) <= r:
            return True
        return self._arrow_dist(x, y, sh["x1"], sh["y1"], sh["x2"], sh["y2"]) <= tol

    def _arrow_end_at(self, sh, x, y):
        """点在哪个端点上（1=起点 2=终点 0=都不是）"""
        r = float(self.px(ARROW_HANDLE_R + 5))
        if hypot(x - sh["x1"], y - sh["y1"]) <= r:
            return 1
        if hypot(x - sh["x2"], y - sh["y2"]) <= r:
            return 2
        return 0

    def _shape_origin(self, sh):
        """记录按下瞬间的几何，拖动时按偏移量重算（避免累计误差）

        目前只有箭头可以选中 / 拖动（画笔和马赛克是笔迹，不参与选中），
        所以这里只处理箭头一种形状。"""
        return (sh["x1"], sh["y1"], sh["x2"], sh["y2"])

    def _fit_delta(self, bx, by, bw, bh, dx, dy):
        """把平移量收进选区：标注整体不许出框"""
        if not self.sel:
            return dx, dy
        sx, sy, sw_, sh_ = self.sel
        nx = bx + dx
        ny = by + dy
        nx = max(sx, min(nx, sx + sw_ - bw)) if bw <= sw_ else sx
        ny = max(sy, min(ny, sy + sh_ - bh)) if bh <= sh_ else sy
        return nx - bx, ny - by

    def _move_shape(self, sh, origin, dx, dy):
        """整体平移一个箭头标注，并保证不出选区"""
        x1, y1, x2, y2 = origin
        bx, by = min(x1, x2), min(y1, y2)
        bw, bh = abs(x2 - x1), abs(y2 - y1)
        dx, dy = self._fit_delta(bx, by, bw, bh, dx, dy)
        sh["x1"], sh["y1"] = x1 + dx, y1 + dy
        sh["x2"], sh["y2"] = x2 + dx, y2 + dy
        sh.pop("_bb", None)

    def _translate_shapes(self, dx, dy):
        """选区整体移动时，标注跟着一起走"""
        if not dx and not dy:
            return
        for sh in self.shapes:
            t = sh.get("t")
            if t == "arrow":
                sh["x1"] += dx; sh["y1"] += dy
                sh["x2"] += dx; sh["y2"] += dy
            elif t in ("mosaic", "pen"):
                sh["pts"] = [(px_ + dx, py_ + dy) for px_, py_ in sh.get("pts") or []]
                sh["_v"] = sh.get("_v", 0) + 1      # 网格对齐变了，缓存作废
            sh.pop("_bb", None)
        if self.pending:
            self._translate_shapes_one(self.pending, dx, dy)

    def _translate_shapes_one(self, sh, dx, dy):
        t = sh.get("t")
        if t == "arrow":
            sh["x1"] += dx; sh["y1"] += dy
            sh["x2"] += dx; sh["y2"] += dy
        elif t in ("mosaic", "pen"):
            sh["pts"] = [(px_ + dx, py_ + dy) for px_, py_ in sh.get("pts") or []]
            sh["_v"] = sh.get("_v", 0) + 1

    def _hit_shape(self, x, y):
        for sh in reversed(self.shapes):
            t = sh.get("t")
            if t == "arrow" and self._arrow_near(sh, x, y):
                return sh
        return None

    def _draw_one_shape(self, g, src, sh, ox, oy):
        t = sh.get("t")
        if t == "pen":
            self._draw_pen_item(g, sh, ox, oy)
        elif t == "mosaic":
            self._draw_mosaic_item(g, sh, ox, oy)
        elif t == "arrow":
            x1, y1, x2, y2 = sh["x1"], sh["y1"], sh["x2"], sh["y2"]
            self._draw_arrow_item(g, x1 + ox, y1 + oy, x2 + ox, y2 + oy,
                                  sh.get("color", 0))
            pad = self.px(6)
            sh["_bb"] = (min(x1, x2) - pad, min(y1, y2) - pad,
                         abs(x2 - x1) + pad * 2, abs(y2 - y1) + pad * 2)

    def _paint_overlay(self, hwnd):
        ps = PAINTSTRUCT()
        user32.BeginPaint(hwnd, byref(ps))
        self._redraw_overlay()
        user32.EndPaint(hwnd, byref(ps))

    def _redraw_overlay(self):
        vsw, vsh = self.vs[2], self.vs[3]
        self._compose_overlay(vsw, vsh)
        self._present_overlay()

    def _on_overlay(self, hwnd, msg, wp, lp):
        if msg == 0x8002:                    # 重建工具条（选项排展开/收起）
            if self.sel:
                self._show_tool()
            return 0
        if msg == 0x0021:                    # WM_MOUSEACTIVATE
            return user32.DefWindowProcW(hwnd, msg, wp, lp)
        if msg == WM_PAINT:
            self._paint_overlay(hwnd)
            return 0
        if msg == WM_ERASEBKGND:
            return 1
        if msg == WM_ACTIVATE:
            # 遮罩一旦被激活就会盖到工具条上面（整屏不透明），立刻把工具条顶回来
            self._raise_tool()
            return user32.DefWindowProcW(hwnd, msg, wp, lp)
        if msg == WM_MOUSEMOVE:
            x, y = lo(lp), hi(lp)
            self.mouse = (x, y)
            if self.mode and not self.drag_mode:
                # 画笔 / 马赛克时鼠标是专用笔头，不再是十字
                user32.SetCursor(self._cursor_for())
                if self.draw_start:
                    self._update_pending(x, y)
                    self._redraw_overlay()
                return 0
            if self.drag_mode:
                if (not self.press_moved and self.press_pt
                        and abs(x - self.press_pt[0]) + abs(y - self.press_pt[1]) > self.px(4)):
                    self.press_moved = True
                    if self.drag_mode == "new":
                        # 开始拖新选区 = 放弃上一批标注。
                        # 否则旧笔迹会被裁进新选区（位置错乱），保存时也会被一起烤进去。
                        # 行为与「重选」按钮保持一致。
                        self.sel = None
                        if self.shapes or self.sel_shape is not None:
                            self.shapes = []
                            self.sel_shape = None
                            self.undo_stack.clear()
                            self.redo_stack.clear()
                if self.drag_mode == "shape":
                    if self.sel_shape is not None and self.shape0:
                        self._move_shape(self.sel_shape, self.shape0,
                                         x - self.press_pt[0], y - self.press_pt[1])
                        self.press_moved = True
                elif self.drag_mode == "arrow_end":
                    sh = self.sel_shape
                    if sh is not None and self.drag_end:
                        ex, ey = self._clamp_to_sel(x, y)
                        if self.drag_end == 1:
                            sh["x1"], sh["y1"] = ex, ey
                        else:
                            sh["x2"], sh["y2"] = ex, ey
                        sh.pop("_bb", None)
                        self.press_moved = True
                elif self.drag_mode == "new":
                    if self.press_moved:
                        self.sel = self._norm(self.press_pt, (x, y))
                elif self.drag_mode == "move":
                    if self.press_moved and self.sel0:
                        ns = self._move_sel(self.sel0, x, y)
                        if self.sel:
                            self._translate_shapes(ns[0] - self.sel[0],
                                                   ns[1] - self.sel[1])
                        self.sel = ns
                elif self.drag_mode == "resize":
                    if self.sel0:
                        self.sel = self._resize_sel(self.sel0, self.drag_handle, x, y)
            else:
                if self._hit_shape(x, y) is not None:
                    if self.hcur_move:
                        user32.SetCursor(self.hcur_move)
                self._update_auto(x, y)
            self._redraw_overlay()
            return 0
        if msg == WM_LBUTTONDOWN:
            self._raise_tool()          # 点击遮罩时保证工具条不被压到下面
            x, y = lo(lp), hi(lp)
            self.drag_snap = self._snap()      # 动作前的状态（拖动/绘制结束后入撤销栈）

            # ① 选中箭头的端点优先：拖端点 = 改方向 / 改长度
            cur = self.sel_shape
            if cur is not None and cur.get("t") == "arrow":
                e = self._arrow_end_at(cur, x, y)
                if e:
                    self.drag_mode, self.drag_end = "arrow_end", e
                    self.press_pt = (x, y)
                    self.press_moved = False
                    return 0

            if self.mode == "pen":
                cx, cy = self._clamp_to_sel(x, y)
                self.draw_start = (cx, cy)
                self.pending = {"t": "pen", "pts": [(cx, cy)],
                                "color": self._color_idx(),
                                "w": self._pen_w()}
                self._redraw_overlay()
                return 0
            if self.mode == "mosaic":
                cx, cy = self._clamp_to_sel(x, y)
                self.draw_start = (cx, cy)
                self.pending = {"t": "mosaic", "pts": [(cx, cy)],
                                "r": self._mosaic_r(),
                                "blk": self._mosaic_blk()}
                self._redraw_overlay()
                return 0
            if self.mode == "arrow":
                # 箭头工具下点已有箭头 = 选中它（可拖端点 / 整体），否则画新的
                sh = self._hit_shape(x, y)
                if sh is not None and sh.get("t") == "arrow":
                    self.sel_shape = sh
                    self._sync_opts(sh)
                    e = self._arrow_end_at(sh, x, y)
                    if e:
                        self.drag_mode, self.drag_end = "arrow_end", e
                    else:
                        self.drag_mode = "shape"
                        self.shape0 = self._shape_origin(sh)
                    self.press_pt = (x, y)
                    self.press_moved = False
                    self._redraw_overlay()
                    self._refresh_tool()
                    return 0
                cx, cy = self._clamp_to_sel(x, y)
                self.draw_start = (cx, cy)
                self.pending = None
                self._redraw_overlay()
                return 0
            # 先看是不是点中了已有的标注：点中就进入拖动
            sh = self._hit_shape(x, y)
            if sh is not None:
                self.sel_shape = sh
                self._sync_opts(sh)
                self.drag_mode = "shape"
                self.shape0 = self._shape_origin(sh)
                self.press_pt = (x, y)
                self.press_moved = False
                self.dragging = False
                self._redraw_overlay()
                self._refresh_tool()
                return 0
            if self.sel_shape is not None:
                self.sel_shape = None
                self._redraw_overlay()
                self._refresh_tool()
            self._close_tool()
            self.mouse = (x, y)
            self.press_pt = (x, y)
            self.press_moved = False
            self.dragging = True
            z = self._hit_zone(x, y)
            if z == "move":
                self.drag_mode, self.sel0 = "move", self.sel
            elif z == "outside":
                self.drag_mode, self.sel0 = "new", self.sel
            else:
                self.drag_mode, self.drag_handle, self.sel0 = "resize", z, self.sel
            self._redraw_overlay()
            return 0
        if msg == WM_LBUTTONUP:
            if self.drag_mode in ("shape", "arrow_end"):
                if self.press_moved:
                    self._push_snap(self.drag_snap)     # 拖动完了才记一步撤销
                self.drag_mode = None
                self.drag_end = 0
                self.shape0 = None
                self._redraw_overlay()
                if self.sel:
                    self._show_tool()
                return 0
            if self.mode in ("pen", "mosaic", "arrow"):
                added = False
                if self.draw_start and self.pending:
                    sh = self.pending
                    if sh["t"] == "arrow":
                        if hypot(sh["x2"] - sh["x1"], sh["y2"] - sh["y1"]) > self.px(12):
                            self.shapes.append(sh)
                            added = True
                    elif len(sh["pts"]) >= 1:
                        self.shapes.append(sh)
                        added = True
                if added:
                    self._push_snap(self.drag_snap)
                self.pending = None
                self.draw_start = None
                self._redraw_overlay()     # 保持当前工具，可以连续画多笔
                return 0
            if self.mode:
                return 0
            was_new = (self.drag_mode == "new")
            if self.drag_mode == "move" and self.press_moved:
                self._push_snap(self.drag_snap)         # 选区带着标注一起移动，可撤销
            self.dragging = False
            if not self.press_moved and was_new and self.auto_rect:
                # 没拖动 = 单击，直接采纳自动识别到的区域
                self.sel = self.auto_rect
                self.auto_rect = None
            self.drag_mode = None
            self.drag_handle = None
            self.sel0 = None
            if self.sel:
                self.sel = self._clamp_sel(self.sel)
            if not self.sel or self.sel[2] < self.px(5) or self.sel[3] < self.px(5):
                self.sel = None
                self._redraw_overlay()
                return 0
            self._redraw_overlay()
            self._show_tool()
            return 0
        if msg == WM_LBUTTONDBLCLK:
            x, y = lo(lp), hi(lp)
            sh = self._hit_shape(x, y)
            if sh is None and self.sel and self._in(x, y, self.sel):
                self._action_copy()             # 双击空白 = 直接复制
            return 0
        if msg == WM_RBUTTONDOWN:
            if self.mode:
                self._leave_mode()
            elif self.sel_shape is not None:
                self.sel_shape = None
                self._redraw_overlay()
                self._refresh_tool()
            elif self.shapes:
                self._push_snap(self._snap())      # 右键删最后一笔，也能撤销回来
                self.shapes.pop()
                self._redraw_overlay()
            else:
                self._cancel_capture()
            return 0
        if msg == WM_KEYDOWN and wp == VK_ESCAPE:
            if self.sel_shape is not None:
                self.sel_shape = None
                self._redraw_overlay()
                self._refresh_tool()
            elif self.mode:
                self._leave_mode()
            else:
                self._cancel_capture()
            return 0
        if msg == WM_KEYDOWN:
            ctrl = bool(user32.GetKeyState(VK_CONTROL) & 0x8000)
            shift = bool(user32.GetKeyState(VK_SHIFT) & 0x8000)
            if wp == 0x2E and self.sel_shape is not None:      # Delete
                if self.sel_shape in self.shapes:
                    self._push_snap(self._snap())
                    self.shapes.remove(self.sel_shape)
                self.sel_shape = None
                self._redraw_overlay()
                self._refresh_tool()
                return 0
            if (wp == 0x59 and ctrl) or (wp == 0x5A and ctrl and shift):
                self._redo()                                    # Ctrl+Y / Ctrl+Shift+Z
                return 0
            if (wp == 0x5A and ctrl) or wp == 0x08:
                self._undo()                                    # Ctrl+Z / Backspace
                return 0
            # 方向键微调：1px，按住 Shift 是 10px（连续按算一步撤销）
            if self.sel_shape is not None and wp in (0x25, 0x26, 0x27, 0x28):
                step = self.px(10) if shift else 1
                dx = -step if wp == 0x25 else (step if wp == 0x27 else 0)
                dy = -step if wp == 0x26 else (step if wp == 0x28 else 0)
                now = time.time()
                if now - self.nudge_t > 0.8:
                    self._push_snap(self._snap())
                self.nudge_t = now
                self._move_shape(self.sel_shape, self._shape_origin(self.sel_shape),
                                 dx, dy)
                self._redraw_overlay()
                return 0
        if msg == WM_SETCURSOR:
            user32.SetCursor(self._cursor_for())
            return 1
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _cursor_for(self):
        if self.mode == "mosaic":
            return self._brush_cursor() or self.hcur_cross     # 马赛克：圆形笔头
        if self.mode == "pen":
            return self._pen_cursor() or self.hcur_cross       # 画笔：圆形笔头
        if self.mode:
            return self.hcur_cross
        if self.drag_mode in ("move", "shape", "arrow_end"):
            return self.hcur_move
        if self.drag_mode == "resize":
            h = self.drag_handle or ""
            if h in ("nw", "se"):
                return self.hcur_nwse
            if h in ("ne", "sw"):
                return self.hcur_nesw
            if h in ("n", "s"):
                return self.hcur_ns
            return self.hcur_we
        z = self._hit_zone(*self.mouse) if self.sel else "outside"
        if z == "move":
            return self.hcur_move
        if z in ("nw", "se"):
            return self.hcur_nwse
        if z in ("ne", "sw"):
            return self.hcur_nesw
        if z in ("n", "s"):
            return self.hcur_ns
        if z in ("e", "w"):
            return self.hcur_we
        return self.hcur_cross

    def _hit_zone(self, x, y):
        if not self.sel:
            return "outside"
        tol = self.px(7)
        for name, hx, hy, hw_, hh_ in self._handle_rects(self.sel):
            if hx - tol <= x < hx + hw_ + tol and hy - tol <= y < hy + hh_ + tol:
                return name
        if self._in(x, y, self.sel):
            return "move"
        return "outside"

    def _clamp_sel(self, sel):
        vsw, vsh = self.vs[2], self.vs[3]
        x, y, ww, hh = sel
        x = max(0, min(x, vsw - 1))
        y = max(0, min(y, vsh - 1))
        ww = max(1, min(ww, vsw - x))
        hh = max(1, min(hh, vsh - y))
        return (x, y, ww, hh)

    def _move_sel(self, sel0, x, y):
        dx = x - self.press_pt[0]
        dy = y - self.press_pt[1]
        x0, y0, ww, hh = sel0
        nx = max(0, min(x0 + dx, self.vs[2] - ww))
        ny = max(0, min(y0 + dy, self.vs[3] - hh))
        return (nx, ny, ww, hh)

    def _resize_sel(self, sel0, handle, x, y):
        x0, y0, ww, hh = sel0
        x1, y1 = x0 + ww, y0 + hh
        minw, minh = self.px(8), self.px(8)
        if "w" in handle:
            x0 = max(0, min(x, x1 - minw))
        if "e" in handle:
            x1 = min(self.vs[2], max(x, x0 + minw))
        if "n" in handle:
            y0 = max(0, min(y, y1 - minh))
        if "s" in handle:
            y1 = min(self.vs[3], max(y, y0 + minh))
        return (x0, y0, x1 - x0, y1 - y0)

    def _norm(self, a, b):
        vsw, vsh = self.vs[2], self.vs[3]
        x1, y1 = a
        x2, y2 = b
        x1 = max(0, min(x1, vsw))
        x2 = max(0, min(x2, vsw))
        y1 = max(0, min(y1, vsh))
        y2 = max(0, min(y2, vsh))
        return (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))

    def _close_overlay(self):
        if self.hwnd_overlay:
            user32.DestroyWindow(self.hwnd_overlay)
            self.hwnd_overlay = None
        self._release_capture_buffers()
        self.shot_buf = None
        self.sel = None
        self.dragging = False
        self.drag_mode = None
        self.drag_handle = None
        self.drag_end = 0
        self.sel0 = None
        self.auto_rect = None
        self.win_hwnd = None
        self.elem_rect = None
        self.shapes = []
        self.sel_shape = None
        self.mode = None
        self.undo_stack = []
        self.redo_stack = []
        self.drag_snap = None
        self.capturing = False
        self.main_rect = None
        if self.main_was_visible and self.hwnd_main:
            user32.ShowWindow(self.hwnd_main, SW_SHOW)
            # 截图期间主窗口停在“全透明”那一帧，这里必须显式画回来。
            # 一帧就提交整窗内容，不需要再 InvalidateRect 触发第二次重绘。
            self._present_main()
            user32.SetForegroundWindow(self.hwnd_main)
        self.main_was_visible = False

    def _cancel_capture(self):
        self._close_tool()
        self._close_overlay()

    # ================================================== 标注：文字 / 马赛克 / 箭头
    def _begin_mode(self, kind):
        if not self.sel:
            return
        self.mode = kind
        self.sel_shape = None
        self.drag_mode = None
        self.drag_end = 0
        self.pending = None
        self.draw_start = None
        if kind == "pen":
            # 画笔只有六色（红蓝绿黄白黑），箭头有七色（多一个灰）。
            # 从灰色箭头切过来时钳一下，否则会画出一个选项排里根本没有的
            # 颜色，色块全都不高亮，看着像"没选中"。
            self.anno_color = min(self.anno_color, 5)
        self._redraw_overlay()
        self._refresh_tool()        # 展开颜色/粗细等选项排

    def _leave_mode(self):
        self.mode = None
        self.pending = None
        self.draw_start = None
        self.drag_end = 0
        self._redraw_overlay()
        self._refresh_tool()

    def _refresh_tool(self):
        """选项排展开/收起时重排工具条（原地改尺寸，不销毁窗口）"""
        self._show_tool()

    def _snap(self):
        """把标注列表拍成可比较、可回放的快照（不带 _bb / _cells 这类缓存）"""
        out = []
        for sh in self.shapes:
            d = {}
            for k, v in sh.items():
                if k.startswith("_"):
                    continue
                d[k] = list(v) if k == "pts" else v
            out.append(d)
        return out

    def _push_snap(self, snap):
        if snap is None:
            return
        self.undo_stack.append(snap)
        if len(self.undo_stack) > 60:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def _undo(self):
        if not self.undo_stack:
            return
        self.redo_stack.append(self._snap())
        self.shapes = self.undo_stack.pop()
        self.sel_shape = None
        self._redraw_overlay()
        self._refresh_tool()

    def _redo(self):
        if not self.redo_stack:
            return
        self.undo_stack.append(self._snap())
        self.shapes = self.redo_stack.pop()
        self.sel_shape = None
        self._redraw_overlay()
        self._refresh_tool()

    def _sync_opts(self, sh):
        """把选中标注当前的样式同步到工具条（只有箭头可选中）"""
        if sh.get("t") == "arrow":
            self.anno_color = max(0, min(sh.get("color", 0), len(ANNO_COLORS) - 1))
            return

    def _apply_opts(self):
        """工具条上的颜色 / 粗细改动，立刻作用到当前画笔（或选中的箭头）"""
        sh = self.sel_shape
        if sh is None:
            # 画笔 / 马赛克模式：选项只影响接下来画的笔，无需推快照
            return
        pre = self._snap()
        if sh.get("t") == "arrow":
            sh["color"] = self.anno_color
            if self._snap() != pre:
                self._push_snap(pre)
            self._redraw_overlay()
            return
        self._redraw_overlay()

    def _update_pending(self, x, y):
        if not self.draw_start:
            return
        # 笔画一律收进选区，避免涂到框外
        x, y = self._clamp_to_sel(x, y)
        x0, y0 = self.draw_start
        if self.mode == "pen":
            if self.pending is None:
                self.pending = {"t": "pen", "pts": [(x0, y0)],
                                "color": self._color_idx(),
                                "w": self._pen_w()}
            pts = self.pending["pts"]
            lx, ly = pts[-1]
            # 欧氏距离：曼哈顿距离在 45° 斜线上间距偏大，笔迹会出现棱角
            if hypot(x - lx, y - ly) >= 2.0:
                if len(pts) < MAX_PEN_POINTS:
                    pts.append((x, y))
            return
        if self.mode == "mosaic":
            if self.pending is None:
                self.pending = {"t": "mosaic", "pts": [(x0, y0)],
                                "r": self._mosaic_r(),
                                "blk": self._mosaic_blk()}
            pts = self.pending["pts"]
            lx, ly = pts[-1]
            if hypot(x - lx, y - ly) >= 2.0:
                if len(pts) < MAX_PEN_POINTS:
                    pts.append((x, y))
            return
        if self.mode == "arrow":
            self.pending = {"t": "arrow", "x1": x0, "y1": y0, "x2": x,
                            "y2": y, "color": self._color_idx()}

    def _clamp_to_sel(self, x, y):
        if not self.sel:
            return x, y
        sx, sy, sw_, sh_ = self.sel
        return (max(sx + 2, min(x, sx + sw_ - 2)),
                max(sy + 2, min(y, sy + sh_ - 2)))

    # ================================================== 工具条
    def _opt_visible(self):
        return (self.mode in ("pen", "arrow", "mosaic")
                or self._arrow_opts())

    def _pen_opts(self):
        """画笔模式：颜色 + 粗细两排"""
        return self.mode == "pen"

    def _arrow_opts(self):
        """选中箭头时只露出颜色点（箭头可改颜色）"""
        sh = self.sel_shape
        return bool(sh) and sh.get("t") == "arrow"

    def _color_opts(self):
        """颜色点属于画笔 / 箭头"""
        return self._pen_opts() or self._arrow_opts() or self.mode == "arrow"

    def _mosaic_opts(self):
        return self.mode == "mosaic"

    def _tool_layout(self):
        """返回 (组宽, 组高, 矩形列表)。按钮彼此独立，之间留空隙。
        矩形第 5 项之后是“选项排”（颜色 / 粗细等），对应模式出现。"""
        bw, bh = self.px(80), self.px(44)
        gap = self.px(6)
        n = len(TB_ITEMS)
        ww = bw * n + gap * (n - 1)
        rects = [("@btn", i * (bw + gap), 0, bw, bh) for i in range(n)]
        hh = bh
        if self._opt_visible():
            oy = bh + self.px(8)
            oh = self.px(40)
            cx = self.px(8)
            if self._color_opts():
                d = self.px(26)
                ncolors = 6 if self._pen_opts() else len(ANNO_COLORS)
                for i in range(ncolors):
                    rects.append(("@color%d" % i, cx, oy + (oh - d) / 2.0, d, d))
                    cx += d + self.px(7)
                cx += self.px(10)
            if self._pen_opts():
                lw = self.px(46)
                cw = self.px(44)
                rects.append(("@label_p", cx, oy, lw, oh))
                cx += lw + self.px(2)
                for i in range(len(PEN_WIDTHS)):
                    rects.append(("@penw%d" % i, cx, oy, cw, oh))
                    cx += cw + self.px(5)
            if self._mosaic_opts():
                lw = self.px(46)
                cw = self.px(44)
                rects.append(("@label_b", cx, oy, lw, oh))
                cx += lw + self.px(2)
                for i in range(len(MOSAIC_BRUSHES)):
                    rects.append(("@brush%d" % i, cx, oy, cw, oh))
                    cx += cw + self.px(5)
                cx += self.px(16)
                rects.append(("@label_k", cx, oy, lw, oh))
                cx += lw + self.px(2)
                for i in range(len(MOSAIC_BLOCKS)):
                    rects.append(("@block%d" % i, cx, oy, cw, oh))
                    cx += cw + self.px(5)
            hh = oy + oh
        return ww, hh, rects

    def _show_tool(self):
        if not self.sel:
            self._close_tool()
            return
        ww, hh, rects = self._tool_layout()
        vsx, vsy, vsw, vsh = self.vs
        sx, sy, sw_, sh_ = self.sel
        gap = self.px(10)

        # 水平居中于选区，浮在选区下方
        px_ = vsx + sx + int(round((sw_ - ww) / 2.0))
        py_ = vsy + sy + sh_ + gap
        if py_ + hh > vsy + vsh - self.px(6):
            py_ = vsy + sy - hh - gap          # 下方放不下就翻到上方
        if py_ < vsy + self.px(6):
            py_ = min(vsy + vsh - hh - self.px(6), vsy + sy + sh_ + gap)
        px_ = max(vsx + self.px(6), min(px_, vsx + vsw - ww - self.px(6)))
        py_ = max(vsy + self.px(6), min(py_, vsy + vsh - hh - self.px(6)))
        self.tb_pos = (px_, py_)

        topmost = w.HWND(HWND_TOPMOST)
        if self.hwnd_tool:
            # 已存在就原地改尺寸/位置：销毁重建会丢掉正在进行的点击
            user32.SetWindowPos(self.hwnd_tool, topmost, px_, py_, ww, hh,
                                SWP_NOACTIVATE)
            self._present_tool()
            return

        # NOACTIVATE：点工具条不抢焦点，遮罩始终是前台窗口。
        # 否则点一下工具条遮罩就失去激活，键盘（Esc / Delete）会失灵。
        self.hwnd_tool = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE,
            "SnapLiteTool", "", WS_POPUP, px_, py_, ww, hh,
            None, None, self.hinst, None)
        if not self.hwnd_tool:
            return
        # 先把整帧提交到窗口，再显示 → 不会出现空白/黑块过渡帧
        self._present_tool()
        user32.ShowWindow(self.hwnd_tool, SW_SHOWNA)
        self._raise_tool()          # 新建的窗口可能落到底层，明确顶到最上

    def _raise_tool(self):
        """把工具条重新提到最顶层。
        遮罩是整屏不透明的分层窗口，一旦被激活就会盖到工具条上面 —— 看起来就是
        “菜单消失了，按一下右键才回来”（右键会重排工具条 → SetWindowPos 把它顶回去）。"""
        if not self.hwnd_tool:
            return
        user32.SetWindowPos(self.hwnd_tool, w.HWND(HWND_TOPMOST),
                            0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _close_tool(self):
        if self.hwnd_tool:
            user32.DestroyWindow(self.hwnd_tool)
            self.hwnd_tool = None
        self.hover_tool = -1
        self._release_tool_surface()

    # ---------- 逐像素透明呈现（圆角抗锯齿，不再走窗口区域裁剪）
    def _tool_layout_key(self):
        """布局签名。

        画笔 / 马赛克 / 箭头三种模式的选项排**宽高完全一样**（都是 682×92），
        只有里面矩形的位置和个数不同。以前表面重建只比 tb_size，
        于是切模式时沿用了上一种模式的蒙版和玻璃形状 —— 轻重不一样的地方
        冒出一块不该有的毛玻璃，重的地方控件被蒙版裁掉，就是“错乱 + 残留”。"""
        ww, hh, rects = self._tool_layout()
        return (ww, hh, tuple(rects))

    def _ensure_tool_surface(self):
        key = self._tool_layout_key()
        if self.tb_dc and self.tb_key == key:
            return
        self._release_tool_surface()
        ww, hh = key[0], key[1]
        sdc = self._get_screen_dc()
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = ww
        bi.bmiHeader.biHeight = -hh
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0
        bits = c_void_p()
        bmp = gdi32.CreateDIBSection(sdc, byref(bi), DIB_RGB_COLORS, byref(bits), None, 0)
        dc = gdi32.CreateCompatibleDC(sdc)
        old = gdi32.SelectObject(dc, bmp)
        self.tb_dc, self.tb_bmp, self.tb_old, self.tb_bits = dc, bmp, old, bits
        self.tb_size = (ww, hh)
        self.tb_key = key
        self.tb_mask, self.tb_edge = self._build_tool_mask(ww, hh)
        self.tb_scale = bytes(min(255, i * TB_ALPHA // 255) for i in range(256))
        self.tb_alpha = bytes(min(255, v * TB_ALPHA // 255) for v in self.tb_mask)

    def _release_tool_surface(self):
        if self.tb_dc:
            if self.tb_old:
                gdi32.SelectObject(self.tb_dc, self.tb_old)
            gdi32.DeleteDC(self.tb_dc)
            self.tb_dc = None
        if self.tb_bmp:
            gdi32.DeleteObject(self.tb_bmp)
            self.tb_bmp = None
        self.tb_old = None
        self.tb_bits = None
        self.tb_size = None
        self.tb_key = None
        self.tb_mask = None
        self.tb_edge = None
        # 玻璃底跟尺寸/位置/遮罩内容绑定，表面重建时必须一并作废
        self.tb_glass = None
        self.tb_glass_key = None
        self.tb_cov = None
        self.tb_cov_key = None

    def _build_tool_mask(self, W, H):
        """覆盖蒙版：只负责“哪些像素属于按钮区域”，形状细节交给 GDI+ 抗锯齿。
        以前这里用圆角 SDF 覆盖 alpha，会把 GDI+ 已经算好的抗锯齿边重新压硬 —— 锯齿就是这么来的。
        现在还要往外放一圈 pad：颜色点选中时的白色高亮环画在圆点矩形之外，
        蒙版卡死在矩形上会把环切成四段硬边（这就是“选中高亮还有锯齿”的原因）。"""
        _, _, rects = self._tool_layout()
        pad = int(self.px(6))
        mask = bytearray(W * H)
        for (_n, x0, y0, ww, hh) in rects:
            xa = max(0, int(x0) - pad)
            xb = min(W, int(x0 + ww) + 1 + pad)
            ya = max(0, int(y0) - pad)
            yb = min(H, int(y0 + hh) + 1 + pad)
            if xb <= xa:
                continue
            row = bytes([255]) * (xb - xa)
            for yy in range(ya, yb):
                o = yy * W
                mask[o + xa:o + xb] = row
        return mask, None

    def _tool_shape_cov(self, W, H):
        """**只有主排那 8 个按钮**的形状覆盖率（玻璃底只贴在它们身上）。

        选项排（颜色点 / 粗细 / 笔头 / 块大小）不给玻璃底：它们是圆点和小按钮，
        自己带底色，垫一层模糊玻璃只会在字后面多出一块方块，切换模式时还容易
        看出错位。GDI+ 画形状取 alpha = 带抗锯齿的最准形状；按钮之间那几个像素
        的缝必须保持全透明，否则整条工具条会变成一块大玻璃，版式就变了。"""
        key = self._tool_layout_key()
        if self.tb_cov is not None and self.tb_cov_key == key:
            return self.tb_cov
        sdc = self._get_screen_dc()
        d = _new_dib(sdc, W, H)
        try:
            g = GFX(d[0])
            try:
                for (name, x, y, ww, hh) in self._tool_layout()[2]:
                    if name != "@btn":
                        continue
                    g.fill_round(0xFFFFFFFF, x, y, ww, hh, self.px(12))
            finally:
                g.close()
            cov = bytes(ctypes.string_at(d[3], W * H * 4)[3::4])
        finally:
            _free_dib(d)
        self.tb_cov, self.tb_cov_key = cov, key
        return cov

    def _tool_glass_layer(self, W, H, pos):
        """工具条背后的磨砂底。

        不抓屏：工具条飘在遮罩上方，抓屏抓到的就是它自己。遮罩里本来就存着
        「该位置的桌面」——dc_dim 是压暗后的整屏原图，dc_shot 是原图；选区
        在遮罩里是亮的，所以落在选区里的那一块要从 dc_shot 取。取完模糊，
        再用按钮形状覆盖率削出形状，就是一块「按位置贴合的玻璃」。"""
        if not (self.dc_dim and self.dc_shot):
            return None
        key = (pos[0], pos[1], W, H, self.sel, self.use_shot_glass,
               self._tool_layout_key())
        if self.tb_glass is not None and self.tb_glass_key == key:
            return self.tb_glass

        vx, vy, vw, vh = self.vs
        lx, ly = int(pos[0]) - vx, int(pos[1]) - vy
        sx0, sy0 = max(0, lx), max(0, ly)
        sx1, sy1 = min(vw, lx + W), min(vh, ly + H)
        if sx1 <= sx0 or sy1 <= sy0:
            return None

        sdc = self._get_screen_dc()
        d = _new_dib(sdc, W, H)
        try:
            if self.use_shot_glass:
                # 玻璃底取「原图」而不是压暗后的：磨砂才看得出来
                gdi32.BitBlt(d[0], sx0 - lx, sy0 - ly, sx1 - sx0, sy1 - sy0,
                             self.dc_shot, sx0, sy0, SRCCOPY)
            else:
                gdi32.BitBlt(d[0], sx0 - lx, sy0 - ly, sx1 - sx0, sy1 - sy0,
                             self.dc_dim, sx0, sy0, SRCCOPY)
            sel = self.sel
            if sel and sel[2] > 0 and sel[3] > 0 and not self.use_shot_glass:
                ix0, iy0 = max(sx0, sel[0]), max(sy0, sel[1])
                ix1 = min(sx1, sel[0] + sel[2])
                iy1 = min(sy1, sel[1] + sel[3])
                if ix1 > ix0 and iy1 > iy0:
                    gdi32.BitBlt(d[0], ix0 - lx, iy0 - ly, ix1 - ix0, iy1 - iy0,
                                 self.dc_shot, ix0, iy0, SRCCOPY)
            raw = ctypes.string_at(d[3], W * H * 4)
        finally:
            _free_dib(d)

        data = bytearray(self._blur_bgra(raw, W, H))
        cov = self._tool_shape_cov(W, H)
        # 形状外的像素直接扔掉（按钮之间要能看见遮罩，缝不能糊上一层玻璃）；
        # 形状边缘按覆盖率预乘颜色 —— ULW 与 GDI+ 都按预乘 alpha 处理，
        # 不预乘的话圆角会有一圈偏亮的边。
        for i, a in enumerate(cov):
            if a == 255:
                continue
            o = i * 4
            if a == 0:
                data[o] = data[o + 1] = data[o + 2] = 0
            else:
                data[o] = data[o] * a // 255
                data[o + 1] = data[o + 1] * a // 255
                data[o + 2] = data[o + 2] * a // 255
        data[3::4] = cov
        layer = bytes(data)
        self.tb_glass, self.tb_glass_key = layer, key
        return layer

    def _present_tool(self):
        """整帧合成后一次性提交（per-pixel alpha），无锯齿、无闪烁"""
        if not self.hwnd_tool:
            return
        self._ensure_tool_surface()
        W, H = self.tb_size
        total = W * H * 4
        glass = self._tool_glass_layer(W, H, self.tb_pos)
        if glass:
            # 先垫玻璃底，按钮的底色是半透明的 → 抹上去就是磨砂，不是实心灰
            ctypes.memmove(self.tb_bits, glass, total)
        else:
            ctypes.memset(self.tb_bits, 0, total)

        g = GFX(self.tb_dc)
        self._draw_tool(g, W, H)
        g.close()

        raw = ctypes.string_at(self.tb_bits, total)
        ba = bytearray(raw)
        # GDI+ 已把每个像素写成“直通 alpha”（预乘）。这里只能整体缩放，
        # 不能覆盖 alpha —— 否则圆角、圆形、文字的抗锯齿边缘全变硬边。
        ba[0::4] = ba[0::4].translate(self.tb_scale)
        ba[1::4] = ba[1::4].translate(self.tb_scale)
        ba[2::4] = ba[2::4].translate(self.tb_scale)
        ma = self.tb_alpha                    # 覆盖蒙版 × 整体透明度
        ag = ba[3::4]                         # GDI+ 给的抗锯齿 alpha
        ba[3::4] = ma
        # 半透明边缘再按覆盖率折算一次（颜色仍保持预乘）
        for i in [k for k, a in enumerate(ag) if a != 255]:
            a0 = ag[i]
            a = a0 * ma[i] // 255
            o = i * 4
            if a <= 0:
                ba[o] = ba[o + 1] = ba[o + 2] = ba[o + 3] = 0
            else:
                ba[o + 3] = a
                ba[o] = raw[o] * a // a0
                ba[o + 1] = raw[o + 1] * a // a0
                ba[o + 2] = raw[o + 2] * a // a0
        ctypes.memmove(self.tb_bits, bytes(ba), total)

        pt_dst = w.POINT(self.tb_pos[0], self.tb_pos[1])
        size = w.SIZE(W, H)
        pt_src = w.POINT(0, 0)
        bf = BLENDFUNCTION(0, 0, 255, 1)      # AC_SRC_OVER + AC_SRC_ALPHA
        sdc = self._get_screen_dc()
        user32.UpdateLayeredWindow(self.hwnd_tool, sdc, byref(pt_dst), byref(size),
                                   self.tb_dc, byref(pt_src), 0, byref(bf), 2)

    def _paint_tool(self, hwnd):
        # ULW 负责全部呈现，这里只清掉重绘请求
        ps = PAINTSTRUCT()
        user32.BeginPaint(hwnd, byref(ps))
        user32.EndPaint(hwnd, byref(ps))

    def _draw_tool(self, g, W, H):
        _, _, rects = self._tool_layout()
        rad = self.px(12)
        nb = len(TB_ITEMS)
        for i in range(nb):
            _, x, y, ww, hh = rects[i]
            kind, label = TB_ITEMS[i]
            hot = (self.hover_tool == i)

            if kind == "copy":
                bg = C_TB_COPY_HI if hot else C_TB_COPY
                tc = C_WHITE if hot else C_TB_COPY_TXT
                ic = C_WHITE if hot else C_TB_COPY_ICO
            elif kind == "cancel":
                bg = C_TB_CANCEL_HI if hot else C_TB_BTN
                tc = C_WHITE if hot else C_TB_CANCEL_TXT
                ic = C_WHITE if hot else C_TB_CANCEL_ICO
            else:
                bg = C_TB_BTN_HI if hot else C_TB_BTN
                tc = C_WHITE if hot else C_TB_TXT
                ic = C_WHITE if hot else C_TB_ICO
            if self.mode and kind == self.mode:
                bg = C_ACCENT if not hot else C_ACCENT_HI
                tc = C_WHITE
                ic = C_WHITE

            g.fill_round(bg, x, y, ww, hh, rad)
            g.stroke_round(0x33FFFFFF if hot else 0x1FFFFFFF, 1,
                           x + 0.5, y + 0.5, ww - 1, hh - 1, rad)

            isz = self.px(17)
            self._draw_tool_icon(g, kind, x + self.px(13),
                                 y + (hh - isz) / 2.0, isz, ic, bg)
            g.text(label, self.px(12), tc,
                   x + self.px(35), y, ww - self.px(35) - self.px(3), hh, 0, hot)

        # 选项排：颜色  ·  画笔粗细 / 马赛克笔头 / 块大小
        if not self._opt_visible():
            return
        for i in range(nb, len(rects)):
            name, x, y, ww, hh = rects[i]
            if name in ("@label_b", "@label_k", "@label_p"):
                txt = {"@label_b": "笔头", "@label_k": "块大小", "@label_p": "粗细"}[name]
                g.text(txt, self.px(11.5), C_TB_TXT, x, y, ww, hh, 1)
            elif name.startswith("@penw"):
                k = int(name[5:])
                on = (self.pen_width == k)
                g.fill_round(C_ACCENT if on else C_TB_BTN, x, y, ww, hh, self.px(9))
                if not on:
                    g.stroke_round(0x1FFFFFFF, 1, x + 0.5, y + 0.5,
                                   ww - 1, hh - 1, self.px(9))
                g.text(PEN_WIDTH_NAMES[k], self.px(13),
                       C_WHITE if on else C_TB_TXT, x, y, ww, hh, 1, on)
            elif name.startswith("@brush") or name.startswith("@block"):
                is_brush = name.startswith("@brush")
                k = int(name[6:])
                on = (self.mosaic_brush == k) if is_brush else (self.mosaic_block == k)
                g.fill_round(C_ACCENT if on else C_TB_BTN, x, y, ww, hh, self.px(9))
                if not on:
                    g.stroke_round(0x1FFFFFFF, 1, x + 0.5, y + 0.5,
                                   ww - 1, hh - 1, self.px(9))
                txt = MOSAIC_BRUSH_NAMES[k] if is_brush else MOSAIC_BLOCK_NAMES[k]
                g.text(txt, self.px(13), C_WHITE if on else C_TB_TXT, x, y, ww, hh, 1, on)
            else:
                k = int(name[6:])
                c = ANNO_COLORS[k]
                on = (self.anno_color == k)
                if on:
                    m = self.px(3)
                    g.fill_round(0xFFFFFFFF, x - m, y - m, ww + m * 2, hh + m * 2,
                                 (ww + m * 2) / 2.0)
                g.fill_round(0xFF000000 | (c & 0xFFFFFF), x, y, ww, hh, ww / 2.0)
                g.stroke_round(0x40FFFFFF, 1, x + 0.5, y + 0.5,
                               ww - 1, hh - 1, ww / 2.0)

    def _draw_tool_icon(self, g, kind, x, y, sz, color, bg=None):
        """15×15 单位网格上的线条图标，按 sz 缩放"""
        lw = max(1.15, self.px(1.35))
        u = sz / 15.0

        def L(x1, y1, x2, y2):
            g.line(color, lw, x + x1 * u, y + y1 * u, x + x2 * u, y + y2 * u)

        if kind == "reselect":
            L(1.2, 6.2, 1.2, 1.2); L(1.2, 1.2, 6.2, 1.2)
            L(8.8, 1.2, 13.8, 1.2); L(13.8, 1.2, 13.8, 6.2)
            L(13.8, 8.8, 13.8, 13.8); L(13.8, 13.8, 8.8, 13.8)
            L(6.2, 13.8, 1.2, 13.8); L(1.2, 13.8, 1.2, 8.8)
        elif kind == "pen":
            # 一支斜放的笔：笔杆从左上到右下，笔尖在右下
            L(2.6, 12.4, 12.4, 2.6)
            g.fill_poly(color, [
                (x + 12.4 * u, y + 2.6 * u),
                (x + 10.2 * u, y + 4.8 * u),
                (x + 13.0 * u, y + 7.6 * u)])
        elif kind == "copy":
            g.stroke_round(color, lw, x + 4.6 * u, y + 1.6 * u,
                           8.8 * u, 8.8 * u, 2.2 * u)
            if bg is not None:
                # 这张“前纸”的填充要盖住下面那张纸的线条，必须不透明。
                # 按钮底色已经改成半透明（磨砂），直接拿它填会在图标处
                # 把玻璃底再压暗一层，看起来像多了一块深色方块。
                g.fill_round(bg | 0xFF000000, x + 0.9 * u, y + 4.5 * u,
                             8.9 * u, 8.9 * u, 2.3 * u)
            g.stroke_round(color, lw, x + 1.6 * u, y + 4.6 * u,
                           8.8 * u, 8.8 * u, 2.2 * u)
        elif kind == "save":
            L(7.5, 1.5, 7.5, 9.8)
            L(4.2, 6.6, 7.5, 10.0)
            L(10.8, 6.6, 7.5, 10.0)
            L(2.6, 13.0, 12.4, 13.0)
        elif kind == "mosaic":
            # 九宫格马赛克
            for gx in range(3):
                for gy in range(3):
                    if (gx + gy) % 2 == 0:
                        g.fill(color, x + (1.6 + gx * 4.2) * u,
                               y + (1.6 + gy * 4.2) * u, 3.8 * u, 3.8 * u)
        elif kind == "arrow":
            # 细杆 + 实心三角头，跟实际画出来的箭头一致
            ux_, uy_ = 0.7071, -0.7071
            x0, y0, tipx, tipy = 2.6, 12.4, 12.8, 2.4
            L(x0, y0, (x0 + tipx) / 2.0, (y0 + tipy) / 2.0)
            hl, hw_ = 4.4, 2.3
            bx_, by_ = tipx - ux_ * hl, tipy - uy_ * hl
            nx_, ny_ = -uy_, ux_
            g.fill_poly(color, [
                (x + tipx * u, y + tipy * u),
                (x + (bx_ + nx_ * hw_) * u, y + (by_ + ny_ * hw_) * u),
                (x + (bx_ - nx_ * hw_) * u, y + (by_ - ny_ * hw_) * u)])
        elif kind == "undo":
            # 逆时针回环箭头：从右上起，绕顶部回到左下，箭头在末端指向左下。
            # （角度在本窗口坐标里增大 = 顺时针，所以这里用递减才是逆时针）
            cx0, cy0, rr = 7.8, 8.2, 4.5
            pts = []
            deg = 342.0
            while deg >= 166.0:
                a = radians(deg)
                pts.append((cx0 + rr * cos(a), cy0 + rr * sin(a)))
                deg -= 12.0
            for i in range(len(pts) - 1):
                L(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
            tx, ty = pts[-1]
            bx0, by0 = pts[-2]
            ddx, ddy = tx - bx0, ty - by0
            dl = hypot(ddx, ddy) or 1.0
            ddx, ddy = ddx / dl, ddy / dl
            nx_, ny_ = -ddy, ddx
            hl, hw_ = 3.4, 1.8
            g.fill_poly(color, [
                (x + (tx + ddx * hl * 0.62) * u, y + (ty + ddy * hl * 0.62) * u),
                (x + (tx - ddx * hl * 0.48 + nx_ * hw_) * u,
                 y + (ty - ddy * hl * 0.48 + ny_ * hw_) * u),
                (x + (tx - ddx * hl * 0.48 - nx_ * hw_) * u,
                 y + (ty - ddy * hl * 0.48 - ny_ * hw_) * u)])
        else:
            L(3.2, 3.2, 11.8, 11.8)
            L(11.8, 3.2, 3.2, 11.8)

    def _on_tool(self, hwnd, msg, wp, lp):
        if msg == WM_PAINT:
            self._paint_tool(hwnd)
            return 0
        if msg == WM_ERASEBKGND:
            return 1
        if msg == WM_MOUSEMOVE:
            x, y = lo(lp), hi(lp)
            _, _, rects = self._tool_layout()
            hot = -1
            for i in range(min(len(rects), len(TB_ITEMS))):
                if self._in(x, y, rects[i][1:]):
                    hot = i
                    break
            if hot != self.hover_tool:
                self.hover_tool = hot
                self._present_tool()
            if not self.tracking_tool:
                self.tracking_tool = True
                tme = TRACKMOUSEEVENT(sizeof(TRACKMOUSEEVENT), 2, hwnd, 0)
                user32.TrackMouseEvent(byref(tme))
            return 0
        if msg == WM_MOUSELEAVE:
            self.tracking_tool = False
            if self.hover_tool != -1:
                self.hover_tool = -1
                self._present_tool()
            return 0
        if msg == WM_LBUTTONUP:
            x, y = lo(lp), hi(lp)
            _, _, rects = self._tool_layout()
            nb = len(TB_ITEMS)
            for i, r in enumerate(rects):
                if not self._in(x, y, r[1:]):
                    # 颜色块被选中时外面会套一圈高亮环，这一圈也得能点中，
                    # 否则用户想点"当前颜色"反而点了没反应
                    if not r[0].startswith("@color"):
                        continue
                    m = self.px(3)
                    if not self._in(x, y, (r[1] - m, r[2] - m,
                                           r[3] + m * 2, r[4] + m * 2)):
                        continue
                if i >= nb:
                    name = r[0]
                    if name.startswith("@color"):
                        self.anno_color = int(name[6:])
                        self._apply_opts()
                    elif name.startswith("@penw"):
                        self.pen_width = int(name[5:])
                        user32.SetCursor(self._pen_cursor())
                    elif name.startswith("@brush"):
                        self.mosaic_brush = int(name[6:])
                        user32.SetCursor(self._brush_cursor())
                    elif name.startswith("@block"):
                        self.mosaic_block = int(name[6:])
                    else:
                        return 0                       # @label：纯文字，不响应
                    self._present_tool()
                    return 0
                kind = TB_ITEMS[i][0]
                if kind == "reselect":
                    self._close_tool()
                    self.sel = None
                    self.sel_shape = None
                    self.shapes = []
                    self.mode = None
                    self.auto_rect = None
                    # 撤销栈也要清：否则 Ctrl+Z 能把上一轮的标注
                    # 整批拉回到新选区里（坐标还是旧的，看着像乱码）
                    self.undo_stack.clear()
                    self.redo_stack.clear()
                    self._redraw_overlay()
                elif kind in ("pen", "mosaic", "arrow"):
                    if self.mode == kind:
                        self._leave_mode()
                    else:
                        self._begin_mode(kind)
                elif kind == "undo":
                    self._undo()
                elif kind == "copy":
                    self._action_copy()
                elif kind == "save":
                    self._action_save()
                else:
                    self._cancel_capture()
                return 0
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    # ================================================== 动作
    def _render_result(self):
        """裁出选区（含文字标注），返回 (BGRA buffer, w, h)"""
        if not self.sel or not self.dc_shot or not self.hdc_screen:
            return None, 0, 0
        x, y, ww, hh = self._clamp_sel(self.sel)
        sdc = self.hdc_screen
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = ww
        bi.bmiHeader.biHeight = -hh
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0
        bits = c_void_p()
        bmp = gdi32.CreateDIBSection(sdc, byref(bi), DIB_RGB_COLORS, byref(bits), None, 0)
        if not bmp:
            return None, 0, 0
        dc = gdi32.CreateCompatibleDC(sdc)
        old = gdi32.SelectObject(dc, bmp)
        gdi32.BitBlt(dc, 0, 0, ww, hh, self.dc_shot, x, y, SRCCOPY)
        if self.shapes:
            g = GFX(dc)
            self._draw_shapes(g, -x, -y, False)   # 不把选框虚线烤进成品
            g.close()
        raw = ctypes.string_at(bits, ww * hh * 4)
        gdi32.SelectObject(dc, old)
        gdi32.DeleteDC(dc)
        gdi32.DeleteObject(bmp)
        # 必须返回 ctypes 缓冲（持有内存），GDI+ 位图只是引用这块内存
        return ctypes.create_string_buffer(raw, len(raw)), ww, hh

    def _action_copy(self):
        buf, ww, hh = self._render_result()
        if not buf:
            return
        dib = make_dib(buf, ww, hh)
        ok = False
        for _ in range(5):
            if user32.OpenClipboard(self.hwnd_tool or self.hwnd_overlay):
                user32.EmptyClipboard()
                mem = windll.kernel32.GlobalAlloc(0x0042, len(dib))  # GMEM_MOVEABLE
                if mem:
                    ptr = windll.kernel32.GlobalLock(mem)
                    if ptr:
                        ctypes.memmove(ptr, dib, len(dib))
                        windll.kernel32.GlobalUnlock(mem)
                        if user32.SetClipboardData(8, mem):   # CF_DIB
                            ok = True
                        else:
                            windll.kernel32.GlobalFree(mem)   # 交给剪贴板失败 → 自己释放
                    else:
                        # 锁定失败（内存配额耗尽等）→ 绝不能往空指针写
                        windll.kernel32.GlobalFree(mem)
                user32.CloseClipboard()
                break
            time.sleep(0.05)
        if not ok:
            # 复制失败就别把截图扔了：保持原样，用户还能重试或改用保存
            self._balloon("复制失败", "剪贴板被别的程序占用，请重试")
            return
        self._cancel_capture()
        self._balloon("已复制到剪贴板", "%d × %d 像素" % (ww, hh))

    def _restore_after_dialog(self):
        """对话框取消 / 保存失败后，把遮罩和工具条放回来（截图不能丢）"""
        if not self.hwnd_overlay:
            return
        user32.ShowWindow(self.hwnd_overlay, SW_SHOW)
        user32.SetForegroundWindow(self.hwnd_overlay)
        user32.SetFocus(self.hwnd_overlay)
        self._redraw_overlay()
        self._show_tool()

    def _action_save(self):
        buf, ww, hh = self._render_result()
        if not buf:
            return
        # 默认文件名：截图_2026-10-25_02-51-36.png（一眼看出是什么时候截的）
        default = "截图_%s.png" % time.strftime("%Y-%m-%d_%H-%M-%S")
        folder = self._pictures_dir()
        # 只把遮罩藏起来而不是销毁：万一点「取消」，截的图和标注还能继续用
        if self.hwnd_overlay:
            user32.ShowWindow(self.hwnd_overlay, SW_HIDE)
        self._close_tool()
        # 对话框不挂在已隐藏的主窗口上（owner=None）：否则个别情况下
        # 它会弹到别的窗口后面，看起来像「点了保存没反应」
        path = self._save_dialog(None, folder, default)
        if not path:
            self._restore_after_dialog()
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        img = buf_to_image(buf, ww, hh)
        status = gdip.GdipSaveImageToFile(img, path, byref(PNG_CLSID), None)
        gdip.GdipDisposeImage(img)
        if status != 0:
            # 写盘失败（没权限 / 磁盘满）不能连截图一起丢掉
            self._restore_after_dialog()
            self._balloon("保存失败", "请检查目标文件夹权限或磁盘空间")
            return
        self._close_overlay()
        self._balloon("已保存", os.path.basename(path))

    def _pictures_dir(self):
        d = os.path.join(os.path.expanduser("~"), "Pictures")
        if not os.path.isdir(d):
            d = os.path.expanduser("~")
        return d

    def _save_dialog(self, owner, folder, default):
        ofn = OPENFILENAMEW()
        ofn.lStructSize = sizeof(OPENFILENAMEW)
        ofn.hwndOwner = owner
        buf = ctypes.create_unicode_buffer(1024)
        buf.value = default
        filt = ctypes.create_unicode_buffer("PNG 图片\0*.png\0所有文件\0*.*\0", 128)
        ofn.lpstrFile = ctypes.cast(buf, w.LPWSTR)
        ofn.nMaxFile = 1024
        ofn.lpstrFilter = ctypes.cast(filt, w.LPCWSTR)
        ofn.nFilterIndex = 1
        ofn.lpstrInitialDir = folder
        ofn.lpstrTitle = "保存截图"
        ofn.lpstrDefExt = "png"
        ofn.Flags = 0x00000002 | 0x00000800 | 0x00080000 | 0x00000008
        if comdlg32.GetSaveFileNameW(byref(ofn)):
            return buf.value
        return None

    # ================================================== 托盘 & 热键
    def _make_icon(self):
        S = 32
        buf = ctypes.create_string_buffer(S * S * 4)   # 全 0 → 透明背景
        img = P()
        # 必须是 32bppARGB(0x0026200A)，用 32bppRGB 会丢掉 alpha，圆角外变黑
        gdip.GdipCreateBitmapFromScan0(S, S, S * 4, 0x0026200A, buf, byref(img))
        g = P()
        gdip.GdipGetImageGraphicsContext(img, byref(g))
        gdip.GdipSetSmoothingMode(g, SMOOTH_AA)
        gdip.GdipSetPixelOffsetMode(g, PIXEL_HALF)

        b = P()
        gdip.GdipCreateSolidFill(C_ACCENT, byref(b))
        path = P()
        gdip.GdipCreatePath(0, byref(path))
        d = 16.0
        gdip.GdipAddPathArc(path, 2, 2, d, d, 180, 90)
        gdip.GdipAddPathArc(path, 30 - d, 2, d, d, 270, 90)
        gdip.GdipAddPathArc(path, 30 - d, 30 - d, d, d, 0, 90)
        gdip.GdipAddPathArc(path, 2, 30 - d, d, d, 90, 90)
        gdip.GdipClosePathFigure(path)
        gdip.GdipFillPath(g, b, path)
        gdip.GdipDeletePath(path)
        gdip.GdipDeleteBrush(b)

        pen = P()
        gdip.GdipCreatePen1(0xFFFFFFFF, 2.2, 0, byref(pen))
        pts = [
            (9, 13, 9, 9), (9, 9, 13, 9),
            (23, 13, 23, 9), (23, 9, 19, 9),
            (9, 19, 9, 23), (9, 23, 13, 23),
            (23, 19, 23, 23), (23, 23, 19, 23),
        ]
        for x1, y1, x2, y2 in pts:
            gdip.GdipDrawLine(g, pen, x1, y1, x2, y2)
        gdip.GdipDeletePen(pen)
        b2 = P()
        gdip.GdipCreateSolidFill(0xFFFFFFFF, byref(b2))
        gdip.GdipFillRectangle(g, b2, 14.6, 14.6, 2.8, 2.8)
        gdip.GdipDeleteBrush(b2)

        gdip.GdipDeleteGraphics(g)
        hicon = w.HICON()
        gdip.GdipCreateHICONFromBitmap(img, byref(hicon), 0)
        gdip.GdipDisposeImage(img)
        return hicon

    def create_tray(self):
        self.hicon = self._make_icon()
        nid = self.tray
        nid.cbSize = sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd_main
        nid.uID = 1
        nid.uFlags = 0x1 | 0x2 | 0x4      # MESSAGE | ICON | TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.hicon
        nid.szTip = APP_TITLE + " · 截图  " + self._hotkey_label()
        shell32.Shell_NotifyIconW(0, byref(nid))

    def _update_tray_tip(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd_main
        nid.uID = 1
        nid.uFlags = 0x4                  # NIF_TIP
        nid.szTip = APP_TITLE + " · 截图  " + self._hotkey_label()
        shell32.Shell_NotifyIconW(1, byref(nid))   # NIM_MODIFY

    def _balloon(self, title, text):
        nid = NOTIFYICONDATAW()
        nid.cbSize = sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd_main
        nid.uID = 1
        nid.uFlags = 0x10      # NIF_INFO
        nid.szInfoTitle = title
        nid.szInfo = text
        nid.dwInfoFlags = 0x1  # NIIF_INFO
        shell32.Shell_NotifyIconW(1, byref(nid))

    def _on_tray_msg(self, lp):
        if lp == WM_RBUTTONUP or lp == WM_RBUTTONDOWN:
            if lp == WM_RBUTTONUP:
                self._tray_menu()
        elif lp == WM_LBUTTONUP:
            self._show_panel()
        elif lp == WM_LBUTTONDBLCLK:
            self.start_capture()

    def _tray_menu(self):
        pt = w.POINT()
        user32.GetCursorPos(byref(pt))
        hm = user32.CreatePopupMenu()
        user32.AppendMenuW(hm, 0x0, 1, "截图\t" + self._hotkey_label())
        user32.AppendMenuW(hm, 0x0, 2, "显示主界面")
        user32.AppendMenuW(hm, 0x800, 0, None)
        user32.AppendMenuW(hm, 0x0, 5, "更改快捷键…")
        checked = self.autostart_enabled()
        user32.AppendMenuW(hm, 0x8 if checked else 0x0, 4, "开机自动启动")
        user32.AppendMenuW(hm, 0x800, 0, None)
        user32.AppendMenuW(hm, 0x0, 3, "退出 SnapLite")
        user32.SetForegroundWindow(self.hwnd_main)
        cmd = user32.TrackPopupMenu(hm, 0x0100 | 0x0080 | 0x0004 | 0x0008,
                                    pt.x, pt.y, 0, self.hwnd_main, None)
        user32.PostMessageW(self.hwnd_main, WM_NULL, 0, 0)
        user32.DestroyMenu(hm)
        if cmd == 1:
            self.start_capture()
        elif cmd == 2:
            self._show_panel()
        elif cmd == 3:
            self.quit()
        elif cmd == 4:
            self.set_autostart(not checked)
        elif cmd == 5:
            self._show_panel()
            self._begin_record()

    # ---------- 开机自启
    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    RUN_NAME = "SnapLite"

    def _launch_command(self):
        if getattr(sys, "frozen", False):
            return '"%s"' % sys.executable
        return '"%s" "%s"' % (sys.executable, os.path.abspath(__file__))

    def autostart_enabled(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.RUN_KEY) as k:
                v, _ = winreg.QueryValueEx(k, self.RUN_NAME)
                return str(v) == self._launch_command()
        except FileNotFoundError:
            return False          # 键或值不存在 = 没开
        except Exception:
            return False

    def set_autostart(self, on):
        try:
            # 用 CreateKey 而不是 OpenKey：Run 键在全新系统上可能还不存在，
            # OpenKey 会抛 FileNotFoundError 被下面的 except 吞掉，
            # 结果就是「提示已开启、实际没写入」。
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.RUN_KEY) as k:
                if on:
                    winreg.SetValueEx(k, self.RUN_NAME, 0, winreg.REG_SZ,
                                      self._launch_command())
                else:
                    try:
                        winreg.DeleteValue(k, self.RUN_NAME)
                    except FileNotFoundError:
                        pass
            self._balloon("开机自动启动", "已开启，下次登录自动待命" if on else "已关闭")
        except Exception:
            self._balloon("设置失败", "无法写入当前用户的启动项")

    def quit(self):
        try:
            shell32.Shell_NotifyIconW(2, byref(self.tray))
        except Exception:
            pass
        user32.UnregisterHotKey(None, HOTKEY_ID)
        if self.hwnd_tool:
            user32.DestroyWindow(self.hwnd_tool)
        self.hwnd_tool = None
        # 工具条的离屏表面（DC + DIB）也要释放，否则退出时留一堆 GDI 句柄
        try:
            self._release_tool_surface()
        except Exception:
            pass
        try:
            self._release_main_surface()
        except Exception:
            pass
        if self.hwnd_overlay:
            user32.DestroyWindow(self.hwnd_overlay)
        self.hwnd_overlay = None
        self._release_capture_buffers()
        # 托盘图标是自己加载的，销毁窗口不会自动释放
        try:
            if self.hicon:
                user32.DestroyIcon(self.hicon)
                self.hicon = None
        except Exception:
            pass
        if self.hwnd_main:
            user32.DestroyWindow(self.hwnd_main)

    # ================================================== 主循环
    def run(self):
        self.create_main()
        self.create_tray()
        self._register_hotkey()
        user32.InvalidateRect(self.hwnd_main, None, False)
        dbg("loop start hotkey=%s(%s) visible=%s"
            % (self.hotkey_ok, self._hotkey_label(),
               user32.IsWindowVisible(self.hwnd_main)))

        msg = MSG()
        while user32.GetMessageW(byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self.start_capture()
                continue
            user32.TranslateMessage(byref(msg))
            user32.DispatchMessageW(byref(msg))


def main():
    try:
        try:
            if SetProcessDpiAwarenessContext is not None:
                SetProcessDpiAwarenessContext(c_void_p(-4))   # PER_MONITOR_AWARE_V2
            else:
                user32.SetProcessDPIAware()                   # 老系统降级
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass
        prev = user32.FindWindowW("SnapLiteMain", APP_NAME)
        dbg("start frozen=%s prev=%s" % (getattr(sys, "frozen", False), prev))
        if prev:
            user32.ShowWindow(prev, SW_SHOW)
            user32.SetForegroundWindow(prev)
            return
        App().run()
    except Exception:
        _write_log(LOG_FILE,
                   "\n===== %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S")
                   + traceback.format_exc())


if __name__ == "__main__":
    main()
