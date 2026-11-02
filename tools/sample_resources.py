"""采样一个正在运行的 SnapLite 进程：CPU 时间、内存、GDI/USER 句柄、线程数。

    python -u tools/sample_resources.py            # 采样 60 秒，每 10 秒一行
    python -u tools/sample_resources.py 120 5      # 120 秒，每 5 秒一行
    python -u tools/sample_resources.py 60 10 22272  # 指定 PID

看三件事：
1. **CPU 时间增量** —— 静置时应该接近 0。每 250ms 一次的空闲探测抓屏会占一点，
   但单核 1% 以内算正常；如果超过 3%，说明有东西在空转。
2. **GDI 句柄** —— 必须一条直线。涨就说明有 DIB / 画笔 / 字体漏了（GDI 上限
   每个进程 10000，漏起来几天就崩）。
3. **私有内存** —— 平稳即可，慢慢涨说明缓存无界。

用 GetGuiResources 而不是句柄总数：总数里混着文件、事件、线程句柄，涨一点看
不出是谁漏的；GDI/USER 分开看，漏在哪一侧一目了然。
"""
import ctypes
import sys
import time
from ctypes import wintypes as w

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GR_GDIOBJECTS = 0
GR_USEROBJECTS = 1


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]


k32.OpenProcess.restype = w.HANDLE
k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
k32.GetProcessTimes.argtypes = [w.HANDLE, ctypes.POINTER(w.FILETIME),
                                ctypes.POINTER(w.FILETIME),
                                ctypes.POINTER(w.FILETIME),
                                ctypes.POINTER(w.FILETIME)]
user32.GetGuiResources.argtypes = [w.HANDLE, w.DWORD]
user32.GetGuiResources.restype = w.DWORD
psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE,
                                       ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
                                       w.DWORD]


def _ft(hi, lo):
    return (hi << 32) | lo


def _cpu_ms(h):
    c, e, k, u = w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME()
    if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                               ctypes.byref(k), ctypes.byref(u)):
        return None
    return (_ft(k.dwHighDateTime, k.dwLowDateTime)
            + _ft(u.dwHighDateTime, u.dwLowDateTime)) / 10000.0


def _mem(h):
    m = PROCESS_MEMORY_COUNTERS_EX()
    m.cb = ctypes.sizeof(m)
    if not psapi.GetProcessMemoryInfo(h, ctypes.byref(m), m.cb):
        return None
    return m.WorkingSetSize / 1048576.0, m.PrivateUsage / 1048576.0


def find_pids():
    """找出所有 SnapLite.exe（PyInstaller onefile 是两个：引导 + 真进程）"""
    out = []
    for pid in range(4, 40000):
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            continue
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = w.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                if buf.value.lower().endswith("snaplite.exe"):
                    out.append((pid, buf.value))
        finally:
            k32.CloseHandle(h)
    return out


def main():
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    step = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    if len(sys.argv) > 3:
        pids = [(int(sys.argv[3]), "")]
    else:
        pids = find_pids()
    if not pids:
        print("没找到 SnapLite.exe，先把它跑起来")
        return 1

    # 挑私有内存最大的那个：onefile 的引导进程只有几 MB
    best = None
    for pid, path in pids:
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            continue
        mm = _mem(h)
        k32.CloseHandle(h)
        if mm and (best is None or mm[1] > best[2]):
            best = (pid, path, mm[1])
    if not best:
        print("打不开进程（权限？）")
        return 1
    pid, path, _ = best
    print("进程 %d  %s" % (pid, path))

    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        print("OpenProcess 失败")
        return 1
    try:
        c0 = _cpu_ms(h)
        t0 = time.time()
        print("%6s  %10s  %6s  %5s  %5s  %7s  %7s  %5s"
              % ("秒", "CPU累计ms", "增量ms", "GDI", "USER", "工作集MB", "私有MB", "线程"))
        last_c = c0
        rows = []
        while True:
            el = time.time() - t0
            c = _cpu_ms(h)
            g = user32.GetGuiResources(h, GR_GDIOBJECTS)
            u = user32.GetGuiResources(h, GR_USEROBJECTS)
            mm = _mem(h)
            print("%6.1f  %10.0f  %6.0f  %5d  %5d  %7.1f  %7.1f"
                  % (el, c, c - last_c, g, u, mm[0], mm[1]))
            rows.append((el, c - last_c, g, u, mm[0], mm[1]))
            last_c = c
            if el >= dur:
                break
            time.sleep(min(step, max(0.2, dur - el)))

        total_ms = rows[-1][1] and (c - c0)
        span = rows[-1][0]
        print()
        print("---- %.0f 秒汇总 ----" % span)
        print("CPU 时间增量 %.0f ms  → 单核占用 %.2f%%（8 核机器上约 %.3f%% 整机）"
              % (c - c0, (c - c0) / (span * 10.0), (c - c0) / (span * 10.0) / 8))
        print("GDI 句柄 %d → %d（%+d）" % (rows[0][2], rows[-1][2], rows[-1][2] - rows[0][2]))
        print("USER 句柄 %d → %d（%+d）" % (rows[0][3], rows[-1][3], rows[-1][3] - rows[0][3]))
        print("私有内存 %.1f → %.1f MB（%+.2f）"
              % (rows[0][5], rows[-1][5], rows[-1][5] - rows[0][5]))
        return 0
    finally:
        k32.CloseHandle(h)


if __name__ == "__main__":
    sys.exit(main())
