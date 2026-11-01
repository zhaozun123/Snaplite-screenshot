# -*- coding: utf-8 -*-
"""跑一遍所有 tests/ 和关键 tools/，统计 snap.py 里哪些函数真的被执行过。

比“源码里出现过这个名字”靠谱得多：直接看运行时进没进过这个函数。

用法：python tools/coverage_review.py [--list]   加 --list 打印全部未覆盖函数
"""
import ast
import io
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.path.join(ROOT, "snap.py")
RUNNER = os.path.join(ROOT, "tools", "_trace_runner.py")
LIST = "--list" in sys.argv

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 要跑的脚本：全部测试 + 几个覆盖主流程的验证工具
TARGETS = [
    "tests/test_shapes.py",
    "tests/test_overlay_msgs.py",
    "tests/test_pen.py",
    "tests/test_pen_visual.py",
    "tests/test_tb_center.py",
    "tests/test_anno_render.py",
    "tests/test_main_panel.py",
    "tools/verify_tool_opts.py",
    "tools/verify_tool_final.py",
    "tools/verify_tool_glass.py",
    "tools/verify_capture_cycles.py",
    "tools/verify_glass_follow.py",
]

# 解析 snap.py：行号 → (类.方法 或 顶层函数)
tree = ast.parse(io.open(SNAP, encoding="utf-8").read())
funcs = {}          # firstlineno -> 名字
order = []          # 保持文件顺序，便于打印
for node in tree.body:
    if isinstance(node, ast.FunctionDef):
        funcs[node.lineno] = node.name
        order.append(node.name)
    elif isinstance(node, ast.ClassDef):
        for sub in node.body:
            if isinstance(sub, ast.FunctionDef):
                funcs[sub.lineno] = "%s.%s" % (node.name, sub.name)
                order.append("%s.%s" % (node.name, sub.name))

hit = set()
print("跑 %d 个脚本…" % len(TARGETS))
for t in TARGETS:
    path = os.path.join(ROOT, t)
    if not os.path.exists(path):
        print("  跳过（不存在） %s" % t)
        continue
    fd, out = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    r = subprocess.run([sys.executable, RUNNER, path, out],
                       capture_output=True, text=True, timeout=300)
    try:
        got = json.loads(io.open(out, encoding="utf-8").read() or "[]")
    except Exception:
        got = []
    os.unlink(out)
    hit |= {(ln, name) for ln, name in got}
    print("  %-34s exit=%d  进入 %d 个函数" % (t, r.returncode, len(got)))
    if r.returncode != 0:
        tail = (r.stdout or "")[-400:] + (r.stderr or "")[-400:]
        print("    ↑ 非 0 退出，末尾输出：\n%s" % tail)

covered = set()
for ln, name in hit:
    covered.add(funcs.get(ln, name))

print()
print("=" * 70)
print("函数覆盖率（snap.py）")
print("=" * 70)
total = len(order)
done = [n for n in order if n in covered]
miss = [n for n in order if n not in covered]
print("  共 %d 个函数/方法，被执行过 %d 个 = %.1f%%" % (total, len(done), 100.0 * len(done) / total))

# 按「段」聚一下未覆盖的，看是哪一块没测
SEG = {
    "热键": ("_try_hotkey", "_register_hotkey", "_on_record_key", "_begin_record", "_end_record"),
    "托盘": ("_tray_menu", "_on_tray_msg", "create_tray", "_update_tray_tip"),
    "保存": ("_action_save", "_save_dialog", "_pictures_dir"),
    "主界面拖动": ("_start_main_drag", "_move_main_drag", "_end_main_drag", "_drag_glass_bytes"),
    "桌面监听": ("_watch_desktop",),
    "截图期主界面": ("_keep_main_surface", "_blend_main_into_shot"),
}
print()
for name, ms in SEG.items():
    miss_n = [m for m in miss if any(m.endswith(x) for x in ms)]
    done_n = [m for m in done if any(m.endswith(x) for x in ms)]
    if miss_n or done_n:
        print("  %-10s 覆盖 %d / %d" % (name, len(done_n), len(done_n) + len(miss_n)))

if LIST:
    print()
    print("未被执行过的函数（%d）:" % len(miss))
    for m in miss:
        print("     %s" % m)
