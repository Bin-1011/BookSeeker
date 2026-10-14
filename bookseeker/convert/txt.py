"""TXT 导出：可选按显示宽度折行、章节分隔线、多种编码。"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from ..models import BookContent, Chapter
from .htmlutil import html_to_text

#: 章节之间的分隔线
SEPARATOR = "-" * 56


def display_width(text: str) -> int:
    """按终端 / 阅读器的显示宽度计算（全角算 2）。"""
    total = 0
    for ch in text:
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def wrap_by_width(text: str, width: int) -> str:
    """按显示宽度折行；西文尽量在空格处断开。"""
    if width <= 0:
        return text
    out: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if not line or display_width(line) <= width:
            out.append(line)
            continue
        current = ""
        current_w = 0
        for ch in line:
            w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            if current_w + w > width:
                # 西文回退到最近的空格，避免把单词劈开
                if ch.isascii() and ch.isalnum() and " " in current[-(width // 3 or 1):]:
                    cut = current.rfind(" ")
                    out.append(current[:cut].rstrip())
                    current = current[cut + 1:] + ch
                else:
                    out.append(current.rstrip())
                    current = ch
                current_w = display_width(current)
            else:
                current += ch
                current_w += w
        if current.strip():
            out.append(current.rstrip())
    return "\n".join(out)


def _chapter_text(ch: Chapter, index: int) -> str:
    title = (ch.title or "").strip() or "\u7b2c %d \u7ae0" % index
    body = html_to_text(ch.html or "")
    return "%s\n\n%s" % (title, body) if body else title


def build_txt(
    content: BookContent,
    *,
    chapter_separator: bool = True,
    wrap_width: int = 0,
    include_header: bool = True,
) -> str:
    """生成 TXT 全文。"""
    blocks: list[str] = []

    if include_header:
        header = [content.title or "\u672a\u547d\u540d"]
        if content.author_text:
            header.append("\u4f5c\u8005\uff1a%s" % content.author_text)
        if content.source_name:
            header.append("\u6765\u6e90\uff1a%s" % content.source_name)
        if content.year:
            header.append("\u5e74\u4ee3\uff1a%s" % content.year)
        if content.source_url:
            header.append("\u539f\u6587\uff1a%s" % content.source_url)
        header.append("\u5b57\u6570\uff1a\u7ea6 %s \u5b57" % format(content.word_count, ","))
        # 缺章时在元信息里明说一句，别让用户以为书就是完整的
        notice = content.missing_notice()
        if notice:
            header.append(notice)
        blocks.append("\n".join(header))

    chapters = list(content.chapters) or [Chapter(title=content.title or "\u6b63\u6587", html="")]
    for idx, ch in enumerate(chapters, 1):
        blocks.append(_chapter_text(ch, idx))

    joiner = ("\n\n" + SEPARATOR + "\n\n") if chapter_separator else "\n\n"
    text = joiner.join(blocks)
    # 正文末尾再提示一次，读者看到最后仍会被提醒
    tail = content.missing_notice(prefix="\u26a0 \u63d0\u793a\uff1a")
    if tail:
        text = text + "\n\n" + SEPARATOR + "\n\n" + tail
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"

    if wrap_width and wrap_width > 0:
        text = wrap_by_width(text, wrap_width) + "\n"
    return text


def write_txt(
    content: BookContent,
    dest: Path,
    *,
    encoding: str = "utf-8",
    chapter_separator: bool = True,
    wrap_width: int = 0,
) -> Path:
    """把书籍写成 TXT 文件，返回目标路径。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = build_txt(
        content, chapter_separator=chapter_separator, wrap_width=wrap_width
    )
    try:
        data = text.encode(encoding, "replace")
    except LookupError:
        data = text.encode("utf-8")
    dest.write_bytes(data)
    return dest