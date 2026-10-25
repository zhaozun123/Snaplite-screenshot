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
        # 定义处算 1 次引用；再算实际引用
        n = used[name]
        if n <= 1:
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
print('类方法（引用<=1 视为未使用）')
print('=' * 60)
for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
    for m in cls.body:
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if m.name.startswith('__') and m.name.endswith('__'):
                continue
            if used[m.name] <= 1:
                print('  DEAD? %s.%-26s line %-6s refs=%d' % (cls.name, m.name, m.lineno, used[m.name]))
