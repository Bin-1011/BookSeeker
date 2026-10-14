"""书源注册表：内置书源加载、插件发现、多源并发检索。"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .. import config as config_mod
from ..models import Book, BookContent
from .base import Source, SourceError, dedupe_books, rank_books

#: 内置书源的模块名（位于 bookseeker.sources 包内）
BUILTIN_SOURCES: tuple[str, ...] = (
    "gutenberg",
    "standard_ebooks",
    "wikisource",
    "aozora",
    "archive_org",
    "openlibrary",
)

_PLUGIN_MODULE_PREFIX = "bookseeker_plugin_"


class Registry:
    """管理所有可用书源。"""

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg if cfg is not None else config_mod.CONFIG
        self._sources: dict[str, Source] = {}
        self.load_reports: list[dict] = []

    # ------------------------------------------------------------------
    # 注册与查询
    # ------------------------------------------------------------------

    def register(self, source: Source, replace: bool = True) -> bool:
        sid = (getattr(source, "id", "") or "").strip()
        if not sid:
            raise ValueError("书源缺少 id 属性：%r" % (source,))
        if sid in self._sources and not replace:
            return False
        self._sources[sid] = source
        return True

    def get(self, source_id: str) -> Source | None:
        return self._sources.get(source_id)

    def all(self) -> list[Source]:
        return sorted(self._sources.values(), key=lambda s: (s.priority, s.name or s.id))

    def ids(self) -> list[str]:
        return [s.id for s in self.all()]

    def disabled_ids(self) -> set[str]:
        value = self.cfg.get("disabled_sources") or []
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        return set(value)

    def is_enabled(self, source: Source) -> bool:
        if source.id in self.disabled_ids():
            return False
        return bool(getattr(source, "enabled_by_default", True))

    def enabled(self) -> list[Source]:
        return [s for s in self.all() if self.is_enabled(s)]

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def search_all(
        self,
        query: str,
        source_ids: Sequence[str] | None = None,
        limit_per_source: int | None = None,
        max_workers: int = 8,
        on_batch: Callable[[Source, list[Book]], None] | None = None,
        on_error: Callable[[Source, Exception], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> list[Book]:
        """并发检索所有（或指定的）书源，合并后按相关度排序。

        :param on_batch: 每个书源一返回结果就立刻回调，便于界面增量显示。
        :param on_error: 某个书源失败时回调，其余书源不受影响。
        """
        query = (query or "").strip()
        if not query:
            return []

        limit = int(limit_per_source or self.cfg.get("max_results_per_source", 20) or 20)
        if source_ids:
            sources = [s for s in (self.get(i) for i in source_ids) if s is not None]
        else:
            sources = self.enabled()
        if not sources:
            return []

        def run(src: Source) -> tuple[Source, list[Book], Exception | None]:
            if should_stop is not None and should_stop():
                return src, [], None
            try:
                books = list(src.search(query, limit) or [])
            except Exception as exc:  # 单源失败不影响整体
                return src, [], exc
            for b in books:
                b.source = b.source or src.id
                b.source_name = b.source_name or src.name or src.id
            return src, books, None

        collected: list[Book] = []
        lock = threading.Lock()
        workers = max(1, min(int(max_workers), len(sources)))

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bs-search") as pool:
            futures = [pool.submit(run, s) for s in sources]
            for fut in as_completed(futures):
                try:
                    src, books, err = fut.result()
                except Exception as exc:  # pragma: no cover - 防御性
                    continue
                if err is not None:
                    if on_error is not None:
                        try:
                            on_error(src, err)
                        except Exception:
                            pass
                    continue
                if not books:
                    continue
                with lock:
                    collected.extend(books)
                if on_batch is not None:
                    try:
                        on_batch(src, books)
                    except Exception:
                        pass

        return rank_books(dedupe_books(collected), query)

    def search_sources(self, query: str, **kw: Any) -> dict[str, Any]:
        """返回 ``{书源id: 结果或异常}``，便于界面按源分组展示。"""
        out: dict[str, Any] = {}

        def batch(src: Source, books: list[Book]) -> None:
            out[src.id] = books

        def error(src: Source, exc: Exception) -> None:
            out[src.id] = exc

        self.search_all(query, on_batch=batch, on_error=error, **kw)
        return out

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    def fetch(self, book: Book, progress: Any = None) -> BookContent:
        src = self.get(book.source)
        if src is None:
            raise SourceError("未知书源：%s" % book.source)
        content = src.fetch(book, progress)
        if content is None:
            raise SourceError("书源 %s 未返回内容" % book.source)
        content.source_name = content.source_name or src.name or src.id
        content.source_url = content.source_url or book.url
        if not content.title:
            content.title = book.title
        if not content.authors:
            content.authors = list(book.authors)
        if not content.language:
            content.language = book.language
        return content


# ----------------------------------------------------------------------
# 加载内置书源与插件
# ----------------------------------------------------------------------


def _collect_sources(module) -> list[Source]:
    """从模块里找出所有 Source 子类 / 实例。"""
    classes: list[type] = []
    instances: list[Source] = []

    explicit = getattr(module, "SOURCES", None)
    if isinstance(explicit, (list, tuple)):
        for item in explicit:
            if isinstance(item, Source):
                instances.append(item)
            elif isinstance(item, type) and issubclass(item, Source):
                classes.append(item)

    module_name = getattr(module, "__name__", "")
    for name, obj in vars(module).items():
        if name.startswith("_"):
            continue
        if isinstance(obj, type) and issubclass(obj, Source) and obj is not Source:
            if not getattr(obj, "id", "") or obj in classes:
                continue
            # 只认本模块定义的类；从别处 import 进来的书源不算，
            # 否则 openlibrary 里 import 的 ArchiveOrgSource 会被重复上报。
            if module_name and getattr(obj, "__module__", "") != module_name:
                continue
            classes.append(obj)

    out = list(instances)
    for cls in classes:
        try:
            out.append(cls())
        except Exception:
            traceback.print_exc()
    return out


def load_builtins(registry: Registry) -> list[dict]:
    """导入内置书源模块并注册。单个模块出错不影响其它模块。"""
    reports: list[dict] = []
    for name in BUILTIN_SOURCES:
        report = {"module": name, "ok": False, "sources": [], "message": ""}
        try:
            module = importlib.import_module("..sources.%s" % name, __package__)
        except Exception as exc:
            report["message"] = "%s: %s" % (type(exc).__name__, exc)
            reports.append(report)
            continue
        found = _collect_sources(module)
        for src in found:
            try:
                registry.register(src)
                report["sources"].append(src.id)
                report["ok"] = True
            except Exception as exc:
                report["message"] = str(exc)
        reports.append(report)
    return reports


def load_plugin_file(path: str | Path, registry: Registry) -> dict:
    """加载单个插件文件，返回加载报告。"""
    path = Path(path)
    report: dict[str, Any] = {
        "file": str(path), "ok": False, "sources": [], "message": "",
    }
    module_name = _PLUGIN_MODULE_PREFIX + path.stem
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError("无法为该文件创建导入规格")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception as exc:
        report["message"] = "%s: %s" % (type(exc).__name__, exc)
        return report

    try:
        found = _collect_sources(module)
    except Exception as exc:
        report["message"] = "收集书源失败：%s" % exc
        return report

    for src in found:
        try:
            registry.register(src)
            report["sources"].append(src.id)
        except Exception as exc:
            report["message"] = str(exc)
    report["ok"] = bool(found)
    if not found and not report["message"]:
        report["message"] = "文件里没有找到 Source 子类"
    return report


def load_plugins(
    registry: Registry, plugin_dir: str | Path | None = None, recursive: bool = False
) -> list[dict]:
    """扫描插件目录（默认 ``BOOKSEEKER_HOME/plugins``）并加载所有 .py。"""
    directory = Path(plugin_dir) if plugin_dir else config_mod.PLUGIN_DIR
    reports: list[dict] = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return reports
    pattern = "**/*.py" if recursive else "*.py"
    for path in sorted(directory.glob(pattern)):
        if path.name.startswith("_"):
            continue
        reports.append(load_plugin_file(path, registry))
    return reports


def write_example_plugin(dest_dir: str | Path | None = None, overwrite: bool = False) -> Path | None:
    """把内置的示例插件复制到插件目录，方便用户照着改。"""
    directory = Path(dest_dir) if dest_dir else config_mod.PLUGIN_DIR
    directory.mkdir(parents=True, exist_ok=True)
    # 冻结成 exe 后包内资源在 sys._MEIPASS 下，不能再用 __file__ 定位
    source = config_mod.package_path("examples", "example_source.py")
    if not source.is_file():
        fallback = Path(__file__).resolve().parent.parent / "examples" / "example_source.py"
        if fallback.is_file():
            source = fallback
        else:
            return None
    target = directory / "example_source.py"
    if target.exists() and not overwrite:
        return target
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def build_registry(cfg: dict | None = None, with_plugins: bool = True) -> Registry:
    """构建完整的书源注册表（内置 + 插件）。"""
    registry = Registry(cfg)
    registry.load_reports = load_builtins(registry)
    if with_plugins:
        try:
            write_example_plugin()
        except Exception:
            pass
        registry.load_reports.extend(load_plugins(registry))
    return registry


_default_registry: Registry | None = None
_registry_lock = threading.Lock()


def get_registry(refresh: bool = False, cfg: dict | None = None) -> Registry:
    """获取进程内共享的注册表（懒加载）。"""
    global _default_registry
    with _registry_lock:
        if _default_registry is None or refresh:
            _default_registry = build_registry(cfg)
        return _default_registry
