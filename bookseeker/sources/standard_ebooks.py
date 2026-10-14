# -*- coding: utf-8 -*-
"""Standard Ebooks 书源（网页抓取）。

`Standard Ebooks <https://standardebooks.org>`_ 是一个志愿项目，专门把公版
英文书重新精校、精排，产出质量很高的 EPUB，全部免费且无版权限制。

**为什么走网页而不是官方 OPDS**：站点的 ``/feeds/opds/all`` 目前返回
401 Unauthorized，``/feeds/atom/all`` 也不是随时可用，所以这里稳定地走
网页检索页 ``/ebooks?query=<关键字>&per-page=<n>``。

两个实测踩到的坑，写在这里免得后人再踩：

1. **下载中转页**。直接 GET ``/downloads/xxx.epub`` 时，站点会先回一个
   ``text/html`` 的「Your Download Has Started!」中转页（带 ``meta
   refresh``），而不是 EPUB 本体。请求时带上查询串 ``?source=download``
   才能直接拿到 ``application/epub+zip``；万一还是中转页，就解析
   ``meta refresh`` 再跟一次。
2. **slug 可能是 3 段**。有译者的书详情页形如
   ``/ebooks/<作者slug>/<书名slug>/<译者slug>``，所以 uid / 详情页链接
   不能假设只有两段；下载文件名则是把 slug 各段用 ``_`` 连起来。

站点还有一个 ``/honeypot`` 蜜罐链接（跟着访问会被封 IP 24 小时），
本模块只认 ``/ebooks/<...>/<...>`` 形式的路径，绝不会碰到它。
"""

from __future__ import annotations

import re
import urllib.parse

from .. import htmldom
from ..convert.htmlutil import html_to_text
from ..epubread import read_epub
from ..models import Book, BookContent
from .base import ProgressFn, Source, SourceError

#: 站点根地址
BASE = "https://standardebooks.org"
#: 检索页
SEARCH_URL = BASE + "/ebooks"

#: 详情页路径：``/ebooks/<作者slug>/<书名slug>[/<译者slug>]``
_SLUG_RE = re.compile(r"^/ebooks/([^/?#]+)/([^/?#]+)(?:/([^/?#]+))?/?$")
#: 第三段里这些不是「译者 slug」而是功能目录，要排除
_NOT_A_SLUG = frozenset({"downloads", "download", "images", "files", "feed"})
#: 站点每页结果数只认这几个档位，取最接近且不小于 limit 的
_PER_PAGE_STEPS = (12, 24, 48, 96)
#: 单本 EPUB 体积上限：超过就放弃，避免大文件把界面卡死
MAX_EPUB_BYTES = 25 * 1024 * 1024
#: 从 ``meta refresh`` 的 content 里抠出目标地址
_REFRESH_URL_RE = re.compile(r"url\s*=\s*['\"]?([^'\";>\s]+)", re.I)


class StandardEbooksSource(Source):
    """Standard Ebooks：公版英文书的精校电子书，检索走网页，正文取 EPUB。"""

    id = "standard_ebooks"
    name = "Standard Ebooks"
    homepage = "https://standardebooks.org"
    description = "精校精排的公版英文电子书（EPUB，英文）"
    languages = ("en",)
    supports = ("epub", "txt", "html")
    priority = 20

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> list[Book]:
        """按关键字检索。任何异常都不向外抛，最坏情况返回空列表。"""
        q = (query or "").strip()
        if not q:
            return []
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 20
        if limit <= 0:
            limit = 20

        url = "%s?query=%s&per-page=%d" % (
            SEARCH_URL, urllib.parse.quote(q, safe=""), self._per_page(limit),
        )
        try:
            html = self.get_text(url)
        except Exception:
            return []
        if not html:
            return []

        try:
            return self._parse_list(html, limit)[:limit]
        except Exception:
            return []

    @staticmethod
    def _per_page(limit: int) -> int:
        """把用户要的条数映射成站点支持的 per-page 档位。"""
        for step in _PER_PAGE_STEPS:
            if limit <= step:
                return step
        return _PER_PAGE_STEPS[-1]

    def _parse_list(self, html: str, limit: int) -> list[Book]:
        """从检索结果页抽取书籍列表；解析不出来的条目直接跳过，绝不抛异常。"""
        doc = htmldom.parse(html)
        # 第一条是当前站点的真实结构，后两条是站点改版时的兜底
        items = (
            doc.find_all("ol.ebooks-list > li")
            or doc.find_all("li[about]")
            or doc.find_all("ol > li")
        )
        out: list[Book] = []
        seen: set[str] = set()
        for item in items:
            try:
                book = self._parse_item(item)
            except Exception:
                continue
            if book is None or book.uid in seen:
                continue
            seen.add(book.uid)
            out.append(book)
            if len(out) >= max(1, limit):
                break
        return out

    def _parse_item(self, item) -> Book | None:
        """解析单个 ``<li>``；拿不到 slug 就返回 ``None``。"""
        # ---- 详情页链接 / slug ----
        slug = ""
        for a in item.find_all("a[href]"):
            raw = (a.get("href") or "").strip()
            if not raw:
                continue
            slug = self._slug_of(urllib.parse.urlsplit(raw).path)
            if slug:
                break
        if not slug:
            # 有些版本把详情页写在列表项的 about 属性上
            slug = self._slug_of(urllib.parse.urlsplit(item.get("about") or "").path)
        if not slug:
            return None

        # ---- 书名：优先不属于作者段落的 schema:name ----
        title = ""
        for span in item.find_all("span[property='schema:name']"):
            if self._has_ancestor_class(span, "author", item):
                continue
            title = span.text(strip=True)
            if title:
                break
        if not title:
            node = item.find("p")
            title = node.text(strip=True) if node is not None else ""
        if not title:
            return None

        # ---- 作者：可能有多位，逐个收集 ----
        authors: list[str] = []
        paras = item.find_all("p.author") or item.find_all("[property='schema:author']")
        for p in paras:
            names = [s.text(strip=True) for s in p.find_all("span[property='schema:name']")]
            names = [n for n in names if n]
            if not names:
                text = p.text(strip=True)
                names = [text] if text else []
            for name in names:
                if name not in authors:
                    authors.append(name)

        return self.make_book(
            uid=slug,
            title=title,
            authors=authors,
            language="en",
            url=self._detail_url(slug),
        )

    @staticmethod
    def _slug_of(path: str) -> str:
        """把 ``/ebooks/a/b[/c]`` 形式的路径还原成 slug，不是书籍页则返回空串。"""
        m = _SLUG_RE.match(path or "")
        if not m:
            return ""
        if m.group(3) and m.group(3).lower() in _NOT_A_SLUG:
            return ""
        return "/".join(part for part in m.groups() if part)

    @staticmethod
    def _has_ancestor_class(node, cls: str, stop) -> bool:
        """判断 ``node`` 到 ``stop``（不含）之间的祖先里有没有 class ``cls``。"""
        parent = getattr(node, "parent", None)
        while parent is not None and parent is not stop:
            if cls in getattr(parent, "classes", ()):
                return True
            parent = getattr(parent, "parent", None)
        return False

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    def fetch(self, book: Book, progress: ProgressFn | None = None) -> BookContent:
        """抓详情页 → 找 EPUB → 下载 → 解析成 :class:`BookContent`。"""
        detail = (book.url or "").strip() or self._detail_url(book.uid)
        if not detail:
            raise SourceError("这本书缺少详情页地址：%r" % (book.uid,))

        self.report(progress, 0, 3, "读取详情页")
        try:
            html = self.get_text(detail)
        except Exception as exc:
            raise SourceError("访问详情页失败：%s（%s）" % (detail, exc)) from exc
        if not html:
            raise SourceError("详情页内容为空：%s" % detail)

        epub_url = self._find_epub_url(html, detail, book.uid)
        if not epub_url:
            raise SourceError("详情页里找不到 EPUB 下载链接：%s" % detail)

        self.report(progress, 1, 3, "下载 EPUB")
        data = self._download_epub(epub_url, detail)
        if not data:
            raise SourceError("EPUB 下载失败（可能被中转页拦截）：%s" % epub_url)
        if len(data) > MAX_EPUB_BYTES:
            raise SourceError(
                "EPUB 体积过大（%.1f MB，上限 %d MB），已放弃下载"
                % (len(data) / 1048576.0, MAX_EPUB_BYTES // 1048576)
            )

        self.report(progress, 2, 3, "解析 EPUB")
        try:
            content = read_epub(data)
        except Exception as exc:
            raise SourceError("EPUB 解析失败：%s" % exc) from exc

        # ---- 用详情页补全元数据 ----
        content.source_name = self.name
        content.source_url = detail
        if not content.title:
            content.title = book.title
        if not content.authors:
            content.authors = list(book.authors)
        if not content.language:
            content.language = book.language or "en"
        desc = self._parse_description(html)
        if desc:
            content.description = desc
        self.report(progress, 3, 3, "完成")
        return content

    @staticmethod
    def _detail_url(uid: str) -> str:
        """slug → 详情页绝对地址；slug 不足两段则返回空串。"""
        parts = [p for p in (uid or "").strip().strip("/").split("/") if p]
        if len(parts) < 2:
            return ""
        return BASE + "/ebooks/" + "/".join(parts)

    def _find_epub_url(self, html: str, page_url: str, uid: str = "") -> str:
        """在详情页里找出最合适的 EPUB 下载链接。

        优先 ``#download`` 区里的「Compatible epub」，其次页内其它 ``.epub``
        （``kepub`` 与 ``advanced`` 版本排在后面），都找不到才按 slug 拼一个。
        """
        doc = htmldom.parse(html)
        ranked: list[tuple[int, str]] = []
        for scope in (doc.find("section#download"), doc.find("div.downloads-container"), doc):
            if scope is None:
                continue
            for a in scope.find_all("a[href]"):
                href = (a.get("href") or "").strip()
                low = href.lower().split("?", 1)[0]
                if not low.endswith(".epub") or "/downloads/" not in low:
                    continue
                score = 0
                if "epub" in (a.get("class") or "").lower().split():
                    score -= 2          # 站点的「Compatible epub」带 class="epub"
                if ".kepub." in low:
                    score += 4          # Kobo 专用，最后考虑
                if "_advanced.epub" in low:
                    score += 1          # 高级排版版，内容一样但更花哨
                ranked.append((score, href))
            if ranked:
                break
        if ranked:
            ranked.sort(key=lambda pair: pair[0])
            return self.absolute(page_url, ranked[0][1])

        # 最后手段：按实测规律拼一个（slug 至少两段时才拼）
        detail = self._detail_url(uid)
        if detail:
            name = "_".join(detail[len(BASE + "/ebooks/"):].split("/"))
            return "%s/downloads/%s.epub" % (detail, name)
        return ""

    def _download_epub(self, url: str, referer: str) -> bytes:
        """下载 EPUB 字节；遇到「下载已开始」中转页就跟一次真实地址。"""
        target = self._with_source_download(url)
        headers = {
            "Referer": referer or BASE,
            "Accept": "application/epub+zip,application/octet-stream,*/*;q=0.8",
        }
        for _attempt in range(3):
            try:
                resp = self.get(target, headers=headers, use_cache=False)
            except Exception as exc:
                raise SourceError("下载 EPUB 出错：%s（%s）" % (target, exc)) from exc
            data = resp.data or b""
            if data[:2] == b"PK":        # ZIP 魔数，就是 EPUB 本体
                return data
            nxt = self._refresh_target(data, resp.url or target)
            if not nxt or nxt == target:
                return b""
            target = nxt
        return b""

    @staticmethod
    def _with_source_download(url: str) -> str:
        """给下载地址补上 ``?source=download``，绕开站点中转页。"""
        try:
            parts = urllib.parse.urlsplit(url)
            query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
            query["source"] = ["download"]
            new = parts._replace(query=urllib.parse.urlencode(query, doseq=True))
            return urllib.parse.urlunsplit(new)
        except Exception:
            return url + ("&" if "?" in url else "?") + "source=download"

    @staticmethod
    def _refresh_target(data: bytes, base: str) -> str:
        """从中转页 HTML 的 ``meta refresh`` 里取出真实下载地址。"""
        if not data:
            return ""
        head = data[:8192].decode("utf-8", "replace")
        doc = htmldom.parse(head)
        node = doc.find("meta[http-equiv=refresh]") or doc.find("meta[http-equiv=Refresh]")
        content = (node.get("content") or "") if node is not None else head
        m = _REFRESH_URL_RE.search(content)
        if not m:
            return ""
        return urllib.parse.urljoin(base, m.group(1).strip())

    @staticmethod
    def _parse_description(html: str) -> str:
        """取详情页的简介段落并转成纯文本；取不到返回空串。"""
        doc = htmldom.parse(html)
        node = (
            doc.find("section#description div[property='schema:description']")
            or doc.find("div[property='schema:description']")
        )
        if node is not None:
            text = html_to_text(node)
            if text:
                return text
        # 兜底：用 meta 里的简介
        for sel in ("meta[property='schema:abstract']", "meta[name=description]"):
            node = doc.find(sel)
            if node is not None:
                text = (node.get("content") or "").strip()
                if text:
                    return text
        return ""


#: 显式声明一下（注册表也会按模块里的 Source 子类自动发现）
SOURCES = (StandardEbooksSource,)
