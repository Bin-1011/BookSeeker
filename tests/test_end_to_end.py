"""端到端验证：对每个已加载书源真实检索 → 下载 → 导出 → 校验产物。

用法::

    python tests/test_end_to_end.py              # 全部书源
    python tests/test_end_to_end.py aozora       # 只测指定书源
    python tests/test_end_to_end.py --verbose    # 打印每本书的正文抽样
"""

from __future__ import annotations

import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from bookseeker import config, exporter  # noqa: E402
from bookseeker.convert import htmlutil  # noqa: E402
from bookseeker.models import normalize_cjk  # noqa: E402
from bookseeker.sources import get_registry  # noqa: E402

OUT = Path(__file__).resolve().parent / "_out"
OUT.mkdir(parents=True, exist_ok=True)

#: 每个书源用哪个关键字测；(关键字, 首选书名片段) —— 首选片段用来避开短篇/边角料
KEYWORDS: dict[str, tuple[str, str]] = {
    "gutenberg": ("pride and prejudice", "Pride and Prejudice"),
    "standard_ebooks": ("sherlock", "Adventures of Sherlock Holmes"),
    "wikisource": ("红楼梦", "红楼梦"),
    "aozora": ("夏目漱石", "坊"),
    "archive_org": ("pride and prejudice", "Pride and prejudice"),
    "openlibrary": ("pride and prejudice", "Pride and Prejudice"),
    "local": ("测试", "测试"),
}

#: 这些书源依赖的网络有时不通，失败只报告不计为硬失败
OPTIONAL = {"openlibrary"}

VERBOSE = "--verbose" in sys.argv


class Check:
    def __init__(self, name: str):
        self.name = name
        self.ok = 0
        self.fail = 0
        self.notes: list[str] = []

    def expect(self, condition: bool, message: str) -> bool:
        if condition:
            self.ok += 1
        else:
            self.fail += 1
            self.notes.append("✗ " + message)
        return bool(condition)

    @property
    def passed(self) -> bool:
        return self.fail == 0


def validate_epub(path: Path, check: Check) -> None:
    """校验 EPUB 结构、mimetype、XHTML 良构性、目录。"""
    zf = zipfile.ZipFile(path)
    names = zf.namelist()

    check.expect(names[0] == "mimetype", "mimetype 必须是第一个条目")
    check.expect(zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED, "mimetype 必须不压缩")
    check.expect(zf.read("mimetype") == b"application/epub+zip", "mimetype 内容不对")
    check.expect(zf.testzip() is None, "zip 结构损坏")

    for required in ("META-INF/container.xml", "OEBPS/content.opf", "OEBPS/nav.xhtml", "OEBPS/toc.ncx"):
        check.expect(required in names, "缺少 %s" % required)

    bad_xml = []
    for name in names:
        if name.endswith((".opf", ".ncx", ".xhtml", ".xml")):
            try:
                ET.fromstring(zf.read(name))
            except Exception as exc:
                bad_xml.append("%s (%s)" % (name, exc))
    check.expect(not bad_xml, "不是良构 XML：%s" % "; ".join(bad_xml[:3]))

    opf = zf.read("OEBPS/content.opf").decode("utf-8")
    check.expect('properties="nav"' in opf, "manifest 里 nav 缺少 properties")
    check.expect("<dc:title>" in opf and "<dc:language>" in opf, "OPF 元数据不完整")
    check.expect("<dc:identifier" in opf and 'unique-identifier="bookid"' in opf, "缺少唯一标识")
    check.expect(opf.split("<spine")[1].split("</spine>")[0].count("itemref") >= 1, "spine 是空的")

    nav = zf.read("OEBPS/nav.xhtml").decode("utf-8")
    check.expect("<ol>" in nav and "<a href=" in nav, "nav 目录没有条目")


def validate_txt(path: Path, check: Check) -> None:
    text = path.read_text(encoding="utf-8")
    check.expect(len(text) > 50, "TXT 内容太短")
    check.expect("\ufffd" not in text, "TXT 里有乱码替换字符")
    check.expect(text.endswith("\n"), "TXT 结尾应该是换行")


def try_fetch(source, books, check, attempts: int = 3):
    """依次尝试前几本书，返回第一本正文足够长的内容。"""
    last_error = None
    for index, book in enumerate(books[:attempts]):
        print("  正在下载[%d]：%s" % (index + 1, book.title))
        started = time.time()
        try:
            content = source.fetch(book, progress=lambda d, t, m: None)
        except Exception as exc:
            last_error = exc
            print("    ✗ 失败：%s" % exc)
            continue
        length = len("\n".join(htmlutil.html_to_text(c.html) for c in content.chapters))
        print("    完成，耗时 %.1fs，正文 %d 字" % (time.time() - started, length))
        if length >= 300 or index == min(len(books), attempts) - 1:
            return book, content
        print("    正文偏短，换下一本再试…")
    if last_error is not None and not books:
        check.expect(False, "fetch 抛异常：%s" % last_error)
    return (None, None)


def run_source(source_id: str, registry) -> Check:
    check = Check(source_id)
    source = registry.get(source_id)
    if source is None:
        check.expect(False, "书源未加载")
        return check

    query, prefer = KEYWORDS.get(source_id, ("pride", ""))
    print("\n" + "=" * 74)
    print("书源：%s (%s)   关键字：%s   首选：%s" % (source.name or source_id, source_id, query, prefer or "-"))
    print("=" * 74)

    started = time.time()
    try:
        books = source.search(query, 10)
    except Exception as exc:
        check.expect(False, "search 抛异常：%s" % exc)
        print("  ✗ search 抛异常：%s" % exc)
        return check
    print("  检索到 %d 条，耗时 %.1fs" % (len(books), time.time() - started))
    check.expect(len(books) > 0, "没有检索到任何结果")
    if not books:
        return check

    for book in books[:5]:
        print("    · %-50s | %-20s | %s" % (
            (book.title or "")[:50], (book.author_text or "")[:20], book.language or "?"))
    check.expect(all(b.title for b in books), "有结果缺少书名")
    check.expect(all(b.source == source_id for b in books), "结果的 source 字段不对")

    # 首选书名也要做繁简归一化，否则「红楼梦」匹配不上繁体标题的《紅樓夢》
    wanted = normalize_cjk(prefer) if prefer else ""
    ordered = sorted(books, key=lambda b: (wanted not in normalize_cjk(b.title or "")) if wanted else False)
    book, content = try_fetch(source, ordered, check)
    if content is None:
        check.expect(False, "所有候选都下载失败")
        return check
    check.expect(book is not None, "没有选到书")

    check.expect(len(content.chapters) >= 1, "没有章节")
    check.expect(bool(content.title), "content.title 为空")

    text_sample = "\n".join(htmlutil.html_to_text(c.html) for c in content.chapters)
    check.expect(len(text_sample) > 500, "正文太短（%d 字）" % len(text_sample))
    check.expect("\ufffd" not in text_sample, "正文里有乱码替换字符")
    check.expect(content.word_count > 400, "字数太少：%d" % content.word_count)
    check.expect("PK\x03\x04" not in text_sample[:200], "正文开头像二进制（zip 没解压？）")

    joined = "".join(c.html for c in content.chapters)
    check.expect("<script" not in joined.lower(), "正文里残留 <script>")
    check.expect("onclick=" not in joined.lower(), "正文里残留 onclick")
    check.expect("<br>" not in joined, "正文里有非自闭合的 <br>（EPUB 会不良构）")

    print("  章节 %d 个 | 约 %s 字 | 封面 %s" % (
        len(content.chapters), format(content.word_count, ","),
        ("%d B" % len(content.cover)) if content.cover else "无"))
    for chapter in content.chapters[:3]:
        print("    - %s" % (chapter.title or "(无题)")[:60])
    print("  首章开头：%s" % text_sample[:140].replace("\n", " "))
    if VERBOSE:
        print("  正文中段：%s" % text_sample[len(text_sample) // 2: len(text_sample) // 2 + 140].replace("\n", " "))

    safe = ("%s-%s" % (source_id, book.uid.replace(":", "_").replace("/", "_")))[:60]
    written = exporter.export(content, OUT, ["epub", "txt", "html"],
                              options={"download_dir": str(OUT)}, overwrite=True, basename=safe)
    for fmt, path in written.items():
        if str(path).startswith("!ERROR"):
            check.expect(False, "%s 导出失败：%s" % (fmt, str(path)[7:]))
        else:
            print("  ✓ [%s] %s (%d B)" % (fmt, path.name, path.stat().st_size))
            check.expect(path.stat().st_size > 0, "%s 是空文件" % fmt)

    if "epub" in written and not str(written["epub"]).startswith("!ERROR"):
        validate_epub(written["epub"], check)
    if "txt" in written and not str(written["txt"]).startswith("!ERROR"):
        validate_txt(written["txt"], check)

    return check


def main() -> int:
    config.bootstrap()
    registry = get_registry()
    print("配置目录：%s" % config.HOME)
    print("已加载书源：%s" % ", ".join(registry.ids()))

    for report in registry.load_reports:
        if not report.get("ok"):
            who = report.get("file") or report.get("module")
            print("  ⚠ %s：%s" % (who, report.get("message", "")))

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    targets = args or [s for s in registry.ids() if s in KEYWORDS]

    results: list[Check] = []
    for source_id in targets:
        results.append(run_source(source_id, registry))

    print("\n" + "=" * 74)
    print("汇总")
    print("=" * 74)
    hard_fail = 0
    for check in results:
        optional = check.name in OPTIONAL
        status = "通过" if check.passed else ("失败(可选源)" if optional else "失败")
        print("  %-18s %-12s 通过 %d 项，失败 %d 项" % (check.name, status, check.ok, check.fail))
        for note in check.notes:
            print("      %s" % note)
        if not check.passed and not optional:
            hard_fail += check.fail

    print("\n导出产物目录：%s" % OUT)
    print("硬失败 %d 项" % hard_fail)
    return 1 if hard_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
