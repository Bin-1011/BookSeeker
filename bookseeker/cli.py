"""BookSeeker 命令行界面。

用法示例::

    python BookSeeker.py                      # 启动图形界面
    python BookSeeker.py search 红楼梦
    python BookSeeker.py search "夏目漱石" -n 10
    python BookSeeker.py search dickens --json
    python BookSeeker.py get 1                # 下载上一次检索的第 1 条
    python BookSeeker.py get gutenberg:1342 -f epub,txt -o D:\\books
    python BookSeeker.py get --all -f epub
    python BookSeeker.py sources
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, config, exporter
from .models import Book, language_label
from .sources import get_registry

LAST_SEARCH_FILE = lambda: config.HOME / "last_search.json"  # noqa: E731


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------


def _book_to_dict(book: Book) -> dict:
    return {
        "key": book.key,
        "source": book.source,
        "source_name": book.source_name,
        "uid": book.uid,
        "title": book.title,
        "authors": book.authors,
        "language": book.language,
        "year": book.year,
        "description": book.description,
        "subjects": book.subjects,
        "url": book.url,
        "download_urls": book.download_urls,
        # 很多书源把正文地址、IA 标识符之类放在 extra 里，必须一起存下来，
        # 否则 `get <序号>` 重新构造 Book 时就丢了。
        "extra": book.extra or {},
    }


def _save_last_search(query: str, books: list[Book]) -> None:
    try:
        config.ensure_dirs()
        LAST_SEARCH_FILE().write_text(
            json.dumps(
                {"query": query, "books": [_book_to_dict(b) for b in books]},
                ensure_ascii=False, indent=2, default=str,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _load_last_search() -> tuple[str, list[Book]]:
    path = LAST_SEARCH_FILE()
    if not path.is_file():
        return "", []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "", []
    books = []
    for item in raw.get("books", []):
        books.append(Book(
            source=item.get("source", ""),
            uid=item.get("uid", ""),
            title=item.get("title", ""),
            authors=item.get("authors") or [],
            language=item.get("language", ""),
            year=item.get("year", ""),
            description=item.get("description", ""),
            subjects=item.get("subjects") or [],
            url=item.get("url", ""),
            download_urls=item.get("download_urls") or {},
            extra=item.get("extra") or {},
        ))
        books[-1].source_name = item.get("source_name", "")
    return raw.get("query", ""), books


def _print_books(books: list[Book]) -> None:
    if not books:
        print("（没有结果）")
        return
    width = max((len(b.title or "") for b in books), default=10)
    width = min(max(width, 12), 52)
    print("%-4s %-*s %-18s %-6s %s" % ("#", width, "书名", "作者", "语言", "来源"))
    print("-" * (width + 48))
    for idx, book in enumerate(books, 1):
        title = (book.title or "")[:width]
        author = (book.author_text or "")[:18]
        pad = width - sum(2 if ord(c) > 0x2000 else 1 for c in title)
        print("%-4d %s%s %-18s %-6s %s" % (
            idx, title, " " * max(1, pad), author,
            language_label(book.language), book.source_name or book.source,
        ))


# ----------------------------------------------------------------------
# 子命令
# ----------------------------------------------------------------------


def cmd_search(args: argparse.Namespace) -> int:
    registry = get_registry(cfg=config.CONFIG)
    source_ids = args.source or None
    if source_ids:
        unknown = [s for s in source_ids if registry.get(s) is None]
        if unknown:
            print("未知书源：%s" % ", ".join(unknown), file=sys.stderr)
            print("可用书源：%s" % ", ".join(registry.ids()), file=sys.stderr)
            return 2

    print("正在检索「%s」…" % args.query, file=sys.stderr)

    def on_error(src, exc):
        print("  ⚠ %s 检索失败：%s" % (src.name or src.id, exc), file=sys.stderr)

    books = registry.search_all(
        args.query,
        source_ids=source_ids,
        limit_per_source=args.limit,
        on_error=on_error,
    )
    _save_last_search(args.query, books)

    if args.json:
        print(json.dumps({
            "query": args.query,
            "count": len(books),
            "books": [_book_to_dict(b) for b in books],
        }, ensure_ascii=False, indent=2))
        return 0

    _print_books(books)
    print("\n共 %d 条结果。用 `BookSeeker.py get <序号>` 下载，例如 `BookSeeker.py get 1`。" % len(books))
    return 0


def _resolve_targets(args: argparse.Namespace, registry) -> list[Book]:
    _, books = _load_last_search()
    if args.all:
        if not books:
            print("没有上一次检索结果，请先执行 search。", file=sys.stderr)
            return []
        return books

    targets: list[Book] = []
    for token in args.keys or []:
        if token.isdigit():
            index = int(token)
            if 1 <= index <= len(books):
                targets.append(books[index - 1])
            else:
                print("序号 %d 超出范围（上一次检索共 %d 条）" % (index, len(books)), file=sys.stderr)
        elif ":" in token:
            source_id, uid = token.split(":", 1)
            match = next((b for b in books if b.source == source_id and b.uid == uid), None)
            if match is None:
                src = registry.get(source_id)
                if src is None:
                    print("未知书源：%s" % source_id, file=sys.stderr)
                    continue
                match = Book(source=source_id, uid=uid, title=uid)
                match.source_name = src.name or source_id
            targets.append(match)
        else:
            print("无法识别的目标：%s（应为序号或 source:uid）" % token, file=sys.stderr)
    return targets


def cmd_get(args: argparse.Namespace) -> int:
    registry = get_registry(cfg=config.CONFIG)
    targets = _resolve_targets(args, registry)
    if not targets:
        return 2

    formats = [f.strip() for f in args.format.split(",") if f.strip()]
    invalid = [f for f in formats if f not in exporter.FORMATS]
    if invalid:
        print("不支持的格式：%s（可选：%s）" % (", ".join(invalid), ", ".join(exporter.FORMATS)), file=sys.stderr)
        return 2

    out_dir = Path(args.out) if args.out else Path(config.CONFIG.get("download_dir", "."))
    failed = 0

    for index, book in enumerate(targets, 1):
        print("[%d/%d] %s  (%s)" % (index, len(targets), book.title, book.source_name or book.source))

        last = {"text": ""}

        def progress(done, total, text, _last=last):
            if text and text != _last["text"]:
                _last["text"] = text
                print("      %s" % text, flush=True)

        try:
            content = registry.fetch(book, progress=progress)
        except Exception as exc:
            print("      ✗ 下载失败：%s" % exc, file=sys.stderr)
            failed += 1
            continue

        print("      已获取 %d 章，约 %s 字" % (len(content.chapters), format(content.word_count, ",")))
        written = exporter.export(content, out_dir, formats, options=config.CONFIG)
        for fmt, path in written.items():
            if str(path).startswith("!ERROR"):
                print("      ✗ [%s] %s" % (fmt, str(path)[7:]), file=sys.stderr)
                failed += 1
            else:
                print("      ✓ [%s] %s" % (fmt, path))

    if not args.all and len(targets) == 1:
        pass
    print("\n导出目录：%s" % out_dir)
    return 1 if failed else 0


def cmd_sources(args: argparse.Namespace) -> int:
    registry = get_registry(cfg=config.CONFIG, refresh=args.reload)
    disabled = registry.disabled_ids()
    print("共 %d 个书源（%d 个启用）\n" % (len(registry.all()), len(registry.enabled())))
    for src in registry.all():
        flag = " " if src.id in disabled else "✓"
        print("%s %-18s %-22s %s" % (flag, src.id, src.name or "", src.description or ""))
        if src.languages:
            print("  %-18s 语言：%s   主页：%s" % ("", "/".join(src.languages), src.homepage or "-"))

    plugins = [r for r in registry.load_reports if r.get("file")]
    if plugins:
        print("\n插件加载情况：")
        for report in plugins:
            print("  %s %s  %s" % ("✓" if report.get("ok") else "✗",
                                   Path(report["file"]).name, report.get("message", "")))

    builtin_failures = [r for r in registry.load_reports if not r.get("file") and not r.get("ok")]
    if builtin_failures:
        print("\n未能加载的内置书源：")
        for report in builtin_failures:
            print("  ✗ %s  %s" % (report.get("module"), report.get("message", "")))

    print("\n配置目录：%s" % config.HOME)
    print("插件目录：%s" % config.PLUGIN_DIR)
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import run_gui

    return run_gui()


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="BookSeeker",
        description="跨书源检索公版/开放版权书籍，并导出 EPUB / TXT / HTML。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-V", "--version", action="version", version="BookSeeker %s" % __version__)
    sub = parser.add_subparsers(dest="command")

    p_gui = sub.add_parser("gui", help="启动图形界面")
    p_gui.set_defaults(func=cmd_gui)

    p_search = sub.add_parser("search", help="按关键字检索")
    p_search.add_argument("query", help="关键字（中/日/英均可）")
    p_search.add_argument("-n", "--limit", type=int, default=20, help="每个书源最多返回多少条（默认 20）")
    p_search.add_argument("-s", "--source", action="append", help="只检索指定书源，可重复")
    p_search.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_search.set_defaults(func=cmd_search)

    p_get = sub.add_parser("get", help="下载并导出上一次检索的结果")
    p_get.add_argument("keys", nargs="*", help="序号（如 1）或 source:uid（如 gutenberg:1342）")
    p_get.add_argument("--all", action="store_true", help="下载上一次检索的全部结果")
    p_get.add_argument("-f", "--format", default="epub,txt", help="导出格式，逗号分隔（epub,txt,html）")
    p_get.add_argument("-o", "--out", help="导出目录")
    p_get.set_defaults(func=cmd_get)

    p_sources = sub.add_parser("sources", help="列出所有书源")
    p_sources.add_argument("--reload", action="store_true", help="重新扫描插件目录")
    p_sources.set_defaults(func=cmd_sources)

    return parser


def main(argv: list[str] | None = None) -> int:
    config.setup_console()
    config.bootstrap()

    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    if not argv:
        # 双击 / 无参数启动时直接开界面
        argv = ["gui"]

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0

    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
