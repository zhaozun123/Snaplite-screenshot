"""录制 Win 组合时，按键到底有没有到窗口？

两种可能必须分开：
  A. 按键到了窗口，但 RegisterHotKey 被系统占着 → 界面提示「该组合已被占用」
  B. 按下 Win 的瞬间窗口就失焦了，后续字母键根本没收到 → 界面毫无反应

用 SendInput 注入真实按键序列（Win↓ 键↓ 键↑ Win↑），分别打一个「能注册」的
组合和一个「已被占用」的组合，看 app 的状态就能区分。

用法：python -u tools/audit_hotkey_win_input.py
"""
import ctypes
import os
import sys
import time
from ctypes import c_void_p, byref, wintypes

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import snap
from snap import user32, w

try:
    snap.SetProcessDpiAwarenessContext(c_void_p(-4))
except Exception:
    pass

u = ctypes.windll.user32
ULONG_PTR = ctypes.c_size_t


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


KEYEVENTF_KEYUP = 0x0002
VK_LWIN, VK_RWIN = 0x5B, 0x5C


def key(vk, up=False):
    inp = INPUT(type=1)
    inp.ki = KEYBDINPUT(wVk=vk, wScan=0,
                        dwFlags=KEYEVENTF_KEYUP if up else 0, time=0, dwExtraInfo=0)
    u.SendInput(1, byref(inp), ctypes.sizeof(INPUT))


def combo(mod_vk, key_vk):
    key(mod_vk)
    time.sleep(0.05)
    key(key_vk)
    time.sleep(0.05)
    key(key_vk, up=True)
    time.sleep(0.05)
    key(mod_vk, up=True)


def release_all():
    """万一中途卡住：把所有可能按下的键都抬一遍"""
    for vk in (VK_LWIN, VK_RWIN, 0x10, 0x11, 0x12):
        key(vk, up=True)


app = snap.App()
app.create_main()
hwnd = app.hwnd_main
user32.SetForegroundWindow(hwnd)
user32.SetFocus(hwnd)
time.sleep(0.4)


def run(name, mod, key_vk, note):
    print("\n--- %s（%s）---" % (name, note))
    app._end_record()
    app._begin_record()
    print("  录制已开始，前台窗口=%s" % (u.GetForegroundWindow() == hwnd))
    time.sleep(0.15)
    combo(mod, key_vk)
    time.sleep(0.35)
    print("  还在录制=%s  提示=%r  错误=%s  成功=%s 快捷键=%s"
          % (app.recording, app.rec_msg, app.rec_err,
             not app.recording and not app.rec_err, app._hotkey_label()))
    if app.recording:
        app._end_record()


# 先记下原来的，最后还回去
orig_mod, orig_vk = app.hotkey_mod, app.hotkey_vk

run("Win + F2", VK_LWIN, 0x71, "这个组合能抢到")
run("Win + K", VK_LWIN, ord("K"), "这个被系统占着")
run("Win + Y", VK_LWIN, ord("Y"), "Win+字母里少数能抢到的")
run("Win + S", VK_LWIN, ord("S"), "被系统截图占用，必失败")

release_all()
time.sleep(0.2)

# 恢复原热键
app.hotkey_mod, app.hotkey_vk = orig_mod, orig_vk
app._register_hotkey()
print("\n已恢复原热键：%s" % app._hotkey_label())
user32.DestroyWindow(hwnd)
