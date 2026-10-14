import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
from bookseeker import http

cases = [
    ("bom-utf8", b"\xef\xbb\xbfhello", "", "utf-8-sig"),
    ("http-gbk", "中文测试".encode("gbk"), "text/html; charset=gbk", "gb18030"),
    ("meta-sjis", ('<meta http-equiv="Content-Type" content="text/html; charset=Shift_JIS">').encode("ascii") + "あいうえお".encode("shift_jis"), "", "shift_jis"),
    ("meta-eucjp", ('<meta charset="euc-jp">').encode("ascii") + "こんにちは".encode("euc-jp"), "", "euc-jp"),
    ("plain-utf8", "hello 中文 world".encode("utf-8"), "", "utf-8"),
]
ok = True
for name, data, ct, expect in cases:
    got = http.detect_encoding(data, ct)
    flag = "OK " if got == expect else "FAIL"
    if got != expect: ok = False
    print(f"[{flag}] {name:12} -> {got} (expect {expect})")

print("-" * 50)
r = http.get_json("https://gutendex.com/books?search=dickens")
print("gutendex  :", r["count"], "results; first =", r["results"][0]["title"])
print("stats     :", http.default.stats)
print("ALL OK" if ok else "SOME FAILED")