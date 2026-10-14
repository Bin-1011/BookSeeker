"""导出调度：把 :class:`~bookseeker.models.BookContent` 按指定格式写到磁盘。"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping

from . import config
from .convert import epub as epub_mod
from .convert import htmlout
from .convert import txt as txt_mod
from .models import BookContent, safe_filename

#: 支持的导出格式（有序，供界面下拉框使用）
FORMATS: dict[str, str] = {
    "epub": "EPUB 电子书 (.epub)",
    "txt": "纯文本 (.txt)",
    "html": "单文件网页 (.html)",
}

EXTENSIONS = {"epub": ".epub", "txt": ".txt", "html": ".html"}


def unique_path(path: Path) -> Path:
    """如果文件已存在，自动加 ``(1)``、``(2)`` 后缀避免覆盖。"""
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for i in range(1, 1000):
        candidate = parent / ("%s (%d)%s" % (stem, i, suffix))
        if not candidate.exists():
            return candidate
    return parent / ("%s (%d)%s" % (stem, 1000000, suffix))


def export(
    content: BookContent,
    out_dir: str | Path | None = None,
    formats: Iterable[str] | str = ("epub", "txt"),
    options: Mapping | None = None,
    basename: str | None = None,
    overwrite: bool = False,
) -> dict[str, Path]:
    """把书导出成若干格式。

    返回 ``{格式: 实际写出的路径}``。某个格式失败不会影响其它格式。
    """
    cfg = dict(config.CONFIG)
    if options:
        cfg.update(options)

    if isinstance(formats, str):
        formats = [formats]
    formats = [f.strip().lower() for f in formats if f and f.strip()]
    if not formats:
        formats = ["epub"]

    directory = Path(out_dir) if out_dir else Path(cfg.get("download_dir") or ".")
    directory.mkdir(parents=True, exist_ok=True)

    name = safe_filename(basename or content.title or "book")
    written: dict[str, Path] = {}

    for fmt in formats:
        try:
            dest = directory / (name + EXTENSIONS.get(fmt, "." + fmt))
            if not overwrite:
                dest = unique_path(dest)

            if fmt == "epub":
                epub_mod.build_epub(
                    content,
                    dest,
                    include_cover=bool(cfg.get("epub_include_cover", True)),
                    split_chapters=bool(cfg.get("epub_split_chapters", True)),
                    include_source_link=bool(cfg.get("epub_include_source_link", True)),
                )
            elif fmt == "txt":
                txt_mod.write_txt(
                    content,
                    dest,
                    encoding=str(cfg.get("txt_encoding", "utf-8")),
                    chapter_separator=bool(cfg.get("txt_chapter_separator", True)),
                    wrap_width=int(cfg.get("txt_wrap_width", 0) or 0),
                )
            elif fmt == "html":
                htmlout.write_html(content, dest)
            else:
                continue
            written[fmt] = dest
        except Exception as exc:  # 单个格式失败不影响其它
            written[fmt] = Path("!ERROR: %s" % exc)
    return written


def export_report(written: Mapping[str, Path]) -> str:
    """把 :func:`export` 的结果整理成人类可读的多行字符串。"""
    lines = []
    for fmt, path in written.items():
        if str(path).startswith("!ERROR"):
            lines.append("  [%s] 失败 %s" % (fmt, str(path)[7:]))
        else:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            lines.append("  [%s] %s (%s)" % (fmt, path, _human(size)))
    return "\n".join(lines)


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.0f %s" % (size, unit) if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%d B" % size