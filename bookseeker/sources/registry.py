"""书源注册表：内置书源加载、插件发现、多源并发检索。"""

from __future__ import annotations

import importlib
import importlib.util
import math
import queue
import re
import sys
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, wait
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .. import config as config_mod
from .. import http as http_mod
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

#: 配置里没写 search_deadline 时用这个值
DEFAULT_SEARCH_DEADLINE = 12.0

#: 等待下一批结果时最多一次等这么久，避免极端配置把 wait 的 timeout 撑爆
_MAX_WAIT_SLICE = 300.0

#: 汉字 / 假名 / 谚文，用来粗判查询词的语言
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_HANGUL_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")


def query_languages(query: str) -> tuple[str, ...]:
    """粗判查询词可能是哪种语言，供「跳过不可能命中的书源」使用。

    只在查询词**几乎全是中日韩文字**时才给结论；只要混进了较多拉丁字母、
    数字之类，就返回空元组表示「判断不了」，调用方应当老老实实查所有源。
    汉字在中文和日文里都常见，所以出现汉字时把中日常一起当作候选。
    """
    text = query or ""
    if not text.strip():
        return ()
    han = len(_HAN_RE.findall(text))
    kana = len(_KANA_RE.findall(text))
    hangul = len(_HANGUL_RE.findall(text))
    cjk = han + kana + hangul
    if cjk < 2:
        return ()
    # 非中日韩字符（空格、标点、数字、字母）占比略高就不做判断，宁可多查
    if len(text) - cjk > cjk * 0.25 + 2:
        return ()
    langs: set[str] = set()
    if han:
        langs.update(("zh", "ja"))
    if kana:
        langs.add("ja")
    if hangul:
        langs.add("ko")
    return tuple(sorted(langs))


def _as_float(value: Any, fallback: float) -> float:
    """把配置里的值转成 float，转不动就用 ``fallback``。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def _notify(callback: Callable | None, *args: Any) -> None:
    """调用回调；回调自己抛异常也不能影响检索。"""
    if callback is None:
        return
    try:
        callback(*args)
    except Exception:
        pass


class _DaemonPool:
    """守护线程的小线程池：任务到点做不完，也不会把进程拖住。

    这里**没有**用 ``concurrent.futures.ThreadPoolExecutor``，原因是实测出来的：
    它的工作线程是非守护线程，``concurrent.futures.thread._python_exit`` 会在
    解释器退出时把这些线程再 join 一遍，``shutdown(wait=False)`` 挡不住这一步。
    于是「检索已经到点、但还有源卡在网络上」的时候，进程照样被拖住：实测
    ``search 红楼梦`` 12 秒就出结果，进程却要 37 秒才退出，sherlock 更是 60 秒。
    换成守护线程后，``shutdown(wait=False)`` 是真的「说走就走」。

    只需要 ``submit`` / ``shutdown`` 两个方法，返回的是标准的
    :class:`concurrent.futures.Future`，所以 ``concurrent.futures.wait`` 照样能用。
    """

    def __init__(self, max_workers: int, thread_name_prefix: str = "bs-search") -> None:
        self.max_workers = max(1, int(max_workers))
        self._prefix = thread_name_prefix
        self._queue: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, fn: Callable, *args: Any, **kwargs: Any) -> Future:
        future: Future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("线程池已关闭，不能再提交任务")
            self._queue.put((future, fn, args, kwargs))
            if len(self._threads) < self.max_workers:
                worker = threading.Thread(
                    target=self._work,
                    name="%s-%d" % (self._prefix, len(self._threads) + 1),
                    daemon=True,
                )
                self._threads.append(worker)
                worker.start()
        return future

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:          # shutdown 时投的哨兵
                return
            future, fn, args, kwargs = item
            if not future.set_running_or_notify_cancel():
                continue              # 已经被 cancel_futures 取消掉了
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # 任务自己的异常交给 future 带走
                future.set_exception(exc)

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        """关闭线程池。``wait=False`` 立刻返回，不等没跑完的任务。"""
        with self._lock:
            self._closed = True
            if cancel_futures:
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not None:
                        item[0].cancel()   # 还没轮到的任务直接取消
            for _ in self._threads:
                self._queue.put(None)
            threads = list(self._threads)
        if wait:
            for worker in threads:
                worker.join()


class Registry:
    """管理所有可用书源。"""

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg if cfg is not None else config_mod.CONFIG
        self._sources: dict[str, Source] = {}
        self.load_reports: list[dict] = []
        #: 正在下载正文的书源 id：检索临时换客户端时要避开它们
        self._fetching: set[str] = set()
        self._fetch_lock = threading.Lock()

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
        deadline: float | None = None,
        on_source_done: Callable[[Source, float], None] | None = None,
    ) -> list[Book]:
        """并发检索所有（或指定的）书源，合并后按相关度排序。

        :param on_batch: 每个书源一返回结果就立刻回调，便于界面增量显示。
        :param on_error: 某个书源失败或超时时回调，其余书源不受影响。
        :param deadline: 本次检索最多等多少秒（从调用时刻算起）；``None``
            表示用配置里的 ``search_deadline``（默认 12 秒）。到点就返回
            **已经拿到**的结果，还在跑的书源按超时处理——会用
            :class:`TimeoutError` 走 ``on_error`` 上报，其结果不再合并进来。
        :param on_source_done: 每个书源正常返回后回调一次，参数是书源和它在
            本次检索里花掉的秒数；一条结果都没有的源也会回调（``on_batch``
            只在真有结果时才回调，界面才不会被空批次打扰）。
        """
        query = (query or "").strip()
        if not query:
            return []

        limit = int(limit_per_source or self.cfg.get("max_results_per_source", 20) or 20)
        if source_ids:
            sources = [s for s in (self.get(i) for i in source_ids) if s is not None]
        else:
            # 用户点名指定了书源就照办；只有「全部书源」才做智能筛选
            sources = self._filter_by_language(self.enabled(), query)
        if not sources:
            return []

        wait_seconds = self._resolve_deadline(deadline)
        search_timeout, search_retries = self._search_profile()
        search_client = self._make_search_client(search_timeout, search_retries, wait_seconds)

        def run(src: Source) -> tuple[Source, list[Book], Exception | None]:
            if should_stop is not None and should_stop():
                return src, [], None
            swapped = self._use_search_client(src, search_client)
            try:
                books = list(src.search(query, limit) or [])
            except Exception as exc:  # 单源失败不影响整体
                return src, [], exc
            finally:
                if swapped:
                    self._restore_client(src)
            for b in books:
                b.source = b.source or src.id
                b.source_name = b.source_name or src.name or src.id
            return src, books, None

        collected: list[Book] = []
        workers = max(1, min(int(max_workers), len(sources)))
        started = time.monotonic()

        def elapsed() -> float:
            return time.monotonic() - started

        def consume(fut, src: Source) -> None:
            """收下一个已经结束的书源：合并结果、发回调。"""
            try:
                _src, books, err = fut.result()
            except Exception as exc:  # pragma: no cover - 防御性
                books, err = [], exc
            if err is not None:
                _notify(on_error, src, err)
                return
            if books:
                collected.extend(books)
                _notify(on_batch, src, books)
            if on_source_done is not None:
                _notify(on_source_done, src, elapsed())

        pool = _DaemonPool(max_workers=workers, thread_name_prefix="bs-search")
        futures = {pool.submit(run, s): s for s in sources}
        pending = set(futures)
        timed_out: list[Source] = []
        try:
            # as_completed 会一直等到所有源都回来，这里改成「每轮等剩余时间」，
            # 到点立刻收工
            while pending:
                remaining = wait_seconds - elapsed()
                done, pending = wait(
                    pending,
                    timeout=max(0.0, min(remaining, _MAX_WAIT_SLICE)),  # wait 不收负数
                    return_when=FIRST_COMPLETED,
                )
                for fut in done:
                    consume(fut, futures[fut])
                if not done:
                    break          # 到点了，还没回来的源按超时处理
        finally:
            timed_out = [futures[fut] for fut in pending]
            # 不等没跑完的线程：已经到点了，没必要把进程拖住
            pool.shutdown(wait=False, cancel_futures=True)

        for src in timed_out:
            _notify(on_error, src, TimeoutError("检索超时（超过 %.1f 秒）" % wait_seconds))

        return rank_books(dedupe_books(collected), query)

    # ------------------------------------------------------------------
    # 检索参数
    # ------------------------------------------------------------------

    def _resolve_deadline(self, deadline: float | None) -> float:
        """把 ``deadline`` 换算成「本次检索最多等多少秒」。

        ``None`` 表示用配置里的 ``search_deadline``；配置或参数里写了 0 /
        负数就表示一到点（也就是立刻）返回已经拿到的结果。
        """
        value = self.cfg.get("search_deadline") if deadline is None else deadline
        return _as_float(value, DEFAULT_SEARCH_DEADLINE)

    def _search_profile(self) -> tuple[float | None, int | None]:
        """检索路径的请求参数：更短的超时 + 更少的重试（可配置关闭）。

        返回 ``(超时或 None, 尝试次数或 None)``，``None`` 表示沿用默认值。
        """
        timeout = _as_float(self.cfg.get("search_timeout"), 0.0)
        try:
            retries = int(self.cfg.get("search_retries") or 0)
        except (TypeError, ValueError):
            retries = 0
        return (timeout if timeout > 0 else None), (retries if retries > 0 else None)

    def _make_search_client(
        self, timeout: float | None, retries: int | None, deadline: float = 0.0
    ):
        """造一个「检索专用」HTTP 客户端；两个参数都为 None 时返回 None。

        为什么要换客户端而不是临时改超时：书源**内部**还会自己起线程池
        （例如 wikisource 按 zh/ja/en 三个子域并发检索），那些线程读的是
        ``self.http``，也就是这个客户端。只在自己线程里改设置管不到它们，
        它们会按默认的 30 秒 × 3 次重试一直挂在网络上——检索早就到点了，
        进程却还得等它们几十秒。把客户端换掉，书源里所有线程（含它自己起的）
        都会用检索该用的参数。

        单次请求的超时不会超过本次检索的预算 ``deadline``：预算本来就不让它
        跑那么久，超时设得比预算还长只会让「到点了还挂在网络上」的线程留得更久。

        其余设置（缓存目录 / TTL / 限速 / 代理）都跟默认客户端保持一致，
        所以缓存照样命中，也不会对站点更粗暴。
        """
        if timeout is None and retries is None:
            return None
        if timeout is not None and deadline > 0:
            timeout = max(1.0, min(float(timeout), float(deadline)))
        base = http_mod.default
        client = http_mod.HttpClient(
            timeout=timeout if timeout is not None else base.timeout,
            retries=retries if retries is not None else base.retries,
            proxy=base.proxy,
            min_interval=base.min_interval,
            cache_enabled=base.cache_enabled,
            cache_ttl=base.cache_ttl,
            hard_retries=True,          # 检索失败一次就放弃，被限流也只多等几秒再试一次
        )
        client.is_search_client = True
        return client

    def _use_search_client(self, src: Source, client) -> bool:
        """把 ``client`` 临时装到书源上，返回是否真的换了（换过就要还原）。

        只在书源用的是共享默认客户端、而且此刻没人在下载它的正文时才换：
        下载正文必须保持原来的重试行为。
        """
        if client is None or getattr(src, "_client", None) is not None:
            return False
        with self._fetch_lock:
            if src.id in self._fetching:
                return False
            src._client = client
            return True

    def _restore_client(self, src: Source) -> None:
        """还原成「用共享默认客户端」。"""
        with self._fetch_lock:
            src._client = None

    def _filter_by_language(self, sources: Sequence[Source], query: str) -> list[Source]:
        """纯中日韩关键字时，跳过语言声明里完全不含该语言的书源。

        做得很保守：查询词里混了较多非中日韩字符就不判断；书源没有声明
        ``languages`` 也一律保留；万一过滤完一个源都不剩，就退回不过滤。
        """
        if not bool(self.cfg.get("smart_language_filter", True)):
            return list(sources)
        langs = set(query_languages(query))
        if not langs:
            return list(sources)
        kept = [
            s for s in sources
            if not getattr(s, "languages", ()) or langs & set(s.languages)
        ]
        if not kept or len(kept) == len(sources):
            return list(sources)
        return kept

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
        # 标记「这个源正在下载正文」：检索不会给它换上检索专用客户端，
        # 万一已经被换上了也立刻换回共享默认客户端，保证下载的重试行为不变。
        with self._fetch_lock:
            self._fetching.add(src.id)
            if getattr(getattr(src, "_client", None), "is_search_client", False):
                src._client = None
        try:
            content = src.fetch(book, progress)
        finally:
            with self._fetch_lock:
                self._fetching.discard(src.id)
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
