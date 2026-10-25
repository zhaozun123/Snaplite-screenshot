# -*- coding: utf-8 -*-
"""未定义名检查（symtable 版，精确）：抓出所有 NameError 风险点"""
import symtable, builtins, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

src = open(os.path.join(ROOT, 'snap.py'), encoding='utf-8').read()
st = symtable.symtable(src, 'snap.py', 'exec')
BUILTINS = set(dir(builtins))

mod_names = set(s.get_name() for s in st.get_symbols())
problems = []

def walk(t, path):
    for s in t.get_symbols():
        n = s.get_name()
        if not s.is_referenced():
            continue
        if s.is_assigned() or s.is_parameter() or s.is_imported():
            continue
        if s.is_local() or s.is_free():
            continue
        # 全局名：模块表里有 → OK；否则可能是未定义
        if n not in mod_names and n not in BUILTINS:
            problems.append((t.get_lineno(), path, n))
    for c in t.get_children():
        walk(c, path + '.' + c.get_name())

walk(st, '<module>')
problems = sorted(set(problems))
if problems:
    print('发现 %d 个未定义名：' % len(problems))
    for ln, path, n in problems:
        print('  line %-6s %-28s %s' % (ln, path, n))
else:
    print('未发现未定义名 ✓')
