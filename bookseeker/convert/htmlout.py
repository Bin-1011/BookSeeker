"""HTML 导出：把整本书打包成单文件 HTML，自带目录与锚点。"""

from __future__ import annotations

from pathlib import Path

from ..models import BookContent, Chapter
from .htmlutil import escape_attr, escape_xml

CSS = """
:root { color-scheme: light dark; }
body { max-width: 42em; margin: 0 auto; padding: 2em 1.2em 6em;
       font-family: "Songti SC","SimSun","Noto Serif CJK SC",Georgia,serif;
       line-height: 1.8; text-align: justify; }
h1 { text-align: center; font-size: 1.7em; margin: 2em 0 0.6em; }
h2 { font-size: 1.25em; margin: 2.2em 0 0.8em; padding-top: 0.6em;
     border-top: 1px solid rgba(128,128,128,.35); }
p { margin: 0.5em 0; text-indent: 2em; }
p.no-indent, li p, blockquote p { text-indent: 0; }
blockquote { margin: 1em 2em; opacity: .9; }
img { max-width: 100%; height: auto; }
hr { border: none; border-top: 1px solid rgba(128,128,128,.4); margin: 2em 20%; }
nav.toc { border: 1px solid rgba(128,128,128,.35); border-radius: 8px;
          padding: 1em 1.4em; margin: 1.5em 0 2.5em; }
nav.toc h2 { border: none; margin: 0 0 .6em; padding: 0; font-size: 1.1em; }
nav.toc ol { margin: 0; padding-left: 1.4em; }
nav.toc li { margin: .25em 0; }
.meta { font-size: .9em; opacity: .8; text-align: center; }
.top { font-size: .8em; opacity: .6; text-decoration: none; }
"""


def _chapter_title(ch: Chapter, index: int) -> str:
    return (ch.title or "").strip() or "\u7b2c %d \u7ae0" % index


def build_html(content: BookContent) -> str:
    """生成单文件 HTML 全文。"""
    chapters = list(content.chapters) or [
        Chapter(title=content.title or "\u6b63\u6587", html="")
    ]

    toc_items = []
    for idx, ch in enumerate(chapters, 1):
        toc_items.append(
            '      <li><a href="#ch%04d">%s</a></li>'
            % (idx, escape_xml(_chapter_title(ch, idx)))
        )

    parts: list[str] = []
    for idx, ch in enumerate(chapters, 1):
        parts.append('<section id="ch%04d">' % idx)
        parts.append("<h2>%s</h2>" % escape_xml(_chapter_title(ch, idx)))
        parts.append(ch.html or "")
        parts.append(
            '<p class="no-indent"><a class="top" href="#top">\u2191 \u8fd4\u56de\u76ee\u5f55</a></p>'
        )
        parts.append("</section>")

    meta_bits = []
    if content.author_text:
        meta_bits.append(escape_xml(content.author_text))
    if content.source_name:
        meta_bits.append("\u6765\u6e90\uff1a" + escape_xml(content.source_name))
    if content.year:
        meta_bits.append(escape_xml(str(content.year)))
    if content.source_url:
        meta_bits.append('<a href="%s">\u539f\u6587</a>' % escape_attr(content.source_url))

    return """<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<style>{css}</style>
</head>
<body id="top">
<h1>{title}</h1>
<p class="meta">{meta}</p>
{desc}
<nav class="toc">
  <h2>\u76ee\u5f55</h2>
  <ol>
{toc}
  </ol>
</nav>
{body}
</body>
</html>
""".format(
        lang=escape_attr(content.guess_language() or "zh"),
        title=escape_xml(content.title or "\u672a\u547d\u540d"),
        css=CSS,
        meta=" \u00b7 ".join(meta_bits),
        desc=(
            '<p class="meta">%s</p>' % escape_xml(content.description)
        ) if content.description else "",
        toc="\n".join(toc_items),
        body="\n".join(parts),
    )


def write_html(content: BookContent, dest: Path) -> Path:
    """写出单文件 HTML，返回目标路径。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(build_html(content), encoding="utf-8")
    return dest