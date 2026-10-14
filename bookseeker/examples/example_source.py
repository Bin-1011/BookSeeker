"""示例书源插件：把本地文件夹里的电子书也变成一个"书源"。

把这个文件复制到插件目录（界面上的「打开插件目录」按钮），改成你自己的逻辑即可。
BookSeeker 启动时会自动加载插件目录下所有 ``.py``，并注册里面所有 :class:`Source` 子类。

要点
----
1. 一个插件文件里可以有任意多个 ``Source`` 子类，也可以定义 ``SOURCES = [类1, 类2]``。
2. ``search()`` 只返回元数据（:class:`~bookseeker.models.Book`）。
3. ``fetch()`` 返回正文（:class:`~bookseeker.models.BookContent`）。
4. 只用标准库。项目已经内置了 HTTP 客户端、轻量 DOM 选择器和格式转换，
   直接从 ``bookseeker`` 里 import 就行。
"""

from __future__ import annotations

import re
from pathlib import Path

from bookseeker import config
from bookseeker.convert import htmlutil
from bookseeker.epubread import read_epub
from bookseeker.models import Book, BookContent, Chapter
from bookseeker.sources.base import ProgressFn, Source, SourceError

#: 支持的本地文件后缀
TEXT_SUFFIXES = {".txt", ".md", ".text"}
BOOK_SUFFIXES = {".epub"} | TEXT_SUFFIXES

#: 章节标题的常见写法
CHAPTER_RE = re.compile(
    r"^\s*(?:第\s*[0-9０-９一二三四五六七八九十百千〇零两]+\s*[章回節节卷篇部話话]"
    r"|(?:chapter|part|book)\b)",
    re.I,
)


class LocalLibrarySource(Source):
    """把本地文件夹当作书源：按文件名检索，直接读取文件内容。"""

    id = "local"
    name = "本地书库"
    homepage = ""
    description = "扫描本地文件夹里的 EPUB / TXT，按文件名检索（无需联网）"
    languages = ("zh", "ja", "en")
    priority = 5                     # 数字越小排越前，让本地书优先出现
    enabled_by_default = True
    requires_network = False

    # ------------------------------------------------------------------

    def library_dir(self) -> Path:
        """书库目录，可以用配置项 ``local_library_dir`` 覆盖。"""
        custom = self.cfg.get("local_library_dir")
        if custom:
            return Path(custom).expanduser()
        return Path(config.HOME) / "library"

    def _scan(self) -> list[Path]:
        directory = self.library_dir()
        if not directory.is_dir():
            return []
        files: list[Path] = []
        for path in directory.rglob("*"):
            try:
                if path.is_file() and path.suffix.lower() in BOOK_SUFFIXES:
                    files.append(path)
            except OSError:
                continue
        return sorted(files)

    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> list[Book]:
        query = (query or "").strip().lower()
        if not query:
            return []
        tokens = [t for t in re.split(r"\s+", query) if t]
        books: list[Book] = []
        for path in self._scan():
            stem = path.stem
            haystack = stem.lower()
            if not all(t in haystack for t in tokens):
                continue
            books.append(
                self.make_book(
                    uid=str(path),
                    title=stem,
                    authors=[],
                    language="",
                    url=path.as_uri(),
                    description="本地文件：%s" % path,
                    extra={"path": str(path), "fmt": path.suffix.lower().lstrip(".")},
                )
            )
            if len(books) >= limit:
                break
        return books

    def fetch(self, book: Book, progress: ProgressFn | None = None) -> BookContent:
        raw_path = (book.extra or {}).get("path") or book.uid
        path = Path(raw_path)
        if not path.is_file():
            raise SourceError("本地文件不存在：%s" % path)

        self.report(progress, 0, 1, "读取 %s" % path.name)

        if path.suffix.lower() == ".epub":
            content = read_epub(path.read_bytes())
        else:
            text = self._read_text(path)
            content = BookContent(
                title=path.stem,
                chapters=self._split_chapters(text),
                language="",
            )

        content.source_name = self.name
        content.source_url = path.as_uri()
        if not content.title:
            content.title = path.stem
        self.report(progress, 1, 1, "完成")
        return content

    # ------------------------------------------------------------------

    @staticmethod
    def _read_text(path: Path) -> str:
        """读文本文件，依次尝试几种常见编码。"""
        data = path.read_bytes()
        for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5", "shift_jis", "euc-jp"):
            try:
                return data.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return data.decode("utf-8", "replace")

    @staticmethod
    def _split_chapters(text: str) -> list[Chapter]:
        """按章节标题大致切分；切不出来就当单章。"""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        chunks: list[tuple[str, list[str]]] = []
        title = ""
        buffer: list[str] = []
        for line in lines:
            if CHAPTER_RE.match(line) and len(line.strip()) <= 40:
                body = "\n".join(buffer).strip()
                if body:
                    chunks.append((title, buffer))
                title = line.strip()
                buffer = [line]
            else:
                buffer.append(line)
        body = "\n".join(buffer).strip()
        if body:
            chunks.append((title, buffer))

        if len(chunks) <= 1:
            return [Chapter(title=title or "正文", html=htmlutil.text_to_xhtml(text))]

        chapters: list[Chapter] = []
        for chunk_title, chunk_lines in chunks:
            chapter_text = "\n".join(chunk_lines)
            if chunk_title:
                chapter_text = "\n".join(chunk_lines[1:]).strip()
            chapters.append(
                Chapter(title=chunk_title or "", html=htmlutil.text_to_xhtml(chapter_text))
            )
        return [c for c in chapters if c.html.strip()]


#: 也可以显式声明本文件提供哪些书源
SOURCES = [LocalLibrarySource]
