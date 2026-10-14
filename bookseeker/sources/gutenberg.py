"""Project Gutenberg 书源（古腾堡计划，全球最早的公版电子书站）。

检索走 **Gutendex** JSON API（元数据干净的 JSON，比抓 HTML 稳得多）；正文优先下载
``<id>.epub.noimages``（体积小、章节结构完整，实测约 560KB），失败再退回纯文本
（``.txt.utf-8`` / ``cache/epub/<id>/pg<id>.txt``），手工切掉版权头尾后按章节标题切分。
只依赖标准库与 BookSeeker 内部模块。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from ..convert import htmlutil
from ..epubread import read_epub
from ..models import Book, BookContent, Chapter
from .base import Source, SourceError

API = "https://gutendex.com/books"                      # Gutendex 检索接口
EBOOK = "https://www.gutenberg.org/ebooks/%s"           # 详情页（给人看的）
# 无图 EPUB：体积小（~600KB）；绝对不要用 .epub.images，实测 24MB / 360 秒
EPUB_NOIMAGES = "https://www.gutenberg.org/ebooks/%s.epub.noimages"
TXT_UTF8 = "https://www.gutenberg.org/ebooks/%s.txt.utf-8"
TXT_CACHE = "https://www.gutenberg.org/cache/epub/%s/pg%s.txt"
PAGE_SIZE = 32            # Gutendex 每页条数
MAX_PAGES = 20            # 最多翻几页，避免关键字太空时无限翻页
SEARCH_TIMEOUT = 120.0    # 中日韩关键字实测要 40~100 秒，超时给足
FETCH_TIMEOUT = 90.0      # 正文下载超时（EPUB ~600KB，纯文本可能 2MB+）

# 版权声明头尾标记（英文原文，大小写不敏感）
_START_RES = (
    re.compile(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*", re.I),
    re.compile(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG ETEXT.*?\*\*\*", re.I),
)
_END_RES = (
    re.compile(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*", re.I),
    re.compile(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG ETEXT.*?\*\*\*", re.I),
)
# 章节标题：英文 CHAPTER / PART / BOOK / LETTER / ACT / SCENE ...，中文「第一回 / 第 3 章 / 卷三」
_CHAPTER_RE = re.compile(
    r"^(?:"
    r"(?:chapter|part|book|letter|act|scene|section|canto|volume|"
    r"prologue|epilogue|introduction|preface|foreword|appendix|conclusion)\b"
    r"|第\s*[0-9０-９一二三四五六七八九十百千〇零两]+\s*[章回節节卷篇部話话]"
    r"|[0-9]+\s*[章回節节]"
    r")",
    re.I,
)
_MIN_CHAPTER_CHARS = 40   # 太短的「章节」多半是插图说明或残留页眉，丢弃
# formats 里 media-type -> 统一格式名（顺序即优先级）
_FORMAT_PRIORITY = (
    ("application/epub+zip", "epub"),
    ("application/x-mobipocket-ebook", "mobi"),
    ("text/html", "html"),
    ("text/plain", "txt"),
)


def _clean_lang(value: Any) -> str:
    """语言代码统一成小写两字母：``zh-TW`` -> ``zh``。"""
    text = str(value or "").strip().replace("_", "-").lower()
    if not text:
        return ""
    base = text.split("-", 1)[0]
    return base if len(base) == 2 else text

def _text(value: Any) -> str:
    """把可能是 str / list / dict 的字段拍平成一段文字（summaries 有时是列表）。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return " ".join(t for t in (_text(v) for v in value) if t).strip()
    return "" if value is None else str(value).strip()

def _year_from_authors(authors: list[dict]) -> str:
    """尽量从作者生卒年推一个年份：优先卒年，其次生年；推不出返回空串。"""
    for key in ("death_year", "birth_year"):
        for author in authors:
            if not isinstance(author, dict):
                continue
            value = author.get(key)
            if isinstance(value, int) and 1000 <= value <= 2100:
                return str(value)
            if isinstance(value, str) and value.strip().isdigit():
                return value.strip()
    return ""
def _truncate(text: str, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"
def _why(exc: BaseException) -> str:
    """把异常整理成一行便于汇总的说明。"""
    return "%s: %s" % (type(exc).__name__, exc)


class GutenbergSource(Source):
    """Project Gutenberg 书源：Gutendex 检索 + noimages EPUB / 纯文本正文。"""

    id = "gutenberg"
    name = "Project Gutenberg"
    homepage = "https://www.gutenberg.org"
    description = "古腾堡计划：7 万余本公版电子书，以英/法/德文为主，EPUB 制作精良"
    languages = ("en", "fr", "de", "zh", "ja", "es", "it", "pt", "nl", "fi")
    supports = ("epub", "txt", "html")
    priority = 10
    enabled_by_default = True
    requires_network = True
    # -- 检索 ---------------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> list[Book]:
        """用 Gutendex API 检索，按页翻到取够 ``limit`` 条为止。"""
        query = (query or "").strip()
        if not query:
            return []
        limit = max(1, int(limit or 20))
        out: list[Book] = []
        page = 1
        while len(out) < limit and page <= MAX_PAGES:
            url = "%s?search=%s&page=%d" % (API, self.quote(query), page)
            try:
                data = self.get_json(url, timeout=SEARCH_TIMEOUT)
            except Exception as exc:
                # 单次请求失败不能让整个 search 崩溃：已拿到的结果先返回
                if out:
                    break
                raise SourceError("访问 Gutendex 失败：%s" % _why(exc)) from exc
            if not isinstance(data, dict):
                break
            results = data.get("results") or []
            if not isinstance(results, list):
                break
            for item in results:
                if isinstance(item, dict):
                    book = self._book_from_item(item)
                    if book is not None:
                        out.append(book)
                if len(out) >= limit:
                    break
            if not results or not data.get("next"):
                break
            page += 1
        return out[:limit]

    def _book_from_item(self, item: dict) -> Book | None:
        """把 Gutendex 的一条 result 转成 :class:`Book`。"""
        uid = str(item.get("id") or "").strip()
        title = _text(item.get("title"))
        if not uid or not title:
            return None

        raw_authors = [a for a in (item.get("authors") or []) if isinstance(a, dict)]
        authors: list[str] = []
        for author in raw_authors:
            name = _text(author.get("name"))
            if name and name not in authors:
                authors.append(name)

        languages = item.get("languages") or []
        subjects = [s for s in (_text(x) for x in (item.get("subjects") or [])) if s]
        shelves = [s for s in (_text(x) for x in (item.get("bookshelves") or [])) if s]

        # summaries 是可选字段，可能是列表、字符串或干脆不存在
        summaries = item.get("summaries") or []
        description = _text([s for s in summaries if isinstance(s, str)]) \
            if isinstance(summaries, (list, tuple)) else _text(summaries)
        if not description:
            description = "；".join(subjects[:5])

        raw_formats = item.get("formats")
        formats = (
            {str(k): str(v) for k, v in raw_formats.items() if v}
            if isinstance(raw_formats, dict) else {}
        )
        try:
            downloads = int(item.get("download_count") or 0)
        except (TypeError, ValueError):
            downloads = 0

        # 原始 formats 留给 fetch（找纯文本兜底地址）与 fetch_cover 用
        extra: dict[str, Any] = {
            "gutendex": {
                "id": uid,
                "formats": formats,
                "bookshelves": shelves,
                "download_count": downloads,
                "summaries": summaries if isinstance(summaries, (list, tuple)) else [],
            },
            "download_count": downloads,
        }
        if len(languages) > 1:
            extra["languages"] = [c for c in (_clean_lang(x) for x in languages) if c]

        return self.make_book(
            uid, title,
            authors=authors,
            language=_clean_lang(languages[0]) if languages else "",
            year=_year_from_authors(raw_authors),
            description=_truncate(description, 500),
            subjects=subjects,
            url=EBOOK % uid,
            download_urls=self._download_urls(formats),
            extra=extra,
        )

    @staticmethod
    def _download_urls(formats: dict) -> dict[str, str]:
        """把 Gutendex 的 formats 整理成 ``{"epub": ..., "txt": ..., "html": ...}``。"""
        out: dict[str, str] = {}
        for media, url in sorted(formats.items()):
            low = media.lower()
            for prefix, name in _FORMAT_PRIORITY:
                if not low.startswith(prefix) or name in out:
                    continue
                if name == "txt" and "utf-8" not in low and "us-ascii" not in low:
                    continue  # 只认 UTF-8 / ASCII 纯文本，其它编码容易乱码
                out[name] = url
                break
        return out

    # -- 正文 ---------------------------------------------------------------
    def fetch(self, book: Book, progress: Callable[[int, int, str], None] | None = None) -> BookContent:
        """下载正文：优先 noimages EPUB，失败退回纯文本并按章节标题切分。"""
        uid = str(book.uid or "").strip()
        if not uid:
            raise SourceError("缺少书籍 id，无法下载：%r" % (book.title,))
        errors: list[str] = []
        # 路线 1：无图 EPUB（体积小、章节结构最完整）
        epub_url = EPUB_NOIMAGES % uid
        self.report(progress, 1, 4, "下载 EPUB：%s" % epub_url)
        try:
            data = self.get(epub_url, timeout=FETCH_TIMEOUT).data or b""
            if len(data) < 1024 or not data.startswith(b"PK"):
                raise SourceError("返回内容不是 EPUB（%d 字节）" % len(data))
            self.report(progress, 2, 4, "解析 EPUB（%d KB）" % (len(data) // 1024))
            content = read_epub(data)
            if not content.chapters or content.word_count <= 0:
                raise SourceError("EPUB 解析后没有正文")
            self._finish(content, book, epub_url)
            self.report(progress, 4, 4, "完成：%d 章" % len(content.chapters))
            return content
        except Exception as exc:
            errors.append("EPUB %s：%s" % (epub_url, _why(exc)))
        # 路线 2：纯文本兜底
        for txt_url in self._text_urls(book, uid):
            self.report(progress, 2, 4, "改下载纯文本：%s" % txt_url)
            try:
                raw = self.get(txt_url, timeout=FETCH_TIMEOUT).text()
                if len(raw) < 500:
                    raise SourceError("文本过短（%d 字符）" % len(raw))
                body = self._strip_boilerplate(raw)
                if len(body) < 200:
                    raise SourceError("切掉版权声明后正文过短")
                self.report(progress, 3, 4, "按章节切分（%d 字符）" % len(body))
                content = self._content_from_text(book, body, txt_url)
                self._finish(content, book, txt_url)
                self.report(progress, 4, 4, "完成：%d 章" % len(content.chapters))
                return content
            except Exception as exc:
                errors.append("纯文本 %s：%s" % (txt_url, _why(exc)))

        raise SourceError(
            "无法获取《%s》正文（id=%s）。尝试过的地址：%s"
            % (book.title or "?", uid, "；".join(errors) or "无")
        )

    def _fill_titles(self, chapters: list[Chapter]) -> None:
        """给没有标题的章节补一个（古腾堡的中文 EPUB 常常没有 h1 标题）。"""
        for chapter in chapters:
            if chapter.title or not chapter.html:
                continue
            for line in htmlutil.html_to_text(chapter.html).split("\n"):
                line = line.strip()
                if line:
                    if len(line) <= 60:
                        chapter.title = line
                    break
    def _finish(self, content: BookContent, book: Book, url: str) -> None:
        """补齐书源元数据，保证上层拿到的内容信息完整。"""
        self._fill_titles(content.chapters)
        content.source_name = self.name
        content.source_url = url or book.url
        if not content.title:
            content.title = book.title
        if not content.authors:
            content.authors = list(book.authors)
        if not content.language:
            content.language = book.language
        if not content.description:
            content.description = book.description
        if not content.subjects:
            content.subjects = list(book.subjects)
        if not content.year:
            content.year = book.year
        if not content.identifier:
            content.identifier = "gutenberg:%s" % book.uid
    def _text_urls(self, book: Book, uid: str) -> list[str]:
        """按优先级列出纯文本候选地址（Gutendex 优先，其次自己拼）。"""
        urls: list[str] = []
        formats = ((book.extra or {}).get("gutendex") or {}).get("formats") or {}
        if isinstance(formats, dict):
            plain = sorted(
                ((str(m).lower(), u) for m, u in formats.items()
                 if str(m).lower().startswith("text/plain") and u),
                key=lambda kv: 0 if "utf-8" in kv[0] else 1,
            )
            urls.extend(u for _m, u in plain)
        dl = book.download_urls if isinstance(book.download_urls, dict) else {}
        for candidate in (dl.get("txt", ""), TXT_UTF8 % uid, TXT_CACHE % (uid, uid)):
            if candidate and candidate not in urls:
                urls.append(candidate)
        return urls

    @staticmethod
    def _strip_boilerplate(raw: str) -> str:
        """切掉古腾堡纯文本头尾的版权声明（``*** START/END OF ... ***``）。"""
        text = (raw or "").replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
        starts = [m.end() for rx in _START_RES for m in [rx.search(text)] if m]
        if starts:
            text = text[min(starts):]
        ends = [m.start() for rx in _END_RES for m in [rx.search(text)] if m]
        if ends:
            text = text[:max(ends)]
        return text.strip("\n \t")
    def _content_from_text(self, book: Book, body: str, url: str) -> BookContent:
        """把纯文本按章节标题切分，每章转成 XHTML 片段。"""
        chapters = self._split_chapters(body)
        if len(chapters) <= 1:  # 切不出章节：整本做成单章
            chapters = [Chapter(title=book.title or "正文", html=htmlutil.text_to_xhtml(body))]
        return BookContent(
            title=book.title,
            authors=list(book.authors),
            language=book.language,
            chapters=chapters,
            identifier="gutenberg:%s" % book.uid,
            source_name=self.name,
            source_url=url,
            description=book.description,
            subjects=list(book.subjects),
            year=book.year,
        )

    @staticmethod
    def _split_chapters(body: str) -> list[Chapter]:
        """按章节标题行切分纯文本；切不出来就返回单章（由调用方兜底）。"""
        blocks: list[tuple[str, list[str]]] = []
        title = ""
        current: list[str] = []
        for line in body.split("\n"):
            stripped = line.strip()
            is_head = (
                stripped and len(stripped) <= 90 and len(current) > 3
                and _CHAPTER_RE.match(stripped) is not None
            )
            if is_head:
                blocks.append((title, current))
                title, current = stripped, [line.rstrip()]
            else:
                current.append(line.rstrip())
        blocks.append((title, current))

        chapters: list[Chapter] = []
        for head, lines in blocks:
            text = "\n".join(lines)
            if not text.strip():
                continue
            if chapters and len(re.sub(r"\s+", "", text)) < _MIN_CHAPTER_CHARS:
                continue
            html = htmlutil.text_to_xhtml(text)
            if html:
                chapters.append(Chapter(title=head.strip(), html=html))
        return chapters

    # -- 封面 ---------------------------------------------------------------

    def fetch_cover(self, book: Book) -> bytes | None:
        """用 Gutendex 的 ``image/jpeg`` 地址抓封面（可选实现）。"""
        formats = ((book.extra or {}).get("gutendex") or {}).get("formats") or {}
        if not isinstance(formats, dict):
            return None
        images = sorted(
            (str(u) for m, u in formats.items()
             if str(m).lower().startswith("image/") and u),
            key=lambda u: 0 if "small" in u.lower() else (1 if "medium" in u.lower() else 2),
        )
        for url in images[:2]:  # 小图优先，省流量
            try:
                data = self.get(url, timeout=40).data or b""
                if len(data) > 256:
                    return data
            except Exception:
                continue
        return None


#: 供注册表按 ``SOURCES`` 约定收集
SOURCES = (GutenbergSource,)

__all__ = ["GutenbergSource", "SOURCES"]
