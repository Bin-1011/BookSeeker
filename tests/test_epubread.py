import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
from bookseeker import http, epubread

urls = {
    "PG-epub-cache": "https://www.gutenberg.org/cache/epub/1342/pg1342.epub",
    "PG-epub-noimages": "https://www.gutenberg.org/ebooks/1342.epub.noimages",
    "PG-epub-images": "https://www.gutenberg.org/ebooks/1342.epub.images",
}
data = None
for name, u in urls.items():
    try:
        s = time.time()
        r = http.get(u, timeout=45)
        print(f"{name:18} OK {len(r.data):>8} B  ct={r.content_type}  {time.time()-s:.1f}s")
        if data is None or len(r.data) > len(data):
            data = r.data
    except Exception as e:
        print(f"{name:18} ERR {e}")
assert data, "全部失败"
c = epubread.read_epub(data)
print("\n--- 解析结果 ---")
print("书名 :", c.title)
print("作者 :", c.authors)
print("语言 :", c.language, "| 年份:", c.year, "| 版权:", (c.rights or "")[:60])
print("标识 :", c.identifier)
print("章节 :", len(c.chapters))
print("封面 :", (len(c.cover) if c.cover else 0), "bytes", c.cover_media_type)
print("字数 :", c.word_count)
for ch in c.chapters[:5]:
    print("   -", (ch.title or "(无题)")[:60], "|", len(ch.html), "chars")
print("\n首章片段:")
print((c.chapters[0].html or "")[:300])