# -*- coding: utf-8 -*-
"""轻量覆盖率：只统计「哪个函数被进入过」，不依赖 coverage 包。

原理：sys.settrace 的 call 事件能拿到被调函数的 co_firstlineno，
再和 ast 解析出的「函数名 → 起始行」对上号。

用法（一般由 tools/coverage_review.py 调用，不直接跑）：
    python tools/_trace_runner.py <要跑起来的脚本> <结果 json 路径>
"""
import io
import json
import os
import runpy
import sys

TARGET = sys.argv[1]
OUT = sys.argv[2]
SNAP = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "snap.py"))

hit = set()


def tracer(frame, event, arg):
    if event == "call":
        if os.path.abspath(frame.f_code.co_filename) == SNAP:
            hit.add((frame.f_code.co_firstlineno, frame.f_code.co_name))
        return None          # 不需要 line 事件，快很多
    return None


sys.path.insert(0, os.path.dirname(SNAP))
sys.settrace(tracer)
code = 0
try:
    runpy.run_path(TARGET, run_name="__main__")
except SystemExit as e:
    code = int(e.code or 0)
finally:
    sys.settrace(None)
    io.open(OUT, "w", encoding="utf-8").write(json.dumps(sorted(hit)))
sys.exit(code)
