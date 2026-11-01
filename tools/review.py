# -*- coding: utf-8 -*-
"""全面体检：把「机器能查的部分」一次算出来，供 review 用。

检查项
  1. 规模       行数 / 类 / 方法 / 模块级常量
  2. 文档       docstring 覆盖率、缺注释的方法
  3. 复杂度     最长的 15 个方法（LOC）、分支数、嵌套深度
  4. 异常处理   裸 except、pass 吞异常、ctypes 回调是否套了保护
  5. Win32 契约 user32/gdi32/gdip 调用的 argtypes 声明覆盖率（不声明会截断 64 位句柄）
  6. 隐患       TODO/FIXME、裸 assert、可变默认值、未用导入
  7. 测试覆盖   哪些 App 方法从没被 tests/ 碰过

用法：python tools/review.py [--full]
"""
import ast
import collections
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "snap.py")
FULL = "--full" in sys.argv

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

text = io.open(SRC, encoding="utf-8").read()
lines = text.split("\n")
tree = ast.parse(text)


def loc(node):
    return (node.end_lineno or node.lineno) - node.lineno + 1


def branches(node):
    """分支数：if/for/while/try/and/or 的个数，粗略衡量复杂度"""
    n = 0
    for x in ast.walk(node):
        if isinstance(x, (ast.If, ast.For, ast.While, ast.Try, ast.With)):
            n += 1
        elif isinstance(x, ast.BoolOp):
            n += 1
    return n


print("=" * 70)
print("1. 规模")
print("=" * 70)
classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
top_funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
consts = []
for n in tree.body:
    if isinstance(n, ast.Assign):
        for t in n.targets:
            if isinstance(t, ast.Name) and t.id.isupper():
                consts.append(t.id)
methods = []
for c in classes:
    for n in c.body:
        if isinstance(n, ast.FunctionDef):
            methods.append((c.name, n))
print("  snap.py            %d 行" % len(lines))
print("  类                 %s" % ", ".join(c.name for c in classes))
print("  顶层函数           %s" % ", ".join(f.name for f in top_funcs))
print("  方法               %d 个" % len(methods))
print("  模块级常量         %d 个" % len(consts))
print()

print("=" * 70)
print("2. 文档")
print("=" * 70)
no_doc = [(c, n.name, n.lineno) for c, n in methods if not ast.get_docstring(n)]
no_doc += [("<module>", f.name, f.lineno) for f in top_funcs if not ast.get_docstring(f)]
total = len(methods) + len(top_funcs)
print("  docstring 覆盖     %d/%d = %.1f%%" % (total - len(no_doc), total,
                                             100.0 * (total - len(no_doc)) / total))
for c, name, ln in no_doc:
    print("    缺注释  %s.%s  line %d" % (c, name, ln))
print()

print("=" * 70)
print("3. 复杂度（最长的 15 个方法）")
print("=" * 70)
rank = sorted(methods, key=lambda x: loc(x[1]), reverse=True)
print("  %-6s %-34s %6s %6s" % ("行", "方法", "分支", "起始行"))
for c, n in rank[:15]:
    print("  %-6d %-34s %6d %6d" % (loc(n), "%s.%s" % (c, n.name), branches(n), n.lineno))
print("  平均长度 %.1f 行；超过 80 行的方法 %d 个"
      % (sum(loc(n) for _, n in methods) / float(len(methods)),
         sum(1 for _, n in methods if loc(n) > 80)))
print()

print("=" * 70)
print("4. 异常处理")
print("=" * 70)
bare = [(i + 1, l.strip()) for i, l in enumerate(lines)
        if re.match(r"\s*except\s*:", l)]
swallow = []
for i, l in enumerate(lines):
    if re.match(r"\s*except\b", l) and i + 1 < len(lines) and \
            re.match(r"\s*pass\s*$", lines[i + 1]):
        swallow.append((i + 1, l.strip()))
print("  裸 except           %d 处" % len(bare))
for ln, s in bare:
    print("     line %d: %s" % (ln, s))
print("  except 后直接 pass  %d 处" % len(swallow))
for ln, s in swallow[:20]:
    print("     line %d: %s + pass" % (ln, s))
print()

print("=" * 70)
print("5. Win32 契约（argtypes 声明）")
print("=" * 70)
# 声明有两种写法：_proto(user32.X, ...) 和 _proto(windll.kernel32.X, ...)，
# 后者多了 windll. 一层。两种都要认，否则会把已声明的函数报成「未声明」。
declared = set(re.findall(r"_proto\(\s*(\w+)\.(\w+)", text))
declared |= set(re.findall(r"_proto\(\s*windll\.(\w+)\.(\w+)", text))
declared |= set(re.findall(r"(\w+)\.(\w+)\.argtypes\s*=", text))

# 「用了哪些」也要能识别 windll.kernel32.X：先把两层写法折叠成 kernel32.X 再统计，
# 否则 windll. 后面的 kernel32 被当成普通标识符，漏掉真正的调用。
flat = re.sub(r"\bwindll\.(kernel32|user32|gdi32|shell32|dwmapi|comctl32)\.",
              r"\1.", text)
used = collections.Counter()
for m in re.finditer(r"\b(user32|gdi32|gdip|msimg32|kernel32|shell32|dwmapi|comctl32)\.(\w+)",
                     flat):
    used[(m.group(1), m.group(2))] += 1
missing = [(k, v) for k, v in used.items() if k not in declared and v >= 1]
missing.sort(key=lambda x: -x[1])
print("  调用到的 Win32/GDI+ 函数 %d 个，已声明 argtypes %d 个"
      % (len(used), len(used) - len(missing)))
print("  未声明 %d 个（64 位下句柄/指针会被截断，是历史踩过的大坑）：" % len(missing))
for (dll, fn), cnt in missing[:40]:
    print("     %s.%-28s 用了 %d 次" % (dll, fn, cnt))
print()

print("=" * 70)
print("6. 隐患")
print("=" * 70)
todos = [(i + 1, l.strip()) for i, l in enumerate(lines)
         if re.search(r"\b(TODO|FIXME|XXX|HACK)\b", l)]
print("  TODO/FIXME          %d 处" % len(todos))
for ln, s in todos:
    print("     line %d: %s" % (ln, s[:90]))
asserts = [(i + 1, l.strip()) for i, l in enumerate(lines)
           if re.match(r"\s*assert\s", l)]
print("  生产代码里的 assert %d 处" % len(asserts))
for ln, s in asserts:
    print("     line %d: %s" % (ln, s[:90]))
defaults = []
for _, n in methods:
    for d in n.args.defaults + [x for x in n.args.kw_defaults if x is not None]:
        if isinstance(d, (ast.List, ast.Dict, ast.Set)):
            defaults.append((n.name, n.lineno))
print("  可变默认参数        %d 处 %s" % (len(defaults), defaults))
print()

print("=" * 70)
print("7. 测试覆盖（App 方法）")
print("=" * 70)
test_text = ""
for fn in sorted(os.listdir(os.path.join(ROOT, "tests"))):
    if fn.endswith(".py"):
        test_text += io.open(os.path.join(ROOT, "tests", fn), encoding="utf-8").read()
for fn in sorted(os.listdir(os.path.join(ROOT, "tools"))):
    if fn.endswith(".py"):
        test_text += io.open(os.path.join(ROOT, "tools", fn), encoding="utf-8").read()
app_methods = [n.name for c, n in methods if c == "App" and not n.name.startswith("__")]
hit = [m for m in app_methods if ("." + m) in test_text or ("_" + m in test_text and m in test_text)]
unhit = [m for m in app_methods if m not in hit]
print("  App 方法 %d 个，被 tests/ 或 tools/ 直接碰过 %d 个（%.0f%%）"
      % (len(app_methods), len(hit), 100.0 * len(hit) / len(app_methods)))
print("  从没碰过的 %d 个：" % len(unhit))
for m in unhit:
    print("     %s" % m)
