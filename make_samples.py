"""整理 samples/ 目录：从测试产物里挑出有代表性的成品，改好名字并写说明。"""

import io
import shutil
from pathlib import Path

ROOT = Path(r"F:\Data\Documents\deepseek-harness\ESP32\BookSeeker")
OUT = ROOT / "tests" / "_out"
SAMPLES = ROOT / "samples"

#: (源文件主干, 目标名, 取哪些格式, 来源说明, 章节数, 字数)
PICKS = [
    ("wikisource-zh_紅樓夢", "红楼梦-维基文库", ("epub", "txt"),
     "维基文库 Wikisource（公有领域）", 120, "880,444"),
    ("aozora-050420", "坊っちゃん-青空文庫", ("epub", "txt"),
     "青空文庫（日本公有领域）", 11, "85,832"),
    ("aozora-000773", "こころ-青空文庫", ("epub",),
     "青空文庫（日本公有领域）", 110, "159,980"),
    ("gutenberg-1342", "Pride-and-Prejudice-古腾堡", ("epub", "txt"),
     "Project Gutenberg（公有领域）", 75, "586,285"),
    ("standard_ebooks-arthur-conan-doyle_the-adventures-of-sherloc",
     "The-Adventures-of-Sherlock-Holmes-StandardEbooks", ("epub",),
     "Standard Ebooks（公有领域）", 12, "454,086"),
    ("archive_org-prideprejudice00aust", "Pride-and-Prejudice-InternetArchive", ("epub",),
     "Internet Archive 扫描件 OCR（开放取用）", 56, "596,759"),
]

SAMPLES.mkdir(parents=True, exist_ok=True)
lines = [
    "# 示例导出",
    "",
    "这些文件是 BookSeeker 从真实（合法免费）书源检索、下载、导出的成品，",
    "可以直接打开看看效果：EPUB 用任意阅读器（Apple Books / Calibre / 多看 / 微信读书）打开，",
    "TXT 用记事本或手机阅读器打开。",
    "",
    "| 文件 | 来源 | 章节 | 字数 |",
    "| --- | --- | --- | --- |",
]

for stem, dest, formats, origin, chapters, words in PICKS:
    got = []
    for fmt in formats:
        src = OUT / ("%s.%s" % (stem, fmt))
        if not src.is_file():
            print("缺少 %s" % src.name)
            continue
        target = SAMPLES / ("%s.%s" % (dest, fmt))
        shutil.copyfile(src, target)
        got.append(target.name)
        print("  %s  (%.1f MB)" % (target.name, target.stat().st_size / 1048576))
    if got:
        lines.append("| `%s` | %s | %d | %s |" % ("` / `".join(got), origin, chapters, words))

lines += [
    "",
    "> 全部内容均来自公有领域或明确开放取用的资源，可以自由分发。",
    "",
    "想自己重新生成：先跑 `python tests/test_end_to_end.py`，再跑 `python make_samples.py`。",
    "",
]
io.open(SAMPLES / "README.md", "w", encoding="utf-8", newline="\n").write("\n".join(lines))
print("\nsamples/README.md 已写出")
total = sum(f.stat().st_size for f in SAMPLES.iterdir() if f.is_file())
print("samples/ 总计 %.1f MB" % (total / 1048576))
