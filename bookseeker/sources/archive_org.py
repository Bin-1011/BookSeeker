"""Internet Archive（archive.org）书源。

Internet Archive 的扫描书分成两类：

* **开放获取**（公有领域等）：附带 OCR 全文（``*_djvu.txt``），可直接下载导出；
* **借阅制**：只有扫描图片，:meth:`ArchiveOrgSource.fetch` 会抛
  说明清楚的 :class:`SourceError`。

分工：``search()`` 走 ``advancedsearch.php`` 检索元数据；``fetch()`` 先读
``/metadata/<identifier>`` 判断有没有开放全文，再下载纯文本并切分章节。
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from ..convert.htmlutil import html_to_text, text_to_xhtml
from ..models import Book, BookContent, Chapter
from .base import ProgressFn, Source, SourceError

#: Internet Archive 用 MARC 三字母语言代码，这里映射成项目内部的两字母代码
_LANG_MAP = {
    "eng": "en", "chi": "zh", "zho": "zh", "jpn": "ja", "fre": "fr", "fra": "fr",
    "ger": "de", "deu": "de", "spa": "es", "rus": "ru",
}

#: 全文文件大小上限（字节）：超过就放弃，避免把内存和磁盘撑爆
MAX_TEXT_BYTES = 20 * 1024 * 1024

#: Project Gutenberg 的版权头 / 尾标记
_PG_START_RE = re.compile(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*\n", re.I)
_PG_END_RE = re.compile(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*", re.I)

#: 整行出现的扫描 / 授权样板声明（命中即删除）
_BOILERPLATE_LINE_RES = (
    re.compile(r"^\s*this book made available by the internet archive\.?\s*$", re.I),
    re.compile(r"^\s*https?://(?:www\.)?archive\.org/\S*\s*$", re.I),
    re.compile(r"^\s*digitized by (?:google|the internet archive)\b.*$", re.I),
)

#: 正文之前若出现这些词，说明开头那段是扫描 / 授权声明而不是正文
_PREFIX_HINTS = ("internet archive", "digitized by", "digital copy", "made available by")

#: 英文章节标题（CHAPTER / PART / BOOK / LETTER / ACT / SCENE ...）
_EN_NUM = (r"[IVXLCDM]{1,8}|\d{1,4}|ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN"
           r"|ELEVEN|TWELVE|THIRTEEN|FOURTEEN|FIFTEEN|SIXTEEN|SEVENTEEN|EIGHTEEN"
           r"|NINETEEN|TWENTY")
_EN_HEAD_RE = re.compile(
    r"^(?:CHAPTER|CHAP|BOOK|PART|LETTER|ACT|SCENE|CANTO|SECTION|PROLOGUE|EPILOGUE)"
    r"(?:\s+(?:%s)\b\s*[.:\-–—]?\s*.{0,60})?$" % _EN_NUM, re.I)

#: 中文章节标题（第X回 / 第X章 / 第X节 ...）
_ZH_HEAD_RE = re.compile(
    r"^第\s*[0-9０-９〇零一二三四五六七八九十百千万两]{1,8}\s*[回章节卷篇部集]"
    r"\s*[、.:：\-—]?\s*.{0,50}$")


def _to_int(value: Any, default: int = 0) -> int:
    """宽松地把任意值转成 int（Internet Archive 的字段经常是字符串）。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[str]:
    """把 ``None`` / 字符串 / 列表 / 字典统一成去空的字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        found = next((value[k] for k in ("name", "title", "value") if k in value), None)
        return _as_list(found)
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_as_list(item))
        return out
    return [str(value).strip()] if str(value).strip() else []


def _lang2(value: Any) -> str:
    """三字母语言代码映射成两字母；其它情况原样保留。"""
    items = _as_list(value)
    if not items:
        return ""
    raw = items[0].strip()
    return _LANG_MAP.get(raw.lower(), raw)


def _clean_description(value: Any) -> str:
    """把 Internet Archive 的描述（可能是 HTML / 列表）转成一段纯文本。"""
    text = "\n".join(_as_list(value)).strip()
    return html_to_text(text).strip() if "<" in text else text


def _plausible_text(data: bytes) -> bool:
    """粗略判断下载到的字节是不是纯文本（排除 PDF / ZIP 等二进制）。"""
    head = data[:512]
    return bool(head) and not head.startswith((b"%PDF", b"PK\x03\x04")) and b"\x00" not in head


def _is_heading(line: str) -> bool:
    """判断一行是不是章节标题（兼顾中英文）。"""
    title = line.strip()
    if not title or len(title) > 96:
        return False
    return bool(_EN_HEAD_RE.match(title) or _ZH_HEAD_RE.match(title))


def _first_heading_offset(text: str) -> int:
    """返回第一个章节标题行的字符偏移，找不到返回 -1。"""
    pos = 0
    for line in text.split("\n"):
        if _is_heading(line):
            return pos
        pos += len(line) + 1
    return -1


def _strip_boilerplate(text: str) -> str:
    """切掉 Project Gutenberg / Internet Archive 的版权头尾与扫描声明。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    match = _PG_START_RE.search(text)
    if match:
        text = text[match.end():]
    match = _PG_END_RE.search(text)
    if match:
        text = text[: match.start()]
    keep = (ln for ln in text.split("\n")
            if not any(r.match(ln) for r in _BOILERPLATE_LINE_RES))
    text = "\n".join(keep)
    # 开头若是一段扫描 / 授权声明，且它后面确实跟着章节标题，就整段丢掉
    offset = _first_heading_offset(text)
    if 0 < offset <= 6000 and any(h in text[:offset].lower() for h in _PREFIX_HINTS):
        text = text[offset:]
    return text.strip("\n")


def _join_titles(pending: list[str], title: str) -> str:
    """把被并入的短标题合并成章节标题；过长时收敛（多半是目录页）。"""
    if not pending:
        return title[:90]
    if len(pending) >= 3:  # 连续多个「有标题没正文」的项 = 目录
        return "目录（%d 项）" % (len(pending) + 1)
    full = " / ".join(pending + [title])
    return full if len(full) <= 90 else full[:89] + "…"


def _split_chapters(text: str) -> list[tuple[str, str]]:
    """按章节标题切分全文，返回 ``[(标题, 正文), ...]``；切不出来返回空列表。"""
    lines = text.split("\n")
    marks: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        if not _is_heading(line):
            continue
        title = line.strip()
        if marks and marks[-1][1] == title:  # 同一标题重复出现，忽略
            continue
        marks.append((idx, title))
    if len(marks) < 2:
        return []

    blocks: list[tuple[str, str]] = []
    head = "\n".join(lines[: marks[0][0]]).strip()
    if len(re.sub(r"\s+", "", head)) >= 400:  # 序言 / 卷首之类
        blocks.append(("卷首", head))

    # 内容极短的标题（例如 "PART ONE" 后面紧跟 "CHAPTER I"）并入下一章标题
    pending: list[str] = []
    for i, (idx, title) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(lines)
        body = "\n".join(lines[idx + 1: end]).strip()
        if len(re.sub(r"\s+", "", body)) < 40 and i + 1 < len(marks):
            pending.append(title)
            continue
        blocks.append((_join_titles(pending, title), body))
        pending = []
    if pending:
        if not blocks:
            return []
        prev_title, prev_body = blocks[-1]
        blocks[-1] = (_join_titles(pending, prev_title), prev_body)
    return [(t, b) for t, b in blocks if re.sub(r"\s+", "", b)]


def _build_chapters(text: str) -> list[Chapter]:
    """把纯文本正文转成 :class:`Chapter` 列表（切不出章节就整体一章）。"""
    chapters = [Chapter(title=t, html=text_to_xhtml(body))
                for t, body in _split_chapters(text) if body.strip()]
    if not chapters:
        html = text_to_xhtml(text)
        if not html:
            raise SourceError("Internet Archive 返回的全文是空的")
        chapters = [Chapter(title="全文", html=html)]
    return chapters


def _pick_text_files(files: Any) -> list[tuple[str, int]]:
    """从 ``metadata.files`` 里挑出可用的纯文本全文，按优先级排序。

    第一优先级是名字以 ``_djvu.txt`` 结尾的文件，其次是 ``format`` 为
    ``DjVuTXT`` 或其它 ``.txt`` 的项。``Text PDF`` 虽然也是 OCR 结果，但载体
    仍是 PDF（二进制），导出没用，所以连同 ``.gz`` / ``.zip`` 一起跳过。
    """
    primary: list[tuple[str, int]] = []
    secondary: list[tuple[str, int]] = []
    seen: set[str] = set()
    for item in files or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        low = name.lower()
        if not name or low.endswith((".gz", ".zip", ".pdf")) or name in seen:
            continue
        fmt = str(item.get("format") or "").strip().lower()
        size = _to_int(item.get("size"), 0)
        seen.add(name)
        if low.endswith("_djvu.txt"):
            primary.append((name, size))
        elif fmt == "djvutxt" or low.endswith(".txt"):
            secondary.append((name, size))
    return primary + secondary


def _first_year(meta_info: dict) -> str:
    """从元数据里挑一个像年份的字段。"""
    keys = ("year", "date", "publicdate", "addeddate")
    blob = " ".join(str(meta_info.get(k) or "") for k in keys)
    match = re.search(r"(1[5-9]\d{2}|20\d{2})", blob)
    return match.group(1) if match else ""


class ArchiveOrgSource(Source):
    """Internet Archive（archive.org）书源。"""

    id = "archive_org"
    name = "Internet Archive"
    homepage = "https://archive.org"
    description = "Internet Archive 的扫描书库，开放获取书目可直接下载 OCR 全文"
    languages = ("en", "zh", "ja", "fr", "de", "es", "ru")
    priority = 40

    SEARCH_URL = "https://archive.org/advancedsearch.php"
    METADATA_URL = "https://archive.org/metadata/%s"
    DOWNLOAD_URL = "https://archive.org/download/%s/%s"
    DETAIL_URL = "https://archive.org/details/%s"
    COVER_URL = "https://archive.org/services/img/%s"

    # -- 检索 ---------------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> list[Book]:
        """按关键字检索文本类馆藏；任何异常都不向外抛，失败时返回空列表。"""
        try:
            return self._search(query, limit)
        except Exception:
            return []

    def _search(self, query: str, limit: int = 20) -> list[Book]:
        query = (query or "").strip()
        if not query:
            return []
        total = max(1, min(_to_int(limit, 20) or 20, 100))
        books: list[Book] = []
        seen: set[str] = set()
        for page in range(1, 6):  # 最多翻 5 页，取够 limit 条就停
            docs = self._search_page(query, min(total, 50), page)
            if not docs:
                break
            for doc in docs:
                book = self._doc_to_book(doc)
                if book is None or book.uid in seen:
                    continue
                seen.add(book.uid)
                books.append(book)
                if len(books) >= total:
                    return books
        return books

    def _search_page(self, query: str, rows: int, page: int) -> list[dict]:
        """请求一页检索结果，返回 ``response.docs``。"""
        pairs = [("q", "(%s) AND mediatype:texts" % query)]
        pairs += [("fl[]", f) for f in
                  ("identifier", "title", "creator", "language", "year", "mediatype")]
        pairs += [("rows", str(rows)), ("page", str(page)), ("output", "json"),
                  ("sort[]", "downloads desc")]
        data = self.get_json(self.SEARCH_URL + "?" + urllib.parse.urlencode(pairs))
        response = data.get("response") if isinstance(data, dict) else None
        docs = response.get("docs") if isinstance(response, dict) else None
        return [d for d in docs if isinstance(d, dict)] if isinstance(docs, list) else []

    def _doc_to_book(self, doc: dict) -> Book | None:
        identifier = str(doc.get("identifier") or "").strip()
        if not identifier:
            return None
        title = doc.get("title")
        if isinstance(title, list):
            title = title[0] if title else ""
        title = str(title or "").strip() or identifier
        year = _as_list(doc.get("year"))
        return self.make_book(
            uid=identifier, title=title, authors=_as_list(doc.get("creator")),
            language=_lang2(doc.get("language")), year=year[0] if year else "",
            url=self.DETAIL_URL % urllib.parse.quote(identifier, safe=""),
            extra={"identifier": identifier, "mediatype": str(doc.get("mediatype") or "")})

    # -- 下载 ---------------------------------------------------------------

    def fetch(self, book: Book, progress: ProgressFn | None = None) -> BookContent:
        """下载开放全文并切分章节；借阅制书目抛 :class:`SourceError`。"""
        ident = str(book.extra.get("identifier") or book.uid or "").strip()
        if not ident:
            raise SourceError("缺少 Internet Archive identifier，无法下载")

        self.report(progress, 0, 3, "读取 Internet Archive 元数据…")
        try:
            meta = self.get_json(self.METADATA_URL % urllib.parse.quote(ident, safe=""))
        except Exception as exc:
            raise SourceError("读取 Internet Archive 元数据失败：%s" % exc) from None
        if not isinstance(meta, dict):
            raise SourceError("Internet Archive 元数据格式异常（%s）" % ident)
        meta_info = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}

        candidates = _pick_text_files(meta.get("files"))
        if not candidates:
            raise SourceError("这本书在 Internet Archive 上不提供开放全文下载（可能是借阅制）")

        text, too_big, last_error = "", False, None
        for name, size in candidates:
            if size and size > MAX_TEXT_BYTES:
                too_big = True
                continue
            self.report(progress, 1, 3, "下载全文 %s…" % name)
            url = self.DOWNLOAD_URL % (
                urllib.parse.quote(ident, safe=""),
                # 有些条目的文件名带子目录（如 "Aogu/xxx_djvu.txt"），保留 "/"
                urllib.parse.quote(name, safe="/"))
            try:
                resp = self.get(url)
            except Exception as exc:
                last_error = exc
                continue
            if len(resp.data) > MAX_TEXT_BYTES:
                too_big = True
                continue
            if not _plausible_text(resp.data):
                last_error = "文件 %s 不是纯文本" % name
                continue
            text = resp.text()
            if text.strip():
                break
            last_error = "文件 %s 内容为空" % name

        if not text.strip():
            if too_big:
                raise SourceError(
                    "Internet Archive 全文文件过大（上限 %d 字节 / %d MB），已放弃下载"
                    % (MAX_TEXT_BYTES, MAX_TEXT_BYTES // (1024 * 1024)))
            raise SourceError(
                "这本书在 Internet Archive 上不提供开放全文下载（可能是借阅制）：%s"
                % (last_error or "未找到可用的纯文本文件"))

        self.report(progress, 2, 3, "切分章节…")
        chapters = _build_chapters(_strip_boilerplate(text))
        self.report(progress, 3, 3, "完成，共 %d 章" % len(chapters))

        return BookContent(
            title=book.title or str(meta_info.get("title") or ident),
            authors=list(book.authors) or _as_list(meta_info.get("creator")),
            language=book.language or _lang2(meta_info.get("language")),
            chapters=chapters,
            identifier=ident,
            source_name=self.name,
            source_url=book.url or (self.DETAIL_URL % urllib.parse.quote(ident, safe="")),
            description=_clean_description(meta_info.get("description")),
            subjects=_as_list(meta_info.get("subject")) or _as_list(meta_info.get("subjects")),
            year=book.year or _first_year(meta_info),
            publisher=", ".join(_as_list(meta_info.get("publisher"))),
            rights=str(meta_info.get("rights") or meta_info.get("licenseurl") or "").strip(),
            cover=self._cover_bytes(book))

    def _cover_bytes(self, book: Book) -> bytes | None:
        """内部用：拿封面图；``Content-Type`` 不是 ``image/*`` 就当没有。"""
        ident = str(book.extra.get("identifier") or book.uid or "").strip()
        if not ident:
            return None
        try:
            resp = self.get(self.COVER_URL % urllib.parse.quote(ident, safe=""))
        except Exception:
            return None
        ctype = (resp.content_type or "").split(";")[0].strip().lower()
        return resp.data if ctype.startswith("image/") and resp.data else None

    def fetch_cover(self, book: Book) -> bytes | None:
        """抓封面图，失败返回 ``None``。"""
        return self._cover_bytes(book)
