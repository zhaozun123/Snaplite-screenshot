# -*- coding: utf-8 -*-
"""
SnapLite - 轻量截图工具
纯 Windows 原生接口实现（GDI / GDI+），无任何第三方依赖。

功能：
  · 全局热键唤起（可在面板或托盘菜单里改成任意组合）
  · 自动识别鼠标下的窗口/控件区域，单击直接捕获；也可自由拖拽框选
  · 框选后可用 8 个手柄调整范围、拖动整体移动
  · 画笔 / 马赛克 / 箭头 / 方框 / 圆形标注
  · 复制到剪贴板 / 保存为 PNG
  · 常驻托盘；✕ 与 — 均为收进托盘，退出在托盘右键菜单

代码结构：
  单文件约 4700 行，顶层依次是：Win32 结构体与函数原型 → 配置 → 配色常量
  → GFX（GDI+ 绘图封装）→ 屏幕抓取 → 悬停自动识别 → App（程序主体）。
  App 内部按职责分八段，搜「=== N.」可跳到每段开头，段目见 App 的类说明。
"""
import ctypes
import os
import sys
import threading
import time
import traceback
import winreg
from math import hypot, radians, cos, sin, floor, ceil, sqrt
from ctypes import (windll, byref, sizeof, POINTER, WINFUNCTYPE, Structure,
                    c_int, c_uint, c_long, c_ulong, c_ushort, c_ulonglong,
                    c_void_p, c_float, c_ubyte, c_short, c_wchar)

import ctypes.wintypes as w

# GIL 切换周期从默认 5ms 压到 1ms。
#
# 拖动时后台预取线程每轮都要做几步纯 Python 的活（拼缓冲、逐行裁切、判「是不是
# 抓到自己」）。默认 5ms 意味着它一旦拿到 GIL 就能攥满 5ms，主线程这期间既画不
# 了下一帧、也没法更新取景目标，而窗口还在往前跑 —— 覆盖跟不上，于是频繁触发
# 主线程补抓（一次十几毫秒），那一下就是「快速拖动时的抖动」。
#
# 实测（面板 340×148、每帧 40 像素、50 帧）：补抓 15 次 → 1 次，提交间隔的
# 变异系数 33.8% → 13.2%。代价是线程切换略频繁，可忽略。
try:
    sys.setswitchinterval(0.001)
except Exception:
    pass

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
APP_VERSION = "1.4.0"
APP_TITLE = "%s %s" % (APP_NAME, APP_VERSION)

DEBUG = bool(os.environ.get("SNAPLITE_DEBUG"))

# 配置与日志统一放 %APPDATA%\SnapLite\：
# 绿色版 exe 可能被放在 C:\Program Files\ 这类只读目录，写不进去。
CFG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "SnapLite")
CFG_FILE = os.path.join(CFG_DIR, "config.ini")
LOG_FILE = os.path.join(CFG_DIR, "snap_error.log")
DEBUG_LOG = os.path.join(CFG_DIR, "snap_debug.log")


LOG_MAX_BYTES = 512 * 1024      # 单个日志文件的上限，超了只留最后一半


def _write_log(path, text):
    """写日志失败也不能影响主流程（磁盘满、权限不足都可能）。

    必须限长：`_safe_proc` 把每个窗口过程都包了起来，万一某个异常在**每帧**
    都触发一次（8ms 一帧），日志会以每秒兆字节的速度往盘上写，几分钟就把
    系统盘塞满。超过上限时只保留最后一半，既保住现场又不失控。"""
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            keep = LOG_MAX_BYTES // 2
            with open(path, "rb") as f:
                f.seek(-keep, os.SEEK_END)
                tail = f.read()
            with open(path, "wb") as f:
                f.write(b"[... older entries dropped ...]\n" + tail)
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


def dbg(msg):
    """调试日志：只在 DEBUG 打开时写文件，正式版一次磁盘 IO 都不做"""
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
    """读快捷键配置。文件不存在、读不出来、内容非法，一律退回默认 Ctrl+Alt+A"""
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
    """写快捷键配置，返回是否成功。失败（无权限等）不影响程序继续跑"""
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
# 分辨率 / 显示器插拔 / 改缩放：窗口可能被留在屏幕外，玻璃底的坐标也全变了
WM_DISPLAYCHANGE = 0x007E
WM_SETCURSOR = 0x0020
WM_MOUSELEAVE = 0x02A3
WM_HOTKEY = 0x0312
WM_TIMER = 0x0113
WM_CAPTURECHANGED = 0x0215
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_GLASS_FRAME = WM_APP + 2       # 后台线程算好一帧，叫主线程贴上去

# 玻璃底自动跟随桌面。两档节拍，理由见下面的实测数字。
#
# 为什么必须自己「拍」：视频播放、网页动画、滚动这些「画面自己在变」的情况
# 不产生任何窗口事件（只有搬家 / 切前台才产生），光靠事件钩子的话玻璃底会
# 一直停在很久以前那一帧。所以只能按节拍主动抓来比对。
#
# 为什么分两档：桌面静止时反复做完整刷新是纯浪费，空闲档只做「抓 + 比对」
# （发现像素没变就既不模糊也不重画），确认真的变了才切高频档。
#
# 【重要】耗时里「等」和「算」必须分开看。用 time.process_time 量出来：
#     抓屏      墙钟 6.16ms   真 CPU 0.00ms   ← 全在等 DWM 给快照
#     提交一帧  墙钟 3.07ms   真 CPU 1.04ms
#     整条刷新  墙钟 12.3ms   真 CPU 0.26ms   ← 98% 是等待
# 所以「12.5ms 一帧」不等于「烧掉 12.5ms 的 CPU」。按墙钟算占用会高估 40 倍，
# 早前注释里写的 2.4% / 10% 就是这么算错的，已作废。
# 真实占用 = 0.26ms × 帧率：12fps→0.3%、30fps→0.8%、60fps→1.6%（单核）。
# 唯一的代价是「主线程被阻塞」：12.3ms 里主线程动不了，所以帧间隔不能贴到
# 12.3ms 以下，否则消息响应会被挤满。
#
# 【定时器精度】SetTimer 会被量化到系统时间片 15.6ms 的整数倍，实测：
#     设  8ms → 实际 15.6ms（64 次/秒）
#     设 20ms → 实际 31.3ms（32 次/秒）
#     设 33ms → 实际 47.6ms（21 次/秒）  ← 想设 33 会掉到 21 帧，不能这么写
#
# 【为什么不靠定时器做活跃档】定时器的毫秒数跟屏幕刷新率**除不尽**，就会产生
# 「拍频」：帧有时跨 5 次刷新、有时跨 6 次，多出来的那一次每几帧出现一次，
# 在人眼里就是一阵一阵的顿挫。实测（166Hz 屏，消隐 6.01ms）：
#     定时器 31ms 一帧 = 5.13 个刷新周期 → 顿挫频率 4.2Hz  ← 人眼最敏感的一段
#     拖动   9.2ms 一帧 = 1.54 个刷新周期 → 顿挫频率 58Hz   ← 快到看不见
# 这解释了「拖动没问题、静止还抖」。关键不是「对齐消隐」，是**帧率要够高**：
# 帧间隔的小数部分会被摊到每一两帧上，帧率越高摊得越密，顿挫频率就越往
# 人眼分辨不出的地方跑。所以活跃档改由后台线程驱动，一轮做完立刻下一轮。
#
# 试过「每轮先 DwmFlush 等消隐」，实测反而更差，已放弃（数字见 _glass_live_loop）：
#     等消隐 58.7 帧 / 顿挫 17.0Hz      不等 85.0 帧 / 顿挫 51.9Hz
#
# 定时器保留下来做两件事：桌面静止时的低频探测（250ms）、以及老系统（没有
# 「抓屏排除自己」）的退路。活跃档由线程接手后，主线程这一拍直接返回。
TIMER_GLASS = 0x51
GLASS_TICK_IDLE = 250            # 空闲档：桌面没动静时的轻探（只抓不画）
GLASS_TICK_LIVE = 20             # 老系统退路用：31ms 一拍，约 32fps
GLASS_LIVE_HOLD = 1.0            # 画面静止多久退回空闲档（秒），避免来回跳档
GLASS_USE_LIVE_THREAD = True     # 活跃档交给后台线程（单元测试里关掉走同步路径）
# 拖动时的取背景方式。True = 后台线程一直抓「窗口周围一大块桌面」，主线程
# 每帧只从里面裁；False = 老办法，主线程每帧现抓（单帧 5.7~27.9ms 不等）。
# 关掉只给单元测试用：测试里没有真消息循环，多一条线程只会让断言更难写。
DRAG_PREFETCH = True
# 预取快照的**余量**。整块要模糊一次（546×364 约 6ms，放在后台线程里），
# 片越大每轮越慢、快照反而跟不上手，所以「加大余量」是错的方向：面积从 198k
# 涨到 325k，整块模糊从 5.8ms 涨到 11ms，抢走主线程时间，单帧中位 3.8→9.7ms
# —— 补抓没了，整体反而更钝（见 ARCHITECTURE 坑 20）。
#
# 正确的做法是**面积不变、只改形状**：面板是 340×148 的横条，正方形余量在运动
# 方向上只有 96px，垂直那 96px 基本浪费。按运动方向把余量拉长、另一方向收窄，
# 面积一样（每轮耗时不变），覆盖能力按方向翻几倍。实测同一趟 5000px/s：
#     96×96（现状）     补抓 9/110   单帧最大 29.7ms
#     192×48（同面积）  补抓 0/110   单帧最大  5.2ms
# DRAG_PAD 现在只是「对称兜底」：首帧补抓、以及没有方向信息时用它。
DRAG_PAD = 96
# 余量形状的面积目标（像素²）。现状 546×364 = 198744，保持这个面积，模糊耗时
# 就还是那 6ms，主线程一帧都不会变慢。
DRAG_AREA = 546 * 364
# 前向余量最多占总余量的比例。0.8 = 前 80% / 后 20%（单方向全速时）。
DRAG_LEAN_MAX = 0.80
# 单帧位移达到这么多像素就算「全速」，前向倾斜到 DRAG_LEAN_MAX。
# 12ms 一轮的 60px ≈ 5000px/s，正好是最容易抖的那档。
DRAG_LEAN_FULL = 60.0
# 主线程补抓时只抓这么点余量：一帧最多移动几十像素，够用就行。
# 补抓是拖动里唯一会让主线程等 DWM 的地方，抓得越小卡得越轻（39 ≈ 6ms，
# 而按 DRAG_PAD 抓要大一半）。它靠「尺寸取 BLUR_F 的整数倍」保证与小片
# 相位一致 —— 见 _drag_world_rect 的说明。
DRAG_PAD_SMALL = 39
# 抓和模糊拆成两条线程并行（流水线）。**默认关闭**：后台刷新周期确实从 12ms
# 降到 6.4ms、补抓从 14/50 降到 1/50，但两个 Python 线程争 GIL，主线程每帧从
# 3.6ms 涨到 10.9ms，帧间隔 12ms → 19ms，整体反而更不匀（CV 6.6% → 21.4%）。
# 留在这里是因为换个语言/去掉 Python 层拷贝就能吃到这个收益，别当它没验过。
DRAG_PIPELINE = False
# 预取线程的优先级。抓屏的 BitBlt 会放掉 GIL，这一刻两个线程在争 CPU 与 DWM
# 表面的交出顺序；把后台降到 BELOW_NORMAL，主线程要画帧时就能先拿到手。
# 实测不改变中位（3.7ms），但把最坏那一帧收敛了（30 → 15ms 一档）。
DRAG_THREAD_LOW_PRIORITY = True
THREAD_PRIORITY_BELOW_NORMAL = -1
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
SWP_NOACTIVATE = 0x0010
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
GWL_EXSTYLE = -20            # GetWindowLongW 的索引：取扩展样式（含 WS_EX_TOPMOST）

SW_HIDE = 0
SW_SHOW = 5
SW_SHOWNA = 8

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0

# 「抓屏时排除自己」的窗口显示属性（SetWindowDisplayAffinity）
WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011
CAPTURE_EXCLUDE_MIN_BUILD = 19041    # Win10 2004 才有；更早的系统会抓一片黑

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
    """按四段拼一个 GUID 结构体。GDI+ 保存图片要传编码器 CLSID"""
    return GUID(d1, d2, d3, (c_ubyte * 8)(*tail))


PNG_CLSID = _guid(0x557CF406, 0x1A04, 0x11D3, (0x9A, 0x73, 0x00, 0x00, 0xF8, 0x1E, 0xF3, 0x2E))


class RTL_OSVERSIONINFOW(ctypes.Structure):
    """RtlGetVersion 的入参/出参。GetVersionEx 会被兼容层按 manifest 骗，
    这个不会 —— 判断「系统支持哪个 API」时必须以它为准。"""
    _fields_ = [("dwOSVersionInfoSize", c_ulong), ("dwMajorVersion", c_ulong),
                ("dwMinorVersion", c_ulong), ("dwBuildNumber", c_ulong),
                ("dwPlatformId", c_ulong), ("szCSDVersion", c_wchar * 128)]


_win_build_cache = [0]


def win_build():
    """真实的 Windows build 号（如 19045 / 22631），取不到返回 0"""
    if not _win_build_cache[0]:
        try:
            fn = _proto(windll.ntdll.RtlGetVersion,
                        [POINTER(RTL_OSVERSIONINFOW)], c_long)
            vi = RTL_OSVERSIONINFOW()
            vi.dwOSVersionInfoSize = sizeof(RTL_OSVERSIONINFOW)
            fn(byref(vi))
            _win_build_cache[0] = int(vi.dwBuildNumber)
        except Exception:
            _win_build_cache[0] = 0
    return _win_build_cache[0]


# ============================================================ 函数原型
def _proto(fn, args, ret=c_int):
    """统一声明 DLL 函数签名。

    64 位下不声明 argtypes，指针会被当成 32 位 int 截断 —— 表现是随机
    报错或直接崩溃，而且崩在莫名奇妙的地方。所有 DLL 调用都必须过这里。"""
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
_proto(user32.GetWindowLongW, [w.HWND, c_int], c_long)
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
# 让窗口「抓屏时不存在，屏幕上照常显示」。Win10 2004 起才能拿到背后内容，
# 更早的系统会退化成抓一片黑，所以调用前必须先过 win_build() 判断。
_proto(user32.SetWindowDisplayAffinity, [w.HWND, w.DWORD], w.BOOL)
_proto(user32.UpdateLayeredWindow, [w.HWND, w.HDC, POINTER(w.POINT), POINTER(w.SIZE),
                                   w.HDC, POINTER(w.POINT), w.DWORD, c_void_p, w.DWORD],
       w.BOOL)
_proto(user32.RegisterHotKey, [w.HWND, c_int, c_uint, c_uint], w.BOOL)
_proto(user32.UnregisterHotKey, [w.HWND, c_int], w.BOOL)
_proto(user32.GetKeyState, [c_int], c_short)
_proto(user32.GetAsyncKeyState, [c_int], c_short)

# 窗口事件钩子：别的窗口一动就知道，玻璃底才有得跟
WINEVENTPROC = WINFUNCTYPE(None, w.HANDLE, w.DWORD, w.HWND, w.LONG, w.LONG,
                           w.DWORD, w.DWORD)
_proto(user32.SetWinEventHook, [w.DWORD, w.DWORD, w.HANDLE, WINEVENTPROC,
                                w.DWORD, w.DWORD, w.DWORD], w.HANDLE)
_proto(user32.UnhookWinEvent, [w.HANDLE], w.BOOL)
_proto(user32.SetTimer, [w.HWND, ctypes.c_size_t, w.UINT, c_void_p], ctypes.c_size_t)
_proto(user32.KillTimer, [w.HWND, ctypes.c_size_t], w.BOOL)
# explorer 重启后会给所有顶层窗口广播这条消息，收到就得把托盘图标重新挂上去
# （图标本身跟着进程，但任务栏那一条是 explorer 的内存，重启就没了）。
_proto(user32.RegisterWindowMessageW, [w.LPCWSTR], w.UINT)

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
_proto(gdip.GdipAddPathRectangle, [P, c_float, c_float, c_float, c_float])
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
# GlobalFree 必须声明：入参是 HGLOBAL 句柄，不声明会被当成 32 位 int 截断，
# 表现是释放到一个非法地址 —— 要么直接崩，要么悄悄漏掉这块内存。
# 返回值同样是 HGLOBAL，NULL 才算成功。
_proto(windll.kernel32.GlobalFree, [c_void_p], c_void_p)
_proto(windll.kernel32.GetModuleHandleW, [w.LPCWSTR], w.HINSTANCE)
_proto(windll.kernel32.GetCurrentProcessId, [], w.DWORD)
# 预取线程降优先级用。GetCurrentThread 返回的是伪句柄（-2），必须声明成指针
# 宽度的 HANDLE，否则会被当成 32 位 int 截断成 0xFFFFFFFE 之外的垃圾。
_proto(windll.kernel32.GetCurrentThread, [], w.HANDLE)
_proto(windll.kernel32.SetThreadPriority, [w.HANDLE, c_int], w.BOOL)
# 单实例互斥。用命名互斥而不是「查窗口是否存在」：后者有竞态 —— onefile 要
# 解压 2~3 秒，这期间窗口还没建，两次双击都能通过检查，结果起两个面板互相抓屏。
_proto(windll.kernel32.CreateMutexW, [c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE)
_proto(windll.kernel32.GetLastError, [], w.DWORD)
ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\SnapLite-SingleInstance"
_instance_mutex = None      # 单实例互斥的句柄：故意不关，活到进程退出


def _lower_this_thread():
    """把**当前**线程降到 BELOW_NORMAL（抓屏时让主线程先走）。

    BitBlt 那一下会放掉 GIL，此时两个线程在抢 CPU 和 DWM 表面的交出顺序。
    后台是「慢一点无所谓、别抢主线程」，降一档正好。失败就算了：优先级只
    影响排队顺序，不影响正确性。"""
    if not DRAG_THREAD_LOW_PRIORITY:
        return
    try:
        windll.kernel32.SetThreadPriority(
            windll.kernel32.GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL)
    except Exception:
        pass


# ============================================================ 配色
C_TAG_BG = 0x24FFFFFF          # 玻璃上的“胶囊”：半透明白，不再是一块实心灰
C_TITLE = 0xFFF5F7FA
C_SUB = 0xFFB7C0CE
C_HINT = 0xFF97A1B1
C_ACCENT = 0xFF0A84FF          # iOS 系统蓝
C_ACCENT_HI = 0xFF3D9BFF
C_HOVER_SOFT = 0x2EFFFFFF

# 「这是我自己」的指纹色：主按钮正常/高亮两种底色。
# 拖动时靠它判断抓回来的是桌面还是面板本身 —— 玻璃底每帧都在换（窗口位置变了，
# 背后桌面就不一样），只有 UI 固定在窗口里，按钮那块纯色是唯一不变的锚点。
SELF_BTN_COLORS = (C_ACCENT, C_ACCENT_HI)

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
# 工具条整体缩放。下面所有 TB_* 尺寸是 TB_SCALE=1.0 时的设计稿数值，
# 实际像素 = TB_* × TB_SCALE × DPI 缩放（见 App.tb）。
# 想整体大一点/小一点只改这一个数，别再逐个调按钮宽高和字号。
# 0.86：按钮 76→65、高 44→38、字号 12→10，整条 796→677（1920 屏宽的 35%）。
# 选它是因为实测这一档在 100% 缩放的屏上，字仍然清晰、也没挤到边。
# 注意它同时作用于图标和字号，所以不要和 TB_BTN_W 各自乱调 —— 会错位。
TB_SCALE = 0.86
# 字号单独缩放，故意和 TB_SCALE 不同步。
# 外框按 0.86 收能省 120px 宽度，但字号跟着缩到 10px 就偏小了 —— 中文在 10px
# 下笔画会糊成一片。所以字号按 1.17 放大（12 → 14px）：实测 11px 看着吃力，
# 14px 是「马赛克」在 69px 按钮里能放下的最大一档（再大就折行，见 TB_BTN_W）。
# 改这里只影响文字大小，不影响按钮、图标、圆角。
TB_FONT_SCALE = 1.17
# 按钮版式。这几个数决定工具条总宽 = TB_BTN_W×N + TB_BTN_GAP×(N-1)。
# 按钮内部不再用固定坐标摆图标和文字，而是「图标 + 缝 + 文字」作为一整块
# 在按钮里水平居中（见 _draw_tool）。这里写的是 TB_SCALE=1.0 时的设计稿宽度。
# 76→80 是为了放下 14px 的「马赛克」：字号 1.17 时文字推进宽度 47.9px，
# 65px 按钮里能用的文字框只有 47.3px，会折成两行。80×0.86=69px 后可用 49.3px，
# 富余 1.4px。改成 72px 就能放到 15px，但那要多占 70px 宽度，不值。
# 真正会不会被挤出按钮，由 tests/test_tb_center.py 的「单行没折」一项把关
# —— 改 TB_SCALE / TB_FONT_SCALE / TB_BTN_W / 标签文字后都要跑它。
TB_BTN_W = 80           # 单颗按钮宽（×TB_SCALE = 69px）
TB_BTN_H = 44           # 单颗按钮高
TB_BTN_GAP = 4          # 按钮之间的缝（缝必须全透明，见 _tool_shape_cov）
TB_ICON_SIZE = 17       # 图标边长
TB_ICON_GAP = 6         # 图标右边缘到文字左边缘的缝
# 每个图标在 TB_ICON_SIZE 方框里的「真实墨迹边界」(左, 上, 右, 下)，闭区间。
# 图标是画在 15 单位网格上的，但墨迹并没有占满：× 只占中间 11px，框选图标
# 占满 15px。不记这个，按方框居中就会让「取消」看起来偏右 6px。
# 由 tools/measure_ink.py 量出；tests/test_tb_center.py 会校验这张表没过期。
TB_ICON_INK = {
    "reselect": (1, 1, 15, 15),
    "pen": (3, 3, 14, 13),
    "mosaic": (2, 2, 15, 15),
    "arrow": (3, 3, 13, 13),
    "rect": (2, 3, 14, 13),
    "ellipse": (2, 3, 15, 13),
    "undo": (2, 4, 13, 11),
    "copy": (1, 1, 15, 15),
    "save": (3, 2, 13, 14),
    "cancel": (3, 4, 13, 13),
}
# 中文字形在字宽里左右留的空白（side bearing）。measure() 返回的是推进宽度，
# 比肉眼看到的字形两侧各宽出这么多；拿推进宽度当居中依据，内容会整体偏左。
# 由 tools/measure_ink.py 按「当前字号」实测：14px 下左 2~4px、右 4.5~6.5px。
# 关键关系：左留白 − 右留白 = TB_TEXT_SIDE_R − 该字的实际右留白。所以右值取 5px
# 能让 10 个标签全部落在 ±1.5px 内（「方框」的「框」字右边天然空 6.5px，是最差的那个）。
# 左值只影响「文字框够不够宽」（会不会折行）：取 4px，14px 的「马赛克」还剩 0.95px。
# 改字号后必须重跑 tools/measure_ink.py 更新这两个数，否则整块内容会偏。
TB_TEXT_SIDE_L = 4.65
TB_TEXT_SIDE_R = 5.81
# 文字垂直补偿：GDI+ 的「行内居中」按字体行高算，而微软雅黑的行高里
# descent 部分留白多，实际字形会整体偏上。实测 12px 下偏上 1px，
# 用整数补偿（半像素位移会让字形发虚），补完文字和图标就落在同一条线上。
TB_TEXT_DY = 1
TB_ITEMS = (("reselect", "重选"), ("pen", "画笔"), ("mosaic", "马赛克"),
            ("arrow", "箭头"), ("rect", "方框"), ("ellipse", "圆形"),
            ("undo", "撤销"), ("copy", "复制"),
            ("save", "保存"), ("cancel", "取消"))
# TB_ITEMS 里所有"绘图类"工具的 kind。这类按钮的行为统一是「点一次进入、
# 再点一次退出」，由 _on_tool 里同一段逻辑处理。加新绘图工具时必须同时
# 加进这里，否则 _on_tool 认不出它，会掉进兜底分支（曾导致点方框直接取消截图）。
ANNO_KINDS = ("pen", "mosaic", "arrow", "rect", "ellipse")

# 方框 / 圆形：拖动两个对角画出来；选中后拖四角改大小
BOX_HANDLE_R = 5          # 角上手柄半径
BOX_HIT_TOL = 7           # 点到边框多近算选中
BOX_MIN_SIZE = 6          # 小于这个尺寸当作误点，不落笔
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
    """取低 16 位并转成有符号数（拆鼠标消息里的 x 坐标）"""
    return c_short(v & 0xFFFF).value


def hi(v):
    """取高 16 位并转成有符号数（拆鼠标消息里的 y 坐标）"""
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
    """虚拟键码 → 给人看的名字（F1 / Enter / A…），认不出就显示 VK 十六进制"""
    if 0x70 <= vk <= 0x87:
        return "F%d" % (vk - 0x70 + 1)
    if vk in _VK_SPECIAL:
        return _VK_SPECIAL[vk]
    ch = user32.MapVirtualKeyW(vk, 2)
    if ch and 0x20 <= ch < 0x7F:
        return chr(ch).upper()
    return "VK%02X" % vk


def hotkey_label(mod, vk):
    """把修饰键和主键拼成 Ctrl+Alt+A 这种文本，界面和托盘提示都用它"""
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


# ============================================================ 绘图封装
# 字体全局共享：原来缓存在 GFX 实例上，而 GFX 是每帧新建的 —— 等于每帧
# 重建 6 个字体（GdipCreateFontFamilyFromName 会枚举字体族，很贵）。
# 实测一帧 2.80ms 里，文字占 2.0ms；提到全局后掉到 0.3ms 左右。
# 字体对象全进程只有这么几个，不回收，留到进程结束。
_FONT_CACHE = {}


class GFX:
    """GDI+ 轻量封装"""

    def __init__(self, hdc=None, ptr=None):
        """绑定一块画布（HDC 或已有 Graphics），并统一打开抗锯齿等绘制模式。

        抗锯齿、半像素偏移、双三次插值这三项每个 GFX 都要设，少一项线条就毛糙。"""
        self.hdc = hdc                       # 记下 GDI DC，供 GDI 文字渲染/度量用
        self.g = ptr if ptr else P()
        if not ptr:
            gdip.GdipCreateFromHDC(hdc, byref(self.g))
        gdip.GdipSetSmoothingMode(self.g, SMOOTH_AA)
        gdip.GdipSetTextRenderingHint(self.g, TEXT_HINT_GRIDFIT)
        gdip.GdipSetPixelOffsetMode(self.g, PIXEL_HALF)
        gdip.GdipSetInterpolationMode(self.g, 7)

    def close(self):
        """释放 GDI+ 资源。必须幂等：异常路径上可能被调用两次。

        字体不在这里删：它们是全进程共享的（见 _FONT_CACHE），删掉会连累
        别的 GFX。全进程就那几个，留到进程结束。"""
        if getattr(self, "_closed", False):
            return
        self._closed = True
        gdip.GdipDeleteGraphics(self.g)

    def __enter__(self):
        """支持 with GFX(dc) as g: 写法，保证异常时也会走 __exit__ 释放"""
        return self

    def __exit__(self, exc_type, exc, tb):
        # 异常时也必须释放：否则每帧泄漏一个 GDI+ Graphics + 字体
        """离开 with 块时释放资源；返回 False 表示异常照常往外抛，不吞掉"""
        self.close()
        return False

    def __del__(self):
        # 兜底：绘制中途抛异常时，局部变量 g 离开作用域 → 引用计数归零 →
        # 这里兜住 close()，避免 GDI+ 对象无限累积
        """兜底释放：绘制中途抛异常、对象被回收时，也不会漏下 GDI+ 句柄"""
        try:
            self.close()
        except Exception:
            pass

    # ---------- 裁剪：标注只能落在选区里（画布外的一律裁掉）
    def clip_rect(self, x, y, ww, hh):
        """限制后续绘制范围（标注不许画出选区）。

        GDI+ 和 GDI 是两套裁剪，必须同时设。只设 GDI+ 的话，由 GDI 画的
        文字仍然会漏到选区外面。"""
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
        """恢复 clip_save 时的裁剪状态。clip_rect 是「相交」语义，
        嵌套裁剪不配对恢复的话，里层画完外层就漏了"""
        gdip.GdipRestoreGraphics(self.g, st)
        stack = getattr(self, "_gdi_stack", [])
        if self.hdc and stack:
            gdi32.RestoreDC(self.hdc, stack.pop())

    # ---------- 基础图形
    def _brush(self, argb):
        """建一支纯色画刷。调用方负责 GdipDeleteBrush —— 这里不删"""
        b = P()
        gdip.GdipCreateSolidFill(argb, byref(b))
        return b

    def fill(self, argb, x, y, ww, hh):
        """用纯色填满矩形（alpha 直接参与混合，半透明色点也走这里）"""
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
        """画实心圆，参数是圆心和半径（箭头头部、颜色圆点用）"""
        if r <= 0:
            return
        b = self._brush(argb)
        gdip.GdipFillEllipse(self.g, b, cx - r, cy - r, r * 2.0, r * 2.0)
        gdip.GdipDeleteBrush(b)

    def _path_round(self, x, y, ww, hh, r):
        """造一个圆角矩形路径。半径会自动夹到不超过短边的一半，不会画出畸形"""
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
        """填充圆角矩形（主面板底、按钮底）"""
        b = self._brush(argb)
        p = self._path_round(x, y, ww, hh, r)
        gdip.GdipFillPath(self.g, b, p)
        gdip.GdipDeletePath(p)
        gdip.GdipDeleteBrush(b)

    def stroke_round(self, argb, width, x, y, ww, hh, r):
        """描边圆角矩形（按钮高光边、面板棱边）"""
        pen = P()
        gdip.GdipCreatePen1(argb, width, 0, byref(pen))
        p = self._path_round(x, y, ww, hh, r)
        gdip.GdipDrawPath(self.g, pen, p)
        gdip.GdipDeletePath(p)
        gdip.GdipDeletePen(pen)

    def line(self, argb, width, x1, y1, x2, y2, cap=0):
        """画一条直线。cap 可指定端点形状（圆头 / 平头），箭头杆要用圆头"""
        pen = P()
        gdip.GdipCreatePen1(argb, width, 0, byref(pen))
        if cap:
            gdip.GdipSetPenStartCap(pen, cap)
            gdip.GdipSetPenEndCap(pen, cap)
        gdip.GdipDrawLine(self.g, pen, x1, y1, x2, y2)
        gdip.GdipDeletePen(pen)

    def stroke_box(self, argb, width, x, y, ww, hh):
        """直角矩形描边。走路径而不是四条线：四条线在四个角上会露缝"""
        pen = P()
        gdip.GdipCreatePen1(argb, width, 0, byref(pen))
        p = P()
        gdip.GdipCreatePath(0, byref(p))
        gdip.GdipAddPathRectangle(p, float(x), float(y),
                                  float(max(0.0, ww)), float(max(0.0, hh)))
        gdip.GdipDrawPath(self.g, pen, p)
        gdip.GdipDeletePath(p)
        gdip.GdipDeletePen(pen)

    def stroke_ellipse(self, argb, width, x, y, ww, hh):
        """画椭圆描边（圆形标注用）"""
        pen = P()
        gdip.GdipCreatePen1(argb, width, 0, byref(pen))
        gdip.GdipDrawEllipse(self.g, pen, float(x), float(y),
                             float(max(0.0, ww)), float(max(0.0, hh)))
        gdip.GdipDeletePen(pen)

    # ---------- 文字
    def _font(self, size_px, bold, fam=None):
        """按 (字号, 粗体, 字体名) 取字体对象，取过就复用（全进程共享）。

        每帧新建字体会让 GDI+ 句柄一直涨，而且慢：GdipCreateFontFamilyFromName
        要枚举字体族。字体名按 微软雅黑UI → 雅黑 → Segoe UI 依次回退，
        某些系统缺字体时不至于一个字都画不出来。"""
        key = (round(size_px, 2), bool(bold), fam)
        f = _FONT_CACHE.get(key)
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
        _FONT_CACHE[key] = fnt
        return fnt

    def text(self, s, size_px, argb, x, y, ww, hh, align=0, bold=False,
             valign=1, fam=None, nowrap=False):
        """在矩形区域内画文字。

        nowrap=True 时强制不换行也不裁剪 —— "马赛克" 这种三字标签在窄按钮里
        才不会被折成两行或切掉半个字。"""
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
        """量一段文字占多宽多高。按钮宽度、提示条宽度都靠它算"""
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
    """释放 _new_dib 造出来的那组句柄。容错：重复释放不会炸"""
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
    """返回 (buffer, w, h)，32bpp top-down，BGRA

    源矩形超出屏幕时 BitBlt **不报错**，越界那几列/几行留成纯黑 —— 面板
    拖到屏幕边缘就会贴出一条黑边，宽度还随位置伸缩。所以先把源矩形钳到
    虚拟屏内，只抓相交的那块，缺的边用最近的边缘像素补（见 _fill_edges）。
    """
    hdc = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, ww, hh)
    old = gdi32.SelectObject(mem, bmp)

    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    sx, sy = max(x, vx), max(y, vy)
    ex, ey = min(x + ww, vx + vw), min(y + hh, vy + vh)
    inside = ex > sx and ey > sy
    if inside:
        gdi32.BitBlt(mem, sx - x, sy - y, ex - sx, ey - sy, hdc, sx, sy, SRCCOPY)

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

    if not inside:
        return buf                        # 整块都在屏外：只能给黑的
    if sx > x or sy > y or ex < x + ww or ey < y + hh:
        buf = _fill_edges(buf, ww, hh, sx - x, ex - x, sy - y, ey - y)
    return buf


def _fill_edges(buf, ww, hh, lx, rx, ty, by):
    """把越界留白的边用最近的有效像素补上。

    lx/rx 是有效的列区间，ty/by 是有效的行区间（都是相对本块左上角）。
    补法就是「边缘复制」：越界那几列取最外面那一条有效列，越界那几行取最
    外面那一条有效行。比留成黑边好得多 —— 黑边在深色面板里一眼就能看见，
    边缘复制看起来只是背景往屏外延了一点。"""
    stride = ww * 4
    b = bytearray(ctypes.string_at(buf, ww * hh * 4))
    if rx > lx:
        for yy in range(hh):
            o = yy * stride
            if lx > 0:
                b[o:o + lx * 4] = b[o + lx * 4:o + lx * 4 + 4] * lx
            if rx < ww:
                b[o + rx * 4:o + stride] = b[o + (rx - 1) * 4:o + rx * 4] * (ww - rx)
    if by > ty:
        if ty > 0:
            b[0:ty * stride] = b[ty * stride:ty * stride + stride] * ty
        if by < hh:
            b[by * stride:hh * stride] = b[(by - 1) * stride:by * stride] * (hh - by)
    return ctypes.create_string_buffer(bytes(b), len(b))


def _quick_luma(buf):
    """一块 BGRA 的平均亮度，只采约 200 个点（约 0.05ms）。

    拖动中每帧都要调用，所以不能整块扫：200 个点足够看出「某一帧突然掉下去」。"""
    n = len(buf)
    if n < 8:
        return 0.0
    step = max(52, (n // 800) * 4)
    tot = cnt = 0
    for i in range(0, n - 3, step):
        tot += buf[i] + buf[i + 1] + buf[i + 2]
        cnt += 3
    return tot / float(cnt) if cnt else 0.0


def _crop_bgra(data, sw, x, y, w, h):
    """从一块 sw 宽的 BGRA 大图里裁出 (x, y) 起 w×h 的一块（按行拷贝）

    拖动时每帧都要从缓存的大图里裁一块，148 行各拷一次，约 0.05ms ——
    比现抓一次（BitBlt 要等 DWM 交表面，5.7~27.9ms 不等）便宜两个数量级。"""
    out = bytearray(w * h * 4)
    src_w = w * 4
    for r in range(h):
        so = ((y + r) * sw + x) * 4
        do = r * src_w
        out[do:do + src_w] = data[so:so + src_w]
    return bytes(out)


def buf_to_image(buf, ww, hh):
    """把 BGRA 缓冲包成 GDI+ 位图。

    不拷贝像素，只接管指针 —— 调用方必须一直持有这块缓冲，否则会读到已
    释放的内存（随机崩溃）。"""
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
    """拼 CF_DIB 需要的 BITMAPINFOHEADER（复制到剪贴板用）"""
    import struct
    return struct.pack("<IiiHHIIiiII", 40, ww, hh, 1, 32, 0, size, 0, 0, 0, 0)


# ============================================================ 自动识别区域
_SKIP_CLASSES = ("Progman", "WorkerW", "SnapLiteOverlay",
                 "SnapLiteTool")

_ACC_LOC = WINFUNCTYPE(c_long, c_void_p, POINTER(c_long), POINTER(c_long),
                       POINTER(c_long), POINTER(c_long), VARIANT)
_ACC_REL = WINFUNCTYPE(c_ulong, c_void_p)


def _cloaked(hwnd):
    """窗口是否被 DWM 隐藏（UWP 应用切到后台会“隐身”，但仍在窗口列表里）"""
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
    会把整个截图工具冻住。查不到就退回「按窗口」识别，功能不受影响。

    只在**截图过程中**才有请求，但线程从程序启动就一直活着。所以这里绝不能
    写成「睡 15ms 轮询」：那是 64 次/秒的空转唤醒，永远不停，静置时那点 CPU
    几乎全是它 —— 更重要的是它让进程永远进不了深度空闲。改成事件驱动后，
    没有请求时线程真的在等，一次都不醒。
    """

    def __init__(self):
        """只初始化状态，线程在 start() 之后才真正跑起来"""
        threading.Thread.__init__(self, daemon=True)
        self._lock = threading.Lock()
        self._req = None
        self._res = None
        self._seq = 0
        self._wake = threading.Event()
        self.ready = False

    def request(self, sx, sy):
        """投一次查询，返回递增序号（用来丢掉过期结果）"""
        with self._lock:
            self._seq += 1
            self._req = (sx, sy, self._seq)
        self._wake.set()
        return self._seq

    def latest(self):
        """取最近一次查询结果，格式 (矩形或 None, 序号)"""
        with self._lock:
            return self._res

    def run(self):
        """线程主循环：等到有请求才查一次 UI 元素矩形，其余时间在休眠。

        查不到不算错，调用方会退回「按窗口」识别。

        `wait(1.0)` 那 1 秒是兜底，不是轮询：万一哪天 request 里漏了 set，
        最多晚 1 秒也能自己醒过来，不会把线程永久卡死。正常路径下每秒 0 次
        唤醒（有请求时立刻醒）。"""
        try:
            ole32.CoInitializeEx(None, 2)      # STA
            self.ready = True
        except Exception:
            return
        while True:
            self._wake.wait(1.0)
            self._wake.clear()
            # 一次唤醒把排队的请求做完：鼠标拖得快时可能连着投了好几次，
            # 只做最新的那次就行，旧的已经被更新的位置取代了。
            while True:
                with self._lock:
                    item = self._req
                    self._req = None
                if item is None:
                    break
                rect = None
                try:
                    rect = msaa_rect(item[0], item[1])
                except Exception:
                    rect = None
                with self._lock:
                    self._res = (rect, item[2])



# ============================================================ 主程序
class App:
    """程序主体：三个窗口 + 一个托盘图标，全部状态都挂在这个对象上。

    三个窗口各管一段：
      · hwnd_main     主面板 —— 液态玻璃、自己接管拖动
      · hwnd_overlay  全屏遮罩 —— 截图时盖住屏幕，承载选区、标注、提示条
      · hwnd_tool     浮动工具条 —— 截图后出现在选区下方

    下面 146 个方法按职责分成八段，搜索「=== N.」可以直接跳到第 N 段开头：
      1. 基础设施        DPI 换算、光标、GDI+ 启动、窗口类注册
      2. 全局快捷键      注册 / 录制 / 组合被占用时的回退
      3. 主面板          窗口创建、离屏呈现、玻璃背景、自接管拖动、绘制
      4. 截图流程        遮罩创建、抓屏、离屏缓冲、悬停自动识别
      5. 遮罩合成与标注  整帧合成，以及画笔 / 马赛克 / 箭头 / 方框 / 圆形
      6. 遮罩交互        消息分派、选区拖拽改大小、标注状态与撤销栈
      7. 浮动工具条      版式、毛玻璃底、按钮绘制、点击派发
      8. 成品输出与系统  复制 / 保存 / 托盘 / 开机启动 / 消息循环
    """

    # ================================================== 1. 基础设施 —— DPI / 光标 / GDI+ / 窗口类
    def __init__(self):
        """初始化全部状态 —— 本身只按顺序调 9 个 _init_*，不直接赋值。

        分组原则：一块状态一个 _init_*，出问题一眼能看出是哪块没初始化。
        缓冲一律先置 None、用到才建（见 _ensure_*_surface），启动时不占几十 MB。"""
        self._init_env()             # 进程句柄 / DPI / 虚拟屏 / 面板尺寸
        self._init_windows()         # 三个窗口句柄 + 托盘图标数据
        self._init_hotkey()          # 全局热键与录制状态
        self._init_capture_state()   # 选区、拖动、自动识别线程
        self._init_anno_state()      # 标注、撤销栈、当前选中
        self._init_surfaces()        # 离屏缓冲、马赛克缓存、hover
        self._init_tool_surface()    # 工具条逐像素呈现表面
        self._init_main_surface()    # 主面板呈现表面 + 拖动玻璃
        self._init_window_procs()    # 窗口过程、GDI+、窗口类注册

    # ---------- 初始化（按块拆开，见 __init__）
    def _init_env(self):
        """进程句柄、DPI 缩放、虚拟屏范围、主面板尺寸。

        px() 依赖 self.scale，所以这个必须最先跑。"""
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
        # 圆角：20 → 15。收到一圈后四个角的弧线更短，那条 1px 高光在弧线上的
        # 像素相位抖动也就少了，观感比单纯加粗描边更干净。这个值同时决定
        # 面板底、棱边描边和四角遮罩，改一处即整套（见 _draw_main /
        # _apply_round_mask）。
        self.RADIUS = self.px(15)

    def _init_windows(self):
        """三个窗口的句柄（此时都还没创建）和托盘图标数据结构"""
        self.hwnd_main = None
        self.hwnd_overlay = None
        self.hwnd_tool = None
        self.tray = NOTIFYICONDATAW()
        # explorer 重启时会广播这条消息（ID 由系统按名字分配，全局唯一）。
        # 注册失败就是 0，那就当没有这条消息，别拿 0 去比 msg（0 == WM_NULL）。
        try:
            self.wm_taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        except Exception:
            self.wm_taskbar_created = 0

    def _init_hotkey(self):
        """全局热键组合（读配置）和「正在录制新热键」的临时状态"""
        self.hotkey_mod, self.hotkey_vk = load_config()
        self.hotkey_ok = False
        self.recording = False
        self.rec_msg = ""
        self.rec_err = False

    def _init_capture_state(self):
        """截图流程状态 + 自动识别。

        Probe 是一个后台线程，启动后一直活着，靠 latest() 取最新识别结果。"""
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

    def _init_anno_state(self):
        """标注数据本身 + 撤销/重做栈 + 当前选中与拖动目标。

        这里只存「画的是什么」；渲染缓存（_mos_cache / _cells_cache）在
        _init_surfaces 里，两者不要混。"""
        self.shapes = []               # [{t:"pen"|"mosaic"|"arrow"|"rect"|"ellipse", ...}]
        self.mode = None               # None / "pen" / "mosaic" / "arrow" / "rect" / "ellipse"
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

    def _init_surfaces(self):
        """离屏缓冲（消除闪烁的关键）、马赛克缓存、鼠标 hover 状态。

        缓冲先置 None：真正创建在 _build_capture_buffers / _ensure_*_surface。"""
        self.dc_shot = self.dc_dim = self.dc_back = None
        self.bmp_shot = self.bmp_dim = self.bmp_back = None
        self.hdc_screen = None
        self._sdc = None               # 常驻的屏幕 DC（见 _get_screen_dc）
        # 工作线程只能用它自己创建的 DC：GDI 句柄跨线程用会踩到对方的状态
        self._main_tid = threading.get_ident()
        self._sdc_threads = {}         # tid -> DC（只给后台线程用）
        self._excl_ok = None           # 能否「抓屏时排除自己」（见 _can_exclude_from_capture）
        self._excl_owner = None        # 现在这个属性挂在谁名下（见 _excl_acquire）
        self._mos_cache = {}           # 马赛克块的平均色缓存（每次截图重置）
        self._cells_cache = {}         # 马赛克笔迹覆盖的网格（见 _stroke_cells）

        # 交互 hover 状态
        self.hover_main = None
        self.hover_tool = -1
        self.tracking_main = False
        self.tracking_tool = False

    def _init_tool_surface(self):
        """悬浮工具条的逐像素呈现表面与毛玻璃底缓存"""
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

    def _init_main_surface(self):
        """主面板的呈现表面（液态玻璃）、截图留底、拖动玻璃状态"""
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
        # 置顶（右上角图钉）。只活在这一次运行里，不写配置 —— 启动一律不置顶，
        # 免得用户忘了自己开过、还以为别的窗口关不掉。
        self.topmost = False
        # 右键菜单弹着的时候为 True：TrackPopupMenu 是阻塞的，它自带的那个消息
        # 泵会继续派发 WM_TIMER，玻璃底照常抓屏 —— 菜单就盖在面板上，抓回来会
        # 把菜单自己拍进玻璃底。见 _panel_menu / _tick_glass。
        self.menu_open = False

        # 拖动中的玻璃底来源，两条路：
        # - 新系统：每帧现场抓「目标位置」背后那块桌面。窗口还没搬到那里，
        #   那一块天然不含自己，拿到的是全分辨率真背景（无锯齿、无补丁）。
        # - 老系统（没有「抓屏排除自己」）：退回整屏缩略图，见 _begin_drag_glass。
        self.drag_live = False         # True = 走实时抓屏那条路
        self.drag_small = None         # bytes：全屏缩略图（老系统退路）
        self.drag_small_size = None    # (sw, sh)
        self.drag_scale = (1.0, 1.0)   # 屏幕像素 / 缩略图像素
        self.drag_active = False
        self.drag_last_pos = None      # 上次提交时的窗口位置（位置没变就不重画）
        # 拖动时的「桌面大图」快照：(x, y, w, h, bytes)，比面板每边大一圈。
        #
        # 这张快照由**后台线程**持续刷新（见 _drag_prefetch_loop），主线程每帧
        # 只从里面裁一块。之所以不能主线程自己抓：抓一次是 5.7~27.9ms 不等，
        # 那 6ms 全是在等 DWM 交出桌面表面，等待时长随机，于是帧间隔忽长忽短
        # —— 鼠标是匀速拖的、画面却走走停停，壁纸的弧线边缘对这种不匀最敏感，
        # 看着就是一道横向水波（纯色块完全看不出来）。
        #
        # 快照存的**是桌面本身**，不是「面板某处该显示什么」，所以旧一点没关系：
        # 只要还盖得住当前窗口位置，裁出来的内容就是对的。
        self.drag_world = None
        self.drag_target = None        # 主线程每帧更新：(left, top)，线程据此取景
        self.drag_step = (0, 0)        # 主线程每帧更新：相对上次提交的位移，决定余量形状
        self.drag_stat = None          # 这一趟拖动的亮度统计，松手时落盘（见 _drag_stat_flush）
        self.drag_thread = None        # 预取「抓」线程（单线程模式下是唯一线程）
        self.drag_thread2 = None       # 预取「模糊」线程（流水线模式才有）
        self.drag_stop = None          # threading.Event，置位即收工
        # 流水线：抓线程把新抓的整块放这里，模糊线程取走。元组赋值是原子的，
        # 所以不需要锁；带序号是为了让模糊线程跳过被自己落下的旧块。
        self.drag_raw = None
        self.drag_wake = None          # threading.Event：抓线程有新块时叫醒模糊线程
        self.DRAG_WORLD_PAD = 96       # 老办法（DRAG_PREFETCH=False）的缓存外扩

        # 自己接管的窗口拖动（不走系统拖动循环，位置和背景才能同帧提交）
        self.main_drag = False
        self.main_drag_off = (0, 0)    # 按下时鼠标相对窗口左上角的偏移

        # 玻璃底自动跟随桌面变化（窗口事件钩子 + 双档定时器）
        self.glass_hook = None
        self.glass_proc = None         # 回调必须留引用，否则会被 GC 掉
        self.glass_dirty = False       # 事件钩子置位：窗口搬家了，下一拍一定重算
        self.glass_raw = None          # 上一次抓到的原始背景，用来比对画面变没变
        self.glass_tick_ms = 0         # 当前定时器节拍（0 = 还没起）
        self.glass_live_until = 0.0    # 活跃档保持到什么时候（time.time()）

        # 活跃档工作线程（见 _glass_live_loop）：抓屏和模糊都在这里做，
        # 主线程只等消息贴图，于是「等 DWM 出图」那 6ms 不再堵界面。
        self.glass_thread = None       # threading.Thread
        self.glass_stop = None         # threading.Event，置位即收工
        self.glass_slot = None         # 线程算好的帧，等主线程取
        self.glass_posted = False      # 已投递、还没被主线程取走（防止消息堆积）

    def _init_window_procs(self):
        """窗口过程、光标缓存、GDI+ 与窗口类。

        窗口过程统一兜一层异常：ctypes 回调里抛出的异常会被 ctypes 静默吞掉
        （只在 stderr 打一行 "Exception ignored"），用户和开发者都看不到现场，
        窗口还会留在半损坏状态。统一接住 → 写日志 → 返回 0。"""
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
        """把设计稿上的像素值换算成当前 DPI 下的实际像素。

        所有尺寸都必须过这个函数，125% / 150% 缩放下界面才不会挤成一团。"""
        return int(round(v * self.scale))

    def tb(self, v):
        """工具条专用尺寸：在 DPI 缩放之外再乘一次整体缩放系数 TB_SCALE。

        工具条有 10 颗按钮，整条比较长。想让它整体大一点小一点，只改 TB_SCALE
        一个数就行 —— 按钮宽高、图标、字号、圆角、选项排间距会一起等比缩放，
        不会出现「按钮小了但字还那么大」的错位。"""
        return self.px(v * TB_SCALE)

    def tb_font(self, v):
        """工具条里的字号，按 TB_FONT_SCALE 单独缩放（不跟 TB_SCALE 走）。

        外框缩到 0.86 是为了省宽度，但字跟着缩就小了，所以字号只缩到 0.92。
        改字号只改 TB_FONT_SCALE，按钮宽高不受影响。"""
        return self.px(v * TB_FONT_SCALE)

    def _init_gdiplus(self):
        """启动 GDI+。必须在任何绘图之前调用一次，否则后面所有 Gdip 调用静默失败"""
        tok = P()
        si = GdiplusStartupInput(1, None, False, False)
        gdip.GdiplusStartup(byref(tok), byref(si), None)

    def _load_cursor(self, cid):
        """取系统自带光标句柄（箭头 / 十字 / 各种缩放箭头）"""
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

        # 黑白双环：外圈黑 + 紧贴其内的白环。白环保证深色底上看得见，
        # 黑边保证浅色底上看得见；两环都不重叠，避免 AA 混成灰糊一片
        with GFX(ptr=gimg) as g:
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
        """注册三个窗口类：主面板 / 全屏遮罩 / 浮动工具条。

        遮罩类带 CS_DBLCLKS，双击能直接完成截图。"""
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

    # ================================================== 2. 全局快捷键
    def _hotkey_label(self):
        """当前快捷键的显示文本"""
        return hotkey_label(self.hotkey_mod, self.hotkey_vk)

    def _current_mod(self):
        """读键盘此刻按住的修饰键（录制快捷键时判断用户按了什么）"""
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
        """向系统注册全局热键，返回是否成功（组合被别的软件占用就会失败）"""
        self.hotkey_ok = bool(user32.RegisterHotKey(None, HOTKEY_ID,
                                                    self.hotkey_mod, self.hotkey_vk))
        return self.hotkey_ok

    def _begin_record(self):
        """进入快捷键录制：先注销旧热键，否则按下新组合会先把截图触发了"""
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
        """退出录制，把热键重新注册回去"""
        if not self.recording:
            return
        self.recording = False
        self.rec_msg = ""
        self.rec_err = False
        self._register_hotkey()
        if self.hwnd_main:
            user32.InvalidateRect(self.hwnd_main, None, False)

    def _on_record_key(self, vk):
        """录制中收到按键：Esc 取消，纯修饰键忽略，其余拿去试注册"""
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
        """试注册一个组合。失败就恢复原来的那个，不会把用户的快捷键弄丢"""
        user32.UnregisterHotKey(None, HOTKEY_ID)
        if user32.RegisterHotKey(None, HOTKEY_ID, mod, vk):
            return True
        # 失败的组合要放弃，恢复原来的
        user32.RegisterHotKey(None, HOTKEY_ID, self.hotkey_mod, self.hotkey_vk)
        return False

    # ================================================== 3. 主面板 —— 窗口 / 玻璃背景 / 拖动 / 绘制
    def create_main(self):
        """建主面板窗口并上屏。

        先把背后的真实桌面抓成玻璃底再显示，否则会先黑一下再变玻璃。"""
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        x = (sw - self.MW) // 2
        y = max(0, (sh - self.MH) // 2 - self.px(60))
        self.hwnd_main = user32.CreateWindowExW(
            WS_EX_TOOLWINDOW | WS_EX_LAYERED, "SnapLiteMain", APP_NAME,
            WS_POPUP, x, y, self.MW, self.MH, None, None, self.hinst, None)
        if not self.hwnd_main:
            return
        # 先把背后的真实桌面抓下来做成玻璃底，再上屏 —— 避免先黑一下再变玻璃
        self._refresh_glass(blank=False)
        user32.ShowWindow(self.hwnd_main, SW_SHOW)
        self._watch_desktop()
        dbg("create_main hwnd=%s visible=%s" % (self.hwnd_main,
                                                user32.IsWindowVisible(self.hwnd_main)))

    def _main_layout(self):
        """算主面板上几个控件的矩形：关闭、置顶、收到托盘、截图按钮、快捷键行

        右上角从右往左：✕ → — → 图钉。图钉放在最左，跟两个「关窗口」的动作
        隔开一格，避免手快连点时把置顶当成关闭按下去。"""
        W, H = self.MW, self.MH
        sz = self.px(26)
        gap = self.px(4)
        bx = W - self.px(16) - sz
        by = self.px(11)
        close = (bx, by, sz, sz)
        hide = (bx - gap - sz, by, sz, sz)
        pin = (bx - (gap + sz) * 2, by, sz, sz)
        btn = (self.px(18), self.px(46), W - self.px(36), self.px(46))
        hot = (self.px(18), self.px(100), W - self.px(36), self.px(30))
        return close, pin, hide, btn, hot

    # ---------- 液态玻璃：主窗口用逐像素 alpha 分层表面
    def _paint_main(self, hwnd):
        """响应 WM_PAINT。

        整窗都是我们自己画的，这里只清掉重绘请求再提交一帧，不做任何绘制
        （重画由 _present_main 负责，避免两条路径画出两种样子）。"""
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
        拖动几十秒就能把进程的 GDI 句柄耗光。

        后台线程走单独一支：DC 里带着选区、映射模式这些「状态」，两个线程
        共用同一个 DC 会互相覆盖。所以按线程号各建一个，用完一起释放。"""
        tid = threading.get_ident()
        if tid != self._main_tid:
            dc = self._sdc_threads.get(tid)
            if not dc:
                dc = user32.GetDC(None)
                self._sdc_threads[tid] = dc
            return dc
        if self.hdc_screen:
            return self.hdc_screen
        if not self._sdc:
            self._sdc = user32.GetDC(None)
        return self._sdc

    def _release_screen_dc(self):
        """释放常驻的屏幕 DC（退出时调一次）"""
        if self._sdc:
            try:
                user32.ReleaseDC(None, self._sdc)
            except Exception:
                pass
            self._sdc = None
        for dc in list(self._sdc_threads.values()):
            try:
                user32.ReleaseDC(None, dc)
            except Exception:
                pass
        self._sdc_threads.clear()

    def _ensure_main_surface(self):
        """确保主面板的离屏表面存在、且尺寸与当前 DPI 一致"""
        W, H = self.MW, self.MH
        if self.mn_dc and self.mn_size == (W, H):
            return
        self._release_main_surface()
        sdc = self._get_screen_dc()
        self.mn_dc, self.mn_bmp, self.mn_old, self.mn_bits = _new_dib(sdc, W, H)
        self.mn_size = (W, H)

    def _release_main_surface(self):
        """释放主面板表面和那份留底拷贝"""
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
        """把主面板当前状态合成一帧提交到屏幕。

        glass 是模糊过的桌面底；抓不到桌面就退化成实底，保证文字一定看得清。"""
        if not self.hwnd_main:
            return
        self._ensure_main_surface()
        W, H = self.mn_size
        total = W * H * 4
        glass = self.mn_glass
        have = bool(glass) and len(glass) == total
        if have:
            # 玻璃底整块不透明，直接覆盖；不必先清成透明（拖动时省一笔全缓冲写）
            ctypes.memmove(self.mn_bits, glass, total)
        else:
            ctypes.memset(self.mn_bits, 0, total)
        with GFX(self.mn_dc) as g:
            if not have:
                # 抓不到桌面时退化成实底，保证文字一定可读
                g.fill_round(0xFF14161A, 0, 0, W, H, self.RADIUS)
            self._draw_main(g, W, H)
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
        """抓主窗口背后的真实桌面 → 模糊 → 当玻璃底。

        blank=True 时先提交一帧全透明再抓，所以抓到的一定是桌面本身。"""
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

        def _grab(gx, gy, gw, gh, probe):
            # 窗口已经在上一步提交成全透明，不需要再设「抓屏排除自己」
            return grab_screen(gx, gy, gw, gh).raw

        try:
            # 四周多留一圈再模糊：抓的区域正好等于面板大小时，模糊会在四条边上
            # 留下暗一半的边（原因见 _blur_behind），静止时就是那圈「内侧阴影」
            self.mn_glass = self._blur_behind(rc.left, rc.top, grab=_grab)
        except Exception as e:
            dbg("glass grab/blur failed: %r" % (e,))
            self.mn_glass = None
        self._present_main()

    # ---------- 玻璃底自动跟随桌面变化
    def _watch_desktop(self):
        """装一个窗口事件钩子：别的窗口移动 / 显示 / 隐藏 / 切换前台时打个标记。

        以前玻璃底只在「自己动了」或「主动重绘」时重算，别的窗口在下面挪，
        背景就一直停在旧画面。钩子只负责置位，真正的重活留给定时器，
        免得一次拖动几百个事件把主线程堵死。"""
        try:
            self.glass_proc = WINEVENTPROC(self._on_desktop_event)
            self.glass_hook = user32.SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_LOCATIONCHANGE,
                None, self.glass_proc, 0, 0,
                WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
            if not self.glass_hook:
                dbg("SetWinEventHook 失败，玻璃底不会自动跟随")
        except Exception as e:
            dbg("SetWinEventHook 异常: %r" % (e,))
        self._set_glass_tick(GLASS_TICK_IDLE)

    def _set_glass_tick(self, ms):
        """切换定时器节拍。同一个 ID 重设会顶掉旧的，不需要先 KillTimer"""
        if ms == self.glass_tick_ms:
            return
        self.glass_tick_ms = ms
        user32.SetTimer(self.hwnd_main, TIMER_GLASS, ms, None)

    def _settle_glass_tick(self):
        """这一拍画面没变 —— 活跃档过期了就退回空闲档，省掉没必要的完整刷新"""
        if time.time() < self.glass_live_until:
            return
        self._set_glass_tick(GLASS_TICK_IDLE)

    def _on_desktop_event(self, hook, event, hwnd, idobj, idchild, thread, ms):
        """桌面窗口事件回调。

        只置一个标记就走，绝不在这里干重活 —— 拖动一个窗口会瞬间产生几百个
        事件，每个都重算玻璃底会把界面卡死。真正重算交给定时器（_tick_glass）。
        处在截图流程里时不需要跟（遮罩盖着，主面板也停在透明帧）。"""
        if not self.capturing:
            self.glass_dirty = True
        return None

    def _on_display_change(self):
        """分辨率 / 显示器插拔 / 缩放变了。

        不处理的话有两个后果：把分辨率调小（或者拔掉外接屏）之后，面板可能整块
        落在新屏幕之外 —— 你看不见它，也没法把它拖回来，只能按热键截图或者重启；
        玻璃底也还按旧坐标抓，会抓到别处的内容。

        只做两件事：把窗口拉回可视区、把玻璃底标脏重抓。面板的像素尺寸不跟着变
        （本来就该是固定的物理尺寸）；改 DPI 缩放才需要重建字体和表面，那种情况
        目前要重启才干净，先不在这里硬做。"""
        if not self.hwnd_main:
            return
        try:
            if self.capturing:
                # 遮罩是按旧屏幕尺寸铺的，继续框选会错位，先收掉
                self._abort_capture()
            vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
            vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
            vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
            vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
            self.vs = (vx, vy, vw, vh)
            rc = w.RECT()
            user32.GetWindowRect(self.hwnd_main, byref(rc))
            W, H = self.mn_size or (self.MW, self.MH)
            left = max(vx, min(rc.left, vx + vw - W))
            top = max(vy, min(rc.top, vy + vh - H))
            if (left, top) != (rc.left, rc.top):
                user32.SetWindowPos(self.hwnd_main, None, left, top, 0, 0,
                                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
                dbg("display change: moved to (%d,%d)" % (left, top))
            # 坐标系变了，所有按旧坐标抓到的像素一律作废
            self.glass_raw = None
            self.glass_dirty = True
            self.drag_world = None
            self._present_main()
        except Exception as e:
            dbg("display change failed: %r" % (e,))

    def _tick_glass(self):
        """定时器：桌面内容真的变了才重算玻璃底，没变什么都不做。

        以前的判据只有「别的窗口搬家 / 切换前台」这一类事件，而视频播放、
        网页动画、滚动这些「画面自己在变」的情况**不产生任何窗口事件** ——
        于是玻璃底会一直停在很久以前那一帧，看起来像卡死了。

        现在每拍都轻抓一次做比对：抓屏 6ms、比内存 0.02ms，一样就直接返回，
        不模糊也不重画；确认变了就把活跃档交给后台线程（_glass_live_loop）——
        它跟着屏幕垂直消隐出帧，帧间隔永远是整数个刷新周期，不会出现拍频。"""
        if (not self.hwnd_main or self.capturing or self.main_drag
                or self.drag_active or self.recording or self.menu_open):
            return
        if not user32.IsWindowVisible(self.hwnd_main):
            return
        if user32.IsIconic(self.hwnd_main):
            return
        if self._glass_thread_alive():
            # 线程在跟，主线程不插手：两边同时抓屏只会互相拖慢
            return

        block = self._grab_glass_block()
        if block is None:
            # 老系统（Win10 2004 之前）没有「抓屏时排除自己」，走插值那条路
            # 拿不到可比对的原始像素，只能退回「事件置位才刷新」的老逻辑。
            if not self.glass_dirty:
                self._settle_glass_tick()
                return
            self.glass_dirty = False
            self._refresh_glass_live()
            return
        raw, ox, oy, gw, gh = block

        # 窗口搬家 / 切前台已经说明画面一定变了，这一拍省掉比对直接重算
        forced = self.glass_dirty
        self.glass_dirty = False
        if not forced and raw == self.glass_raw:
            self._settle_glass_tick()
            return

        self.glass_raw = raw
        if self._start_glass_live():
            # 线程接手：这一帧连同后续都由它算，主线程立刻回去处理消息
            return
        try:
            glass = self._blur_crop(raw, gw, gh, ox, oy)
        except Exception as e:
            dbg("glass blur failed: %r" % (e,))
            glass = None
        if not glass:
            return
        self.mn_glass = glass
        self._present_main()
        self.glass_live_until = time.time() + GLASS_LIVE_HOLD
        self._set_glass_tick(GLASS_TICK_LIVE)

    # ---------- 活跃档：后台线程跟着垂直消隐出帧
    def _glass_thread_alive(self):
        """活跃档线程是不是还在跑"""
        th = self.glass_thread
        return bool(th and th.is_alive())

    def _start_glass_live(self):
        """确认桌面在变了：把抓屏 + 模糊交给后台线程，返回是否成功接手。

        线程只做「等消隐 → 抓 → 模糊 → 把帧放到 slot → 叫主线程贴」，
        窗口和 UpdateLayeredWindow 仍然只有主线程碰。"""
        if not GLASS_USE_LIVE_THREAD:
            return False
        if not self.hwnd_main or not self._can_exclude_from_capture():
            return False
        if self._glass_thread_alive():
            return True
        self.glass_stop = threading.Event()
        self.glass_slot = None
        self.glass_posted = False
        self.glass_thread = threading.Thread(
            target=self._glass_live_loop, args=(self.glass_stop,),
            name="glass-live", daemon=True)
        self.glass_thread.start()
        # 线程在跑的时候定时器只当心跳，退回低频；线程收工会再调回来
        self._set_glass_tick(GLASS_TICK_IDLE)
        return True

    def _stop_glass_live(self, wait=0.5):
        """叫停活跃档线程并等它退出（退出、截图、拖动收尾时用）"""
        stop = self.glass_stop
        th = self.glass_thread
        if stop:
            stop.set()
        if th and th.is_alive() and th is not threading.current_thread():
            th.join(wait)
        self.glass_thread = None
        self.glass_stop = None
        self.glass_slot = None
        self.glass_posted = False

    def _glass_live_loop(self, stop):
        """后台线程：抓屏 → 变了就模糊 → 交给主线程贴上去，跑到没人看为止。

        节拍不写死毫秒数，也不强行对齐垂直消隐，而是「一轮做完立刻做下一轮」。
        这是实测出来的选择，反直觉但有数据（166Hz 屏，340×148）：

            每轮先 DwmFlush 等消隐   58.7 帧/秒  间隔 16.38ms（2.72 个周期）顿挫 17.0Hz
            不等，跑满               85.0 帧/秒  间隔  8.75ms（1.45 个周期）顿挫 51.9Hz

        原本以为「等消隐 = 帧间隔整数倍 = 没有拍频」，实际不是：DwmFlush 返回
        之后还要抓（约 7ms）、模糊（2ms）、再绕一趟消息到主线程贴图，落点早就
        偏开了；多等的这一拍只是白花时间，帧率还掉三分之一。反而不等更快，
        快到位之后帧间隔只有 1.45 个周期，多出来的零点几次刷新每 1~2 帧就
        轮到一次 —— 顿挫频率 52Hz，眼睛根本分辨不出。

        跟拖动那条路是一回事：拖动也是「来一次算一次」，实测 108 帧、58Hz，
        你看着是顺的。两处用同一种节奏，观感才一致。

        画面静止超过 GLASS_LIVE_HOLD 就收工，把探测交回低频定时器 —— 没人
        在看的时候不该一直占着 GPU 合成。

        「抓屏排除自己」在这个线程活着的时候是**常设**的，不是每抓一次设一次：
        设完属性要等 DWM 重新合成一帧才生效，抓完立刻恢复的话下一轮又得重来，
        等于每帧白等 7ms（实测 61 帧，且间隔忽长忽短）。常设之后一轮只要
        抓（7ms）+ 模糊（2ms），实测 85 帧、顿挫频率 52Hz。
        代价：线程活着这几秒内，别的截图工具拍不到主面板 —— 松手/画面停下
        一秒后线程就收工、属性立刻恢复。截图、拖动、录屏进来时线程都会先让路。"""
        W, H = self.MW, self.MH
        last_raw = None            # None = 还没有基准，第一帧一定算「变了」
        idle_since = None
        hwnd0 = self.hwnd_main
        # 属性可能正被拖动占着：那就直接用（acquire 不会抢归属）
        excluded = self._excl_acquire("live")
        if excluded and self._excl_owner == "live":
            try:
                dwmapi.DwmFlush()   # 等一帧让属性真正生效
            except Exception:
                pass
        try:
            while not stop.is_set():
                if (self.capturing or self.recording or self.main_drag
                        or self.drag_active):
                    idle_since = None
                    stop.wait(0.02)
                    continue
                hwnd = self.hwnd_main
                if (not hwnd or not user32.IsWindowVisible(hwnd)
                        or user32.IsIconic(hwnd)):
                    # 收进托盘 / 最小化：看不见就别跟了，直接收工。留着跑的话
                    # 「抓屏排除自己」会一直挂着，退出时也没人叫停。
                    break
                block = self._grab_live_frame(W, H)
                if block is None:
                    # 抓不到（属性没生效 / 窗口刚隐藏）就下一轮再试，别把线程耗死
                    stop.wait(0.02)
                    continue
                raw, ox, oy, gw, gh = block
                if raw == last_raw:
                    now = time.time()
                    if idle_since is None:
                        idle_since = now
                    if now - idle_since >= GLASS_LIVE_HOLD:
                        break              # 画面静止了，交回低频探测
                    continue               # 没变就不模糊，直接下一轮
                idle_since = None
                last_raw = raw
                self.glass_raw = raw
                try:
                    glass = self._blur_crop(raw, gw, gh, ox, oy)
                except Exception as e:
                    dbg("live glass blur failed: %r" % (e,))
                    continue
                if not glass or stop.is_set():
                    continue
                self.glass_slot = glass
                if not self.glass_posted:
                    # 只投一份：主线程忙的时候帧会过期，堆一串消息只会越贴越旧
                    self.glass_posted = True
                    try:
                        user32.PostMessageW(hwnd, WM_GLASS_FRAME, 0, 0)
                    except Exception:
                        self.glass_posted = False
        finally:
            # 只归还自己那份：拖动还没松手时，属性要继续留着给它用
            if excluded:
                self._excl_release("live")
        hwnd = self.hwnd_main
        if hwnd:
            try:
                user32.PostMessageW(hwnd, WM_GLASS_FRAME, 1, 0)   # 通知主线程收工
            except Exception:
                pass

    def _grab_live_frame(self, W, H):
        """线程用：抓「面板 + 留白」那一整块（属性已由线程常设，不再每抓一次设一次）。

        仍然顺手校验一次「像不像自己」：属性刚设上或窗口刚被唤醒时，偶尔还会
        抓到面板本身。抓到就丢掉这一帧（下一轮再说），不在这里重抓 —— 重抓要
        再等一个合成周期，一帧的成本直接翻倍，宁可掉一帧也不要拖慢整条流水线。

        返回 (raw, ox, oy, gw, gh)，见 _grab_glass_block。"""
        if not self._can_exclude_from_capture():
            return None
        rc = w.RECT()
        user32.GetWindowRect(self.hwnd_main, byref(rc))
        gx, gy, gw, gh = self._glass_rect(rc.left, rc.top)
        ox = max(0, min(rc.left - gx, gw - W))
        oy = max(0, min(rc.top - gy, gh - H))
        try:
            raw = grab_screen(gx, gy, gw, gh).raw
        except Exception as e:
            dbg("live grab failed: %r" % (e,))
            return None
        if not raw or len(raw) != gw * gh * 4:
            return None
        if self._looks_like_me_at(raw, gw, gh, (ox, oy, W, H)):
            return None
        return raw, ox, oy, gw, gh

    def _glass_frame_ready(self):
        """主线程：把线程算好的那一帧贴上去（只做提交，约 3ms）"""
        self.glass_posted = False
        glass = self.glass_slot
        self.glass_slot = None
        if not glass:
            return
        if self.main_drag:
            # 这条消息是线程**在拖动开始之前**投出来的，画面还是旧位置那块的。
            # 拖动中位置只由 _drag_frame 搬（位置和内容同一次提交），这里再贴
            # 一帧就成了「窗口已经到新位置、玻璃还是老地方的」—— 背景在面板里
            # 往后滑一截。纯色背景看不出来，有细纹的地方就是一道水波。
            # 丢掉它：松手后 _end_drag_glass 会置脏并重启线程，不差这一帧。
            return
        self.mn_glass = glass
        self.glass_dirty = False
        self.glass_live_until = time.time() + GLASS_LIVE_HOLD
        self._present_main()

    def _can_exclude_from_capture(self):
        """当前系统能不能「抓屏时把自己排除掉」（Win10 2004 / build 19041 起）。

        更早的系统上 WDA_EXCLUDEFROMCAPTURE 会被当成「遮蔽」处理，抓到的
        是一片黑 —— 比不支持更糟。所以按真实 build 号判断，不支持就走
        旧的插值路径。结果只算一次。"""
        if self._excl_ok is None:
            self._excl_ok = win_build() >= CAPTURE_EXCLUDE_MIN_BUILD
        return self._excl_ok

    def _excl_acquire(self, owner):
        """把「抓屏排除自己」挂到某个名下；已经在自己名下就不重复设。

        为什么要有「归属」：这个属性有三处会用 —— 空闲探测（抓完就还）、
        活跃线程（活着期间常设）、拖动（整段占用）。三者各自 set/clear 的话，
        谁清谁的时序一错，就会在别人抓屏的那一刻被清回 0，抓回来的是面板
        自己；模糊后整块发暗，下一帧又恢复 —— 就是「界面不动，自己黑一下」。
        加归属之后，只有当初设它的那个人能清，别人清不动。
        """
        if not self.hwnd_main or not self._can_exclude_from_capture():
            return False
        if self._excl_owner:
            # 已经有人在用了：属性本来就是开着的，直接用，但不抢归属。
            # 抢过来的话，对方一松手就会把属性清掉，而另一方正在抓屏 ——
            # 于是抓回面板自己，模糊后整块发暗。归属就是防这个的。
            return True
        try:
            ok = bool(user32.SetWindowDisplayAffinity(
                self.hwnd_main, WDA_EXCLUDEFROMCAPTURE))
        except Exception:
            ok = False
        if ok:
            self._excl_owner = owner
        return ok

    def _excl_release(self, owner):
        """归还属性。不是自己的就别动 —— 别人（拖动/线程）正在靠它抓背景。"""
        if self._excl_owner != owner:
            return
        self._excl_owner = None
        try:
            user32.SetWindowDisplayAffinity(self.hwnd_main, WDA_NONE)
        except Exception:
            pass

    def _excl_reset(self):
        """无条件清掉（退出时用，不管现在挂在谁名下）"""
        self._excl_owner = None
        if not self.hwnd_main:
            return
        try:
            user32.SetWindowDisplayAffinity(self.hwnd_main, WDA_NONE)
        except Exception:
            pass

    def _looks_like_self(self, raw, ww, hh):
        """这次抓回来的画面，是不是「面板自己」（说明排除属性没赶上这一帧）。

        只比中间 60% 一块：边缘可能掺进邻接的背景，中间那块一定被面板盖着。
        阈值 8 是实测出来的：排除成功时与面板差 79，抓到面板本身时只有 0~5。
        判据拿不到面板像素时返回 False（当作成功），不因为校验本身失败而卡住。

        取样而不是整块拷：这条判据在拖动时每帧都要走一次，拷 200KB 白花
        时间。中间 60% 里每 8 行取一行、每 4 个像素取一个，约 400 个样本，
        足够判「像不像自己」。"""
        if not self.mn_bits or self.mn_size != (ww, hh):
            return False
        base = self.mn_bits.value
        if not base:
            return False
        stride = ww * 4
        x0, x1 = ww // 5, ww * 4 // 5
        y0, y1 = hh // 5, hh * 4 // 5
        tot = cnt = 0
        try:
            for y in range(y0, y1, 8):
                off = y * stride + x0 * 4
                row = ctypes.string_at(base + off, (x1 - x0) * 4)
                for i in range(0, len(row) - 3, 16):
                    o = off + i
                    tot += (abs(raw[o] - row[i]) + abs(raw[o + 1] - row[i + 1])
                            + abs(raw[o + 2] - row[i + 2]))
                    cnt += 3
        except Exception:
            return False
        return bool(cnt) and (tot / float(cnt)) < 8.0

    def _self_btn_rect(self):
        """「这是我自己」的取样框：主按钮里一段纯色区域，窗口坐标。

        取按钮宽度 62%~72% 那一段：左边是「开始截图」文字、右边是取景角标，
        两头都避开，中间是干净的实心蓝。纵向取中间 35%~65%，避开圆角。"""
        W, H = self.MW, self.MH
        b = self._main_layout()[3]
        x0 = b[0] + b[2] * 0.62
        x1 = b[0] + b[2] * 0.72
        y0 = b[1] + b[3] * 0.35
        y1 = b[1] + b[3] * 0.65
        x0 = max(0, min(W - 2, int(x0)))
        x1 = max(x0 + 1, min(W, int(x1)))
        y0 = max(0, min(H - 2, int(y0)))
        y1 = max(y0 + 1, min(H, int(y1)))
        return x0, x1, y0, y1

    def _is_self_tile(self, buf, W, H):
        """这块画面里是不是「我自己」—— 只看主按钮那段纯色对不对得上。

        为什么不能只靠 _looks_like_self（与 mn_bits 逐像素比）：拖动中 mn_bits
        里的玻璃底是**另一个位置**的桌面，而「抓到面板自己」那张带的是面板当时
        显示的背景，两者对不上 → 校验放行 → 把自己模糊一遍贴上去，整块发暗，
        就是「界面黑一下」。位置越随机越容易撞上，所以这症状位置随机、整块变暗。

        玻璃底每帧都在换，只有 UI 固定在窗口里，所以改用按钮那段纯蓝当锚点：
        抓到自己必然是它，抓到桌面几乎不可能撞出同一种蓝（撞上也只是白丢一帧，
        退化成补抓，不会发黑）。"""
        if not buf or len(buf) != W * H * 4:
            return False
        x0, x1, y0, y1 = self._self_btn_rect()
        tb = tg = tr = 0
        n = 0
        stride = W * 4
        for y in range(y0, y1):
            o = y * stride + x0 * 4
            row = buf[o:o + (x1 - x0) * 4]
            for k in range(0, len(row) - 3, 4):
                tb += row[k]
                tg += row[k + 1]
                tr += row[k + 2]
                n += 1
        if not n:
            return False
        tb /= float(n)
        tg /= float(n)
        tr /= float(n)
        for c in SELF_BTN_COLORS:
            d = (abs(tb - (c & 0xFF)) + abs(tg - ((c >> 8) & 0xFF))
                 + abs(tr - ((c >> 16) & 0xFF)))
            if d < 46.0:
                return True
        return False

    def _looks_like_me(self, buf, W, H):
        """抓回来的画面是不是面板自己 —— 两种判据取并集。

        - `_looks_like_self`：与当前画面逐像素比，窗口**不动**时最准；
        - `_is_self_tile`：看主按钮那段纯色，窗口**在动**时也有效。

        拖动必须走第二个：那时候 mn_bits 里的玻璃底已经是旧位置的桌面，
        第一个判据会漏判（漏判 = 面板黑一下）。"""
        return (self._looks_like_self(buf, W, H)
                or self._is_self_tile(buf, W, H))

    def _crop_bgra(self, buf, ww, px, py, pw, ph):
        """从 stride=ww 的 BGRA 大块里裁一块出来（按行拷，纯字节操作）。

        抓「面板 + 四周留白」之后，凡是原来按面板大小写死的判断（像不像自己、
        模糊后裁中心）都要先裁出面板那一片，统一走这里。"""
        if px == 0 and pw == ww:
            return bytes(buf[:pw * ph * 4])
        out = bytearray(pw * ph * 4)
        for y in range(ph):
            so = ((y + py) * ww + px) * 4
            out[y * pw * 4:(y + 1) * pw * 4] = buf[so:so + pw * 4]
        return bytes(out)

    def _looks_like_self_at(self, raw, ww, hh, probe):
        """在 raw 里取出 probe 那一块，再和面板当前画面比「像不像自己」。

        为什么要单独有这么一个：抓的区域比面板大（外扩留白，见 _blur_behind）
        时，`_looks_like_self` 一看尺寸对不上就直接返回 False（当作成功），
        这道校验等于白设。先把中间那块裁出来再比，语义和只抓面板大小一样。

        probe 是 (x, y, w, h)，相对 raw 左上角；传 None 或整块就退回原判据。"""
        if not probe:
            return self._looks_like_self(raw, ww, hh)
        px, py, pw, ph = probe
        if (px, py, pw, ph) == (0, 0, ww, hh):
            return self._looks_like_self(raw, ww, hh)
        if px < 0 or py < 0 or px + pw > ww or py + ph > hh:
            return False
        return self._looks_like_self(self._crop_bgra(raw, ww, px, py, pw, ph), pw, ph)

    def _looks_like_me_at(self, raw, ww, hh, probe):
        """同上的整版判据（含「面板自己在动」时也有效的按钮纯色那一招）。"""
        px, py, pw, ph = probe
        buf = self._crop_bgra(raw, ww, px, py, pw, ph)
        return self._looks_like_me(buf, pw, ph)

    def _grab_without_self(self, left, top, ww, hh, probe=None):
        """抓 (left, top) 处 ww×hh —— 画面里不含本窗口。失败返回 None。

        probe 指定「拿哪一块做自检」，默认整块。抓的区域比面板大时（外扩留白）
        必须传面板那一块，否则尺寸对不上，自检会直接放行（见 _looks_like_self_at）。

        只在抓的这一小段里把窗口标成「抓屏时排除」，抓完立刻恢复：这个属性
        对系统里所有抓屏程序都生效，不能常驻，否则别的截图工具也拍不到它。

        为什么要重试：DWM 是异步合成的，设置属性后偶尔会「这一帧还没生效」，
        BitBlt 拿到的仍是面板自己（实测十次里会碰上一两次）。所以抓完先看
        一眼像不像自己，像就等 DWM 刷一帧再抓一次。第一次不等，是因为多数
        时候本来就已经生效，白等一帧要多花 16ms。"""
        for attempt in range(2):
            raw = None
            if not self._excl_acquire("tick"):
                return None
            try:
                if attempt:
                    try:
                        dwmapi.DwmFlush()      # 等这一帧把面板从合成里摘掉
                    except Exception:
                        pass
                raw = grab_screen(left, top, ww, hh).raw
            except Exception as e:
                dbg("grab without self failed: %r" % (e,))
                raw = None
            finally:
                self._excl_release("tick")
            if raw and len(raw) == ww * hh * 4 \
                    and not self._looks_like_self_at(raw, ww, hh, probe):
                return raw
        dbg("抓背景两次都抓到自己，改用插值路径")
        return None

    def _grab_glass_block(self):
        """抓「面板 + 四周留白」那块桌面，返回 (raw, ox, oy, gw, gh)。

        raw 是 gw×gh 的原始 BGRA，面板在其中的落点是 (ox, oy)。抓不到、
        或者抓到的是面板自己，都返回 None。

        留白的理由见 _blur_behind（不留白的话模糊会在面板四条边上留下一圈
        暗边）。单独给一个「只抓不模糊」的入口，是为了让定时器能「先比对、再
        决定要不要模糊」—— 抓一次 6ms，比对只要 0.02ms，桌面没变时不必付
        模糊和重画的钱。"""
        if not self._can_exclude_from_capture():
            return None
        W, H = self.MW, self.MH
        rc = w.RECT()
        user32.GetWindowRect(self.hwnd_main, byref(rc))
        gx, gy, gw, gh = self._glass_rect(rc.left, rc.top)
        ox = max(0, min(rc.left - gx, gw - W))
        oy = max(0, min(rc.top - gy, gh - H))
        # 抓的是一整块，交给 _grab_without_self 去重试 + 自检（只看面板那一片）
        raw = self._grab_without_self(gx, gy, gw, gh, probe=(ox, oy, W, H))
        if not raw or len(raw) != gw * gh * 4:
            return None
        return raw, ox, oy, gw, gh

    def _live_glass_excluded(self, W, H):
        """走「排除自己」这条路：抓到的是背后的真实桌面，模糊一下就能用。

        实测与「面板透明后直抓」逐像素差 0.1（旧的插值路径要 13ms，而且中间
        内容是推算出来的假的）。

        抓的范围比面板大一圈（见 _blur_behind）：不放大直接模糊的话，模糊会在
        面板四条边上留下一圈暗边 —— 静止时看到的就是那个。"""
        rc = w.RECT()
        user32.GetWindowRect(self.hwnd_main, byref(rc))
        try:
            return self._blur_behind(rc.left, rc.top)
        except Exception as e:
            dbg("live glass blur failed: %r" % (e,))
            return None

    def _live_glass_patched(self, W, H):
        """老系统的退路：抓窗口周围一大圈，把自己占的那几格用左右插值补掉。

        注意这条路的画面**中间是推算出来的**：面板在缩略图里约占 76% 的宽度，
        补出来是一道水平渐变，背后的真实内容会丢（实测与真背景差 ±36 亮度）。
        只在没有 WDA_EXCLUDEFROMCAPTURE 的系统上才用。"""
        rc = w.RECT()
        user32.GetWindowRect(self.hwnd_main, byref(rc))
        f = self.BLUR_F
        pad = f * 4                       # 四周多留几格，放大时才有余量
        gx, gy = rc.left - pad, rc.top - pad
        gw, gh = W + pad * 2, H + pad * 2
        sw, sh = max(2, gw // f), max(2, gh // f)

        small = None
        try:
            d = _new_dib(self._get_screen_dc(), sw, sh)
            try:
                gdi32.SetStretchBltMode(d[0], 4)          # HALFTONE
                if gdi32.StretchBlt(d[0], 0, 0, sw, sh,
                                    self._get_screen_dc(), gx, gy, gw, gh, SRCCOPY):
                    small = ctypes.string_at(d[3], sw * sh * 4)
            finally:
                _free_dib(d)
        except Exception as e:
            dbg("live glass grab failed: %r" % (e,))
        if not small or len(small) != sw * sh * 4:
            return None

        small = self._patch_own_window(small, sw, sh, gx, gy, gw, gh)

        # 借用拖动那条取块路径：临时把它当成「以这块区域为全屏」来算
        keep = (self.vs, self.drag_small, self.drag_small_size,
                self.drag_scale, self.drag_active)
        try:
            self.vs = (gx, gy, gw, gh)
            self.drag_small = small
            self.drag_small_size = (sw, sh)
            self.drag_scale = (gw / float(sw), gh / float(sh))
            self.drag_active = True
            return self._drag_glass_bytes(rc.left, rc.top)
        except Exception as e:
            dbg("live glass failed: %r" % (e,))
            return None
        finally:
            (self.vs, self.drag_small, self.drag_small_size,
             self.drag_scale, self.drag_active) = keep

    def _refresh_glass_live(self):
        """重取一张「背后的桌面」，重算玻璃底，位置一个像素都不动。

        难点：自己正盖在要抓的那块上，直接抓会抓到自己（自我递归的糊）。
        三种做法都试过：

        1. 先提交全透明帧、等 DWM 合成再抓 —— 40~50ms，而且能看见面板闪
           一下；放进 11fps 的自动刷新里就是持续闪。只用于首次建底。
        2. 抓一大圈、把自己占的那几格用左右像素插值补掉 —— 快，但中间是
           推出来的：面板在缩略图里占 76% 的宽度，补出来一道水平渐变，背
           后的内容全丢（实测差 ±36 亮度）。现在只当老系统的退路。
        3. **SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)** —— 告诉
           DWM「抓屏时把我当不存在」，屏幕上照常显示，抓回来直接就是背后
           的真实桌面。实测与「透明后直抓」差 0.1，代价 < 5ms。

        默认走第 3 条，Win10 2004 之前的系统退回第 2 条。"""
        if not self.hwnd_main:
            return
        W, H = self.MW, self.MH
        glass = None
        if self._can_exclude_from_capture():
            glass = self._live_glass_excluded(W, H)
        if not glass:                    # 老系统，或这一帧没排除成功
            glass = self._live_glass_patched(W, H)
        if glass:
            self.mn_glass = glass
            self._present_main()


    def _blur_bgra(self, raw, W, H):
        """缩到 1/f 再用 GDI+ 双三次插值放回来 → 平滑模糊（零第三方依赖）

        注意：不能用 GDI 的 StretchBlt + HALFTONE，32bpp 目标下会退化成
        最近邻，放大会出现明显马赛克方块。"""
        sdc = self._get_screen_dc()
        f = self.BLUR_F
        sw, sh = max(2, W // f), max(2, H // f)
        made = []
        imgs = []
        try:
            src = _new_dib(sdc, W, H)
            made.append(src)
            ctypes.memmove(src[3], raw, W * H * 4)

            small = _new_dib(sdc, sw, sh)
            made.append(small)
            img = P()
            gdip.GdipCreateBitmapFromScan0(W, H, W * 4, 0x00022009, src[3], byref(img))
            imgs.append(img)
            with GFX(small[0]) as g1:
                gdip.GdipDrawImageRectI(g1.g, img, 0, 0, sw, sh)

            raw_small = ctypes.string_at(small[3], sw * sh * 4)
            out = _new_dib(sdc, W, H)
            made.append(out)
            img2 = P()
            # 注意：GdipCreateBitmapFromScan0 不拷贝像素，只接管指针。
            # 这里必须用变量持有这块缓冲，否则它被回收后 GDI+ 仍然去读 → 随机崩溃。
            hold = ctypes.create_string_buffer(raw_small, len(raw_small))
            gdip.GdipCreateBitmapFromScan0(sw, sh, sw * 4, 0x00022009,
                                           hold, byref(img2))
            imgs.append(img2)
            with GFX(out[0]) as g2:
                gdip.GdipDrawImageRectI(g2.g, img2, 0, 0, W, H)
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

    def _glass_rect(self, left, top):
        """算「模糊该抓哪一块」：包住面板、四周各留 BLUR_F，起点和宽高都对齐到
        BLUR_F 的整数倍。

        对齐有两个用处：
        · 降采样网格永远落在屏幕同一套格线上，静止和拖动算出来的条纹位置一致
          —— 刚按下拖动那一下不会跳一格（完整推导见 _drag_world_rect）；
        · 尺寸取整数倍，`W // BLUR_F` 才精确等于 BLUR_F，比例不偏。

        贴边时以「盖住面板」优先，钳进虚拟屏。屏幕小到连「面板 + 两侧留白」都
        装不下（拔掉外接屏、面板比屏幕还宽）就退回不留白，由 _blur_behind 原样用。
        """
        W, H = self.MW, self.MH
        f = self.BLUR_F
        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

        gw = min((W + 2 * f + f - 1) // f * f, vw // f * f)
        gh = min((H + 2 * f + f - 1) // f * f, vh // f * f)
        if gw < W or gh < H:
            return left, top, W, H                 # 装不下，别硬凑
        gx = max(vx, min((left - f) // f * f, vx + vw - gw))
        gy = max(vy, min((top - f) // f * f, vy + vh - gh))
        return gx, gy, gw, gh

    def _blur_behind(self, left, top, grab=None):
        """抓 (left, top) 处面板大小的**玻璃底**：四周多留一圈再模糊，裁回中心。

        为什么必须外扩 —— 这是「静止时边缘内侧一圈阴影」的唯一原因：

        `_blur_bgra` 是「缩到 1/BLUR_F 再放回来」。降采样时，输入位图最外一圈
        的采样会伸到位图外面，那里没有像素，GDI+ 按黑色补。于是模糊结果的四条
        边正好暗一半（纯灰 128 进去，最外一行出来是 63），往里约 10px 才渐次
        恢复。抓的区域如果正好等于面板大小，这条暗边就正好压在面板边上，看起来
        就是「边缘内侧一圈阴影」。

        拖动那条路（_grab_behind）从一张更大的快照里裁，裁切点碰不到边界，所以
        一直是干净的 —— 这就是「一动不动有阴影、一拖动就没了」的全部原因，和
        透明无关（面板本体是不透明的）。

        外扩 BLUR_F 之后那条暗边落在面板外面，裁切时被扔掉：边缘偏差从
        63/128 降到 2/255 以内，四边对称。代价约 +0.7ms（2.25 → 2.91ms），
        静止路径 250ms 一拍，不在拖动热路径上。

        `grab(gx, gy, gw, gh, probe) -> 原始 BGRA 或 None`；默认走「抓屏排除
        自己」。`_refresh_glass` 已经先把窗口提交成全透明帧，那里可以直接给一个
        裸 grab，省掉一次窗口属性设置。
        """
        W, H = self.MW, self.MH
        gx, gy, gw, gh = self._glass_rect(left, top)
        ox = max(0, min(left - gx, gw - W))
        oy = max(0, min(top - gy, gh - H))
        if grab is None:
            raw = self._grab_without_self(gx, gy, gw, gh, probe=(ox, oy, W, H))
        else:
            raw = grab(gx, gy, gw, gh, (ox, oy, W, H))
        if not raw or len(raw) != gw * gh * 4:
            return None
        return self._blur_crop(raw, gw, gh, ox, oy)

    def _blur_crop(self, raw, gw, gh, ox, oy):
        """把已经抓好的「面板 + 留白」整块模糊，再裁出面板那一片。

        抓和模糊分开，是为了让调用方能拿 raw 做逐字节比对（判断桌面画面变没变）
        —— 比的和模糊的必须是同一块像素，否则会出现「以为没变、其实画面已经换
        了」这种错判。"""
        W, H = self.MW, self.MH
        big = self._blur_bgra(raw, gw, gh)
        if ox == 0 and oy == 0 and (gw, gh) == (W, H):
            return big                             # 没留白（屏幕装不下），原样用
        return self._crop_bgra(big, gw, ox, oy, W, H)

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

    def _drag_stat_begin(self):
        """拖动开始：清空这一趟的亮度统计"""
        self.drag_stat = {"n": 0, "grab": 0, "lo": 999.0, "hi": -1.0, "seq": []}

    def _drag_stat_note(self, glass):
        """拖动中每帧记一笔「玻璃底有多亮」。

        只在内存里累加，拖动结束才落盘一行，每帧约 0.05ms。"""
        st = self.drag_stat
        if not st or not glass:
            return
        st["n"] += 1
        lm = _quick_luma(glass)
        if lm < st["lo"]:
            st["lo"] = lm
        if lm > st["hi"]:
            st["hi"] = lm
        if st["n"] % 4 == 1:
            st["seq"].append(int(lm))

    def _drag_stat_flush(self):
        """拖动结束：把这一趟的亮度曲线写一行到 %TEMP%\\snaplite_drag.log。

        为什么把日志留在产品里：拖动发黑是「偶发 + 位置随机」的毛病，不留这一趟
        的亮度序列，事后只能靠猜。一次拖动只写一行、几毫秒，正常使用无感；文件
        超过 200KB 自动重来，不会长到无限。

        读法：`luma=最小值..最大值`，正常一趟里两者相差很小；出现「黑一下」那一趟
        的最小值会明显塌下去（面板自己发暗时约 15，正常背景 35 左右），`seq` 里
        每 8 帧一个采样点，能看出塌在第几段。"""
        st = self.drag_stat
        self.drag_stat = None
        if not st or st["n"] < 3:
            return
        try:
            # 不用 tempfile：打包时它被 excludes 掉了（省体积），import 会失败
            path = os.path.join(os.environ.get("TEMP") or os.curdir,
                                "snaplite_drag.log")
            if os.path.exists(path) and os.path.getsize(path) > 200 * 1024:
                os.remove(path)
            with open(path, "a", encoding="ascii") as f:
                f.write("%s n=%d grab=%d luma=%d..%d seq=%s live=%d pad=%d\n"
                        % (time.strftime("%m-%d %H:%M:%S"), st["n"], st["grab"],
                           int(st["lo"]), int(st["hi"]),
                           ",".join(str(v) for v in st["seq"]),
                           1 if self.drag_live else 0, DRAG_PAD))
        except Exception as e:
            dbg("drag stat flush failed: %r" % (e,))

    def _end_drag_glass(self):
        """拖动结束：恢复抓屏属性、丢掉缩略图，并马上要求重抓一张。

        拖动期间背景可能已经变了（视频在放、别的窗口在动），松手后必须重新
        拍。这里只置标记 + 切活跃档，真正的重抓留给下一拍，避免在鼠标松开
        这一下多做一次 12.5ms 的活把松开动作拖粘。"""
        # 拖动期间给窗口设过「抓屏排除自己」，必须在这里恢复（幂等）：
        # 万一拖动是被截图/录制打断的、没走 _end_main_drag，也不能残留，
        # 否则别的截图工具从此拍不到主面板。
        # 先停预取线程、再还属性。反过来的话，线程可能正抓在手上，而属性
        # 已经清回 0 —— 那一张抓到的就是面板自己。
        self._drag_stat_flush()
        self._stop_drag_prefetch()
        if self.drag_live:
            self._excl_release("drag")
            self.drag_live = False
        self.drag_active = False
        self.drag_small = None
        self.drag_small_size = None
        self.glass_dirty = True
        self.glass_live_until = time.time() + GLASS_LIVE_HOLD
        self._set_glass_tick(GLASS_TICK_LIVE)

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
        with GFX(out[0]) as g:
            # 先铺实底：越界区域和裁剪偏差都靠它兜住，不会透出桌面
            g.fill(0xFF14161A, 0, 0, W, H)

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
            with GFX(big[0]) as gb:
                try:
                    ctypes.memmove(src[3], bytes(crop), ew * eh * 4)
                    gdip.GdipCreateBitmapFromScan0(ew, eh, ew * 4, 0x00022009,
                                                   src[3], byref(img))
                    gdip.GdipDrawImageRectI(gb.g, img, 0, 0, bw, bh)
                finally:
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

        取背景有两条路：
        - 新系统（Win10 2004+）：**现场抓目标位置背后那块桌面**。窗口还没搬
          到那里，所以那一块天然不含自己，抓到的就是真正的背景 —— 全分辨率，
          不会像缩略图那样放大 13 倍出锯齿，也没有「补掉自己」留下的横向渐变。
        - 老系统：退回按下瞬间那张整屏缩略图（`_drag_glass_bytes`），否则抓屏
          会把自己抓进去。

        返回 True 表示这一帧画了。"""
        if not (self.main_drag and self.hwnd_main):
            return False
        if not self.drag_live and not (self.drag_active and self.drag_small):
            return False
        pos = (left, top)
        if pos == self.drag_last_pos:
            return False

        if self.drag_live:
            # 先告诉预取线程「我要去哪儿」，它按这个位置取景。本帧用的是它
            # **上一轮**抓好的快照：快照在运动方向上多留一截、一轮约 12ms，
            # 正常拖动都盖得住，所以这一帧不用等它。
            #
            # 顺手把「这一帧走了多远」记下来：后台按它把余量往运动方向拉长
            # （见 _drag_pads）。位移是相对上次**成功提交**的位置算的，所以丢帧
            # 期间会自然累积成更大的位移，倾斜也更狠 —— 正是需要的反应。
            prev = self.drag_last_pos
            if prev is not None:
                self.drag_step = (left - prev[0], top - prev[1])
            self.drag_target = pos
            # 注意这里拿到的**已经是玻璃底**了：模糊在预取线程里对整块做过一次，
            # 主线程每帧只裁一片（约 0.05ms）。绝不能在这里再模糊一次 —— 那样
            # 降采样网格会跟着裁切点走，同一段壁纸每帧的条纹位置都在变，就是
            # 拖动时那层连成片的水波。
            glass = self._grab_behind(left, top)
            if glass is None:
                # 快照没盖住（甩得太快，后台还没跟到）——自己补抓一小块，
                # 只给这一帧用，不换掉后台那张大图（换了覆盖更小，会连着重抓）。
                # 首帧没有片子，才顺手存一张大的当起点。
                #
                # 这一步要等 DWM 几毫秒，本来是要避免的；但**绝不能**像之前那样
                # 直接丢帧：丢帧时窗口不动、鼠标位置却一路往前，越丢越远、
                # 越远越丢，几帧之后就彻底脱手了（实测甩动时 47/50 帧全丢）。
                # 宁可偶尔卡一下，也必须跟住手。
                first = self.drag_world is None
                glass = self._grab_behind_now(
                    left, top, store=first, pad=DRAG_PAD if first else None)
            if glass is None:
                # 补抓也失败（属性没生效 / 抓到自己），这一帧不搬窗口：
                # 搬过去配上旧玻璃底，背景会在面板里滑出一道水波。
                return False
            self.mn_glass = glass
        else:
            try:
                self.mn_glass = self._drag_glass_bytes(left, top)
            except Exception as e:
                dbg("drag glass render failed: %r" % (e,))
                return False

        self.drag_last_pos = pos
        self._drag_stat_note(self.mn_glass)
        self._present_main(w.POINT(left, top))
        return True

    def _grab_behind(self, left, top):
        """取 (left, top) 处面板大小的一层**玻璃底**（已模糊，可直接上屏）。

        只从后台线程维护的那张快照里裁（约 0.05ms）。裁不到（甩得太快、
        快照还没跟上）才由调用方决定补抓，见 _drag_frame。

        快照存的是「整块桌面模糊一遍之后的结果」，不是原始桌面：模糊只做一次，
        裁切严格等于平移，同一段壁纸不会因为窗口移动而换了条纹相位。

        调用前提：窗口已经设了 WDA_EXCLUDEFROMCAPTURE，抓屏时抓不到自己。"""
        if not DRAG_PREFETCH:
            return self._grab_behind_now(left, top)
        return self._drag_world_crop(left, top)

    def _drag_world_rect(self, left, top, padx, pady=None, leadx=0, leady=0,
                         back_x=False, back_y=False):
        """算出快照该抓哪一块：以窗口位置为基准，后方留 padx/pady、前方再按
        leadx/leady 多留一截，钳在虚拟屏内，并把左上角对齐到模糊网格。

        `padx/pady` 是**后方**（左上方向）的余量，`pady=None` 时取 padx（对称，
        首帧补抓和单元测试走这条）；`leadx/leady` 是**前方**额外延伸。
        窗口往右拖时，前面（右）需要更多余量、后面（左）留一点就够，于是整块
        长成一条横向的带子 —— 面积不变，但一帧 40px 的快拖能连续裁十几帧不用
        当场抓屏。正方形余量在垂直方向浪费掉的那一半，就是「快拖抖、慢拖不抖」
        的来源：余量只有 96px，两三帧就出界，主线程只能同步补抓一次 15~45ms。
        （按方向分配余量的计算见 `_drag_pads`。）

        `back_x/back_y` 表示「前方在负方向」（往左/往上拖），此时起点要往那边
        多挪 lead，宽度不变 —— 否则向左拖时那一截会加在身后，白留。

        对齐是**画面平滑的关键**，不是优化。模糊用「缩到 1/BLUR_F 再放回」做，
        降采样网格是相对这块图的左上角铺的：左上角每挪一像素，网格相对桌面就
        错一像素，同一段壁纸模糊出来的明暗条纹位置就变一次。拖动时每帧都要重
        算，条纹于是每帧抖一下 —— 细纹理上就是「连成片的水波」（实测偏差
        4.4~5.9/255，随偏移量以 BLUR_F 为周期来回摆）。左上角对齐到 BLUR_F 的
        整数倍后，所有快照的网格都落在屏幕同一套格线上，换快照逐像素一致，
        裁切也严格等于平移。

        尺寸必须取 BLUR_F 的整数倍，这是**相位一致的前提**：`_blur_bgra` 把宽
        缩到 `W // BLUR_F`，实际比例是 `W / (W // BLUR_F)`，只有 W 是 13 的整数
        倍时才精确等于 13。比例一偏，网格间隔就不是 13 而是 13.03/13.3，相位会
        随尺寸和偏移累积漂移 —— 实测非整数倍时同一块桌面差 1.0~1.3/255，取整后
        是 0.02~0.07。做到这一点后，**不同尺寸的快照之间也能无缝互换**，补抓才
        敢只抓一小块（见 DRAG_PAD_SMALL），前后不对称的余量也才敢随速度每轮变。

        还必须钳在虚拟屏内。`grab_screen` 对越界部分会用边缘像素铺满
        （`_fill_edges`），铺出来是一大片纯色；窗口贴着屏幕边拖时，快照一半在
        屏外，裁出来的面板就带进去一大块色带 —— 看着就是「界面一会黑一下」。
        """
        W, H = self.MW, self.MH
        if pady is None:
            pady = padx
        f = self.BLUR_F
        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

        def _tile(need, limit):
            """向上取整到 f 的倍数，但不超过屏幕能给的最大 f 倍数"""
            n = (need + f - 1) // f * f
            return max(f, min(n, limit // f * f))

        gw = _tile(W + padx * 2 + leadx + f, vw)
        gh = _tile(H + pady * 2 + leady + f, vh)
        gx = (left - padx - (leadx if back_x else 0)) // f * f
        gy = (top - pady - (leady if back_y else 0)) // f * f

        def _clamp(g, lo, hi, need):
            """把起点钳进 [lo, hi]，并**尽量**保住网格对齐。

            两件事要同时满足，冲突时覆盖优先：

              · need —— 快照的右/下边界必须盖住窗口，盖不住就是露黑边；
              · 对齐 —— 起点落在 f 的格线上，模糊条纹才不会随位置漂。

            不能直接 max(lo, min(g, hi))：hi = 屏宽 - gw 一般不是 f 的倍数，
            贴右边缘时 gx 会被压成 478（13×36=468、13×37=481），网格相对桌面
            错开 10px，同一段壁纸的条纹位置就变了 —— 拖动时就是「水波」，正是
            上面那一段讲的问题换了个入口回来。所以先把 hi 向下取到 f 的倍数。

            但向下取整会让出最多 f-1 像素的覆盖：窗口贴着屏右边时，对齐后的
            gx 恰好差 10px 盖不住窗口右边那一溜。这时退回不对齐 —— 露黑边比
            条纹跳难看多了，也更容易被当成 bug。"""
            floor = min(max(lo, need), hi)
            hi_a = hi // f * f
            if hi_a >= floor:
                return max(floor, min(g, hi_a))
            return max(floor, min(g, hi))

        # 贴边时覆盖优先：盖不住就直接露黑边了。
        gx = _clamp(gx, vx, vx + vw - gw, left + W - gw)
        gy = _clamp(gy, vy, vy + vh - gh, top + H - gh)
        return gx, gy, gw, gh

    def _drag_pads(self, dx, dy):
        """按这一帧的位移 (dx, dy) 分配余量，返回 (padx, pady, leadx, leady)。

        规则只有两条，都是为了守住同一个面积：

        1. **扇形分配**：位移基本是横向，就把绝大部分余量给横向；斜着拖就按
           两轴位移比例分。`k` 夹在 0.15~0.85，避免某一个方向被压得太薄 ——
           手总是斜着抖一点，纵向余量给太少就会立刻出界补抓。夹紧值实测：
           k=0.85 → 819×247（横向余量 465、纵向 82），k=0.15 → 416×494
           （横向 114、纵向 324）。两边都留够，面积都还是 20 万上下。
        2. **前重后轻**：整块往运动方向前方多留 `lead`，后方只留一点。前面那段
           是「手下一帧要去的地方」，后面那段只用得着一次（裁完就甩在身后）。

        面积由二次方程解出来，使 `(W+TX+f)(H+TY+f) ≈ DRAG_AREA`：面积不变，
        每轮「抓 + 模糊」的耗时就不变，主线程一帧都不会被拖慢。这一点是上一版
        加大余量失败的原因 —— 补抓是没了，但模糊变贵、整体发钝。
        """
        W, H = self.MW, self.MH
        f = self.BLUR_F
        ax, ay = abs(dx), abs(dy)
        mag = ax + ay
        if mag < 1e-6:
            # 没动（刚按下）：给对称余量，等方向出来再说
            return DRAG_PAD, DRAG_PAD, 0, 0
        k = ax / mag
        k = max(0.15, min(0.85, k))

        a = W + f                      # 不含余量时的「长」
        b = H + f
        m = (1.0 - k) / k
        B = m * a + b
        disc = B * B - 4.0 * m * (a * b - DRAG_AREA)
        if disc < 0:
            # 面板比目标面积还大（换了更大的面板/更小的 DRAG_AREA），退对称
            return DRAG_PAD, DRAG_PAD, 0, 0
        tx = (-B + disc ** 0.5) / (2.0 * m)
        ty = m * tx

        # 前向占比：0.5（静止）→ DRAG_LEAN_MAX（单帧位移 ≥ DRAG_LEAN_FULL）
        lean = min(1.0, mag / DRAG_LEAN_FULL)
        p = 0.5 + (DRAG_LEAN_MAX - 0.5) * lean
        padx = tx * (1.0 - p)
        pady = ty * (1.0 - p)
        leadx = tx * (2.0 * p - 1.0)
        leady = ty * (2.0 * p - 1.0)
        # 前方在左/上时，由 _drag_world_rect 的 back_x/back_y 把起点往那边挪，
        # 宽度不变、面积不变。这里只负责给出长度。
        return int(padx), int(pady), int(leadx), int(leady)

    def _grab_behind_now(self, left, top, store=None, pad=None):
        """现抓一块并当场模糊（主线程同步等 DWM + 几毫秒模糊）。

        只有首帧、甩得太快时补抓、以及老退路（DRAG_PREFETCH=False）会走到这里。
        它是整条拖动里唯一会让主线程等的地方，所以抓得**尽量小**：只要盖住这一
        帧（每边多留 DRAG_PAD_SMALL）。抓大块看着保险，实际是把一次 6ms 的卡
        放大成一次 20ms 的卡 —— 快甩时一帧只走几十像素，用不上那么多余量。

        抓小片不会和后台那片大图错开条纹，因为尺寸和左上角都取 BLUR_F 的倍数，
        相位只由左上角决定（见 _drag_world_rect）。

        `store` 控制要不要**替换** self.drag_world。补抓一律不换：换了就等于拿
        一张只有 39 像素余量的片去顶替后台那张 96 像素的，覆盖变小、下一帧立刻
        又不够，反而把补抓喂成连续补抓（实测 15/60 → 20/60）。首帧没片子时才存。"""
        W, H = self.MW, self.MH
        if pad is None:
            pad = DRAG_PAD_SMALL if DRAG_PREFETCH else self.DRAG_WORLD_PAD
        if store is None:
            store = not DRAG_PREFETCH
        gx, gy, gw, gh = self._drag_world_rect(left, top, pad)
        if self.drag_stat:
            self.drag_stat["grab"] += 1     # 这一帧走了同步抓屏（会停顿十几毫秒）
        if not self._excl_acquire("drag"):
            return None
        for attempt in range(2):
            try:
                if attempt:
                    # 第一张就抓到自己，说明属性还没在这一帧生效，等一拍再抓
                    try:
                        dwmapi.DwmFlush()
                    except Exception:
                        pass
                raw = grab_screen(gx, gy, gw, gh).raw
            except Exception as e:
                dbg("drag grab failed: %r" % (e,))
                return None
            if not raw or len(raw) != gw * gh * 4:
                return None
            ox, oy = left - gx, top - gy
            if ox < 0 or oy < 0 or ox + W > gw or oy + H > gh:
                return None
            mid = _crop_bgra(raw, gw, ox, oy, W, H)
            # 判「是不是抓到自己」只看中间那块（窗口就在这附近）：真抓到了
            # 还拿去模糊，面板会整块发暗闪一下，这一帧宁可丢掉等下一帧。
            if not self._looks_like_me(mid, W, H):
                if store:
                    return self._store_drag_world(raw, gx, gy, gw, gh, ox, oy)
                # 只给这一帧用：模糊这一小片、裁出来，不碰后台维护的那张大图
                try:
                    small = self._blur_bgra(raw, gw, gh)
                except Exception as e:
                    dbg("drag patch blur failed: %r" % (e,))
                    return None
                if not small or len(small) != gw * gh * 4:
                    return None
                return _crop_bgra(small, gw, ox, oy, W, H)
        return None

    def _store_drag_world(self, raw, gx, gy, gw, gh, ox, oy):
        """把整块桌面模糊一次存起来，并返回面板位置处那一块（已经是玻璃底）。

        模糊必须作用在**整块**上，不能先裁面板大小的块再模糊：后者每一帧的降
        采样网格都相对当帧的裁切点铺，于是同一段壁纸每帧模糊出来的条纹位置都
        在变，细纹理上就是一层连成片的水波。整块模糊一次之后，片内每一帧都只
        是纯裁切，内容严格等于平移，条纹自然不动了。

        代价是这一次要多花几毫秒（532×340 约 5.8ms），所以只在「抓新片」时付，
        每帧不再付；预取线程里付，主线程一帧都感觉不到。"""
        try:
            glass_big = self._blur_bgra(raw, gw, gh)
        except Exception as e:
            dbg("drag big blur failed: %r" % (e,))
            return None
        if not glass_big or len(glass_big) != gw * gh * 4:
            return None
        # 整块元组赋值是原子的：主线程要么读到旧片、要么读到新片，不会读到
        # 「坐标是新的、像素是旧的」这种撕裂状态。
        self.drag_world = (gx, gy, gw, gh, glass_big)
        return _crop_bgra(glass_big, gw, ox, oy, self.MW, self.MH)

    # ---------- 拖动取背景：后台预取线程 ----------
    def _start_drag_prefetch(self):
        """拖动开始：让后台线程一直抓「窗口周围一大块」，返回是否接手。

        整个拖动期间后台只做「抓一块大图 → 整块模糊一次 → 原子换掉
        self.drag_world」，窗口和 UpdateLayeredWindow 仍然只有主线程碰。

        流水线模式（DRAG_PIPELINE）下这一步是两条线程：一条只抓、一条只模糊，
        靠 self.drag_raw + self.drag_wake 交接。串行做这两件事时一轮 12ms，
        覆盖率卡在 8 像素/毫秒，快甩就跟不上、逼出主线程补抓（一次十几毫秒，
        那一下就是「抖」）；并行后一轮约 6ms，能力翻倍。"""
        if not DRAG_PREFETCH:
            return False
        if self.drag_thread is not None and self.drag_thread.is_alive():
            return True
        self.drag_stop = threading.Event()
        if DRAG_PIPELINE:
            self.drag_raw = None
            self.drag_wake = threading.Event()
            self.drag_thread = threading.Thread(
                target=self._drag_grab_loop, args=(self.drag_stop, self.drag_wake),
                name="drag-grab", daemon=True)
            self.drag_thread2 = threading.Thread(
                target=self._drag_blur_loop, args=(self.drag_stop, self.drag_wake),
                name="drag-blur", daemon=True)
            self.drag_thread.start()
            self.drag_thread2.start()
            return True
        self.drag_thread = threading.Thread(
            target=self._drag_prefetch_loop, args=(self.drag_stop,),
            name="drag-prefetch", daemon=True)
        self.drag_thread.start()
        return True

    def _stop_drag_prefetch(self, wait=0.5):
        """叫停预取线程并等它们退出（松手、退出程序时用）"""
        stop = self.drag_stop
        if stop:
            stop.set()
        me = threading.current_thread()
        for th in (self.drag_thread, self.drag_thread2):
            if th and th.is_alive() and th is not me:
                th.join(wait)
        self.drag_thread = None
        self.drag_thread2 = None
        self.drag_stop = None
        self.drag_wake = None
        self.drag_raw = None
        self.drag_world = None
        self.drag_target = None
        self.drag_step = (0, 0)

    def _drag_aim(self, left, top):
        """按当前运动方向算出这一轮该抓哪块、窗口在块里的偏移。

        返回 (gx, gy, gw, gh, ox, oy)；这块盖不住窗口时返回 None（贴到屏幕外
        或者屏幕比快照还小）。

        余量形状每轮跟着 `drag_step` 变，但宽高和左上角始终是 BLUR_F 的整数倍，
        所以不同形状的快照之间逐像素对齐、可以随便换（见 _drag_world_rect）。"""
        dx, dy = self.drag_step
        padx, pady, leadx, leady = self._drag_pads(dx, dy)
        gx, gy, gw, gh = self._drag_world_rect(
            left, top, padx, pady, leadx, leady, dx < 0, dy < 0)
        W, H = self.MW, self.MH
        ox, oy = left - gx, top - gy
        # 钳过屏之后窗口不一定还在这块里（窗口一半滑出屏幕时）：盖不住就别
        # 存，否则裁出来的是「隔壁的内容」，面板上会出现一块和位置对不上的
        # 画面 —— 比露黑边更难查。
        if ox < 0 or oy < 0 or ox + W > gw or oy + H > gh:
            return None
        return gx, gy, gw, gh, ox, oy

    def _drag_grab_loop(self, stop, wake):
        """流水线第一段：只抓，不模糊。

        抓这一下 6~25ms 几乎全在等 DWM 交出桌面表面（CPU 时间接近 0），把模糊
        挪到另一条线程之后，这段等待就和模糊重叠了 —— 一轮从 12ms 降到 6ms，
        覆盖能力（pad / 轮时长）随之翻倍。

        故意不 sleep：Windows 休眠最小粒度 15.6ms，随手 sleep(0.001) 实际会睡
        15.6ms，抓取频率掉到 28 次/秒（实测踩过这个坑）。
        故意不 DwmFlush：等消隐再抓反而更慢（四组对照验过，别再试）。
        """
        _lower_this_thread()
        seq = 0
        while not stop.is_set():
            if not self.main_drag or not self.drag_live:
                break
            tgt = self.drag_target
            if tgt is None:
                stop.wait(0.002)
                continue
            left, top = tgt
            # 每次按当前位置和当前运动方向重算抓取矩形（会随窗口移动、按方向
            # 拉长余量、并且钳在屏内）
            aim = self._drag_aim(left, top)
            if aim is None:
                # 窗口贴到屏幕外了，这块盖不全它，抓了也裁不出正确画面
                stop.wait(0.005)
                continue
            gx, gy, gw, gh, ox, oy = aim
            try:
                raw = grab_screen(gx, gy, gw, gh).raw
            except Exception as e:
                dbg("drag grab failed: %r" % (e,))
                stop.wait(0.005)
                continue
            if not raw or len(raw) != gw * gh * 4:
                stop.wait(0.005)
                continue
            seq += 1
            # 整块元组赋值是原子的：模糊线程要么读到旧块、要么读到新块，
            # 不会读到「坐标是新的、像素是旧的」这种撕裂状态。
            self.drag_raw = (seq, gx, gy, gw, gh, raw, ox, oy)
            wake.set()
        if self.drag_thread is threading.current_thread():
            self.drag_thread = None

    def _drag_blur_loop(self, stop, wake):
        """流水线第二段：把抓来的整块模糊一次，原子换掉 self.drag_world。

        模糊必须作用在**整块**上，不能先裁面板大小的块再模糊：后者每帧的降采样
        网格都相对当帧裁切点铺，同一段壁纸每帧模糊出来的条纹位置都在变，细纹理
        上就是一层连成片的水波。整块模糊一次之后，片内每帧只是纯裁切，内容严格
        等于平移，条纹自然不动。

        落后就跳过中间块、只模糊最新的那块：旧块就算模糊完也已经盖不住窗口了，
        白花几毫秒反而拖慢覆盖。
        """
        _lower_this_thread()
        W, H = self.MW, self.MH
        last = 0
        while not stop.is_set():
            r = self.drag_raw
            if r is not None and r[0] != last:
                last = r[0]
                _seq, gx, gy, gw, gh, raw, ox, oy = r
                try:
                    mid = _crop_bgra(raw, gw, ox, oy, W, H)
                    # 属性万一没生效就会抓到自己：这种块存进去，面板会整块发暗
                    # （就是「界面自己黑一下」）。丢掉，等下一块。
                    if not self._looks_like_me(mid, W, H):
                        self._store_drag_world(raw, gx, gy, gw, gh, ox, oy)
                except Exception as e:
                    dbg("drag blur failed: %r" % (e,))
                continue
            wake.wait(0.02)
            wake.clear()
        if self.drag_thread2 is threading.current_thread():
            self.drag_thread2 = None

    def _drag_prefetch_loop(self, stop):
        """后台线程：抓一整块 → 模糊一整块 → 原子换掉快照，然后立刻再来一轮。

        模糊放在这里而不是主线程，是这一版最重要的分工：主线程每帧只裁一片
        （0.05ms），拿到的内容严格等于「同一张模糊图平移」，不会因为窗口位置
        变了就换了条纹相位。

        故意不 sleep：Windows 的休眠最小粒度是 15.6ms，随手写个 time.sleep(0.001)
        实际会睡 15.6ms，抓取频率掉到 28 次/秒（实测），跟不上手 —— 那是这条
        路最容易踩的坑。抓一次 6ms + 模糊 5.8ms，天然就是约 85 次/秒。

        故意不 DwmFlush：等消隐再抓反而更慢（这个结论在 _glass_live_loop 里
        用四组对照验过，别再试）。"""
        _lower_this_thread()
        W, H = self.MW, self.MH
        while not stop.is_set():
            if not self.main_drag or not self.drag_live:
                break
            tgt = self.drag_target
            if tgt is None:
                stop.wait(0.002)
                continue
            left, top = tgt
            # 每次按当前位置和运动方向重算抓取矩形：窗口贴着屏幕边时快照要
            # 整块平移进屏，不能让它越界去触发补边。
            aim = self._drag_aim(left, top)
            if aim is None:
                # 窗口贴到屏幕外了，这张快照盖不全它，存了也裁不出正确画面
                stop.wait(0.005)
                continue
            gx, gy, gw, gh, ox, oy = aim
            try:
                raw = grab_screen(gx, gy, gw, gh).raw
            except Exception as e:
                dbg("drag prefetch failed: %r" % (e,))
                stop.wait(0.005)
                continue
            if not raw or len(raw) != gw * gh * 4:
                stop.wait(0.005)
                continue
            mid = _crop_bgra(raw, gw, ox, oy, W, H)
            # 属性万一没生效就会抓到自己：这种快照不能存，存了面板会整块发暗。
            # 丢掉这一轮，下一轮再试（属性是常设的，正常不会走到这里）。
            if self._looks_like_me(mid, W, H):
                stop.wait(0.005)
                continue
            # 模糊在这里做（整块，约 5.8ms），不在主线程做。主线程每帧只裁一片，
            # 内容严格等于「同一张模糊图平移」，条纹不会随位置抖。
            self._store_drag_world(raw, gx, gy, gw, gh, ox, oy)
        if self.drag_thread is threading.current_thread():
            self.drag_thread = None


    def _drag_world_crop(self, left, top):
        """快照能盖住 (left, top) 就裁一片玻璃底，盖不住返回 None。

        快照里存的是「整块模糊后的图」，所以这里裁出来直接就能上屏，主线程
        一帧连模糊都不用做。"""
        wd = self.drag_world
        if not wd:
            return None
        wx, wy, ww, wh, wb = wd
        W, H = self.MW, self.MH
        ox, oy = left - wx, top - wy
        if ox < 0 or oy < 0 or ox + W > ww or oy + H > wh:
            return None
        return _crop_bgra(wb, ww, ox, oy, W, H)

    # ---------- 自己接管的窗口拖动 ----------
    def _start_main_drag(self):
        """按下空白处：记下抓手，并决定这一趟拖动走哪条取背景的路。

        新系统走「实时抓目标位置」：按下不再需要 40ms 抓整屏，点一下反而更轻。
        老系统没有「抓屏排除自己」，只能沿用按下瞬间的整屏缩略图。

        不用 ReleaseCapture + WM_NCLBUTTONDOWN 交给系统拖，是因为那样窗口
        位置由系统改、背景由我们补，两件事不在同一帧里 —— 见 _drag_frame。"""
        if self.main_drag or self.capturing:
            return
        rc = w.RECT()
        user32.GetWindowRect(self.hwnd_main, byref(rc))
        p = w.POINT()
        user32.GetCursorPos(byref(p))

        # 拖动要独占「抓屏排除自己」这个属性：线程退休时会把属性清回 0，
        # 正好把拖动刚设上的 0x11 抹掉，于是拖到旧位置会抓到自己。
        # 干脆先把它停掉，松手后 _end_drag_glass 置脏 + 切活跃档，它会重启。
        self._stop_glass_live()

        self.drag_live = self._excl_acquire("drag")
        if self.drag_live:
            # 只在拖动这一小段时间排除自己，松手立刻恢复 —— 否则别的截图工具
            # 拍不到主面板
            try:
                # 等这一帧真的把它摘掉再开始抓，否则第一帧会抓到自己，
                # 看着像「面板里又套了一个面板」。等的就是下一个垂直消隐，
                # 实测 3~6ms —— 比旧方案按下时抓整屏那 40ms 便宜得多。
                dwmapi.DwmFlush()
            except Exception:
                pass
        else:
            self._begin_drag_glass()
            if not self.drag_active:
                # 抓不到整屏就别拖了：硬拖会得到一个没有背景的空白窗口
                return
        self.main_drag = True
        self.drag_last_pos = None
        self.drag_world = None          # 每次拖动重新抓，别用上一趟的旧快照
        self._drag_stat_begin()
        self.drag_target = (rc.left, rc.top)
        self.drag_step = (0, 0)         # 还没动，先给对称余量
        self.main_drag_off = (p.x - rc.left, p.y - rc.top)
        if self.drag_live:
            # 先在按下这一刻同步抓一张打底，然后交给后台线程续。打底这张要
            # 6~13ms，但花在「刚按下、手还没动」的时候，感觉不到；有它兜着，
            # 拖动第一帧就有背景，不会开局空一下。
            self._grab_behind_now(rc.left, rc.top)
            self._start_drag_prefetch()
        user32.SetCapture(self.hwnd_main)

    def _move_main_drag(self):
        """拖动中：按鼠标位置算目标坐标并画一帧"""
        if not self.main_drag:
            return
        p = w.POINT()
        user32.GetCursorPos(byref(p))
        self._drag_frame(p.x - self.main_drag_off[0],
                         p.y - self.main_drag_off[1])

    def _end_main_drag(self):
        """结束拖动：放掉鼠标捕获，收尾统一交给 _end_drag_glass"""
        if not self.main_drag:
            return
        self.main_drag = False
        user32.ReleaseCapture()
        self._end_drag_glass()

    def _corner_cov(self, R):
        """圆角像素覆盖率（按面积积分），结果缓存，只算一次。

        以前是 3×3 超采样，覆盖率只能取到 9 个值（0/28/57/85/113/142/170/
        227/255）。而圆角弧线上相邻像素的真实覆盖率是连续变化的，卡在这几档
        之间跳，角上就是一段亮一段暗的台阶 —— 面板边框那条 1px 高光因此显得
        「不均匀、有锯齿」。

        现在把像素沿 y 方向切 16 份，每份用圆弧在该高度的**精确水平弦长**求
        覆盖宽度再累加：x 方向是解析解，y 方向 16 等分。和 64×64 超采样真值
        比，最大偏差 16/255 → 2/255，平均 0.70 → 0.02，取值从 9 档变成 22 档
        且单调。

        只算一次（R 变了才重算）：R=20 时 5.9ms，R=40（200% 缩放）时 18.6ms，
        不会进每帧的热路径。
        """
        if self.mn_cov and self.mn_cov_key == R:
            return self.mn_cov
        SUB = 16
        cov = []
        for yy in range(R):
            row = []
            for xx in range(R):
                total = 0.0
                for s in range(SUB):
                    y = yy + (s + 0.5) / SUB
                    dy = R - y
                    if dy <= -R or dy >= R:
                        continue
                    w = sqrt(R * R - dy * dy)
                    lo = xx if xx > R - w else R - w
                    hi = (xx + 1) if (xx + 1) < (R + w) else (R + w)
                    if hi > lo:
                        total += (hi - lo) / SUB
                if total <= 0.0:
                    row.append(0)
                elif total >= 1.0:
                    row.append(255)
                else:
                    row.append(int(round(total * 255.0)))
            cov.append(row)
        self.mn_cov, self.mn_cov_key = cov, R
        return cov

    def _apply_round_mask(self, W, H):
        """把四角按圆角覆盖率削掉（含抗锯齿）。只处理 4 个角，
        直边本来就是不透明的，没必要每帧扫全图。

        用 bytearray 算：整块拷一份 200KB 只要 0.02ms，真正贵的是逐像素
        那个循环，所以换成 ctypes 数组「就地改」反而更慢（下标访问要过类型
        转换），实测 0.29ms → 0.34ms，不划算。"""
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
        """画主面板的全部内容。

        玻璃底（模糊桌面）已经垫在最下面，这里只叠：压暗层 → 顶部高光 →
        棱边 → 文字与按钮。"""
        R = self.RADIUS
        # 液态玻璃：模糊过的桌面已经垫在下面，这里只叠「压暗层 + 受光高光 + 棱边」
        g.fill_round(0x80101217, 0, 0, W, H, R)
        g.fill_round(0x1FFFFFFF, self.px(1), self.px(1),
                     W - self.px(2), self.px(1.6), max(1.0, R - 1))
        g.stroke_round(0x59FFFFFF, 1, 0.5, 0.5, W - 1, H - 1, R)

        close, pin, hide, btn, hot = self._main_layout()

        # 标题 + 版本号。版本号紧跟在 "SnapLite" 后面，与标题同一个框、同样居中
        # —— 标题宽度按实际字体量出来（DPI 或字体变了会自己跟上），不写死偏移。
        # 注意绘制用的 px(120) 是个宽松的布局框（远大于字形），命中判断用的是
        # _title_rect 量出来的字形框，两者别混。
        g.text(APP_NAME, self.px(19), C_TITLE, self.px(19), self.px(10),
               self.px(120), self.px(28), 0, True)
        trect = self._title_rect(g) or (self.px(19), self.px(10), 0, 0)
        g.text("v" + APP_VERSION, self.px(10.5), C_HINT,
               trect[0] + trect[2] + self.px(7), self.px(10),
               self.px(56), self.px(28), 0)

        # 图钉（置顶开关）：开着时图标常亮 + 一层浅底，关着跟 — ✕ 一样是灰的。
        # 用「亮不亮」而不是换图标来表达状态，26px 里改形状反而看不清。
        #
        # 造型是「↑ 顶到一条横线」，不是图钉剪影：26px 里画图钉，帽+斜腰+针
        # 三条线挤在一起会糊成一个漏斗（实测过，见 tools/preview_pin_icons.py），
        # 而箭头只有一条竖线和两撇，任何尺寸下都不会看错。
        hot_pin = (self.hover_main == "pin")
        if hot_pin:
            g.fill_round(C_HOVER_SOFT, pin[0], pin[1], pin[2], pin[3], self.px(7))
        elif self.topmost:
            g.fill_round(C_TAG_BG, pin[0], pin[1], pin[2], pin[3], self.px(7))
        pc = C_WHITE if (hot_pin or self.topmost) else C_SUB
        pcx, pcy = pin[0] + pin[2] / 2, pin[1] + pin[3] / 2
        pw_ = max(1.2, self.px(1.4))
        g.line(pc, pw_, pcx - self.px(4.6), pcy - self.px(5.8),
               pcx + self.px(4.6), pcy - self.px(5.8))
        g.line(pc, pw_, pcx, pcy + self.px(5.8), pcx, pcy - self.px(1.8))
        g.line(pc, pw_, pcx - self.px(3.2), pcy + self.px(0.4),
               pcx, pcy - self.px(3.6))
        g.line(pc, pw_, pcx + self.px(3.2), pcy + self.px(0.4),
               pcx, pcy - self.px(3.6))

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
        """主面板窗口过程 —— 只做分派，具体动作在下面的 _main_* 方法里。

        以前这里是 97 行、34 个分支的 if 链：想改「点 ✕ 干什么」得先通读全文。
        现在按输入类别拆成 5 个方法（键 / 移动 / 离开 / 按下 / 抬起 / 丢捕获），
        和 _on_overlay 同一个套路 —— 一次交互 = 一个方法。"""
        if self.wm_taskbar_created and msg == self.wm_taskbar_created:
            # explorer 重启了：任务栏那格是它的内存，图标要重新挂一次
            self._recreate_tray()
            return 0
        if msg == WM_DISPLAYCHANGE:
            # 分辨率 / 显示器插拔 / 缩放变了：面板可能被留在屏幕外
            self._on_display_change()
            return 0
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
        if msg == WM_TIMER:
            if wp == TIMER_GLASS:
                self._tick_glass()
            return 0
        if msg == WM_GLASS_FRAME:
            if wp == 1:
                # 线程收工：定时器回到低频探测，下次桌面再变由它叫醒线程
                self.glass_thread = None
                self._set_glass_tick(GLASS_TICK_IDLE)
            else:
                self._glass_frame_ready()
            return 0
        if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
            if self._main_key(wp):
                return 0
        if msg == WM_ACTIVATE:
            if (wp & 0xFFFF) == 0 and self.recording:
                self._end_record()
            return 0
        if msg == WM_MOUSEMOVE:
            self._main_mouse_move(hwnd, lp)
            return 0
        if msg == WM_MOUSELEAVE:
            self._main_mouse_leave(hwnd)
            return 0
        if msg == WM_LBUTTONDOWN:
            self._main_click(hwnd, lp)
            return 0
        if msg == WM_RBUTTONUP:
            # 主面板右键 = 托盘那份菜单，但只认「SnapLite」logo 那一块
            self._panel_menu(lp)
            return 0
        if msg == WM_LBUTTONUP:
            if self._main_release():
                return 0
        if msg == WM_CAPTURECHANGED:
            if self._main_capture_lost():
                return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _main_hit(self, x, y):
        """(x, y) 落在主面板哪个可交互元素上：close / pin / hide / btn / hot / None"""
        close, pin, hide, btn, hot = self._main_layout()
        if self._in(x, y, close):
            return "close"
        if self._in(x, y, pin):
            return "pin"
        if self._in(x, y, hide):
            return "hide"
        if self._in(x, y, btn):
            return "btn"
        if self._in(x, y, hot):
            return "hot"
        return None

    def _main_key(self, wp):
        """按键：录制热键时收下这一键，否则吃掉 Esc。

        返回 True = 已处理（调用方直接 return 0）；False = 交给系统默认处理。"""
        if self.recording:
            self._on_record_key(wp)
            return True
        return wp == VK_ESCAPE

    def _main_mouse_move(self, hwnd, lp):
        """鼠标移动：正在拖窗口就只移动窗口，否则只更新高亮元素"""
        if self.main_drag:
            self._move_main_drag()
            return
        x, y = lo(lp), hi(lp)
        hot_id = self._main_hit(x, y)
        if hot_id != self.hover_main:
            self.hover_main = hot_id
            user32.InvalidateRect(hwnd, None, False)
        if not self.tracking_main:
            # 不开 TrackMouseEvent 就收不到 WM_MOUSELEAVE，高亮会一直留着
            self.tracking_main = True
            tme = TRACKMOUSEEVENT(sizeof(TRACKMOUSEEVENT), 2, hwnd, 0)
            user32.TrackMouseEvent(byref(tme))

    def _main_mouse_leave(self, hwnd):
        """鼠标移出面板：清掉高亮，否则按钮会一直亮着"""
        self.tracking_main = False
        if self.hover_main is not None:
            self.hover_main = None
            user32.InvalidateRect(hwnd, None, False)

    def _main_click(self, hwnd, lp):
        """左键按下：✕/— 收进托盘、图钉切置顶、热键条开始录制、截图按钮开拍、空白拖动窗口"""
        x, y = lo(lp), hi(lp)
        where = self._main_hit(x, y)
        if where in ("close", "hide"):
            # ✕ 只收进托盘，不退出；退出走托盘右键菜单
            user32.ShowWindow(hwnd, SW_HIDE)
            return
        if where == "pin":
            self._toggle_topmost()
            return
        if where == "hot":
            if self.recording:
                self._end_record()
            else:
                self._begin_record()
            return
        if where == "btn":
            self.start_capture()
            return
        if self.recording:
            self._end_record()
            return
        # 空白处可拖动窗口（自己接管，位置与背景同帧提交，见 _drag_frame）
        self._start_main_drag()

    def _is_topmost(self):
        """读窗口的真实置顶状态，而不是信自己记的那个书签。

        `SetWindowPos` 是可能失败的（句柄失效、被 Shell 抢 z-order），失败时
        `self.topmost` 照样翻转，图钉就会亮着而窗口其实没置顶 —— 用户看到的是
        「点了没反应」。所以每次点完都回读一次 ex-style，以窗口为准。"""
        if not self.hwnd_main:
            return False
        return bool(user32.GetWindowLongW(self.hwnd_main, GWL_EXSTYLE)
                    & WS_EX_TOPMOST)

    def _toggle_topmost(self):
        """切换「总在最前」。

        用 SetWindowPos 换 z-order，而不是重设 WS_EX_TOPMOST 样式：改样式要走
        SetWindowLong + SetWindowPos(FRAMECHANGED)，分层窗口重排会闪一帧空窗。
        SWP_NOACTIVATE 让焦点留在原处，点按钮不会把面板强行提到前台。"""
        if not self.hwnd_main:
            return
        want = not self.topmost
        ins = w.HWND(HWND_TOPMOST if want else HWND_NOTOPMOST)
        user32.SetWindowPos(self.hwnd_main, ins, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        real = self._is_topmost()
        if real != want:
            dbg("topmost: SetWindowPos 没生效（想要 %s，实际 %s）" % (want, real))
        self.topmost = real            # 以窗口实际状态为准，别让图钉假亮
        user32.InvalidateRect(self.hwnd_main, None, False)
        dbg("topmost -> %s" % self.topmost)

    def _main_release(self):
        """左键抬起：结束窗口拖动。返回 True = 已处理，False = 放给系统"""
        if not self.main_drag:
            return False
        self._move_main_drag()     # 抬手那一下先对齐到最终位置
        self._end_main_drag()
        return True

    def _main_capture_lost(self):
        """鼠标捕获被系统或别的窗口抢走（Alt+Tab、弹窗…）→ 立刻收尾。

        不收回来的话窗口会一直黏在鼠标上。返回 True = 已处理。"""
        if not self.main_drag:
            return False
        self._end_main_drag()
        return True

    def _title_rect(self, g):
        """「SnapLite」这几个字形实际占的那一块（不含版本号）。

        宽度按字形量，不写死：换系统字体、调 DPI、改字号都会自己跟上。绘制那
        边用的 `px(120)` 是个宽松的布局框（远大于字形本身），拿它做命中判断会
        让标题右边一大片空白也能右键出菜单 —— 这就是为什么要单独量一次。

        量不出来（画布句柄无效时 measure 会给 0）就返回 None，调用方按「不中」
        处理：宁可右键没反应，也不要在整块面板上乱弹。"""
        try:
            tw, th = g.measure(APP_NAME, self.px(19), True)
        except Exception:
            return None
        if tw < self.px(10):
            return None
        # +1：measure 给的是浮点，截断后少半像素就可能漏掉最后一个字母的右缘
        return (self.px(19), self.px(10), int(tw) + 1, int(th) + 1)

    def _in_title(self, x, y):
        """(x, y) 是否点在「SnapLite」logo 上 —— 主面板只有这里右键才弹菜单。

        往外放一圈 px(6)：严格卡在字形边界上的话，点在字的边缘会觉得「明明
        点到了却没反应」；但也不放到整条标题栏，右边还有版本号和三个按钮。"""
        try:
            with GFX(self.mn_dc) as g:
                rect = self._title_rect(g)
        except Exception:
            return False
        if rect is None:
            return False
        tx, ty, tw, th = rect
        pad = self.px(6)
        return (tx - pad <= x < tx + tw + pad) and (ty - pad <= y < ty + th + pad)

    def _in(self, x, y, rect):
        """点是否落在矩形内。矩形是 (x, y, w, h) 这种四元组"""
        return rect[0] <= x < rect[0] + rect[2] and rect[1] <= y < rect[1] + rect[3]

    def _show_panel(self):
        """从托盘把主面板叫回来，并重抓一次玻璃底（藏在托盘期间桌面可能变了）"""
        if not self.hwnd_main:
            return
        # 从托盘唤回时重抓玻璃底：藏在托盘期间桌面可能已经变了
        self._refresh_glass()
        user32.ShowWindow(self.hwnd_main, SW_SHOW)
        user32.SetForegroundWindow(self.hwnd_main)
        user32.InvalidateRect(self.hwnd_main, None, False)

    # ================================================== 4. 截图流程 —— 遮罩 / 抓屏 / 缓冲 / 自动识别
    def start_capture(self):
        """开始截图（托盘、热键、面板按钮都走这里）。

        外面包一层异常兜底：中途任何一步失败都要把界面复原，不能把主窗口
        永久留在透明帧上 —— 1.1.0 就有这个 bug，截图失败后界面再也回不来。"""
        if self.capturing:
            return
        # 拖动中途被热键打断时，鼠标捕获还挂在主窗口上 —— 不先收尾的话，
        # 遮罩收不到鼠标（框选失效），主界面还会继续跟着光标跑。
        self._end_main_drag()
        # 截图要独占抓屏：线程还在抓的话，两边都会把自己标成「抓屏排除」
        # 再各自恢复，遮罩和缩略图都可能拍不到面板。先把它停干净再开工。
        self._stop_glass_live()
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
        """截图的准备阶段：留底主界面 → 提交透明帧 → 抓屏建缓冲 → 显示遮罩。

        关键点：主窗口保持“可见”状态但内容全透明，靠遮罩里的副本顶替显示。
        这样它既不会被抓进截图，也不会被整屏遮罩盖住（以前置顶的写法会出现
        “不管有没有框到都不变灰”和“点工具后主界面消失”）。"""
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
        """抓一张整屏，造出三套缓冲：shot 原图 / dim 压暗 / back 呈现。

        压暗层在这里一次算好，之后每帧只做一次 BitBlt，不用反复叠半透明黑。"""
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
        with GFX(self.dc_dim) as g:
            g.fill(0x9A000000, 0, 0, vsw, vsh)

    def _release_capture_buffers(self):
        """释放截图期间的缓冲和屏幕 DC。

        工具条的玻璃底是从这套缓冲里取的，缓冲一换必须一并作废，否则会用到
        已经删掉的 DC（花屏或崩溃）。"""
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
        self._cells_cache = {}
        # 玻璃底取自这套缓冲，缓冲一换就必须作废
        self.tb_glass = None
        self.tb_glass_key = None

    # ---------- 自动识别
    def _request_probe(self, sx, sy, force=False):
        """限流地请求控件识别：换了窗口、或移动超过 70px 才重查一次。

        不这么限的话，鼠标一动就几百次 COM 调用，界面直接卡住。"""
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

    # ================================================== 5. 遮罩合成与标注绘制
    def _compose_overlay(self, W, H):
        """合成遮罩的整帧：压暗底 → 挖亮选区 → 画标注 → 选区框与手柄 → 提示条。

        每帧都从 dc_dim 重新起画（不是在上帧结果上叠加），所以撤销、拖标注、
        切模式都不会留下残影。"""
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

        with GFX(self.dc_back) as g:
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

    def _mode_tip(self):
        """当前模式在提示条上显示的那句话"""
        if self.mode == "pen":
            return "按住左键自由写画   ·   上排可切颜色 / 粗细   ·   Esc 退出"
        if self.mode == "mosaic":
            return "按住左键涂抹马赛克   ·   上排可切笔头粗细 / 块大小   ·   Esc 退出"
        if self.mode == "arrow":
            return "拖动画箭头   ·   选中后拖两端可改方向 / 长度   ·   Esc 退出"
        if self.mode == "rect":
            return "拖动画方框   ·   选中后拖四角改大小   ·   上排可切颜色 / 线宽   ·   Esc 退出"
        if self.mode == "ellipse":
            return "拖动画圆形   ·   选中后拖四角改大小   ·   上排可切颜色 / 线宽   ·   Esc 退出"
        if self.sel_shape is not None:
            if self.sel_shape.get("t") == "arrow":
                return "拖端点改方向 · 拖线身整体移动 · 方向键微调 · Delete 删除"
            if self.sel_shape.get("t") in ("rect", "ellipse"):
                return "拖四角改大小 · 拖边框整体移动 · 方向键微调 · Delete 删除"
            return "拖动标注移动位置 · 方向键微调 · Delete 删除"
        return "拖动空白重新框选   ·   单击标注可选中   ·   Esc 取消"

    def _draw_tip(self, g, W, tip):
        """画底部提示条。先量文字宽度再定气泡宽，文字才不会顶到边框"""
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
        """选区八个手柄（四角 + 四边）的矩形。命中判断和绘制共用这一份，
        两边算的必须完全一致，否则会出现“看着能拖、实际拖不动”"""
        x, y, ww, hh = sel
        out = []
        for name, cx, cy in self._HANDLES:
            s = self.px(8) if name in ("n", "s", "e", "w") else self.px(10)
            hx = x + ww * cx / 2.0 - s / 2.0
            hy = y + hh * cy / 2.0 - s / 2.0
            out.append((name, hx, hy, s, s))
        return out

    def _draw_handles(self, g, sel):
        """画选区手柄：白底 + 深色描边，保证在亮背景和暗背景上都看得见"""
        for name, hx, hy, hw_, hh_ in self._handle_rects(sel):
            g.fill_round(0xFFFFFFFF, hx, hy, hw_, hh_, self.px(2))
            g.stroke_round(C_ACCENT, max(1.0, self.px(1.3)), hx, hy, hw_, hh_, self.px(2))

    # ---------- 标注绘制（画笔 / 马赛克 / 箭头 / 方框 / 圆形）
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
        """笔刷马赛克覆盖到的顶点单元集合（屏幕坐标对齐网格），带缓存。

        缓存不放在标注字典里，而是放在 App 的 _cells_cache —— 标注字典只存
        「画的是什么」，撤销快照、平移、改色都不用再记得去清缓存，也就不会
        出现「清了这里忘了那里」导致的马赛克错位。

        命中条件用「pts 列表本身（同一个对象）+ 点数 + 块大小 + 半径」：
        画的过程中是往同一个列表 append（点数变），平移是换成一个新列表
        （对象变），两种情况都会自然失效。命中时还比对 `is`，所以即使
        列表被回收、id 被别的对象复用，也不会读到别人的结果。"""
        blk = max(4, sh.get("blk") or self._mosaic_blk())
        pts = sh.get("pts") or []
        r = max(2.0, sh.get("r") or self._mosaic_r())
        ck = (id(pts), len(pts), blk, r)
        hit = self._cells_cache.get(ck)
        if hit is not None and hit[0] is pts:
            return hit[1]
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
        # 缓存里同时存一份 pts 引用：只要这条还在，id 就不会被复用
        if len(self._cells_cache) > 64:
            self._cells_cache.clear()
        self._cells_cache[ck] = (pts, cells)
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
            elif sh.get("t") in ("rect", "ellipse"):
                self._draw_box_handles(g, sh, ox, oy)
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
        """点是否落在箭头附近（两端手柄范围或线身的容差内）"""
        tol = float(self.px(ARROW_HIT_TOL))
        r = float(self.px(ARROW_HANDLE_R + 3))
        if hypot(x - sh["x1"], y - sh["y1"]) <= r or hypot(x - sh["x2"], y - sh["y2"]) <= r:
            return True
        return self._arrow_dist(x, y, sh["x1"], sh["y1"], sh["x2"], sh["y2"]) <= tol

    # ---------- 方框 / 圆形
    def _box_rect(self, sh):
        """归一化矩形 (x, y, w, h)：x1..y2 是拖动时的两个对角，可能反向"""
        x1, y1, x2, y2 = sh["x1"], sh["y1"], sh["x2"], sh["y2"]
        return min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)

    def _draw_box_item(self, g, sh, ox=0, oy=0):
        """画一个方框 / 圆形标注（按当前颜色和线宽）"""
        x, y, ww, hh = self._box_rect(sh)
        if ww < 0.5 or hh < 0.5:
            return
        color = ANNO_COLORS[max(0, min(sh.get("color", 0), len(ANNO_COLORS) - 1))]
        w = max(1.5, float(sh.get("w") or self._pen_w()))
        if sh.get("t") == "rect":
            g.stroke_box(color, w, x + ox, y + oy, ww, hh)
        else:
            g.stroke_ellipse(color, w, x + ox, y + oy, ww, hh)

    def _box_corners(self, sh, ox=0, oy=0):
        """四角坐标：左上 / 右上 / 右下 / 左下（顺序和手柄编号一致）"""
        x, y, ww, hh = self._box_rect(sh)
        return ((x + ox, y + oy), (x + ww + ox, y + oy),
                (x + ww + ox, y + hh + oy), (x + ox, y + hh + oy))

    def _draw_box_handles(self, g, sh, ox=0, oy=0):
        """画方框 / 圆形被选中时的四角手柄"""
        r = self.px(BOX_HANDLE_R)
        for cx, cy in self._box_corners(sh, ox, oy):
            g.fill_round(0xFFFFFFFF, cx - r, cy - r, r * 2, r * 2, self.px(2))
            g.stroke_round(C_ACCENT, max(1.0, self.px(1.4)),
                           cx - r, cy - r, r * 2, r * 2, self.px(2))

    def _box_handle_at(self, sh, x, y):
        """点在第几个角上：1..4，0 = 不在任何角上"""
        r = float(self.px(BOX_HANDLE_R + 5))
        for i, (cx, cy) in enumerate(self._box_corners(sh)):
            if hypot(x - cx, y - cy) <= r:
                return i + 1
        return 0

    def _box_near(self, sh, x, y):
        """点是否落在边框附近的环带上（点中间空白不算选中，方便在里面继续画）"""
        tol = float(self.px(BOX_HIT_TOL))
        bx, by, bw, bh = self._box_rect(sh)
        if bw < 1.0 or bh < 1.0:
            return False
        if sh.get("t") == "rect":
            if not (bx - tol <= x <= bx + bw + tol
                    and by - tol <= y <= by + bh + tol):
                return False
            return min(abs(x - bx), abs(x - bx - bw),
                       abs(y - by), abs(y - by - bh)) <= tol
        # 椭圆：换算到单位圆，看归一化半径是否贴近 1
        cx, cy = bx + bw / 2.0, by + bh / 2.0
        rx, ry = bw / 2.0, bh / 2.0
        rr = hypot((x - cx) / rx, (y - cy) / ry)
        band = max(tol / max(rx, ry), 0.06)
        return abs(rr - 1.0) <= band

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

    def _translate_shapes(self, dx, dy):
        """选区整体移动时，标注跟着一起走"""
        if not dx and not dy:
            return
        for sh in self.shapes:
            t = sh.get("t")
            if t in ("arrow", "rect", "ellipse"):
                sh["x1"] += dx; sh["y1"] += dy
                sh["x2"] += dx; sh["y2"] += dy
            elif t in ("mosaic", "pen"):
                # 换成一个新列表 → 马赛克网格缓存按「pts 对象」判失效
                sh["pts"] = [(px_ + dx, py_ + dy) for px_, py_ in sh.get("pts") or []]
        if self.pending:
            self._translate_shapes_one(self.pending, dx, dy)

    def _translate_shapes_one(self, sh, dx, dy):
        """整体平移一条标注（选区跟着动时，标注要一起动）"""
        t = sh.get("t")
        if t in ("arrow", "rect", "ellipse"):
            sh["x1"] += dx; sh["y1"] += dy
            sh["x2"] += dx; sh["y2"] += dy
        elif t in ("mosaic", "pen"):
            sh["pts"] = [(px_ + dx, py_ + dy) for px_, py_ in sh.get("pts") or []]

    def _hit_shape(self, x, y):
        """从后往前找命中的标注：后画的在上层，优先选中它"""
        for sh in reversed(self.shapes):
            t = sh.get("t")
            if t == "arrow" and self._arrow_near(sh, x, y):
                return sh
            if t in ("rect", "ellipse") and self._box_near(sh, x, y):
                return sh
        return None

    def _draw_one_shape(self, g, src, sh, ox, oy):
        """按标注类型分派绘制（画笔 / 马赛克 / 箭头 / 方框 / 圆形）"""
        t = sh.get("t")
        if t == "pen":
            self._draw_pen_item(g, sh, ox, oy)
        elif t == "mosaic":
            self._draw_mosaic_item(g, sh, ox, oy)
        elif t in ("rect", "ellipse"):
            self._draw_box_item(g, sh, ox, oy)
        elif t == "arrow":
            x1, y1, x2, y2 = sh["x1"], sh["y1"], sh["x2"], sh["y2"]
            self._draw_arrow_item(g, x1 + ox, y1 + oy, x2 + ox, y2 + oy,
                                  sh.get("color", 0))

    def _paint_overlay(self, hwnd):
        """响应遮罩的 WM_PAINT：只把已有的一帧重新提交，不重新合成。

        分层窗口的画面由 UpdateLayeredWindow 提交，本来就不靠 WM_PAINT 绘制。
        以前这里调 _redraw_overlay()，等于每来一条重绘请求就把整屏重新合成一遍
        （实测 compose=1 present=1，约 13ms），而那时状态并没有变化 —— 纯浪费。

        现在只做一次 _present_overlay：dc_back 里始终是最近一次合成好的整帧，
        重推一遍即便系统丢过表面也能自愈，代价只是一次 ULW。
        真正改变画面的地方全部显式调 _redraw_overlay()（24 处），不依赖这条消息。"""
        ps = PAINTSTRUCT()
        user32.BeginPaint(hwnd, byref(ps))
        self._present_overlay()
        user32.EndPaint(hwnd, byref(ps))

    def _redraw_overlay(self):
        """重算并提交遮罩的一帧。所有改动最后都收口到这一个函数"""
        vsw, vsh = self.vs[2], self.vs[3]
        self._compose_overlay(vsw, vsh)
        self._present_overlay()

    # ================================================== 6. 遮罩交互 —— 消息 / 选区 / 标注状态
    def _on_overlay(self, hwnd, msg, wp, lp):
        """遮罩窗口过程 —— 只按消息类型分派，具体动作在下面的 _ov_* 方法里。

        以前这里是 317 行的巨型 if 链，想改「拖选区」得先通读全文；现在
        「一次鼠标动作 = 一个方法」，改哪段看哪个方法就行。"""
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
            self._ov_move(lo(lp), hi(lp))
            return 0
        if msg == WM_LBUTTONDOWN:
            self._ov_down(lo(lp), hi(lp))
            return 0
        if msg == WM_LBUTTONUP:
            self._ov_up()
            return 0
        if msg == WM_LBUTTONDBLCLK:
            self._ov_dblclick(lo(lp), hi(lp))
            return 0
        if msg == WM_RBUTTONDOWN:
            self._ov_right()
            return 0
        if msg == WM_KEYDOWN:
            if wp == VK_ESCAPE:
                self._ov_escape()
                return 0
            if self._ov_key(wp):
                return 0
        if msg == WM_SETCURSOR:
            user32.SetCursor(self._cursor_for())
            return 1
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _ov_move(self, x, y):
        """鼠标移动：画标注、改选区、悬停高亮都从这里进。"""
        self.mouse = (x, y)
        if self.mode and not self.drag_mode:
            # 画笔 / 马赛克时鼠标是专用笔头，不再是十字
            user32.SetCursor(self._cursor_for())
            if self.draw_start:
                self._update_pending(x, y)
                self._redraw_overlay()
            return
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
                    self.press_moved = True
            elif self.drag_mode == "box_resize":
                sh = self.sel_shape
                if sh is not None and self.shape0 and self.drag_end:
                    # 对角固定，被拖的角跟着鼠标走
                    x1, y1, x2, y2 = self.shape0
                    bx, by = min(x1, x2), min(y1, y2)
                    bw, bh = abs(x2 - x1), abs(y2 - y1)
                    corners = ((bx, by), (bx + bw, by),
                               (bx + bw, by + bh), (bx, by + bh))
                    ax, ay = corners[(self.drag_end - 1 + 2) % 4]
                    ex, ey = self._clamp_to_sel(x, y)
                    sh["x1"], sh["y1"] = ax, ay
                    sh["x2"], sh["y2"] = ex, ey
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
        return

    def _ov_down(self, x, y):
        """鼠标左键按下：按当前模式决定是「画一笔」还是「开始拖」。"""
        self._raise_tool()          # 点击遮罩时保证工具条不被压到下面
        self.drag_snap = self._snap()      # 动作前的状态（拖动/绘制结束后入撤销栈）

        # ① 选中箭头的端点优先：拖端点 = 改方向 / 改长度
        cur = self.sel_shape
        if cur is not None and cur.get("t") == "arrow":
            e = self._arrow_end_at(cur, x, y)
            if e:
                self.drag_mode, self.drag_end = "arrow_end", e
                self.press_pt = (x, y)
                self.press_moved = False
                return

        if self.mode == "pen":
            cx, cy = self._clamp_to_sel(x, y)
            self.draw_start = (cx, cy)
            self.pending = {"t": "pen", "pts": [(cx, cy)],
                            "color": self._color_idx(),
                            "w": self._pen_w()}
            self._redraw_overlay()
            return
        if self.mode == "mosaic":
            cx, cy = self._clamp_to_sel(x, y)
            self.draw_start = (cx, cy)
            self.pending = {"t": "mosaic", "pts": [(cx, cy)],
                            "r": self._mosaic_r(),
                            "blk": self._mosaic_blk()}
            self._redraw_overlay()
            return
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
                return
            cx, cy = self._clamp_to_sel(x, y)
            self.draw_start = (cx, cy)
            self.pending = None
            self._redraw_overlay()
            return
        if self.mode in ("rect", "ellipse"):
            # 点中同类形状 = 选中它（拖四角改大小 / 拖边框整体移动），否则画新的
            sh = self._hit_shape(x, y)
            if sh is not None and sh.get("t") == self.mode:
                self.sel_shape = sh
                self._sync_opts(sh)
                h = self._box_handle_at(sh, x, y)
                self.drag_mode = "box_resize" if h else "shape"
                self.drag_end = h
                self.shape0 = self._shape_origin(sh)
                self.press_pt = (x, y)
                self.press_moved = False
                self._redraw_overlay()
                self._refresh_tool()
                return
            cx, cy = self._clamp_to_sel(x, y)
            self.draw_start = (cx, cy)
            self.pending = {"t": self.mode, "x1": cx, "y1": cy,
                            "x2": cx, "y2": cy,
                            "color": self._color_idx(), "w": self._pen_w()}
            self._redraw_overlay()
            return
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
            return
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
        return

    def _ov_up(self):
        """鼠标左键抬起：把刚才那一笔落到 shapes 里，或结束一次拖动。"""
        if self.drag_mode in ("shape", "arrow_end", "box_resize"):
            if self.press_moved:
                self._push_snap(self.drag_snap)     # 拖动完了才记一步撤销
            self.drag_mode = None
            self.drag_end = 0
            self.shape0 = None
            self._redraw_overlay()
            if self.sel:
                self._show_tool()
            return
        if self.mode in ("pen", "mosaic", "arrow", "rect", "ellipse"):
            added = False
            if self.draw_start and self.pending:
                sh = self.pending
                if sh["t"] == "arrow":
                    if hypot(sh["x2"] - sh["x1"], sh["y2"] - sh["y1"]) > self.px(12):
                        self.shapes.append(sh)
                        added = True
                elif sh["t"] in ("rect", "ellipse"):
                    # 太小的当误点丢掉，避免屏幕上留下一颗小点
                    mn = self.px(BOX_MIN_SIZE)
                    if (abs(sh["x2"] - sh["x1"]) >= mn
                            and abs(sh["y2"] - sh["y1"]) >= mn):
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
            return
        if self.mode:
            return
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
            return
        self._redraw_overlay()
        self._show_tool()
        return

    def _ov_dblclick(self, x, y):
        """双击空白处 = 直接复制成品（点在标注上不算）。"""
        sh = self._hit_shape(x, y)
        if sh is None and self.sel and self._in(x, y, self.sel):
            self._action_copy()             # 双击空白 = 直接复制
        return

    def _ov_right(self):
        """右键：逐级退出 —— 退出模式 → 取消选中 → 删最后一笔 → 取消截图。"""
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
        return

    def _ov_escape(self):
        """Esc：同样逐级退出，与右键的顺序保持一致。"""
        if self.sel_shape is not None:
            self.sel_shape = None
            self._redraw_overlay()
            self._refresh_tool()
        elif self.mode:
            self._leave_mode()
        else:
            self._cancel_capture()
        return

    def _ov_key(self, wp):
        """其余按键：Delete 删标注、Ctrl+Z/Y 撤销重做、方向键微调。\n\n        返回 True 表示这个键已经被处理掉了，调用方不再往下传。"""
        ctrl = bool(user32.GetKeyState(VK_CONTROL) & 0x8000)
        shift = bool(user32.GetKeyState(VK_SHIFT) & 0x8000)
        if wp == 0x2E and self.sel_shape is not None:      # Delete
            if self.sel_shape in self.shapes:
                self._push_snap(self._snap())
                self.shapes.remove(self.sel_shape)
            self.sel_shape = None
            self._redraw_overlay()
            self._refresh_tool()
            return True
        if (wp == 0x59 and ctrl) or (wp == 0x5A and ctrl and shift):
            self._redo()                                    # Ctrl+Y / Ctrl+Shift+Z
            return True
        if (wp == 0x5A and ctrl) or wp == 0x08:
            self._undo()                                    # Ctrl+Z / Backspace
            return True
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
            return True
        return False

    def _cursor_for(self):
        """按当前模式返回该用的光标（画笔 / 马赛克笔头 / 箭头 / 各种缩放）"""
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
        """判断鼠标在选区哪个部位：outside / inside / 八个手柄名。"""
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
        """把选区夹在虚拟屏范围内（多屏、副屏在左侧时不能跑到屏幕外）"""
        vsw, vsh = self.vs[2], self.vs[3]
        x, y, ww, hh = sel
        x = max(0, min(x, vsw - 1))
        y = max(0, min(y, vsh - 1))
        ww = max(1, min(ww, vsw - x))
        hh = max(1, min(hh, vsh - y))
        return (x, y, ww, hh)

    def _move_sel(self, sel0, x, y):
        """按下的点和当前位置算位移，整体移动选区，标注一起跟着走"""
        dx = x - self.press_pt[0]
        dy = y - self.press_pt[1]
        x0, y0, ww, hh = sel0
        nx = max(0, min(x0 + dx, self.vs[2] - ww))
        ny = max(0, min(y0 + dy, self.vs[3] - hh))
        return (nx, ny, ww, hh)

    def _resize_sel(self, sel0, handle, x, y):
        """拖手柄改选区大小。对角固定，拖过界会自动翻转"""
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
        """把任意两个对角点规整成 (x, y, w, h)，并夹在屏幕内"""
        vsw, vsh = self.vs[2], self.vs[3]
        x1, y1 = a
        x2, y2 = b
        x1 = max(0, min(x1, vsw))
        x2 = max(0, min(x2, vsw))
        y1 = max(0, min(y1, vsh))
        y2 = max(0, min(y2, vsh))
        return (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))

    def _close_overlay(self):
        """销毁遮罩、释放缓冲，并把主窗口的内容画回来"""
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
        """取消截图：收起工具条、关遮罩、恢复主界面"""
        self._close_tool()
        self._close_overlay()

    # ---------- 标注模式与撤销栈
    def _begin_mode(self, kind):
        """进入某个标注模式（画笔 / 马赛克 / 箭头 / 方框 / 圆形）"""
        if not self.sel:
            return
        self.mode = kind
        self.sel_shape = None
        self.drag_mode = None
        self.drag_end = 0
        self.pending = None
        self.draw_start = None
        if kind in ("pen", "rect", "ellipse"):
            # 画笔 / 方框 / 圆形只有六色（红蓝绿黄白黑），箭头有七色（多一个灰）。
            # 从灰色箭头切过来时钳一下，否则会画出一个选项排里根本没有的
            # 颜色，色块全都不高亮，看着像"没选中"。
            self.anno_color = min(self.anno_color, 5)
        self._redraw_overlay()
        self._refresh_tool()        # 展开颜色/粗细等选项排

    def _leave_mode(self):
        """退出标注模式，回到纯选区状态，并收起工具条选项排"""
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
        """把标注列表拍成可比较、可回放的快照。

        只存数据（t / 坐标 / 颜色…），不存任何渲染缓存 —— 缓存现在都放在
        App 上，标注字典里已经没有任何下划线开头的键；这里跳过 `_` 开头
        只是兜底，防止以后有人往里塞临时字段。"""
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
        """把一份标注快照压进撤销栈。变化太小的不算一步（避免墨迹没干就占满栈）"""
        if snap is None:
            return
        self.undo_stack.append(snap)
        if len(self.undo_stack) > 60:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def _undo(self):
        """撤销一步（Ctrl+Z）"""
        if not self.undo_stack:
            return
        self.redo_stack.append(self._snap())
        self.shapes = self.undo_stack.pop()
        self.sel_shape = None
        self._redraw_overlay()
        self._refresh_tool()

    def _redo(self):
        """重做一步（Ctrl+Y）"""
        if not self.redo_stack:
            return
        self.undo_stack.append(self._snap())
        self.shapes = self.redo_stack.pop()
        self.sel_shape = None
        self._redraw_overlay()
        self._refresh_tool()

    def _sync_opts(self, sh):
        """把选中标注当前的样式同步到工具条（箭头 / 方框 / 圆形可选中）"""
        t = sh.get("t")
        if t in ("arrow", "rect", "ellipse"):
            self.anno_color = max(0, min(sh.get("color", 0), len(ANNO_COLORS) - 1))
        if t in ("rect", "ellipse"):
            # 线宽是像素值，映射回「细 / 中 / 粗」里最接近的一档，工具条才不会两个都不亮
            w = float(sh.get("w") or self._pen_w())
            self.pen_width = min(range(len(PEN_WIDTHS)),
                                 key=lambda i: abs(self.px(PEN_WIDTHS[i]) - w))

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
        if sh.get("t") in ("rect", "ellipse"):
            # 方框 / 圆形还能改线宽
            sh["color"] = self.anno_color
            sh["w"] = self._pen_w()
            if self._snap() != pre:
                self._push_snap(pre)
            self._redraw_overlay()
            return
        self._redraw_overlay()

    def _update_pending(self, x, y):
        """拖动中实时更新临时标注。松手时才把 pending 真正落到 shapes 里"""
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
            return
        if self.mode in ("rect", "ellipse"):
            if self.pending is None:
                self.pending = {"t": self.mode, "x1": x0, "y1": y0, "x2": x, "y2": y,
                                "color": self._color_idx(), "w": self._pen_w()}
            else:
                self.pending["x2"], self.pending["y2"] = x, y

    def _clamp_to_sel(self, x, y):
        """把坐标夹进选区：标注不许画到选区外面"""
        if not self.sel:
            return x, y
        sx, sy, sw_, sh_ = self.sel
        return (max(sx + 2, min(x, sx + sw_ - 2)),
                max(sy + 2, min(y, sy + sh_ - 2)))

    # ================================================== 7. 浮动工具条 —— 版式 / 毛玻璃 / 绘制 / 点击
    def _opt_visible(self):
        """是否需要显示选项排（第二排：颜色 / 粗细 / 笔头 / 块大小）"""
        return (self.mode in ("pen", "arrow", "mosaic", "rect", "ellipse")
                or self._arrow_opts())

    def _pen_opts(self):
        """颜色 + 粗细两排：画笔、方框、圆形共用同一组选项"""
        return self.mode in ("pen", "rect", "ellipse")

    def _arrow_opts(self):
        """选中箭头时只露出颜色点（箭头可改颜色）"""
        sh = self.sel_shape
        return bool(sh) and sh.get("t") == "arrow"

    def _color_opts(self):
        """颜色点属于画笔 / 箭头"""
        return self._pen_opts() or self._arrow_opts() or self.mode == "arrow"

    def _mosaic_opts(self):
        """马赛克模式特有的两排选项：笔头 + 块大小"""
        return self.mode == "mosaic"

    def _tool_layout(self):
        """返回 (组宽, 组高, 矩形列表)。按钮彼此独立，之间留空隙。
        矩形第 5 项之后是“选项排”（颜色 / 粗细等），对应模式出现。"""
        bw, bh = self.tb(TB_BTN_W), self.tb(TB_BTN_H)
        gap = self.tb(TB_BTN_GAP)
        n = len(TB_ITEMS)
        ww = bw * n + gap * (n - 1)
        rects = [("@btn", i * (bw + gap), 0, bw, bh) for i in range(n)]
        hh = bh
        if self._opt_visible():
            oy = bh + self.tb(8)
            oh = self.tb(40)
            cx = self.tb(8)
            if self._color_opts():
                d = self.tb(26)
                ncolors = 6 if self._pen_opts() else len(ANNO_COLORS)
                for i in range(ncolors):
                    rects.append(("@color%d" % i, cx, oy + (oh - d) / 2.0, d, d))
                    cx += d + self.tb(7)
                cx += self.tb(10)
            if self._pen_opts():
                lw = self.tb(46)
                cw = self.tb(44)
                rects.append(("@label_p", cx, oy, lw, oh))
                cx += lw + self.tb(2)
                for i in range(len(PEN_WIDTHS)):
                    rects.append(("@penw%d" % i, cx, oy, cw, oh))
                    cx += cw + self.tb(5)
            if self._mosaic_opts():
                lw = self.tb(46)
                cw = self.tb(44)
                rects.append(("@label_b", cx, oy, lw, oh))
                cx += lw + self.tb(2)
                for i in range(len(MOSAIC_BRUSHES)):
                    rects.append(("@brush%d" % i, cx, oy, cw, oh))
                    cx += cw + self.tb(5)
                cx += self.tb(16)
                rects.append(("@label_k", cx, oy, lw, oh))
                cx += lw + self.tb(2)
                for i in range(len(MOSAIC_BLOCKS)):
                    rects.append(("@block%d" % i, cx, oy, cw, oh))
                    cx += cw + self.tb(5)
            hh = oy + oh
        return ww, hh, rects

    def _show_tool(self):
        """按选区位置把工具条摆好并显示。

        优先浮在选区下方、水平居中；贴到屏幕边缘时自动往里收，不会跑出屏幕。"""
        if not self.sel:
            self._close_tool()
            return
        ww, hh, rects = self._tool_layout()
        vsx, vsy, vsw, vsh = self.vs
        sx, sy, sw_, sh_ = self.sel
        gap = self.tb(10)

        # 水平居中于选区，浮在选区下方
        px_ = vsx + sx + int(round((sw_ - ww) / 2.0))
        py_ = vsy + sy + sh_ + gap
        if py_ + hh > vsy + vsh - self.tb(6):
            py_ = vsy + sy - hh - gap          # 下方放不下就翻到上方
        if py_ < vsy + self.tb(6):
            py_ = min(vsy + vsh - hh - self.tb(6), vsy + sy + sh_ + gap)
        px_ = max(vsx + self.tb(6), min(px_, vsx + vsw - ww - self.tb(6)))
        py_ = max(vsy + self.tb(6), min(py_, vsy + vsh - hh - self.tb(6)))
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
        """关掉并销毁工具条窗口"""
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
        """确保工具条的绘制表面、蒙版与当前布局一致。

        布局签名变了就整套重建 —— 只看宽高是不够的，见 _tool_layout_key。"""
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
        """释放工具条表面，并作废与布局绑定的玻璃底缓存"""
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
        pad = int(self.tb(6))
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
            with GFX(d[0]) as g:
                for (name, x, y, ww, hh) in self._tool_layout()[2]:
                    if name != "@btn":
                        continue
                    g.fill_round(0xFFFFFFFF, x, y, ww, hh, self.tb(12))
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

        with GFX(self.tb_dc) as g:
            self._draw_tool(g, W, H)

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
        """响应工具条的 WM_PAINT：呈现全靠 ULW，这里只清掉重绘请求"""
        ps = PAINTSTRUCT()
        user32.BeginPaint(hwnd, byref(ps))
        user32.EndPaint(hwnd, byref(ps))

    def _draw_tool(self, g, W, H):
        """画工具条：十颗按钮 + 选项排（颜色 / 粗细 / 笔头 / 块大小）"""
        _, _, rects = self._tool_layout()
        rad = self.tb(12)
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

            # 居中算法：先把「图标墨迹 + 缝 + 文字墨迹」当成一块，让它左右
            # 留白相等；再把图标按它自己的墨迹边界摆进去。
            # 直接用固定坐标或按方框算，两字标签右边会空出十几像素（看着就是
            # 没居中），「取消」的 × 又会因为墨迹小 4px 而偏右。
            isz = self.tb(TB_ICON_SIZE)
            fs = self.tb_font(12)
            s = isz / float(TB_ICON_SIZE)          # 表按 17px 量的，按实际缩放
            il, it, ir, ib = TB_ICON_INK[kind]
            iw = (ir - il + 1) * s                 # 图标墨迹宽
            ih = (ib - it + 1) * s                 # 图标墨迹高
            adv, _ = g.measure(label, fs)
            sl = self.tb(TB_TEXT_SIDE_L)
            sr = self.tb(TB_TEXT_SIDE_R)
            ink_w = adv - sl - sr                  # 文字墨迹宽
            gap = self.tb(TB_ICON_GAP)
            bx = x + (ww - (iw + gap + ink_w)) / 2.0     # 图标墨迹左端
            tx = bx + iw + gap - sl                      # 文字框左边（减去左留白）
            self._draw_tool_icon(g, kind, bx - il * s,
                                 y + (hh - ih) / 2.0 - it * s, isz, ic, bg)
            # 文字框右边界一直给到按钮右边缘（不是正好 ink_w）：DrawString 的
            # 矩形差一点点就会把「马赛克」折成两行，多留空间最稳。
            g.text(label, fs, tc, tx, y + self.tb(TB_TEXT_DY),
                   x + ww - tx, hh, 0, hot)

        # 选项排：颜色  ·  画笔粗细 / 马赛克笔头 / 块大小
        # 这里的文字都要加 TB_TEXT_DY 补偿，原因见常量区注释（GDI+ 行内居中
        # 会让中文字形整体偏上，和旁边按钮里的文字不在一条线上）。
        if not self._opt_visible():
            return
        tdy = self.tb(TB_TEXT_DY)
        for i in range(nb, len(rects)):
            name, x, y, ww, hh = rects[i]
            if name in ("@label_b", "@label_k", "@label_p"):
                txt = {"@label_b": "笔头", "@label_k": "块大小", "@label_p": "粗细"}[name]
                g.text(txt, self.tb_font(11.5), C_TB_TXT, x, y + tdy, ww, hh, 1)
            elif name.startswith("@penw"):
                k = int(name[5:])
                on = (self.pen_width == k)
                g.fill_round(C_ACCENT if on else C_TB_BTN, x, y, ww, hh, self.tb(9))
                if not on:
                    g.stroke_round(0x1FFFFFFF, 1, x + 0.5, y + 0.5,
                                   ww - 1, hh - 1, self.tb(9))
                g.text(PEN_WIDTH_NAMES[k], self.tb_font(13),
                       C_WHITE if on else C_TB_TXT, x, y + tdy, ww, hh, 1, on)
            elif name.startswith("@brush") or name.startswith("@block"):
                is_brush = name.startswith("@brush")
                k = int(name[6:])
                on = (self.mosaic_brush == k) if is_brush else (self.mosaic_block == k)
                g.fill_round(C_ACCENT if on else C_TB_BTN, x, y, ww, hh, self.tb(9))
                if not on:
                    g.stroke_round(0x1FFFFFFF, 1, x + 0.5, y + 0.5,
                                   ww - 1, hh - 1, self.tb(9))
                txt = MOSAIC_BRUSH_NAMES[k] if is_brush else MOSAIC_BLOCK_NAMES[k]
                g.text(txt, self.tb_font(13), C_WHITE if on else C_TB_TXT,
                       x, y + tdy, ww, hh, 1, on)
            else:
                k = int(name[6:])
                c = ANNO_COLORS[k]
                on = (self.anno_color == k)
                if on:
                    m = self.tb(3)
                    g.fill_round(0xFFFFFFFF, x - m, y - m, ww + m * 2, hh + m * 2,
                                 (ww + m * 2) / 2.0)
                g.fill_round(0xFF000000 | (c & 0xFFFFFF), x, y, ww, hh, ww / 2.0)
                g.stroke_round(0x40FFFFFF, 1, x + 0.5, y + 0.5,
                               ww - 1, hh - 1, ww / 2.0)

    def _draw_tool_icon(self, g, kind, x, y, sz, color, bg=None):
        """15×15 单位网格上的线条图标，按 sz 缩放"""
        lw = max(1.15, self.tb(1.35))
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
        elif kind == "rect":
            # 方框：四角闭合的细框（圆头线段，四角不留缺口）
            lw2 = max(1.15, self.tb(1.35))

            def LR(x1, y1, x2, y2):
                g.line(color, lw2, x + x1 * u, y + y1 * u,
                       x + x2 * u, y + y2 * u, 2)

            LR(1.9, 3.4, 13.1, 3.4)
            LR(13.1, 3.4, 13.1, 11.6)
            LR(13.1, 11.6, 1.9, 11.6)
            LR(1.9, 11.6, 1.9, 3.4)
        elif kind == "ellipse":
            # 圆形：空心椭圆，跟实际画出来的一致
            g.stroke_ellipse(color, max(1.15, self.tb(1.35)),
                             x + 1.7 * u, y + 3.4 * u, 11.6 * u, 8.2 * u)
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
        """工具条窗口过程：悬停高亮、点击派发、鼠标移出时的清理"""
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
                elif kind in ANNO_KINDS:
                    # 再点一次同一个工具 = 退出该模式。这里必须穷举 TB_ITEMS
                    # 里所有"绘图类"工具：漏掉哪个，点它就会掉到下面的分支，
                    # 而兜底是取消截图（v1.3.0 就漏了 rect/ellipse，点方框直接
                    # 退出截图）。新增工具时同步 ANNO_KINDS，别只改 TB_ITEMS。
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
                elif kind == "cancel":
                    self._cancel_capture()
                # 其余未知 kind 一律不响应：宁可点了没反应，也不能误取消截图
                return 0
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    # ================================================== 8. 成品输出与系统集成
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
            with GFX(dc) as g:
                self._draw_shapes(g, -x, -y, False)   # 不把选框虚线烤进成品
        raw = ctypes.string_at(bits, ww * hh * 4)
        gdi32.SelectObject(dc, old)
        gdi32.DeleteDC(dc)
        gdi32.DeleteObject(bmp)
        # 必须返回 ctypes 缓冲（持有内存），GDI+ 位图只是引用这块内存
        return ctypes.create_string_buffer(raw, len(raw)), ww, hh

    def _action_copy(self):
        """复制成品到剪贴板（CF_DIB 格式，能直接粘进微信、Word 等）"""
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
        """另存为 PNG"""
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
        """默认保存目录：用户目录下的「图片」文件夹，没有就退到桌面"""
        d = os.path.join(os.path.expanduser("~"), "Pictures")
        if not os.path.isdir(d):
            d = os.path.expanduser("~")
        return d

    def _save_dialog(self, owner, folder, default):
        """弹系统「另存为」对话框，返回用户选的完整路径（取消返回 None）"""
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

    # ---------- 托盘 / 热键 / 开机自启
    def _make_icon(self):
        """画托盘图标：圆角方块 + 白色取景框，32×32 带 alpha 通道"""
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
        """把图标放进系统托盘（右键菜单、悬停提示都靠它）"""
        self.hicon = self._make_icon()
        self._add_tray_icon()

    def _add_tray_icon(self):
        """往任务栏挂一次图标。

        单独拆出来是因为 explorer 重启后要再挂一次：图标（HICON）在进程里一直
        活着，但任务栏那一格是 explorer 自己的内存，它一重启就全没了。只重建
        图标（create_tray）会把 HICON 漏掉一个，所以重挂走这里。"""
        nid = self.tray
        nid.cbSize = sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd_main
        nid.uID = 1
        nid.uFlags = 0x1 | 0x2 | 0x4      # MESSAGE | ICON | TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.hicon
        nid.szTip = APP_TITLE + " · 截图  " + self._hotkey_label()
        shell32.Shell_NotifyIconW(0, byref(nid))

    def _recreate_tray(self):
        """explorer 重启了：把托盘图标重新挂上去。

        不重挂的后果是图标永久消失 —— 程序还在跑、热键还能用，但用户从任务栏
        找不到入口，只能去任务管理器结束它。"""
        try:
            if not self.hicon:
                self.hicon = self._make_icon()
            self._add_tray_icon()
            dbg("taskbar recreated, tray icon re-added")
        except Exception as e:
            dbg("re-add tray icon failed: %r" % (e,))

    def _update_tray_tip(self):
        """刷新托盘的悬停提示，把当前快捷键显示出来"""
        nid = NOTIFYICONDATAW()
        nid.cbSize = sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd_main
        nid.uID = 1
        nid.uFlags = 0x4                  # NIF_TIP
        nid.szTip = APP_TITLE + " · 截图  " + self._hotkey_label()
        shell32.Shell_NotifyIconW(1, byref(nid))   # NIM_MODIFY

    def _balloon(self, title, text):
        """弹一个托盘气泡通知（改快捷键成功、出错提示都用它）"""
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
        """托盘图标消息：右键弹菜单，左键单击直接开始截图"""
        if lp == WM_RBUTTONUP or lp == WM_RBUTTONDOWN:
            if lp == WM_RBUTTONUP:
                self._tray_menu()
        elif lp == WM_LBUTTONUP:
            self._show_panel()
        elif lp == WM_LBUTTONDBLCLK:
            self.start_capture()

    def _tray_menu(self):
        """弹托盘右键菜单：改快捷键 / 开机启动 / 退出"""
        cmd = self._menu_popup(True)
        if cmd:
            self._menu_command(cmd)

    def _panel_menu(self, lp):
        """主面板上右键：**只有点在「SnapLite」logo 上**才弹菜单。

        别的地方（空白、按钮、热键条）一律不响应 —— 整块面板都能弹的话，想拖
        一下窗口或者随手误触右键就会蹦出菜单。

        菜单跟托盘同一份，只是不挂「显示主界面」：面板本来就在眼前，那条点了
        也只会把焦点挪一下，留着反而让人以为面板被藏起来了。Command ID 是固定
        编号，少挂一条不会让下面的错位。

        弹之前先停活跃线程、弹完标脏：菜单是弹在鼠标位置的系统窗口，正好压在
        面板上，菜单期间抓屏会把菜单自己拍进玻璃底，菜单一关就露出半截影子。"""
        x, y = lo(lp), hi(lp)
        if not self._in_title(x, y):
            return
        if self.main_drag:
            # 左键按住拖窗口时又点了右键：这时候弹菜单，拖动循环会卡在中间
            return
        if self.recording:
            self._end_record()          # 录制中右键 = 放弃这次录制，然后照常弹
        self._stop_glass_live()
        self.menu_open = True
        if self.hover_main is not None:
            # 菜单弹出来后鼠标可能已经不在面板上了，高亮会一直挂着
            self.hover_main = None
            user32.InvalidateRect(self.hwnd_main, None, False)
        try:
            cmd = self._menu_popup(False)
        finally:
            self.menu_open = False
            self.glass_dirty = True     # 菜单盖了这一阵，桌面可能早变了
        if cmd:
            self._menu_command(cmd)

    def _menu_popup(self, include_show):
        """建并跟踪右键菜单，返回用户点了哪一项（0 = 点别处 / 没选）。

        托盘图标和主面板共用这一份，两处唯一的差别就是要不要「显示主界面」。"""
        pt = w.POINT()
        user32.GetCursorPos(byref(pt))
        hm = user32.CreatePopupMenu()
        user32.AppendMenuW(hm, 0x0, 1, "截图\t" + self._hotkey_label())
        if include_show:
            user32.AppendMenuW(hm, 0x0, 2, "显示主界面")
        user32.AppendMenuW(hm, 0x800, 0, None)
        user32.AppendMenuW(hm, 0x0, 5, "更改快捷键…")
        checked = self.autostart_enabled()
        user32.AppendMenuW(hm, 0x8 if checked else 0x0, 4, "开机自动启动")
        user32.AppendMenuW(hm, 0x800, 0, None)
        user32.AppendMenuW(hm, 0x0, 3, "退出 SnapLite")
        # 不先抢前台的话，点菜单外面它不会自己收起来（TrackPopupMenu 的老规矩）
        user32.SetForegroundWindow(self.hwnd_main)
        try:
            return user32.TrackPopupMenu(hm, 0x0100 | 0x0080 | 0x0004 | 0x0008,
                                         pt.x, pt.y, 0, self.hwnd_main, None)
        finally:
            # 放在 finally 里：万一上面抛异常，菜单句柄也不能漏（每弹一次漏一个
            # HMENU，攒久了 GDI 句柄会耗尽）。PostMessage(WM_NULL) 是老规矩，
            # 让菜单关掉之后键盘焦点能正常回到自己的窗口。
            user32.PostMessageW(self.hwnd_main, WM_NULL, 0, 0)
            user32.DestroyMenu(hm)

    def _menu_command(self, cmd):
        """菜单选中之后的动作，托盘和主面板走的都是这一份"""
        if cmd == 1:
            self.start_capture()
        elif cmd == 2:
            self._show_panel()
        elif cmd == 3:
            self.quit()
        elif cmd == 4:
            self.set_autostart(not self.autostart_enabled())
        elif cmd == 5:
            self._show_panel()
            self._begin_record()

    # ---------- 开机自启
    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    RUN_NAME = "SnapLite"

    def _launch_command(self):
        """取「怎么启动自己」的命令行。

        打包后是 exe 路径，开发时是 python + 脚本路径 —— 开机启动要写进注册表。"""
        if getattr(sys, "frozen", False):
            return '"%s"' % sys.executable
        return '"%s" "%s"' % (sys.executable, os.path.abspath(__file__))

    def autostart_enabled(self):
        """是否已设成开机启动（读注册表 Run 键）"""
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.RUN_KEY) as k:
                v, _ = winreg.QueryValueEx(k, self.RUN_NAME)
                return str(v) == self._launch_command()
        except FileNotFoundError:
            return False          # 键或值不存在 = 没开
        except Exception:
            return False

    def set_autostart(self, on):
        """开 / 关开机启动。

        用 CreateKey 而不是 OpenKey：全新系统上 Run 键可能还不存在，
        OpenKey 会直接失败，用户就永远设不上开机启动。"""
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
        """退出：注销热键、移除托盘图标、卸钩子、释放全部缓冲"""
        # 先把活跃档线程收掉，否则它可能正在用马上要释放的缓冲和窗口
        try:
            self._stop_glass_live()
        except Exception:
            pass
        try:
            self._stop_drag_prefetch()
        except Exception:
            pass
        # 不管属性现在挂在谁名下，退出前一律清掉
        self._excl_reset()
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

    # ---------- 主循环
    def run(self):
        """程序主循环：建窗口 → 装托盘 → 注册热键 → 跑消息循环"""
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
    """程序入口。单实例检查：已经有一个在跑就直接把它的窗口叫到前面。

    检查分两道：先抢命名互斥（原子，没有竞态），再找窗口（兼容没有互斥的旧版本）。
    只靠找窗口是不够的 —— onefile 启动要先解压 2~3 秒，这段时间窗口根本不存在，
    两次快速双击都能通过检查，结果就是两个面板叠在一起互相抓屏。"""
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
        # 句柄故意不关：它活到进程退出，互斥才一直占着
        global _instance_mutex
        _instance_mutex = windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if _instance_mutex and windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            dbg("another instance holds the mutex")
            prev = user32.FindWindowW("SnapLiteMain", APP_NAME)
            if prev:
                user32.ShowWindow(prev, SW_SHOW)
                user32.SetForegroundWindow(prev)
            return
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
