# -*- coding: utf-8 -*-
"""静态体检：找出定义后从未被引用的顶层名 / 类方法 / 常量"""
import ast, collections, sys, re, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

src = open(os.path.join(ROOT, 'snap.py'), encoding='utf-8').read()
tree = ast.parse(src)

# 统计所有 Name/Attribute 的使用次数
used = collections.Counter()
for node in ast.walk(tree):
    if isinstance(node, ast.Name):
        used[node.id] += 1
    elif isinstance(node, ast.Attribute):
        used[node.attr] += 1
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        pass

# 顶层定义
top_funcs, top_classes, top_consts = [], [], []
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        top_funcs.append((node.name, node.lineno))
    elif isinstance(node, ast.ClassDef):
        top_classes.append((node.name, node.lineno))
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id.isupper():
                top_consts.append((t.id, node.lineno))

def report(title, items):
    print('=' * 60)
    print(title)
    print('=' * 60)
    dead = []
    for name, ln in items:
        # 只统计「使用处」：FunctionDef 的 name 不是 ast.Name 节点，定义本身
        # 不产生计数。所以 refs==0 才是真没人用；写成 n<=1 会把「只被调用
        # 一次」的函数全部误报成死代码（托盘、保存那几个就是这么被冤枉的）。
        n = used[name]
        if n == 0:
            dead.append((name, ln, n))
    for name, ln, n in dead:
        print('  DEAD? %-28s line %-6s refs=%d' % (name, ln, n))
    if not dead:
        print('  (无)')
    return dead

d1 = report('顶层函数（引用<=1 视为未使用）', top_funcs)
d2 = report('顶层类', top_classes)
d3 = report('顶层常量（引用<=1 视为未使用）', top_consts)

# 类方法
print('=' * 60)
print('类方法（refs==0 才算未使用）')
print('=' * 60)
for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
    for m in cls.body:
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if m.name.startswith('__') and m.name.endswith('__'):
                continue
            # 同上：定义不计数，所以「只调用一次」是 refs==1，正常；
            # refs==0 才是真的没人调。
            if used[m.name] == 0:
                print('  DEAD? %s.%-26s line %-6s refs=%d' % (cls.name, m.name, m.lineno, used[m.name]))
