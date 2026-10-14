import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
from bookseeker.convert import htmlutil as H

bad = """<div onclick="evil()"><script>alert(1)</script>
<h2>第一章</h2><p style="x">正文 & 符号 <b>粗体</b><br>换行</p>
<a href="javascript:bad()">恶意链接</a><a href="https://example.com">正常链接</a>
<font color=red>字体</font><marquee>滚动</marquee>
<table><tr><td>甲</td><td>乙</td></tr></table>
<img src="https://x/y.jpg" width=999 onerror="x()"><img src="javascript:x">
</div>"""
x = H.sanitize_to_xhtml(bad)
print("=== sanitize ===")
print(x)
print()
assert "script" not in x and "onclick" not in x and "javascript:" not in x, "清洗失败"
assert "<br/>" in x and "&amp;" in x, "转义失败"
assert "<table>" in x and "<td>" in x, "表格应保留"
assert "<font>" in x and "color" not in x, "font 保留但属性应被过滤"
print("=== html_to_text ===")
print(H.html_to_text(bad))
print()
plain = "第一行\n第二行\n\n第二段\n\n\n第三段"
print("=== text_to_xhtml ===")
print(H.text_to_xhtml(plain))
print()
print("=== pick_content ===")
n = H.pick_content("<html><body><div id='content'><p>正文在这里</p></div></body></html>", "div#content")
print("picked:", n.tag, n.text(strip=True))
print("looks_cjk:", H.looks_cjk("中文测试"), H.looks_cjk("hello world"))
print("ALL OK")