import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
from bookseeker import htmldom

HTML = """
<html><head><title>T</title><style>p{color:red}</style></head><body>
<div id="main" class="wrap">
  <ul class="list">
    <li class="item"><a href="/1" class="title">第一本</a><span class="au">作者甲</span>
    <li class="item"><a href="/2" class="title">第二本</a><span class="au">作者乙</span>
  </ul>
  <p>未闭合的段落
  <p>第二段 &amp; 实体
  <div class="foot"><a href="/x">尾</a></div>
</div>
</body></html>
"""
doc = htmldom.parse(HTML)
def show(label, nodes):
    print(f"{label}: {len(nodes)} -> {[n.text(strip=True) for n in nodes]}")

show('div.item', doc.find_all("div.item"))
show('li.item', doc.find_all("li.item"))
show('li > a.title', doc.find_all("li > a.title"))
show('a[href]', doc.find_all("a[href]"))
show('a[href="/2"]', doc.find_all('a[href="/2"]'))
show('div#main .au', doc.find_all("div#main .au"))
show('ul.list, div.foot', doc.find_all("ul.list, div.foot"))
print("li 数量(自动闭合):", len(doc.find_all("li")))
print("p  数量(自动闭合):", len(doc.find_all("p")), [p.text(strip=True) for p in doc.find_all("p")])
print("第一本书名:", doc.find("ul.list li a.title").text(strip=True), "| href =", doc.find("a.title").get("href"))
print("title 已跳过:", "T" not in doc.find("body").text())
print("实体解码:", "&" in doc.find_all("p")[1].text())