"""EPUB 读取器：把下载到的 .epub 解析成 :class:`BookContent`。

Project Gutenberg、Standard Ebooks 这类书源本身就提供制作精良的 EPUB，
直接下载再解析，比自己去啃纯文本、猜章节切分要可靠得多。
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from pathlib import Path

from . import htmldom
from .convert import htmlutil
from .models import BookContent, Chapter

_XHTML_TYPES = {"application/xhtml+xml", "text/html", "application/xml"}

#: 电子书里常见的"正文之外"的页面，读取时跳过
_SKIP_HREF_HINTS = (
    "cover", "titlepage", "title-page", "title_page", "copyright",
    "colophon", "halftitle", "half-title", "toc", "contents", "nav",
    "uncopyright", "imprint", "advert", "backmatter",
)

#: 章节标题的常见写法（英/中/日）
_CHAPTER_RE = re.compile(
    r"^\s*(?:"
    r"(?:chapter|part|book|letter|act|scene|section|canto|volume|"
    r"prologue|epilogue|introduction|preface|foreword|afterword|appendix|"
    r"conclusion|postscript)\b"
    r"|第\s*[0-9０-９一二三四五六七八九十百千〇零两]+\s*[章回節节卷篇部話话]"
    r"|[0-9]+\s*[章回節节]"
    r")",
    re.I,
)


def _clean_heading(text: str) -> str:
    """标题里混进插图说明时，把真正的章节名抠出来。

    例：``"I hope Mr. Bingley will like it. CHAPTER II."`` → ``"CHAPTER II."``
    """
    text = (text or "").strip()
    if not text:
        return text
    if _CHAPTER_RE.match(text):
        return text
    match = _CHAPTER_RE.search(text)
    if match and match.start() > 0:
        candidate = text[match.start():].strip()
        if 0 < len(candidate) <= 120:
            return candidate
    return text


def _local(tag: str) -> str:
    """去掉命名空间前缀：``{ns}title`` / ``dc:title`` 都还原成 ``title``。"""
    tag = (tag or "").rsplit("}", 1)[-1]
    return tag.rsplit(":", 1)[-1].lower()


def _text_of(elem) -> str:
    """取节点内的纯文本。"""
    try:
        return elem.text(strip=True)
    except Exception:
        return "".join(
            c for c in elem.children if isinstance(c, str)
        ).strip()


def _resolve(base_dir: str, href: str) -> str:
    href = (href or "").split("#", 1)[0]
    if not href:
        return ""
    if base_dir:
        return posixpath.normpath(posixpath.join(base_dir, href))
    return posixpath.normpath(href)


def _marker(node) -> str:
    return ((node.get("id") or "") + " " + (node.get("class") or "")).lower()


def _strip_boilerplate(doc) -> None:
    """删掉电子书里常见的版权页 / 页眉页脚容器（Gutenberg 的 pg-header 等）。"""
    doomed = []
    for node in doc.iter_descendants():
        marker = _marker(node)
        if not marker.strip():
            continue
        if (
            "pg-header" in marker
            or "pg-footer" in marker
            or "pgheader" in marker
            or "pgfooter" in marker
            or "gutenberg-header" in marker
            or "gutenberg-footer" in marker
        ):
            doomed.append(node)
    for node in doomed:
        parent = node.parent
        if parent is not None and node in parent.children:
            parent.children.remove(node)


def _pick_heading(doc) -> str:
    """从文档里猜一个标题：优先「像章节名」的标题，其次 h1 > h2 > h3。"""
    headings = []
    for node in doc.iter_descendants():
        if node.tag in ("h1", "h2", "h3", "h4"):
            text = _clean_heading(node.text(strip=True))
            if text and len(text) <= 120:
                headings.append((node.tag, text))
    if not headings:
        title_node = doc.find("title")
        return title_node.text(strip=True) if title_node is not None else ""
    for tag, text in headings:
        if _CHAPTER_RE.match(text):
            return text
    for wanted in ("h1", "h2", "h3", "h4"):
        for tag, text in headings:
            if tag == wanted:
                return text
    return headings[0][1]


def _split_fragment(fragment: str, fallback_title: str = "", min_chars: int = 300) -> list[Chapter]:
    """把一个 xhtml 片段按 h1~h4 切成多章。

    太短的块（插图说明、页眉残留）会并入下一块，并用「像章节名」的那个标题命名。
    """
    doc = htmldom.parse(fragment)
    chunks: list[tuple[str, list[str]]] = []
    pending_title = ""
    pending: list[str] = []

    for child in doc.children:
        tag = getattr(child, "tag", "")
        html = child if isinstance(child, str) else child.outer_html()
        if tag in ("h1", "h2", "h3", "h4"):
            text = _clean_heading(child.text(strip=True))
            length = len(htmlutil.html_to_text("".join(pending)))
            if pending and length >= min_chars:
                chunks.append((pending_title, pending))
                pending = [html]
                pending_title = text
            else:
                pending.append(html)
                if _CHAPTER_RE.match(text or "") or not pending_title:
                    pending_title = text or pending_title
        else:
            pending.append(html)

    if pending:
        chunks.append((pending_title, pending))

    chapters = [
        Chapter(title=(title or "").strip(), html="".join(parts))
        for title, parts in chunks
        if "".join(parts).strip()
    ]
    if len(chapters) == 1 and not chapters[0].title:
        chapters[0].title = fallback_title
    return chapters


def read_epub(data: bytes | Path | str, *, max_chapters: int = 3000) -> BookContent:
    """解析 EPUB（2 或 3），返回书籍内容。

    :param data: epub 文件的字节内容，或磁盘路径。
    """
    if isinstance(data, (str, Path)):
        raw = Path(data).read_bytes()
    else:
        raw = data

    zf = zipfile.ZipFile(io.BytesIO(raw))
    names = set(zf.namelist())

    # ---- 1. 找到 OPF 包文件 ----
    opf_path = ""
    if "META-INF/container.xml" in names:
        try:
            container = htmldom.parse(
                zf.read("META-INF/container.xml").decode("utf-8", "replace")
            )
            for node in container.iter_descendants():
                if node.tag == "rootfile" and node.get("full-path"):
                    opf_path = node.get("full-path")
                    break
        except Exception:
            opf_path = ""
    if not opf_path or opf_path not in names:
        for candidate in sorted(names):
            if candidate.lower().endswith(".opf"):
                opf_path = candidate
                break
    if not opf_path:
        raise ValueError("不是有效的 EPUB：找不到 OPF 包文件")

    opf = htmldom.parse(zf.read(opf_path).decode("utf-8", "replace"))
    base_dir = posixpath.dirname(opf_path)

    # ---- 2. 元数据 / manifest / spine ----
    content = BookContent(title="", language="")
    manifest: dict[str, tuple[str, str]] = {}   # id -> (href, media-type)
    spine_ids: list[str] = []
    cover_id = ""

    for node in opf.iter_descendants():
        tag = _local(node.tag)
        if tag == "item" and node.get("id"):
            manifest[node.get("id")] = (
                node.get("href", ""),
                (node.get("media-type") or "").lower(),
            )
            if "cover-image" in (node.get("properties") or "").lower():
                cover_id = node.get("id")
        elif tag == "itemref" and node.get("idref"):
            spine_ids.append(node.get("idref"))
        elif tag == "meta" and (node.get("name") or "").lower() == "cover":
            cover_id = cover_id or (node.get("content") or "")

    for node in opf.iter_descendants():
        tag = _local(node.tag)
        value = _text_of(node)
        if not value:
            continue
        if tag == "title" and not content.title:
            content.title = value
        elif tag == "creator":
            if value not in content.authors:
                content.authors.append(value)
        elif tag == "language" and not content.language:
            content.language = value
        elif tag == "identifier" and not content.identifier:
            content.identifier = value
        elif tag == "description" and not content.description:
            content.description = value
        elif tag == "subject":
            content.subjects.append(value)
        elif tag == "date" and not content.year:
            content.year = value
        elif tag == "publisher" and not content.publisher:
            content.publisher = value
        elif tag == "rights" and not content.rights:
            content.rights = value

    # ---- 3. 封面 ----
    if cover_id and cover_id in manifest:
        href, media = manifest[cover_id]
        path = _resolve(base_dir, href)
        if path in names:
            content.cover = zf.read(path)
            content.cover_media_type = media or "image/jpeg"
    if content.cover is None:
        for _item_id, (href, media) in manifest.items():
            if media.startswith("image/") and "cover" in (href or "").lower():
                path = _resolve(base_dir, href)
                if path in names:
                    content.cover = zf.read(path)
                    content.cover_media_type = media
                    break

    # ---- 4. 正文（按 spine 顺序）----
    def skippable(item_id: str, href: str) -> bool:
        _, media = manifest.get(item_id, ("", ""))
        low = (href or "").lower()
        if media and media not in _XHTML_TYPES:
            return True
        if low.endswith(".ncx"):
            return True
        stem = posixpath.splitext(posixpath.basename(low))[0]
        return any(hint in stem for hint in _SKIP_HREF_HINTS)

    for item_id in spine_ids:
        if item_id not in manifest:
            continue
        href, _media = manifest[item_id]
        if skippable(item_id, href):
            continue
        path = _resolve(base_dir, href)
        if path not in names:
            continue
        try:
            html = zf.read(path).decode("utf-8", "replace")
        except Exception:
            continue

        doc = htmldom.parse(html)
        body = doc.find("body")
        if body is None:
            body = doc
        _strip_boilerplate(body)

        fallback = _pick_heading(body)
        fragment = htmlutil.sanitize_to_xhtml(body)
        if not fragment.strip():
            continue

        content.chapters.extend(_split_fragment(fragment, fallback))
        if len(content.chapters) >= max_chapters:
            content.chapters = content.chapters[:max_chapters]
            break

    if not content.chapters:
        content.chapters.append(Chapter(title=content.title or "正文", html=""))
    else:
        # 去掉开头那页无题的版权/目录残渣（Project Gutenberg 的 EPUB 常见）。
        # 条件很保守：必须没有标题、正文很短、而且后面还至少有 2 章。
        first = content.chapters[0]
        if (
            len(content.chapters) >= 3
            and not (first.title or "").strip()
            and len(htmlutil.html_to_text(first.html or "")) < 6000
        ):
            content.chapters.pop(0)

    return content


def read_epub_file(path: str | Path) -> BookContent:
    return read_epub(Path(path))
