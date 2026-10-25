# 贡献指南

感谢你愿意为 SnapLite 花时间。这个项目很小，门槛也就低——所有功能都在一个
`snap.py` 里，改完直接跑就能看到效果。

## 环境

- Windows 10 / 11
- Python 3.8 以上（只依赖标准库，不需要 `pip install`）
- 想打包 exe 才需要额外装 PyInstaller

```bash
git clone https://github.com/zhaozun123/snaplite-screenshot.git
cd snaplite-screenshot
python snap.py
```

## 提交前自检

```bash
python tools/check_undefined.py     # 未定义名（NameError 风险）必须 0
python tools/audit_deadcode.py      # 零引用的死代码
python tests/test_pen.py            # 功能自测
python tests/test_pen_visual.py     # 像素级验证
```

两条硬性要求：

1. **不留死代码** — 删掉的功能要连常量和 API 声明一起清干净。
   注意：ctypes 的 API 声明在模块加载时执行，删错会导致**导入期**就崩，
   而 Python 不会在编译期报这种错——所以每次改完务必跑 `check_undefined.py`。
2. **不留未定义名** — 同上。`check_undefined.py` 用标准库 `symtable` 实现，
   能抓出「引用了但根本没定义」的名字。

## 代码风格

- 中文注释，英文标识符
- 每个 Win32 API 都在模块顶部用 `_proto()` 显式声明参数和返回值
  （ctypes 默认按 `int` 转换，64 位下会截断指针）
- 新增 Windows API 时注意**版本兼容**：Win10 1703+ 独有的 API
  （如 `GetDpiForSystem`）不能写成模块级 `_proto`，否则老系统在 `import` 阶段
  直接崩。要用 `_lazy_proto()` 惰性获取并准备降级分支
- 坐标一律用全屏虚拟坐标，渲染时通过 `ox / oy` 偏移裁剪进选区

## 提交信息

用中文或英文都行，格式参考：

```
fix(马赛克): 边缘取色越界导致读到屏幕底部像素
feat(画笔): 支持三档笔宽
chore: 清理文字功能的死代码
```

## Pull Request

1. 一个 PR 只做一件事
2. 在描述里写清「改了什么 / 为什么 / 怎么验证的」
3. 附上截图（尤其是界面相关的改动）

## 需要注意的坑

- **ctypes 回调里抛的异常会被静默吞掉**。窗口过程已经统一包了
  `_safe_proc()`，但如果你新增回调，记得也包一层。
- **GDI+ 对象必须释放**。`GFX` 已经实现了 `__del__` 兜底，但显式
  `close()` 仍然更好。
- **全局函数（`SetWindowLongPtr` 等）在 64 位下要用 `c_void_p` 传参**，
  截断会导致句柄失效。
