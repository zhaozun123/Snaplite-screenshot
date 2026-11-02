"""验证：低级键盘钩子（WH_KEYBOARD_LL）能不能收到 Win 组合。

录制器现在靠窗口消息（WM_KEYDOWN），按下 Win 的瞬间输入就被 shell 接管了，
窗口一个键都收不到 —— 这正是「Win 开头的快捷键改不了」的第一个原因。

这个脚本装一个真正的 WH_KEYBOARD_LL，注入 Win+K / Win+F2，看回调里收到什么。
能收到，就说明「录制改用钩子」这条路可行。

用法：python -u tools/audit_ll_hook_win.py
"""
import ctypes
import sys
import time
from ctypes import c_void_p, byref, wintypes, WINFUNCTYPE, POINTER, cast

sys.stdout.reconfigure(line_buffering=True)
ctypes.windll.user32.SetProcessDpiAwarenessContext(c_void_p(-4))
u = ctypes.windll.user32
k32 = ctypes.windll.kernel32

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
ULONG_PTR = ctypes.c_size_t
VK_LWIN, VK_RWIN = 0x5B, 0x5C


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


HOOKPROC = WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)

events = []
hook = None
_keep = []          # 回调必须一直活着，否则被 GC 回收后系统回调就是野指针


def _fmt(vk, down):
    name = {0x5B: "LWIN", 0x5C: "RWIN", 0x10: "SHIFT", 0x11: "CTRL",
            0x12: "ALT", 0x41: "A", 0x4B: "K", 0x71: "F2", 0x59: "Y"}.get(vk)
    return "%s%s" % (name or ("vk%02X" % vk), "↓" if down else "↑")


def proc(nCode, wParam, lParam):
    if nCode == 0:
        kb = cast(lParam, POINTER(KBDLLHOOKSTRUCT)).contents
        if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYUP, WM_SYSKEYUP):
            events.append((kb.vkCode, wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)))
    return u.CallNextHookEx(None, nCode, wParam, lParam)


cb = HOOKPROC(proc)
_keep.append(cb)

# 必须显式声明原型：不声明时 ctypes 按 32 位 int 传 hMod，句柄被截断，
# SetWindowsHookEx 会返回 126（ERROR_MOD_NOT_FOUND），看着像「系统不支持钩子」
u.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, c_void_p, wintypes.DWORD]
u.SetWindowsHookExW.restype = c_void_p
u.UnhookWindowsHookEx.argtypes = [c_void_p]
u.UnhookWindowsHookEx.restype = ctypes.c_bool
u.CallNextHookEx.argtypes = [c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
u.CallNextHookEx.restype = ctypes.c_ssize_t
k32.GetModuleHandleW.restype = c_void_p
k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

hook = u.SetWindowsHookExW(WH_KEYBOARD_LL, cb, k32.GetModuleHandleW(None), 0)
print("钩子安装：%s" % ("成功" if hook else "失败，错误码 %d" % k32.GetLastError()))


def key(vk, up=False):
    inp = INPUT(type=1)
    inp.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=0x0002 if up else 0,
                        time=0, dwExtraInfo=0)
    u.SendInput(1, byref(inp), ctypes.sizeof(INPUT))


def pump(seconds):
    """跑消息循环：低级钩子只在有消息循环的线程上回调"""
    msg = wintypes.MSG()
    end = time.time() + seconds
    while time.time() < end:
        while u.PeekMessageW(byref(msg), None, 0, 0, 1):
            u.TranslateMessage(byref(msg))
            u.DispatchMessageW(byref(msg))
        time.sleep(0.005)


def esc_win_key():
    """把可能卡住的 Win 键抬起来，别把开始菜单留在那儿"""
    for vk in (VK_LWIN, VK_RWIN):
        key(vk, up=True)


pump(0.1)
for label, vk in (("Win+K", 0x4B), ("Win+F2", 0x71), ("Win+Y", 0x59)):
    events.clear()
    key(VK_LWIN)
    pump(0.05)
    key(vk)
    pump(0.05)
    key(vk, up=True)
    pump(0.05)
    key(VK_LWIN, up=True)
    pump(0.15)
    print("%-8s 钩子收到：%s" % (label, " ".join(_fmt(v, d) for v, d in events) or "（无）"))

esc_win_key()
pump(0.1)
if hook:
    u.UnhookWindowsHookEx(hook)
    print("钩子已卸载")
