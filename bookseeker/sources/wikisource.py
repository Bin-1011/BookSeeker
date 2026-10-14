"""维基文库（Wikisource）书源。

维基文库是维基媒体基金会旗下的公版原文站点，按语言分成多个子域：
``zh``（红楼梦、论语…）、``ja``（坊っちゃん、こころ…）、``en``（Moby-Dick…）。

只使用标准库，通过 MediaWiki 的 ``api.php`` 检索与取正文：检索用
``action=query&list=search``（带 ``snippet`` 摘要），正文用
``action=parse&prop=text&formatversion=2``（返回渲染后的 HTML）。

长篇作品通常拆成「母页面 + 子页面」，例如 ``紅樓夢/第001回``。所以 ``fetch()``
会先判断母页面是不是目录页：正文里若出现 **≥3 个** ``<页面标题>/xxx`` 形式的
子页面链接，就按链接顺序逐章抓取；否则把整页当作单篇处理。
"""

from __future__ import annotations

import random
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..convert import htmlutil
from ..convert.htmlutil import escape_xml
from ..models import Book, BookContent, Chapter, normalize_cjk
from .base import ProgressFn, Source, SourceError

#: 本模块支持的语言子域
LANGS: tuple[str, ...] = ("zh", "ja", "en")
#: api.php / 详情页地址模板
API_TMPL = "https://{lang}.wikisource.org/w/api.php"
PAGE_TMPL = "https://{lang}.wikisource.org/wiki/{title}"

#: 检索时每个子域各一个线程；抓正文适度并发（http 客户端已在同域限速）
SEARCH_WORKERS = len(LANGS)
FETCH_WORKERS = 3
#: 单章失败后最多再试几次，以及退避基数（秒）——MediaWiki 会返回 429
FETCH_RETRIES = 3
RETRY_BACKOFF = 2.5
#: 并发第一遍后仍有缺章时，串行慢速补抓的轮数（可在配置里用
#: ``wikisource_recover_rounds`` 覆盖）与每次补抓前的等待（秒）。
#: 轮与轮之间还会额外等待，且等待时间逐轮递增。
RECOVER_ROUNDS = 3
RECOVER_DELAY = 4.0
#: 轮间等待的基数（秒）：第 N 轮结束后等待 ``max(1.0, RECOVER_DELAY / 4) * N``
RECOVER_ROUND_BASE = max(1.0, RECOVER_DELAY / 4)

#: 判定目录页的子页面数量阈值 / 单本最多抓多少章
TOC_MIN_LINKS = 3
MAX_CHAPTERS = 300

#: 匹配「页面标题/子页面」链接时，跳过这些命名空间
_NS_BLACKLIST = (
    "special:", "talk:", "user:", "user talk:", "template:", "template talk:",
    "help:", "category:", "portal:", "wikisource:", "author:", "index:",
    "page:", "translation:", "module:", "mediawiki:", "file:", "book:",
)

#: 从正文里认作者的小规则（中日文按「作者：」这类标记找，英文抓 Author: 链接）
_AUTHOR_JA_RE = re.compile(r"(?:作者|作詞|作词|著者)\s*[：:]\s*([^\n（(]{1,40})")
_AUTHOR_EN_RE = re.compile(r"\bby\s+([A-Z][\w.\-']*(?:\s+[A-Z][\w.\-']*){0,3})")
_AUTHOR_LINK_NS = ("author:", "作者:")


# ------------------------------- 小工具 -----------------------------------


def _clean_text(html_or_text: str) -> str:
    """把一小段 HTML 摘要转成干净的纯文本（去标签、解实体、压缩空白）。"""
    raw = html_or_text or ""
    if "<" in raw:
        raw = htmlutil.html_to_text(raw, keep_indent=False)
    else:
        raw = raw.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", raw).strip()


def _page_url(lang: str, title: str) -> str:
    """拼出页面的可读地址（保留中文，空格转下划线）。"""
    path = urllib.parse.quote((title or "").replace(" ", "_"), safe="/:()!,")
    return PAGE_TMPL.format(lang=lang, title=path)


def _wiki_title(href: str) -> str:
    """把正文里的 ``/wiki/xxx`` 链接还原成页面标题。"""
    href = href or ""
    raw = href[6:].split("#", 1)[0] if href.startswith("/wiki/") else ""
    title = urllib.parse.unquote(raw).replace("_", " ")
    return re.sub(r"\s+", " ", title).strip()


def _api(lang: str, params: dict[str, Any]) -> str:
    """组一个 api.php 的 GET 地址。"""
    query = {"format": "json", **params}
    return API_TMPL.format(lang=lang) + "?" + urllib.parse.urlencode(query)


def _parse_text(payload: Any) -> dict[str, Any]:
    """从 ``action=parse`` 返回里取出 ``{"title":…, "html":…}``；兼容 formatversion 1/2。"""
    node = payload.get("parse") if isinstance(payload, dict) else None
    if not isinstance(node, dict):
        return {}
    text = node.get("text")
    if isinstance(text, dict):
        text = text.get("*") or ""
    if not isinstance(text, str):
        text = ""
    return {"title": (node.get("title") or "").strip(), "html": text}


def _subpage_title(title: str) -> str:
    """取子页面标题的最后一段，例如 ``紅樓夢/第001回`` -> ``第001回``。"""
    tail = (title or "").rsplit("/", 1)[-1].strip()
    return tail or (title or "").strip()


def _missing_placeholder(title: str) -> str:
    """给抓失败的章节生成占位正文（良构 XHTML，标题仍是真实章标题）。

    绝不静默丢章：宁可留一段明确的占位说明，也不让用户拿到一本
    少了章节却毫无提示的电子书。
    """
    name = escape_xml((title or "").strip() or "本章")
    return (
        '<p class="no-indent">⚠ 本章（%s）本次抓取失败，通常是站点限流导致的。'
        "已成功抓取的章节会缓存在本地，稍后重新下载这本书时不会重复下载。</p>" % name
    )


class WikisourceSource(Source):
    """维基文库：多语言公版原文，支持目录页（母页面 + 子页面）自动展开。"""

    id = "wikisource"
    name = "维基文库 Wikisource"
    homepage = "https://zh.wikisource.org"
    description = "维基文库公版原文（中文 / 日文 / 英文），长篇小说自动按子页面分章"
    languages = ("zh", "ja", "en")
    priority = 30
    supports = ("epub", "txt", "html")
    enabled_by_default = True
    requires_network = True

    # ---------------- 检索 ----------------

    def search(self, query: str, limit: int = 20) -> list[Book]:
        """在 zh / ja / en 三个子域里同时检索，单语言失败不影响其它语言。

        任何异常都不会向外抛：整体失败时返回 ``[]``。
        """
        query = (query or "").strip()
        if not query:
            return []
        try:
            per_lang = max(1, min(int(limit or 20), 50))
        except (TypeError, ValueError):
            per_lang = 20

        results = self._search_all_langs(query, per_lang)
        books: list[Book] = []
        seen: set[str] = set()
        for lang, items in results:
            for item in items:
                book = self._make_search_book(lang, item)
                if book is None or book.key in seen:
                    continue
                seen.add(book.key)
                books.append(book)
        return self._drop_redundant_subpages(books)

    @staticmethod
    def _drop_redundant_subpages(books: list[Book]) -> list[Book]:
        """母页面已经在结果里时丢掉它的子页面。

        全文检索会把「紅樓夢/第001回」当成独立结果显示，而 ``fetch()`` 抓母页面时
        会自动把子页面拼成整本书，所以它们是纯噪声。比对前做繁简归一化，否则
        「後紅樓夢」和「后红楼梦/第01回」会被当成两回事。
        """
        def page_of(book: Book) -> str:
            return book.uid.split(":", 1)[-1]

        parents = {"%s:%s" % (b.language, normalize_cjk(page_of(b)))
                   for b in books if "/" not in page_of(b)}
        out: list[Book] = []
        for book in books:
            page = page_of(book)
            if "/" in page:
                root = "%s:%s" % (book.language, normalize_cjk(page.split("/", 1)[0]))
                if root in parents:
                    continue
            out.append(book)
        return out
    def _search_all_langs(self, query: str, limit: int) -> list[tuple[str, list[dict]]]:
        """并发检索所有语言子域，返回 ``[(lang, 结果), …]``；单语言失败只丢该语言。"""

        def one(lang: str) -> tuple[str, list[dict]]:
            try:
                return lang, self._search_lang(lang, query, limit)
            except Exception:
                return lang, []

        out: list[tuple[str, list[dict]]] = []
        with ThreadPoolExecutor(max_workers=SEARCH_WORKERS) as pool:
            futures = [(lang, pool.submit(one, lang)) for lang in LANGS]
            for lang, fut in futures:
                try:
                    out.append(fut.result())
                except Exception:  # pragma: no cover - 防御性
                    out.append((lang, []))
        return out

    def _search_lang(self, lang: str, query: str, limit: int) -> list[dict]:
        """检索单个语言子域。"""
        url = _api(lang, {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": max(1, min(int(limit), 50)),
            "srnamespace": 0,
        })
        payload = self.get_json(url)
        if not isinstance(payload, dict):
            return []
        block = payload.get("query")
        if not isinstance(block, dict):
            return []
        items = block.get("search")
        return [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []

    def _make_search_book(self, lang: str, item: dict) -> Book | None:
        """把一条 ``list=search`` 结果转成 :class:`Book`。"""
        title = (item.get("title") or "").strip()
        if not title:
            return None
        return self.make_book(
            uid="%s:%s" % (lang, title),
            title=title,
            language=lang,
            url=_page_url(lang, title),
            description=_clean_text(item.get("snippet") or ""),
            extra={
                "lang": lang,
                "page": title,
                "pageid": item.get("pageid") or "",
                "size": item.get("size") or 0,
                "wordcount": item.get("wordcount") or 0,
            },
        )

    # ----------------------------- 正文 -----------------------------

    def fetch(self, book: Book, progress: ProgressFn | None = None) -> BookContent:
        """抓取正文：目录页展开成一章一个子页面，否则整页作为单篇。"""
        lang, page = self._resolve_target(book)
        self.report(progress, 0, 1, "获取页面：%s" % page)
        # 母页面也走同一套重试逻辑；这里要留未清洗的 DOM，才能认出目录页链接
        real_title, node = self._fetch_node(lang, page)
        if node is None:
            raise SourceError("维基文库页面取不到正文：%s:%s" % (lang, page))
        title = real_title or page
        authors = self._guess_authors(node)
        subpages = self._find_subpages(node, title)

        if len(subpages) >= TOC_MIN_LINKS:
            chapters, missed, truncated = self._fetch_chapters(lang, [t for t, _x in subpages],
                                                               progress)
        else:
            missed = truncated = 0
            chapters = [Chapter(title=title, html=htmlutil.sanitize_to_xhtml(node))]

        chapters = [c for c in chapters if c.title or c.html.strip()]
        if not chapters:
            raise SourceError("维基文库页面没有可用的正文：%s:%s" % (lang, page))

        content = BookContent(
            title=title, authors=authors, language=lang, chapters=chapters,
            identifier="%s:%s" % (lang, title), source_name=self.name,
            source_url=_page_url(lang, page),
            description=self._make_description(node, subpages, missed, truncated, book),
            missing_chapters=missed, truncated_chapters=truncated,
        )
        self.report(progress, len(chapters), len(chapters), "完成：共 %d 章" % len(chapters))
        return content

    def _resolve_target(self, book: Book) -> tuple[str, str]:
        """从 ``book.extra`` / ``uid`` 里解析出 ``(语言, 页面标题)``。"""
        extra = book.extra or {}
        lang = str(extra.get("lang") or "").strip().lower()
        page = str(extra.get("page") or "").strip()
        uid = str(book.uid or "")
        if (not lang or not page) and ":" in uid:
            head, tail = uid.split(":", 1)
            if not lang:
                lang = head.strip().lower()
            if not page:
                page = tail.strip()
        if not page:
            page = (book.title or "").strip()
        if lang not in LANGS:
            lang = "zh"
        if not page:
            raise SourceError("维基文库书源缺少页面标题：%r" % (book.uid or book.title))
        return lang, page

    def _find_subpages(self, node, base_title: str) -> list[tuple[str, str]]:
        """在正文里找 ``<页面标题>/xxx`` 形式的子页面链接。

        返回 ``[(子页面完整标题, 链接文字), …]``，按链接出现顺序去重。
        """
        prefix = (base_title or "").strip() + "/"
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for link in node.find_all("a[href]"):
            title = _wiki_title(link.get("href"))
            if not title.startswith(prefix):
                continue
            suffix = title[len(prefix):]
            if not suffix or "/" in suffix.rstrip("/"):
                continue
            low = title.lower()
            if any(low.startswith(ns) for ns in _NS_BLACKLIST) or title in seen:
                continue
            seen.add(title)
            out.append((title, _clean_text(link.text(strip=True))))
        return out

    def _recover_rounds(self) -> int:
        """慢速补抓轮数：默认 :data:`RECOVER_ROUNDS`，可用配置覆盖。

        配置项 ``wikisource_recover_rounds`` 允许 0～8 之间的整数；
        缺项、非数字、布尔值、超范围一律退回默认值，绝不抛异常。
        """
        raw = None
        try:
            raw = (self.cfg or {}).get("wikisource_recover_rounds")
        except Exception:  # pragma: no cover - cfg 不是映射时退默认
            raw = None
        if raw is None or isinstance(raw, bool):
            return RECOVER_ROUNDS
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return RECOVER_ROUNDS
        return max(0, min(value, 8))

    def _fetch_chapters(
        self, lang: str, titles: list[str], progress: ProgressFn | None
    ) -> tuple[list[Chapter], int, int]:
        """抓取各章，返回 ``(章节列表, 抓失败的章数, 被截断的章数)``。

        先并发抓一遍（快），缺的再串行、慢速补抓（限速时并发反而更糟），
        轮与轮之间的等待逐轮递增。**所有章节都按原顺序保留位置**：
        最终仍抓不到的那几章用 :func:`_missing_placeholder` 的占位内容代替，
        所以章数永远等于目录里的章数，缺章数量另行返回给上层。
        """
        truncated = max(0, len(titles) - MAX_CHAPTERS)
        titles = titles[:MAX_CHAPTERS]
        total = len(titles)
        self.report(progress, 0, total, "目录页：共 %d 章，开始抓取" % total)

        # 第一遍：并发
        bodies: dict[int, str] = {}
        done = 0
        with ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, max(total, 1))) as pool:
            futures = {pool.submit(self._fetch_one, lang, t): (i, _subpage_title(t))
                       for i, t in enumerate(titles)}
            for fut, (idx, name) in futures.items():
                try:
                    _real, body = fut.result()
                except Exception:
                    body = ""
                done += 1
                if body:
                    bodies[idx] = body
                self.report(progress, done, total, "已抓取 %d/%d：%s" % (done, total, name))

        # 第二遍：串行慢速补抓；每章之前等一会儿，每轮之后等更久（递增）
        rounds = self._recover_rounds()
        for round_no in range(rounds):
            missing = [i for i in range(total) if i not in bodies]
            if not missing:
                break
            self.report(progress, done, total,
                        "还有 %d 章没抓到，第 %d/%d 轮慢速重试"
                        % (len(missing), round_no + 1, rounds))
            for idx in missing:
                time.sleep(RECOVER_DELAY)
                try:
                    _real, body = self._fetch_one(lang, titles[idx])
                except Exception:
                    body = ""
                if body:
                    bodies[idx] = body
                    self.report(progress, done, total,
                                "慢速重试成功：%s" % _subpage_title(titles[idx]))
            # 轮间等待逐轮递增，给站点限流留出恢复时间
            time.sleep(RECOVER_ROUND_BASE * (round_no + 1))

        missed = total - len(bodies)
        if missed:
            self.report(progress, done, total,
                        "有 %d 章最终没抓到，已用占位内容保留章节位置" % missed)

        # 关键：按目录顺序输出每一章，抓失败的章用占位内容顶上，绝不丢章
        chapters = [
            Chapter(title=_subpage_title(titles[i]),
                    html=bodies[i] if i in bodies else _missing_placeholder(
                        _subpage_title(titles[i])))
            for i in range(total)
        ]
        return chapters, missed, truncated

    def _fetch_one(self, lang: str, title: str) -> tuple[str, str]:
        """抓单个页面，返回 ``(真实标题, 清洗后的 XHTML)``；拿不到正文时返回空串。"""
        real, node = self._fetch_node(lang, title)
        if node is None:
            return real, ""
        return real, self._strip_nav(htmlutil.sanitize_to_xhtml(node))

    def _fetch_node(self, lang: str, title: str) -> tuple[str, Any]:
        """抓页面并返回未清洗的 DOM 节点。

        找子页面链接必须用原始 DOM：清洗成 XHTML 时站内相对链接会被丢掉。
        维基媒体对高频访问会返回 429，所以取不到正文时退避后重试几次。
        """
        url = _api(lang, {"action": "parse", "page": title, "prop": "text",
                          "formatversion": 2, "redirects": 1})
        for attempt in range(FETCH_RETRIES):
            parsed: dict[str, Any] = {}
            try:
                parsed = _parse_text(self.get_json(url))
            except Exception:
                parsed = {}
            if parsed.get("html"):
                break
            if attempt < FETCH_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (attempt + 1) + random.uniform(0, 1.0))
        html = parsed.get("html") or ""
        if not html:
            return title, None
        node = htmlutil.pick_content(html, "div.mw-parser-output", "#mw-content-text",
                                     "div#bodyContent")
        return parsed.get("title") or title, node

    @staticmethod
    def _strip_nav(body: str) -> str:
        """去掉正文里残留的导航块（版本信息、姊妹项目、页脚）。"""
        body = body or ""
        for pattern in (
            r'<table id="headerContainer".*?</table>',
            r'<div id="headerContainer".*?</div>\s*',
            r'<div id="(?:ws-data|footer|catlinks)".*?</div>\s*',
        ):
            body = re.sub(pattern, "", body, flags=re.S)
        return body.strip()

    def _make_description(self, node, subpages: list[tuple[str, str]], missed: int,
                          truncated: int, book: Book) -> str:
        """拼一段说明：简介 + 目录章数 + 缺章 / 截断提示（失败时退回检索摘要）。"""
        intro = (book.description or "").strip()
        try:
            for para in node.find_all("p"):
                text = _clean_text(para.text(strip=True))
                if len(text) >= 40:
                    intro = text
                    break
        except Exception:
            pass
        parts = [intro[:300]] if intro else []
        parts.append("维基文库，目录共 %d 章。" % len(subpages))
        if missed:
            parts.append("其中 %d 章本次抓取失败，正文里已用占位说明标出"
                         "（可能是站点限流，稍后重新下载可补齐）。" % missed)
        if truncated:
            parts.append("章节超过上限 %d，已截断后 %d 章。" % (MAX_CHAPTERS, truncated))
        return " ".join(p for p in parts if p)

    def _guess_authors(self, node) -> list[str]:
        """尽量从页面首段/信息框里猜作者，猜不到就返回空列表。"""
        names: list[str] = []
        try:
            for link in node.find_all("a[href]"):
                title = _wiki_title(link.get("href"))
                if not any(title.lower().startswith(ns) for ns in _AUTHOR_LINK_NS):
                    continue
                name = title.split(":", 1)[-1].strip()
                if name and name not in names:
                    names.append(name)
                if len(names) >= 3:
                    return names
        except Exception:
            pass
        if names:
            return names

        try:
            head = " ".join(_clean_text(n.outer_html()) for n in list(node.find_all("table"))[:2])
        except Exception:
            head = ""
        for regex in (_AUTHOR_JA_RE, _AUTHOR_EN_RE):
            m = regex.search(head or "")
            if m:
                name = re.sub(r"\s+", " ", m.group(1)).strip(" 　,，、;；")
                if 1 < len(name) <= 40:
                    return [name]
        return []


#: 便于注册表 / 插件显式引用
SOURCES = [WikisourceSource]
