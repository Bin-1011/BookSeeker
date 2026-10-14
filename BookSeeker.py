#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BookSeeker 启动入口。

用法::

    python BookSeeker.py                 # 启动图形界面
    python BookSeeker.py search 红楼梦    # 命令行检索
    python BookSeeker.py --help          # 查看全部命令

在 Windows 上也可以直接双击 `启动 BookSeeker.bat`。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bookseeker.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
