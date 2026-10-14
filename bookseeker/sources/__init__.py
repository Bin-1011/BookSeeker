"""书源子系统。

对外主要暴露三样东西：

* :class:`Source` —— 自己写书源时继承的基类
* :class:`Registry` —— 书源注册表，支持多源并发检索
* :func:`get_registry` —— 拿到全局注册表
"""

from .base import ProgressFn, Source, SourceError, dedupe_books, rank_books, relevance
from .registry import (
    BUILTIN_SOURCES,
    Registry,
    build_registry,
    get_registry,
    load_builtins,
    load_plugin_file,
    load_plugins,
    write_example_plugin,
)

__all__ = [
    "Source",
    "SourceError",
    "ProgressFn",
    "Registry",
    "BUILTIN_SOURCES",
    "build_registry",
    "get_registry",
    "load_builtins",
    "load_plugins",
    "load_plugin_file",
    "write_example_plugin",
    "dedupe_books",
    "rank_books",
    "relevance",
]
