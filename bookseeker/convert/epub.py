"""EPUB 3.0 生成器（纯标准库，手写 OPF / NAV / NCX）。

生成的包同时包含 EPUB 3 的 ``nav.xhtml`` 和 EPUB 2 的 ``toc.ncx``，
因此新旧阅读器（Apple Books、Calibre、多看、微信读书、Kindle via Send-to-Kindle）
都能正确识别目录。
"""

from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from ..models import BookContent, Chapter
from . import htmlutil
from .htmlutil import escape_attr, escape_xml

CONTAINER_XML = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

DEFAULT_CSS = """@charset "utf-8";

html { font-size: 100%; }

body {
  font-family: "Songti SC", "SimSun", "Noto Serif CJK SC", "Source Han Serif SC",
               "Hiragino Mincho ProN", "Yu Mincho", serif;
  line-height: 1.75;
  margin: 0 5%;
  text-align: justify;
  word-wrap: break-word;
  -epub-hyphens: none;
}

h1, h2, h3, h4, h5, h6 {
  font-weight: bold;
  line-height: 1.4;
  margin: 1.4em 0 0.9em;
  text-align: center;
  page-break-after: avoid;
}

h1 { font-size: 1.5em; }
h2 { font-size: 1.3em; }
h3 { font-size: 1.15em; }

p {
  margin: 0.45em 0;
  text-indent: 2em;
}

p.no-indent, li p, blockquote p, td p { text-indent: 0; }

blockquote {
  margin: 0.8em 2em;
  font-size: 0.95em;
}

ul, ol { margin: 0.7em 0 0.7em 1.6em; padding: 0; }
li { margin: 0.25em 0; text-indent: 0; }

hr {
  border: none;
  border-top: 1px solid #999;
  margin: 1.4em 25%;
  width: 50%;
}

img { max-width: 100%; height: auto; }

table {
  border-collapse: collapse;
  margin: 1em auto;
  font-size: 0.95em;
}
td, th { border: 1px solid #bbb; padding: 0.3em 0.6em; text-indent: 0; }

ruby rt { font-size: 0.55em; }

.center { text-align: center; text-indent: 0; }

div.cover {
  margin: 0;
  padding: 0;
  text-align: center;
  page-break-after: always;
}
div.cover img { max-width: 100%; max-height: 100%; }

.book-meta {
  margin: 2em 0;
  padding: 1em 0;
  border-top: 1px solid #ccc;
  border-bottom: 1px solid #ccc;
  font-size: 0.9em;
  text-indent: 0;
}
.book-meta p { text-indent: 0; margin: 0.25em 0; }
.book-meta .label { color: #666; }

.chapter-title { margin-top: 0; }
"""


def _ensure_wellformed(fragment: str) -> str:
    """保证章节片段是良构 XHTML。

    EPUB 阅读器对 XML 良构性很严格，而插件作者（或某个书源）传进来的 HTML
    可能有 ``<br>`` 这种非自闭合写法。这里先试着解析，失败就重新清洗，
    再失败就退化成纯文本段落——总之绝不让坏的 XML 进到成品里。
    """
    if not fragment:
        return ""
    try:
        ET.fromstring("<div>%s</div>" % fragment)
        return fragment
    except ET.ParseError:
        pass

    repaired = htmlutil.sanitize_to_xhtml(fragment)
    try:
        ET.fromstring("<div>%s</div>" % repaired)
        return repaired
    except ET.ParseError:
        pass

    # 最后兜底：整段当纯文本处理，百分百良构
    plain = htmlutil.html_to_text(fragment)
    return "<p>%s</p>" % escape_xml(plain)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_identifier(content: BookContent) -> str:
    if content.identifier:
        raw = content.identifier
    else:
        raw = "|".join(
            [content.source_url or "", content.title or "", content.author_text, content.source_name]
        )
    return "urn:uuid:%s" % uuid.uuid5(uuid.NAMESPACE_URL, raw or content.title or "book")


def _chapter_title(ch: Chapter, index: int) -> str:
    title = (ch.title or "").strip()
    if not title:
        title = "\u7b2c %d \u7ae0" % index
    return title


def _cover_extension(media_type: str) -> str:
    mapping = {
        "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
        "image/gif": "gif", "image/webp": "webp", "image/svg+xml": "svg",
    }
    return mapping.get((media_type or "").lower(), "jpg")


def _meta_block(content: BookContent, identifier: str) -> str:
    lines = [
        '    <dc:identifier id="bookid">%s</dc:identifier>' % escape_xml(identifier),
        "    <dc:title>%s</dc:title>" % escape_xml(content.title or "\u672a\u547d\u540d"),
        "    <dc:language>%s</dc:language>" % escape_xml(content.guess_language() or "zh"),
    ]
    authors = content.authors or []
    for i, author in enumerate(authors):
        if i == 0:
            lines.append('    <dc:creator id="creator">%s</dc:creator>' % escape_xml(author))
            lines.append(
                '    <meta refines="#creator" property="role" scheme="marc:relators">aut</meta>'
            )
        else:
            lines.append(
                '    <dc:contributor id="contrib%d">%s</dc:contributor>' % (i, escape_xml(author))
            )
    if content.publisher:
        lines.append("    <dc:publisher>%s</dc:publisher>" % escape_xml(content.publisher))
    if content.description:
        lines.append("    <dc:description>%s</dc:description>" % escape_xml(content.description))
    for subject in content.subjects[:12]:
        if subject:
            lines.append("    <dc:subject>%s</dc:subject>" % escape_xml(subject))
    if content.year:
        lines.append("    <dc:date>%s</dc:date>" % escape_xml(str(content.year)))
    if content.rights:
        lines.append("    <dc:rights>%s</dc:rights>" % escape_xml(content.rights))
    if content.source_url:
        lines.append("    <dc:source>%s</dc:source>" % escape_xml(content.source_url))
    lines.append('    <meta property="dcterms:modified">%s</meta>' % _iso_now())
    return "\n".join(lines)


def _nav_xhtml(content: BookContent, chapter_files: list[tuple[str, str]]) -> str:
    items = "\n".join(
        '      <li><a href="%s">%s</a></li>' % (escape_attr(href), escape_xml(title))
        for href, title in chapter_files
    )
    first = chapter_files[0][0] if chapter_files else "nav.xhtml"
    return """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" \
xml:lang="{lang}" lang="{lang}">
<head>
  <meta charset="utf-8"/>
  <title>\u76ee\u5f55</title>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
  <nav epub:type="toc" id="toc">
    <h1 class="chapter-title">\u76ee\u5f55</h1>
    <ol>
{items}
    </ol>
  </nav>
  <nav epub:type="landmarks" hidden="hidden">
    <ol>
      <li><a epub:type="bodymatter" href="{first}">\u6b63\u6587</a></li>
    </ol>
  </nav>
</body>
</html>
""".format(
        lang=escape_attr(content.guess_language() or "zh"),
        items=items,
        first=escape_attr(first),
    )


def _ncx(content: BookContent, identifier: str, chapter_files: list[tuple[str, str]]) -> str:
    points = []
    for i, (href, title) in enumerate(chapter_files, 1):
        points.append(
            '    <navPoint id="navPoint-%d" playOrder="%d">\n'
            "      <navLabel><text>%s</text></navLabel>\n"
            '      <content src="%s"/>\n'
            "    </navPoint>" % (i, i, escape_xml(title), escape_attr(href))
        )
    return """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" \
xml:lang="{lang}">
  <head>
    <meta name="dtb:uid" content="{uid}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{title}</text></docTitle>
  <docAuthor><text>{author}</text></docAuthor>
  <navMap>
{points}
  </navMap>
</ncx>
""".format(
        lang=escape_attr(content.guess_language() or "zh"),
        uid=escape_attr(identifier),
        title=escape_xml(content.title or ""),
        author=escape_xml(content.author_text),
        points="\n".join(points),
    )


def _book_meta_page(content: BookContent) -> str:
    rows = []
    if content.author_text:
        rows.append(("\u4f5c\u8005", content.author_text))
    if content.source_name:
        rows.append(("\u6765\u6e90", content.source_name))
    if content.year:
        rows.append(("\u5e74\u4ee3", str(content.year)))
    if content.rights:
        rows.append(("\u7248\u6743", content.rights))
    if content.source_url:
        rows.append(("\u539f\u6587\u94fe\u63a5", content.source_url))
    rows.append(("\u5b57\u6570", "\u7ea6 %s \u5b57" % format(content.word_count, ",")))

    body = []
    for label, value in rows:
        if label == "\u539f\u6587\u94fe\u63a5":
            body.append(
                '<p><span class="label">%s\uff1a</span><a href="%s">%s</a></p>'
                % (escape_xml(label), escape_attr(value), escape_xml(value))
            )
        else:
            body.append(
                '<p><span class="label">%s\uff1a</span>%s</p>' % (escape_xml(label), escape_xml(value))
            )
    if content.description:
        body.append("<p>%s</p>" % escape_xml(content.description))
    return '<div class="book-meta">\n%s\n</div>' % "\n".join(body)


def build_epub(
    content: BookContent,
    dest: Path,
    *,
    include_cover: bool = True,
    split_chapters: bool = True,
    include_source_link: bool = True,
    css: str | None = None,
) -> Path:
    """把 :class:`BookContent` 写成 EPUB 3 文件，返回目标路径。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    identifier = _make_identifier(content)
    lang = content.guess_language() or "zh"

    chapters = list(content.chapters) or [Chapter(title=content.title or "\u6b63\u6587", html="")]

    # ---- 组装正文文件 -------------------------------------------------
    manifest: list[tuple[str, str, str, str]] = []   # (id, href, media-type, properties)
    spine: list[str] = []
    chapter_files: list[tuple[str, str]] = []

    def add_manifest(item_id: str, href: str, media: str, props: str = "") -> None:
        manifest.append((item_id, href, media, props))

    has_cover = bool(include_cover and content.cover)
    cover_ext = _cover_extension(content.cover_media_type)
    cover_href = "text/cover.xhtml"

    body_parts: list[str] = []

    # 扉页：书名 / 作者 / 书目信息
    if content.title:
        head = ['<h1 class="chapter-title">%s</h1>' % escape_xml(content.title)]
        if content.author_text:
            head.append('<p class="center">%s</p>' % escape_xml(content.author_text))
        if include_source_link:
            head.append(_book_meta_page(content))
        doc = htmlutil.xhtml_document(content.title, "\n".join(head), lang, "../style.css")
        body_parts.append("__FILE__text/title.xhtml__" + doc)
        add_manifest("title", "text/title.xhtml", "application/xhtml+xml")
        spine.append("title")

    if split_chapters:
        for idx, ch in enumerate(chapters, 1):
            name = "ch%04d.xhtml" % idx
            href = "text/" + name
            heading = '<h2 class="chapter-title">%s</h2>' % escape_xml(_chapter_title(ch, idx))
            body = heading + "\n" + _ensure_wellformed(ch.html or "")
            doc = htmlutil.xhtml_document(_chapter_title(ch, idx), body, lang, "../style.css")
            body_parts.append("__FILE__%s__%s" % (href, doc))
            add_manifest("ch%04d" % idx, href, "application/xhtml+xml")
            spine.append("ch%04d" % idx)
            chapter_files.append((href, _chapter_title(ch, idx)))
    else:
        chunks = []
        for idx, ch in enumerate(chapters, 1):
            chunks.append('<h2 class="chapter-title">%s</h2>' % escape_xml(_chapter_title(ch, idx)))
            chunks.append(_ensure_wellformed(ch.html or ""))
        doc = htmlutil.xhtml_document(content.title or "\u6b63\u6587", "\n".join(chunks), lang, "../style.css")
        body_parts.append("__FILE__text/all.xhtml__" + doc)
        add_manifest("ch0001", "text/all.xhtml", "application/xhtml+xml")
        spine.append("ch0001")
        chapter_files.append(("text/all.xhtml", content.title or "\u6b63\u6587"))

    # 目录必须放在最前面
    chapter_files.insert(0, ("nav.xhtml", "\u76ee\u5f55"))

    add_manifest("nav", "nav.xhtml", "application/xhtml+xml", "nav")
    add_manifest("ncx", "toc.ncx", "application/x-dtbncx+xml")
    add_manifest("css", "style.css", "text/css", "")
    if has_cover:
        add_manifest("cover-image", "images/cover.%s" % cover_ext, content.cover_media_type,
                     "cover-image")
        add_manifest("cover", cover_href, "application/xhtml+xml")

    cover_spine: list[str] = []
    if has_cover:
        cover_doc = htmlutil.xhtml_document(
            "\u5c01\u9762",
            '<div class="cover"><img src="../images/cover.%s" alt="\u5c01\u9762"/></div>' % cover_ext,
            lang,
            "../style.css",
        )
        body_parts.append("__FILE__%s__%s" % (cover_href, cover_doc))
        cover_spine.append("cover")

    # ---- content.opf ---------------------------------------------------
    manifest_xml = []
    for item_id, href, media, props in manifest:
        p = ' properties="%s"' % props if props else ""
        manifest_xml.append(
            '    <item id="%s" href="%s" media-type="%s"%s/>'
            % (escape_attr(item_id), escape_attr(href), escape_attr(media), p)
        )

    spine_xml = []
    for item_id in cover_spine + spine:
        spine_xml.append('    <itemref idref="%s"/>' % escape_attr(item_id))

    guide = ""
    guide_items = []
    if has_cover:
        guide_items.append('    <reference type="cover" title="\u5c01\u9762" href="%s"/>' % cover_href)
    href_by_id = {item_id: href for item_id, href, _m, _p in manifest}
    if spine:
        guide_items.append(
            '    <reference type="text" title="\u6b63\u6587" href="%s"/>'
            % escape_attr(href_by_id.get(spine[0], ""))
        )
    if guide_items:
        guide = "  <guide>\n%s\n  </guide>\n" % "\n".join(guide_items)

    cover_meta = (
        '    <meta name="cover" content="cover-image"/>\n' if has_cover else ""
    )

    opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" \
xml:lang="{lang}">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" \
xmlns:opf="http://www.idpf.org/2007/opf">
{meta}
{cover_meta}  </metadata>
  <manifest>
{manifest}
  </manifest>
  <spine toc="ncx">
{spine}
  </spine>
{guide}</package>
""".format(
        lang=escape_attr(lang),
        meta=_meta_block(content, identifier),
        cover_meta=cover_meta,
        manifest="\n".join(manifest_xml),
        spine="\n".join(spine_xml),
        guide=guide,
    )

    # ---- 写 zip --------------------------------------------------------
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        info.external_attr = 0o644 << 16
        zf.writestr(info, "application/epub+zip")
        zf.writestr("META-INF/container.xml", CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/nav.xhtml", _nav_xhtml(content, chapter_files))
        zf.writestr("OEBPS/toc.ncx", _ncx(content, identifier, chapter_files))
        zf.writestr("OEBPS/style.css", css or DEFAULT_CSS)
        if has_cover:
            zf.writestr("OEBPS/images/cover.%s" % cover_ext, content.cover)
        for part in body_parts:
            href, _, doc = part[len("__FILE__"):].partition("__")
            zf.writestr("OEBPS/" + href, doc)
    return dest