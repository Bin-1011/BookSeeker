"""HTML 处理工具：清洗成合法 XHTML、转成纯文本、纯文本转 XHTML。

导出 EPUB 的正文必须是「良构的 XHTML 片段」——网页上的 HTML 往往不是，
所以这里做一层白名单清洗：去掉脚本、样式、表单，过滤危险属性，
把废弃标签解包（保留文字），并把实体重新正确转义。
"""

from __future__ import annotations

import re
from html import escape as _html_escape

from .. import htmldom
from ..htmldom import Node

#: XML 1.0 不允许出现的控制字符
_XML_BAD_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufffe\uffff\U0001fffe\U0001ffff]"
)

#: 直接连内容一起丢弃的标签
DROP_TAGS = frozenset(
    """script style noscript template iframe frame frameset object embed applet
    form input button select textarea option optgroup label fieldset legend
    svg canvas video audio source track map area meta link base col colgroup
    dialog menu""".split()
)

#: 允许保留的标签
ALLOWED_TAGS = frozenset(
    """p div span br hr em strong i b u s strike del ins sub sup small big mark
    h1 h2 h3 h4 h5 h6 blockquote pre code kbd samp var cite q abbr time
    ul ol li dl dt dd
    table thead tbody tfoot tr td th caption
    section article aside header footer main figure figcaption details summary
    ruby rb rt rp rtc center font""".split()
)

#: 每个标签允许保留的属性
_GLOBAL_ATTRS: frozenset[str] = frozenset({"id", "title", "lang", "xml:lang", "dir"})
ALLOWED_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"src", "alt", "title"}),
    "td": frozenset({"colspan", "rowspan", "headers"}),
    "th": frozenset({"colspan", "rowspan", "scope", "headers"}),
    "ol": frozenset({"start", "type", "reversed"}),
    "li": frozenset({"value"}),
    "time": frozenset({"datetime"}),
    "q": frozenset({"cite"}),
    "blockquote": frozenset({"cite"}),
    "ins": frozenset({"cite", "datetime"}),
    "del": frozenset({"cite", "datetime"}),
}

#: 会被当作段落边界的标签（转纯文本时用）
BLOCK_TAGS = frozenset(
    """p div h1 h2 h3 h4 h5 h6 blockquote pre li dt dd tr caption figcaption
    section article aside header footer main details summary""".split()
)

_SAFE_URL_RE = re.compile(r"^(?:https?:|mailto:|tel:|#|data:image/)", re.I)
_WS_RE = re.compile(r"[ \t\u00a0\u3000]+")
_BLANKLINE_RE = re.compile(r"\n{3,}")
_CJK_RE = re.compile(
    "[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)


# --------------------------------------------------------------------------
# 基础转义
# --------------------------------------------------------------------------


def strip_bad_xml_chars(text: str) -> str:
    return _XML_BAD_RE.sub("", text)


def escape_xml(text: str) -> str:
    """转义文本节点中的 & < >（引号无需转义）。"""
    text = strip_bad_xml_chars(text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attr(text: str) -> str:
    return _html_escape(strip_bad_xml_chars(text or ""), quote=True)


def looks_cjk(text: str) -> bool:
    """判断文本是否以中日韩文字为主。"""
    sample = text[:4000]
    if not sample:
        return False
    return len(_CJK_RE.findall(sample)) > len(sample) * 0.10


# --------------------------------------------------------------------------
# HTML -> XHTML 片段
# --------------------------------------------------------------------------


def _safe_url(url: str) -> str:
    url = strip_bad_xml_chars((url or "").strip())
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if _SAFE_URL_RE.match(url):
        return url
    return ""


def _filter_attrs(node: Node) -> str:
    allowed = ALLOWED_ATTRS.get(node.tag, frozenset())
    parts: list[str] = []
    for key, value in node.attrs.items():
        key = key.lower()
        if key not in allowed and key not in _GLOBAL_ATTRS:
            continue
        if key in ("href", "src", "cite"):
            value = _safe_url(value)
            if not value:
                continue
        if key == "id":
            value = re.sub(r"[^\w\-.:]", "_", value or "")
        if not value:
            continue
        parts.append(' %s="%s"' % (key, escape_attr(value)))
    return "".join(parts)


def _serialize(node: Node, out: list[str]) -> None:
    for child in node.children:
        if isinstance(child, str):
            if child:
                out.append(escape_xml(child))
            continue

        tag = child.tag
        if tag in DROP_TAGS or tag == "#document":
            continue
        if tag == "br":
            out.append("<br/>")
            continue
        if tag == "hr":
            out.append("<hr/>")
            continue
        if tag == "img":
            attrs = _filter_attrs(child)
            if "src=" in attrs:
                if "alt=" not in attrs:
                    attrs += ' alt=""'
                out.append("<img%s/>" % attrs)
            continue

        if tag in ALLOWED_TAGS:
            attrs = _filter_attrs(child)
            out.append("<%s%s>" % (tag, attrs))
            _serialize(child, out)
            out.append("</%s>" % tag)
        else:
            # 不在白名单里的标签：解包，只保留里面的内容
            _serialize(child, out)


def _tidy(xhtml: str) -> str:
    xhtml = re.sub(r"(?:<br/>\s*){3,}", "<br/><br/>", xhtml)
    xhtml = re.sub(r"<p>\s*(?:<br/>\s*)*</p>", "", xhtml)
    xhtml = re.sub(r"<(p|div|li|blockquote)>\s*</\1>", "", xhtml)
    xhtml = re.sub(r"[ \t]+\n", "\n", xhtml)
    return xhtml.strip()


def sanitize_to_xhtml(source: str | Node) -> str:
    """把任意 HTML 清洗成良构的 XHTML 片段。"""
    if isinstance(source, str):
        if "<" not in source:
            return text_to_xhtml(source)
        node: Node = htmldom.parse(source)
    else:
        node = source
    out: list[str] = []
    _serialize(node, out)
    return _tidy("".join(out))


# --------------------------------------------------------------------------
# HTML -> 纯文本
# --------------------------------------------------------------------------


def _to_text(node: Node, out: list[str]) -> None:
    for child in node.children:
        if isinstance(child, str):
            out.append(child)
            continue
        tag = child.tag
        if tag in htmldom.SKIP_TEXT_TAGS or tag in DROP_TAGS:
            continue
        if tag == "br":
            out.append("\n")
        elif tag == "hr":
            out.append("\n\n* * *\n\n")
        elif tag == "li":
            out.append("\n")
            out.append("\u00b7 ")
            _to_text(child, out)
            out.append("\n")
        elif tag in BLOCK_TAGS or tag == "tr":
            out.append("\n")
            _to_text(child, out)
            out.append("\n")
        elif tag in ("td", "th"):
            _to_text(child, out)
            out.append("\t")
        else:
            _to_text(child, out)


def clean_text(text: str, keep_indent: bool = True) -> str:
    """规整纯文本：统一换行、去掉行尾空白、压缩连续空行。"""
    text = strip_bad_xml_chars(text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = []
    for line in text.split("\n"):
        if keep_indent:
            line = line.replace("\u3000", "\u3000").rstrip()
        else:
            line = line.strip()
        lines.append(line)
    text = "\n".join(lines)
    text = _BLANKLINE_RE.sub("\n\n", text)
    return text.strip("\n")


def html_to_text(source: str | Node, keep_indent: bool = True) -> str:
    """把 HTML 转成适合放进 TXT 的纯文本。"""
    if isinstance(source, str):
        if "<" not in source:
            return clean_text(source, keep_indent)
        node: Node = htmldom.parse(source)
    else:
        node = source
    out: list[str] = []
    _to_text(node, out)
    return clean_text("".join(out), keep_indent)


# --------------------------------------------------------------------------
# 纯文本 -> XHTML
# --------------------------------------------------------------------------


def text_to_xhtml(text: str) -> str:
    """把纯文本按空行切段，转成 ``<p>`` 序列。

    段内若包含中日韩文字，换行直接拼接（不补空格）；否则按西文习惯补空格。
    """
    text = clean_text(text)
    if not text:
        return ""
    cjk = looks_cjk(text)
    paragraphs = re.split(r"\n\s*\n", text)
    parts: list[str] = []
    for para in paragraphs:
        lines = [ln.strip() for ln in para.split("\n")]
        lines = [ln for ln in lines if ln]
        if not lines:
            continue
        joiner = "" if cjk else " "
        body = joiner.join(lines)
        parts.append("<p>%s</p>" % escape_xml(body))
    return "\n".join(parts)


# --------------------------------------------------------------------------
# 内容抽取辅助（给书源和插件用）
# --------------------------------------------------------------------------


def pick_content(html: str, *selectors: str) -> Node:
    """依次尝试选择器，返回第一个命中的节点；都没命中则返回 ``<body>`` 或文档根。

    书源解析正文时非常常用::

        node = pick_content(html, "div#content", "div.read-content", "article")
    """
    doc = htmldom.parse(html)
    for sel in selectors:
        if not sel:
            continue
        found = doc.find(sel)
        if found is not None and found.text(strip=True):
            return found
    body = doc.find("body")
    return body if body is not None else doc


def html_title(html: str) -> str:
    doc = htmldom.parse(html)
    title = doc.find("title")
    if title is not None:
        return title.text(strip=True)
    h1 = doc.find("h1")
    return h1.text(strip=True) if h1 is not None else ""


# --------------------------------------------------------------------------
# 生成完整 XHTML 文档（EPUB 用）
# --------------------------------------------------------------------------

XHTML_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" \
xml:lang="{lang}" lang="{lang}">
<head>
  <meta charset="utf-8"/>
  <title>{title}</title>
  <link rel="stylesheet" type="text/css" href="{css}"/>
</head>
<body>
{body}
</body>
</html>
"""


def xhtml_document(title: str, body: str, lang: str = "zh", css: str = "style.css") -> str:
    """把正文片段包装成完整的 XHTML 文档。"""
    return XHTML_TEMPLATE.format(
        lang=escape_attr(lang or "zh"),
        title=escape_xml(title or ""),
        css=escape_attr(css),
        body=body,
    )