"""首页「热门书籍」的精选书单。

首页要的是「一打开就有东西看」，所以不用网络发现，直接用人工挑选的书单。
每条只记录 (书名, 作者, 书源, 检索词, 分类)；点开时才去对应书源检索并定位到具体那本书。

挑选标准：公有领域 + 知名度高 + 在对应书源里确实能搜到（都已实测）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .models import Book


@dataclass
class HotBook:
    title: str              # 展示用书名
    author: str             # 展示用作者
    source: str             # 书源 id
    query: str              # 在该书源里检索用的关键字
    category: str           # 分类
    prefer: str = ""        # 结果里优先匹配的书名片段（默认用 title）

    def match_key(self) -> str:
        return self.prefer or self.title


#: 首页书单。顺序即展示顺序，分类用来做筛选。
HOT_BOOKS: tuple[HotBook, ...] = (
    # ---------------- 中国古典 ----------------
    HotBook("红楼梦", "曹雪芹", "wikisource", "红楼梦", "中国古典"),
    HotBook("三国演义", "罗贯中", "wikisource", "三国演义", "中国古典"),
    HotBook("西游记", "吴承恩", "wikisource", "西游记", "中国古典"),
    HotBook("水浒传", "施耐庵", "wikisource", "水浒传", "中国古典"),
    HotBook("聊斋志异", "蒲松龄", "wikisource", "聊斋志异", "中国古典"),
    HotBook("儒林外史", "吴敬梓", "wikisource", "儒林外史", "中国古典"),
    HotBook("封神演义", "许仲琳", "wikisource", "封神演义", "中国古典"),
    HotBook("镜花缘", "李汝珍", "wikisource", "镜花缘", "中国古典"),
    # ---------------- 中文典籍 ----------------
    HotBook("论语", "孔门弟子", "wikisource", "论语", "中文典籍"),
    HotBook("道德经", "老子", "wikisource", "道德经", "中文典籍"),
    HotBook("史记", "司马迁", "wikisource", "史记", "中文典籍"),
    HotBook("唐诗三百首", "蘅塘退士 编", "wikisource", "唐诗三百首", "中文典籍"),
    # ---------------- 日本文学 ----------------
    HotBook("坊っちゃん", "夏目漱石", "aozora", "坊っちゃん", "日本文学"),
    HotBook("こころ", "夏目漱石", "aozora", "こころ", "日本文学"),
    HotBook("吾輩は猫である", "夏目漱石", "aozora", "吾輩は猫である", "日本文学"),
    HotBook("走れメロス", "太宰治", "aozora", "走れメロス", "日本文学"),
    HotBook("人間失格", "太宰治", "aozora", "人間失格", "日本文学"),
    HotBook("羅生門", "芥川龍之介", "aozora", "羅生門", "日本文学"),
    HotBook("銀河鉄道の夜", "宮沢賢治", "aozora", "銀河鉄道の夜", "日本文学"),
    HotBook("注文の多い料理店", "宮沢賢治", "aozora", "注文の多い料理店", "日本文学"),
    HotBook("山月記", "中島敦", "aozora", "山月記", "日本文学"),
    HotBook("伊豆の踊子", "川端康成", "aozora", "伊豆の踊子", "日本文学"),
    # ---------------- 世界名著 ----------------
    HotBook("Pride and Prejudice", "Jane Austen", "gutenberg", "pride and prejudice", "世界名著"),
    HotBook("Moby-Dick", "Herman Melville", "gutenberg", "moby dick", "世界名著"),
    HotBook("Alice's Adventures in Wonderland", "Lewis Carroll", "gutenberg",
            "alice adventures wonderland", "世界名著"),
    HotBook("Frankenstein", "Mary Shelley", "gutenberg", "frankenstein", "世界名著"),
    HotBook("Dracula", "Bram Stoker", "gutenberg", "dracula", "世界名著"),
    HotBook("A Tale of Two Cities", "Charles Dickens", "gutenberg", "tale of two cities", "世界名著"),
    # ---------------- 侦探与科幻 ----------------
    HotBook("The Adventures of Sherlock Holmes", "Arthur Conan Doyle",
            "standard_ebooks", "sherlock", "侦探科幻"),
    HotBook("The Hound of the Baskervilles", "Arthur Conan Doyle",
            "standard_ebooks", "hound baskervilles", "侦探科幻"),
    HotBook("The Time Machine", "H. G. Wells", "standard_ebooks", "time machine", "侦探科幻"),
    HotBook("The War of the Worlds", "H. G. Wells", "standard_ebooks", "war of the worlds", "侦探科幻"),
    HotBook("Treasure Island", "Robert Louis Stevenson", "standard_ebooks", "treasure island", "侦探科幻"),
)

#: 分类顺序（首页筛选条）
CATEGORIES: tuple[str, ...] = (
    "全部", "中国古典", "中文典籍", "日本文学", "世界名著", "侦探科幻",
)


def books_in(category: str) -> list[HotBook]:
    if not category or category == "全部":
        return list(HOT_BOOKS)
    return [b for b in HOT_BOOKS if b.category == category]


# --------------------------------------------------------------------------
# 解析结果的磁盘缓存
#
# 首页**不在启动时联网解析书单**——33 本书就是 33 次检索请求，会把 HTTP 限速
# 额度吃光，用户真正发起的搜索反而被拖慢。所以只在点击某本书时才去解析，
# 结果落到这里，下次启动直接读。
# --------------------------------------------------------------------------

CACHE_TTL = 7 * 86400


def _cache_path() -> Path:
    from . import config

    return Path(config.HOME) / "hot_cache.json"


def load_cache() -> dict[str, Book]:
    """读缓存（过期或损坏就当空）。"""
    import json
    import time

    path = _cache_path()
    try:
        if not path.is_file():
            return {}
        if time.time() - path.stat().st_mtime > CACHE_TTL:
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, Book] = {}
    for title, item in (raw.get("books") or {}).items():
        try:
            out[title] = Book(
                source=item.get("source", ""),
                uid=item.get("uid", ""),
                title=item.get("title", ""),
                authors=list(item.get("authors") or []),
                language=item.get("language", ""),
                year=item.get("year", ""),
                description=item.get("description", ""),
                subjects=list(item.get("subjects") or []),
                url=item.get("url", ""),
                download_urls=dict(item.get("download_urls") or {}),
                extra=dict(item.get("extra") or {}),
            )
            out[title].source_name = item.get("source_name", "")
        except Exception:
            continue
    return out


def save_cache(books: dict[str, Book]) -> None:
    import json

    from . import config

    try:
        config.ensure_dirs()
        data = {"books": {}}
        for title, b in books.items():
            data["books"][title] = {
                "source": b.source, "source_name": b.source_name, "uid": b.uid,
                "title": b.title, "authors": b.authors, "language": b.language,
                "year": b.year, "description": b.description, "subjects": b.subjects,
                "url": b.url, "download_urls": b.download_urls, "extra": b.extra,
            }
        _cache_path().write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    except Exception:
        pass


def pick_best(books: list[Book], hot: HotBook) -> Book | None:
    """在检索结果里挑出最像目标的那一本（简繁归一化后比较书名）。"""
    if not books:
        return None
    from .models import normalize_cjk

    want = normalize_cjk(hot.match_key()).strip().lower()
    exact = [b for b in books if normalize_cjk(b.title or "").strip().lower() == want]
    if exact:
        return exact[0]
    starts = [b for b in books
              if normalize_cjk(b.title or "").strip().lower().startswith(want)]
    if starts:
        return starts[0]
    contains = [b for b in books if want and want in normalize_cjk(b.title or "").lower()]
    if contains:
        return contains[0]
    return books[0]


def resolve(registry, hot: HotBook, timeout: float = 20.0) -> Book | None:
    """到对应书源里检索一次，定位到具体那本书。失败返回 None。"""
    source = registry.get(hot.source)
    if source is None:
        return None
    try:
        books = source.search(hot.query, 8)
    except Exception:
        return None
    return pick_best(list(books or []), hot)


def resolve_all(registry, entries: list[HotBook],
                on_one: Callable[[HotBook, Book | None], None] | None = None,
                should_stop: Callable[[], bool] | None = None) -> dict[str, Book]:
    """逐条解析书单（串行，避免一次性打爆书源）。用于后台预热。"""
    from concurrent.futures import ThreadPoolExecutor

    out: dict[str, Book] = {}

    def one(hot: HotBook):
        if should_stop and should_stop():
            return hot, None
        return hot, resolve(registry, hot)

    # 同一书源的条目串行、不同书源并行，兼顾速度和礼貌
    groups: dict[str, list[HotBook]] = {}
    for hot in entries:
        groups.setdefault(hot.source, []).append(hot)

    def run_group(items: list[HotBook]):
        for hot in items:
            if should_stop and should_stop():
                return
            book = resolve(registry, hot)
            if book is not None:
                out[hot.title] = book
            if on_one:
                try:
                    on_one(hot, book)
                except Exception:
                    pass

    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="bs-hot") as pool:
        list(pool.map(run_group, groups.values()))
    return out
