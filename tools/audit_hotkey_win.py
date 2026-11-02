"""探测哪些「Win + 键」组合还能被 RegisterHotKey 抢到。

用来回答「Win 开头的快捷键为什么改不了」：如果某个组合被 explorer / 系统组件
注册过，RegisterHotKey 会返回 FALSE，程序这边只会显示「该组合已被占用」。

用法：python -u tools/audit_hotkey_win.py
"""
import ctypes
import sys
from ctypes import c_void_p

ctypes.windll.user32.SetProcessDpiAwarenessContext(c_void_p(-4))
u = ctypes.windll.user32
u.RegisterHotKey.argtypes = [c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
u.RegisterHotKey.restype = ctypes.c_bool
u.UnregisterHotKey.argtypes = [c_void_p, ctypes.c_int]
u.UnregisterHotKey.restype = ctypes.c_bool

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8
HOTKEY_ID = 0x5A11

NAMES = {}
for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    NAMES[ord(ch)] = ch
for d in "0123456789":
    NAMES[ord(d)] = d
for i in range(1, 13):
    NAMES[0x6F + i] = "F%d" % i
NAMES.update({0x20: "Space", 0x09: "Tab", 0xC0: "`", 0xBD: "-", 0xBB: "=",
              0xDB: "[", 0xDD: "]", 0xDC: "\\", 0xBA: ";", 0xDE: "'",
              0xBC: ",", 0xBE: ".", 0xBF: "/", 0x25: "Left", 0x27: "Right",
              0x26: "Up", 0x28: "Down"})

VKS = sorted(NAMES)

COMBOS = [("Win", MOD_WIN),
          ("Win+Shift", MOD_WIN | MOD_SHIFT),
          ("Win+Alt", MOD_WIN | MOD_ALT),
          ("Win+Ctrl", MOD_WIN | MOD_CONTROL)]


def probe(mod, vk):
    u.UnregisterHotKey(None, HOTKEY_ID)
    ok = bool(u.RegisterHotKey(None, HOTKEY_ID, mod, vk))
    u.UnregisterHotKey(None, HOTKEY_ID)
    return ok


print("横轴=键，纵轴=修饰键组合；● = 能注册，· = 已被占用\n")
for label, mod in COMBOS:
    free, busy = [], []
    for vk in VKS:
        (free if probe(mod, vk) else busy).append(NAMES[vk])
    print("%-10s 可注册 %3d 个：%s" % (label, len(free), " ".join(free)))
    print("%-10s 被占用 %3d 个：%s" % ("", len(busy), " ".join(busy)))
    print()

# 顺带确认「不加修饰键」和「Win 单独」这类系统不让注册的形状
print("Win 本身能否当主键：", probe(MOD_WIN, 0x5B))
print("Win+Shift+S（系统截图）：", probe(MOD_WIN | MOD_SHIFT, ord("S")))
print("Win+A：", probe(MOD_WIN, ord("A")))
print("Win+F1：", probe(MOD_WIN, 0x70))
print("Win+Ctrl+F12：", probe(MOD_WIN | MOD_CONTROL, 0x7B))
print("Ctrl+Alt+S（现有默认）：", probe(MOD_CONTROL | MOD_ALT, ord("S")))
