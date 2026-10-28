"""扫出「定义了但从没被引用」的模块级常量和 import。"""
import re
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

src = open("snap.py", encoding="utf-8").read()
lines = src.split("\n")

# 模块级常量：行首无缩进的 NAME = ...
names = []
for i, ln in enumerate(lines, 1):
    m = re.match(r"^([A-Z][A-Z0-9_]{2,})\s*=", ln)
    if m:
        names.append((m.group(1), i))

# 出现次数（去掉定义行本身）
print("常量总数 %d" % len(names))
dead = []
for name, ln in names:
    hits = len(re.findall(r"\b%s\b" % re.escape(name), src))
    if hits <= 1:
        dead.append((name, ln, hits))
        print("  未使用: %-26s line %-5d refs=%d" % (name, ln, hits))

if not dead:
    print("  （没有完全未使用的常量）")

# 只出现 2 次的（定义 + 1 处引用）也值得看看
print("\n只被引用 1 处（可能是该删的中间层）：")
for name, ln in names:
    hits = len(re.findall(r"\b%s\b" % re.escape(name), src))
    if hits == 2:
        use_lines = [i for i, l in enumerate(lines, 1)
                     if re.search(r"\b%s\b" % re.escape(name), l) and i != ln]
        print("  %-26s line %-5d -> %s" % (name, ln, use_lines[:3]))

# import 检查
print("\nimport 使用情况：")
for m in re.finditer(r"^(?:from\s+\S+\s+)?import\s+(.+)$", src, re.M):
    mods = [x.strip().split(" as ")[-1].strip() for x in m.group(1).split(",")]
    for mod in mods:
        if not mod or mod.startswith("("):
            continue
        base = mod.split(".")[0]
        if len(re.findall(r"\b%s\b" % re.escape(base), src)) <= 1:
            print("  未使用 import: %s" % mod)
