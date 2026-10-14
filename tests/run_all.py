"""一键跑完所有测试。

用法::

    python tests/run_all.py            # 全部（含联网测试）
    python tests/run_all.py --offline  # 只跑不联网的单元测试
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

OFFLINE_TESTS = [
    ("DOM 与选择器", "test_htmldom.py"),
    ("HTML 清洗与转换", "test_htmlutil.py"),
    ("EPUB/TXT/HTML 生成", "test_export.py"),
]

ONLINE_TESTS = [
    ("HTTP 层与编码嗅探", "test_http.py"),
    ("EPUB 读取器", "test_epubread.py"),
    ("端到端（全部书源）", "test_end_to_end.py"),
]


def run(title: str, script: str) -> bool:
    print("\n" + "=" * 74)
    print("▶ %s  (%s)" % (title, script))
    print("=" * 74)
    proc = subprocess.run(
        [sys.executable, str(HERE / script)],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    ok = proc.returncode == 0
    print("\n%s %s" % ("✅ 通过" if ok else "❌ 失败", title))
    return ok


def main() -> int:
    offline_only = "--offline" in sys.argv
    tests = list(OFFLINE_TESTS) + ([] if offline_only else list(ONLINE_TESTS))

    print("BookSeeker 测试套件")
    print("Python: %s" % sys.version.split()[0])
    print("项目根目录: %s" % ROOT)

    results = [(title, run(title, script)) for title, script in tests]

    print("\n" + "=" * 74)
    print("总结")
    print("=" * 74)
    for title, ok in results:
        print("  %s %s" % ("✅" if ok else "❌", title))
    failed = sum(1 for _t, ok in results if not ok)
    print("\n%d / %d 组通过" % (len(results) - failed, len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
