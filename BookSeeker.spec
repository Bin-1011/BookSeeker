# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— 生成免安装的 BookSeeker。

一次构建产出两个单文件 exe：

* ``dist/BookSeeker.exe``      图形界面版（双击即用，不弹黑框）
* ``dist/BookSeeker-cli.exe``  命令行版（在终端里有正常的输出与阻塞行为）

为什么要两个：Windows 的 GUI 子系统程序不会阻塞调用它的 shell，
PowerShell / cmd 里 `BookSeeker.exe sources > out.txt` 会立刻返回、
输出也可能丢失。命令行场景用专门的控制台版最省心。

构建（在项目根目录）::

    build_exe.bat
    # 或者手动：
    set PYTHONPATH=.build\\pylibs
    set PYINSTALLER_CONFIG_DIR=.build\\pyi-cache
    python -m PyInstaller BookSeeker.spec --noconfirm --workpath .build\\pyi-work --distpath dist
"""

import os

# 书源模块是运行时用 importlib 动态导入的（import_module("." + name)），
# PyInstaller 的静态分析发现不了它们。不显式列出来的话，打出来的 exe
# 会「一个内置书源都加载不到」，而且不会有任何报错——这是最容易踩的坑。
HIDDEN_IMPORTS = [
    "bookseeker.gui",
    "bookseeker.cli",
    "bookseeker.sources.gutenberg",
    "bookseeker.sources.standard_ebooks",
    "bookseeker.sources.wikisource",
    "bookseeker.sources.aozora",
    "bookseeker.sources.archive_org",
    "bookseeker.sources.openlibrary",
]

# 示例插件是运行时读出来复制到插件目录的，属于数据文件
DATAS = [
    ("bookseeker/examples/example_source.py", "bookseeker/examples"),
]

# 明确排除用不到的重型库，能省下不少体积。
# 注意：绝不能排除 html.parser / email —— htmldom 和 urllib 都依赖它们。
EXCLUDES = [
    "numpy", "pandas", "matplotlib", "scipy", "PIL", "Pillow",
    "pytest", "setuptools", "pip", "distutils",
]

ICON = os.path.join("docs", "icon.ico")

a = Analysis(
    ["BookSeeker.py"],
    pathex=[],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)


def make_exe(name, console):
    """两个 exe 共用同一份 Analysis / PYZ，只是子系统不同。"""
    return EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,               # 不压缩：UPX 会明显提高杀软误报率
        runtime_tmpdir=None,
        console=console,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=ICON,
    )


exe_gui = make_exe("BookSeeker", console=False)       # 双击用
exe_cli = make_exe("BookSeeker-cli", console=True)    # 终端用
