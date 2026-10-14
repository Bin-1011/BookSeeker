"""书源基类 —— 内置书源和你自己写的插件都继承这个类。

写一个书源通常只需要实现两个方法：

* ``search(query, limit)`` 返回一批 :class:`~bookseeker.models.Book`（只含元数据）
* ``fetch(book, progress)`` 返回 :class:`~bookseeker.models.BookContent`（正文）

最小示例见 ``examples/example_source.py``。
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Callable, Sequence

from .. import config as config_mod
from .. import http as http_mod
from ..models import Book, BookContent, normalize_cjk

#: 进度回调：``(已完成, 总数, 说明文字)``
ProgressFn = Callable[[int, int, str], None]


class SourceError(Exception):
    """书源在抓取过程中出错（会被上层捕获并展示给用户）。"""


class Source:
    """所有书源的基类。"""

    #: 唯一 id（英文小写，用于配置和调试），子类必须覆盖
    id: str = ""
    #: 界面上显示的名字
    name: str = ""
    #: 站点主页
    homepage: str = ""
    #: 一句话说明，会显示在界面的书源列表里
    description: str = ""
    #: 该源主要提供哪些语言，例如 ("zh",) 或 ("ja",)
    languages: tuple[str, ...] = ()
    #: 该源能产出的导出格式
    supports: tuple[str, ...] = ("epub", "txt", "html")
    #: 排序权重，数字越小越靠前
    priority: int = 100
    #: 是否默认启用
    enabled_by_default: bool = True
    #: 是否需要联网（纯本地索引的源可以设为 False）
    requires_network: bool = True

    def __init__(self, client: http_mod.HttpClient | None = None, cfg: dict | None = None) -> None:
        self._client = client
        self.cfg = cfg if cfg is not None else config_mod.CONFIG

    # ------------------------------------------------------------------
    # 子类必须实现
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> list[Book]:
        """按关键字检索，返回带元数据的 :class:`Book` 列表。"""
        raise NotImplementedError

    def fetch(self, book: Book, progress: ProgressFn | None = None) -> BookContent:
        """下载并解析 ``book`` 的正文。"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 可选覆盖
    # ------------------------------------------------------------------

    def fetch_cover(self, book: Book) -> bytes | None:
        """尝试抓取封面图，返回图片字节；拿不到就返回 ``None``。"""
        return None

    def warmup(self, progress: ProgressFn | None = None) -> None:
        """可选：预热本地索引（例如下载一次性的目录文件）。"""
        return None

    # ------------------------------------------------------------------
    # 给子类用的小工具
    # ------------------------------------------------------------------

    @property
    def http(self) -> http_mod.HttpClient:
        """共享的 HTTP 客户端（带缓存、重试、限速）。"""
        return self._client if self._client is not None else http_mod.default

    def get(self, url: str, **kw: Any) -> http_mod.Response:
        return self.http.request(url, **kw)

    def get_text(self, url: str, encoding: str | None = None, **kw: Any) -> str:
        return self.http.get_text(url, encoding=encoding, **kw)

    def get_json(self, url: str, **kw: Any) -> Any:
        return self.http.get_json(url, **kw)

    @staticmethod
    def absolute(base: str, url: str) -> str:
        """把相对链接补全成绝对链接。"""
        return urllib.parse.urljoin(base, url or "")

    @staticmethod
    def quote(text: str) -> str:
        return urllib.parse.quote(text or "", safe="")

    def make_book(self, uid: str, title: str, **kwargs: Any) -> Book:
        """创建 :class:`Book`，自动填好 ``source`` 与 ``source_name``。"""
        book = Book(source=self.id, uid=str(uid), title=(title or "").strip(), **kwargs)
        book.source_name = self.name or self.id
        return book

    def report(self, progress: ProgressFn | None, done: int, total: int, message: str) -> None:
        if progress is not None:
            try:
                progress(done, total, message)
            except Exception:
                pass

    def __repr__(self) -> str:  # pragma: no cover
        return "<Source %s (%s)>" % (self.id, self.name)


def dedupe_books(books: Sequence[Book]) -> list[Book]:
    """按 (source, uid) 去重，保持原顺序。"""
    seen: set[str] = set()
    out: list[Book] = []
    for b in books:
        if b.key in seen:
            continue
        seen.add(b.key)
        out.append(b)
    return out


def relevance(book: Book, query: str) -> int:
    """给检索结果打分，用于排序（越大越相关）。"""
    q = normalize_cjk((query or "").strip().lower())
    if not q:
        return 0
    # 简繁折叠后再比较，否则「红楼梦」会把繁体的《紅樓夢》判成完全无关
    title = normalize_cjk((book.title or "").lower())
    score = 0
    if title == q:
        score += 200
    elif title.startswith(q):
        score += 120
    elif q in title:
        score += 80
    for token in q.split():
        if token and token in title:
            score += 20
        if token and any(token in normalize_cjk(a.lower()) for a in book.authors):
            score += 12
    if book.description and normalize_cjk(book.description.lower()).find(q) >= 0:
        score += 4
    return score


def rank_books(books: Sequence[Book], query: str) -> list[Book]:
    """按相关度排序，其次按书源 id 和书名。"""
    return sorted(books, key=lambda b: (-relevance(b, query), b.source, b.title))
