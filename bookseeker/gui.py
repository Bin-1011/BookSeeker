"""图形界面入口（兼容层）。

真正的界面代码在 :mod:`bookseeker.ui` 里，本模块只是保持旧的导入路径可用：

    from bookseeker.gui import run_gui
"""

from .ui.app import BookSeekerApp, run_gui

__all__ = ["BookSeekerApp", "run_gui"]
