# 示例导出

这些文件是 BookSeeker 从真实（合法免费）书源检索、下载、导出的成品，
可以直接打开看看效果：EPUB 用任意阅读器（Apple Books / Calibre / 多看 / 微信读书）打开，
TXT 用记事本或手机阅读器打开。

| 文件 | 来源 | 章节 | 字数 |
| --- | --- | --- | --- |
| `红楼梦-维基文库.epub` / `红楼梦-维基文库.txt` | 维基文库 Wikisource（公有领域） | 120 | 880,444 |
| `坊っちゃん-青空文庫.epub` / `坊っちゃん-青空文庫.txt` | 青空文庫（日本公有领域） | 11 | 85,832 |
| `Pride-and-Prejudice-古腾堡.epub` / `Pride-and-Prejudice-古腾堡.txt` | Project Gutenberg（公有领域） | 75 | 586,285 |
| `The-Adventures-of-Sherlock-Holmes-StandardEbooks.epub` | Standard Ebooks（公有领域） | 12 | 454,086 |
| `Pride-and-Prejudice-InternetArchive.epub` | Internet Archive 扫描件 OCR（开放取用） | 56 | 596,759 |

> 全部内容均来自公有领域或明确开放取用的资源，可以自由分发。

想自己重新生成：先跑 `python tests/test_end_to_end.py`，再跑 `python make_samples.py`。
