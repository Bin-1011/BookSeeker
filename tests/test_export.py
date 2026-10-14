import sys, os, zipfile, hashlib
import xml.etree.ElementTree as ET
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
from bookseeker.models import BookContent, Chapter
from bookseeker import exporter, config

config.bootstrap()
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")
os.makedirs(out, exist_ok=True)

chapters = [
    Chapter("第一回 甄士隐梦幻识通灵", "<p>此开卷第一回也。</p><p>作者自云：因曾历过一番梦幻之后，故将真事隐去。</p>"),
    Chapter("第二回 贾夫人仙逝扬州城", "<p>诗云：一局输赢料不真，香销茶尽尚逡巡。</p><blockquote><p>引用测试</p></blockquote>"),
]
content = BookContent(
    title="红楼梦（测试）", authors=["曹雪芹", "高鹗"], language="zh",
    chapters=chapters, source_name="Project Gutenberg", source_url="https://www.gutenberg.org/ebooks/24264",
    description="中国古典四大名著之一。", subjects=["古典小说", "章回体"], year="1791",
    rights="Public domain in the USA.",
    cover=b"\xff\xd8\xff\xe0" + b"FAKEJPEG" * 20, cover_media_type="image/jpeg",
)
written = exporter.export(content, out, ["epub", "txt", "html"], overwrite=True, basename="红楼梦-测试")
print(exporter.export_report(written))
print()

# ---- 校验 EPUB ----
ep = written["epub"]
zf = zipfile.ZipFile(ep)
names = zf.namelist()
print("EPUB 条目:", names)
assert names[0] == "mimetype", "mimetype 必须是第一个条目"
info = zf.getinfo("mimetype")
assert info.compress_type == zipfile.ZIP_STORED, "mimetype 必须不压缩"
assert zf.read("mimetype") == b"application/epub+zip"
required = ["META-INF/container.xml", "OEBPS/content.opf", "OEBPS/nav.xhtml",
            "OEBPS/toc.ncx", "OEBPS/style.css", "OEBPS/text/title.xhtml",
            "OEBPS/text/ch0001.xhtml", "OEBPS/text/ch0002.xhtml", "OEBPS/images/cover.jpg"]
for r in required:
    assert r in names, "缺少 " + r
bad = zf.testzip()
assert bad is None, "zip 损坏: %s" % bad
# 所有 XML/XHTML 必须良构
for n in names:
    if n.endswith((".opf", ".ncx", ".xhtml", ".xml")):
        try:
            ET.fromstring(zf.read(n))
        except Exception as e:
            raise AssertionError("%s 不是良构 XML: %s" % (n, e))
print("EPUB 结构 / mimetype / XHTML 良构性: 全部通过")

opf = zf.read("OEBPS/content.opf").decode()
assert 'properties="nav"' in opf and 'properties="cover-image"' in opf
assert '<dc:title>红楼梦（测试）</dc:title>' in opf
assert opf.count("<dc:creator") == 1 and "<dc:contributor" in opf
spine = opf.split("<spine")[1].split("</spine>")[0]
print("spine 顺序:", [l.strip() for l in spine.splitlines() if "itemref" in l])

nav = zf.read("OEBPS/nav.xhtml").decode()
assert "第一回" in nav and "第二回" in nav
ncx = zf.read("OEBPS/toc.ncx").decode()
assert "playOrder" in ncx
print("目录 nav + ncx: 通过")

# ---- 校验 TXT ----
t = written["txt"]
txt = t.read_text(encoding="utf-8")
assert "红楼梦（测试）" in txt and "曹雪芹, 高鹗" in txt and "第一回" in txt and "此开卷第一回也。" in txt
print("TXT 前 12 行:")
print("\n".join(txt.splitlines()[:12]))
print("TXT 校验通过, 编码 =", writer_enc := "utf-8")

# ---- 校验 HTML ----
h = written["html"].read_text(encoding="utf-8")
assert "<nav class=\"toc\">" in h and 'id="ch0001"' in h and "红楼梦（测试）" in h
print("HTML 校验通过")

# ---- 折行测试 ----
from bookseeker.convert.txt import wrap_by_width, display_width
s = "这是一段很长的中文文本，用来测试按显示宽度自动折行的功能是否正常工作。Hello wonderful world of books."
w = wrap_by_width(s, 20)
print("折行结果:")
for line in w.splitlines():
    print("  [%2d] %s" % (display_width(line), line))
assert all(display_width(l) <= 20 for l in w.splitlines()), "折行超宽"
print("ALL OK")