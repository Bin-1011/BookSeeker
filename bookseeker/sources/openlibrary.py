"""Open Library（openlibrary.org）书源。

Open Library 自己只提供**书目元数据**，但它的 ``ia`` 字段指向 Internet Archive
上对应的扫描件，所以 :meth:`OpenLibrarySource.fetch` 会把下载工作**委托给
Internet Archive 书源**（:class:`~bookseeker.sources.archive_org.ArchiveOrgSource`），
没有 ``ia`` 标识符的条目则明确报错。

重要：本机到 ``openlibrary.org`` 的 TLS 握手可能失败
（``SSL: UNEXPECTED_EOF_WHILE_READING``），因此这里所有网络调用都必须
「安静地失败」——只返回空列表并记下原因，绝不向外抛异常、也不打印堆栈，
以免一个源拖垮整个检索流程。
"""

from __future__ import annotations

import ssl
import urllib.parse

from ..models import Book, BookContent
from .archive_org import ArchiveOrgSource
from .base import ProgressFn, Source, SourceError

#: 三字母 → 两字母语言映射（与 Internet Archive 保持一致）
_LANG_MAP = {
    "eng": "en",
    "chi": "zh",
    "zho": "zh",
    "jpn": "ja",
}

#: ``ebook_access`` → 中文说明
_ACCESS_TEXT = {
    "public": "开放全文（可直接下载）",
    "borrowable": "仅可借阅（无开放全文）",
    "printdisabled": "仅限无障碍借阅",
    "no_ebook": "无电子版",
}


def _lang2(value) -> str:
    """把 ``language`` 字段（通常是列表）映射成两字母代码。"""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    text = str(value or "").strip()
    return _LANG_MAP.get(text.lower(), text)


def _str_list(value) -> list[str]:
    """把任意字段值统一成去空的字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_str_list(item))
        return out
    text = str(value).strip()
    return [text] if text else []


class OpenLibrarySource(Source):
    """Open Library 书源（书目元数据 + 委托 Internet Archive 下载全文）。"""

    id = "openlibrary"
    name = "Open Library"
    homepage = "https://openlibrary.org"
    description = "Open Library 书目库，可联动 Internet Archive 下载开放全文"
    languages = ("en", "zh", "ja")
    supports = ("epub", "txt", "html")
    priority = 60
    # Open Library 只提供书目元数据，且它的 API 在部分网络环境下 TLS 握手会失败
    # （本机实测稳定复现 SSL: UNEXPECTED_EOF_WHILE_READING），每次检索要白等十几秒。
    # 所以默认关闭，想要的人在「工具 → 书源管理」里双击打开即可。
    enabled_by_default = False

    SEARCH_URL = "https://openlibrary.org/search.json"
    FIELDS = "key,title,author_name,first_publish_year,language,ia,ebook_access"

    def __init__(self, client=None, cfg=None) -> None:
        super().__init__(client, cfg)
        #: 最近一次检索失败的原因（给界面 / 调试用，不抛给上层）
        self.last_error = ""

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> list[Book]:
        """检索书目；网络（含 TLS）异常时安静地返回空列表。"""
        self.last_error = ""
        query = (query or "").strip()
        if not query:
            return []
        try:
            rows = max(1, min(int(limit or 20), 100))
        except (TypeError, ValueError):
            rows = 20
        params = urllib.parse.urlencode(
            {"q": query, "limit": str(rows), "fields": self.FIELDS}
        )
        try:
            data = self.get_json(self.SEARCH_URL + "?" + params)
        except ssl.SSLError as exc:
            self.last_error = "TLS 握手失败：%s" % exc
            return []
        except Exception as exc:  # HttpError / 超时 / JSON 解析失败 …
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            return []
        try:
            return self._parse_docs(data, rows)
        except Exception as exc:
            self.last_error = "解析检索结果失败：%s: %s" % (type(exc).__name__, exc)
            return []

    def _parse_docs(self, data, limit: int) -> list[Book]:
        """把 ``{"docs": [...]}`` 转成 :class:`Book` 列表。"""
        if not isinstance(data, dict):
            return []
        docs = data.get("docs")
        if not isinstance(docs, list):
            return []
        books: list[Book] = []
        seen: set[str] = set()
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            book = self._doc_to_book(doc)
            if book is None or book.uid in seen:
                continue
            seen.add(book.uid)
            books.append(book)
            if len(books) >= limit:
                break
        return books

    def _doc_to_book(self, doc: dict) -> Book | None:
        key = str(doc.get("key") or "").strip()
        if not key:
            return None
        title = str(doc.get("title") or "").strip() or key
        year = str(doc.get("first_publish_year") or "").strip()
        access = str(doc.get("ebook_access") or "").strip()
        ia_ids = _str_list(doc.get("ia"))
        description = _ACCESS_TEXT.get(access, "")
        return self.make_book(
            uid=key,
            title=title,
            authors=_str_list(doc.get("author_name")),
            language=_lang2(doc.get("language")),
            year=year,
            description=description,
            url=self.homepage.rstrip("/") + key,
            extra={"ia": ia_ids, "ebook_access": access, "key": key},
        )

    # ------------------------------------------------------------------
    # 下载（委托给 Internet Archive）
    # ------------------------------------------------------------------

    def _ia_book(self, book: Book, identifier: str) -> Book:
        """按 ``identifier`` 造一个临时的 Internet Archive 书籍对象。"""
        temp = Book(
            source=ArchiveOrgSource.id,
            uid=identifier,
            title=book.title,
            authors=list(book.authors),
            language=book.language,
            year=book.year,
            url="%s/details/%s" % (ArchiveOrgSource.homepage, urllib.parse.quote(identifier, safe="")),
        )
        temp.extra["identifier"] = identifier
        return temp

    def fetch(self, book: Book, progress: ProgressFn | None = None) -> BookContent:
        """用第一个 ``ia`` 标识符到 Internet Archive 下载全文。"""
        ia_ids = _str_list(book.extra.get("ia"))
        if not ia_ids:
            raise SourceError(
                "Open Library 只提供书目元数据，这本书没有可下载的开放全文"
            )
        identifier = ia_ids[0]
        source = ArchiveOrgSource(client=self._client, cfg=self.cfg)
        try:
            content = source.fetch(self._ia_book(book, identifier), progress)
        except SourceError:
            raise
        except ssl.SSLError as exc:
            raise SourceError("连接 Internet Archive 时 TLS 握手失败：%s" % exc) from None
        except Exception as exc:
            raise SourceError(
                "从 Internet Archive 下载 %s 失败：%s" % (identifier, exc)
            ) from None
        if content is None:
            raise SourceError("Internet Archive 没有返回 %s 的正文" % identifier)
        content.source_name = self.name
        content.source_url = content.source_url or book.url
        if not content.title:
            content.title = book.title
        return content

    def fetch_cover(self, book: Book) -> bytes | None:
        """有 ``ia`` 标识符时，顺带用 Internet Archive 的封面图。"""
        ia_ids = _str_list(book.extra.get("ia"))
        if not ia_ids:
            return None
        try:
            source = ArchiveOrgSource(client=self._client, cfg=self.cfg)
            return source.fetch_cover(self._ia_book(book, ia_ids[0]))
        except Exception:
            return None
