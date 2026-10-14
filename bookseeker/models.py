"""书源与书籍的统一数据模型。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: 语言代码 -> 中文显示名
LANGUAGE_NAMES = {
    "zh": "中文", "zh-Hans": "简体中文", "zh-Hant": "繁体中文",
    "ja": "日语", "en": "英语", "fr": "法语", "de": "德语",
    "es": "西班牙语", "it": "意大利语", "ru": "俄语", "ko": "韩语",
    "pt": "葡萄牙语", "nl": "荷兰语", "la": "拉丁语", "el": "希腊语",
    "fi": "芬兰语", "sv": "瑞典语", "pl": "波兰语", "da": "丹麦语",
    "cs": "捷克语", "hu": "匈牙利语", "tr": "土耳其语", "ar": "阿拉伯语",
    "he": "希伯来语", "hi": "印地语", "vi": "越南语", "th": "泰语",
    "eo": "世界语", "uk": "乌克兰语", "no": "挪威语", "ro": "罗马尼亚语",
}


#: 常见繁体字 -> 简体字。只收「确定性高、出现频率高」的字，
#: 用途是让简繁两种写法在检索排序和去重时能被看成同一个词。
TRAD_TO_SIMP_PAIRS: tuple[tuple[str, str], ...] = (
    ("們", "们"), ("個", "个"), ("這", "这"), ("來", "来"), ("時", "时"),
    ("會", "会"), ("對", "对"), ("開", "开"), ("關", "关"), ("門", "门"),
    ("問", "问"), ("間", "间"), ("見", "见"), ("現", "现"), ("實", "实"),
    ("學", "学"), ("國", "国"), ("說", "说"), ("話", "话"), ("語", "语"),
    ("讀", "读"), ("書", "书"), ("寫", "写"), ("記", "记"), ("認", "认"),
    ("讓", "让"), ("請", "请"), ("誰", "谁"), ("論", "论"), ("議", "议"),
    ("義", "义"), ("評", "评"), ("證", "证"), ("考", "考"), ("談", "谈"),
    ("紅", "红"), ("樓", "楼"), ("夢", "梦"), ("後", "后"), ("續", "续"),
    ("補", "补"), ("遊", "游"), ("記", "记"), ("傳", "传"), ("劍", "剑"),
    ("俠", "侠"), ("將", "将"), ("軍", "军"), ("戰", "战"), ("爭", "争"),
    ("師", "师"), ("廣", "广"), ("業", "业"), ("產", "产"), ("專", "专"),
    ("轉", "转"), ("運", "运"), ("達", "达"), ("適", "适"), ("選", "选"),
    ("錯", "错"), ("鍾", "钟"), ("鐘", "钟"), ("鐵", "铁"), ("銀", "银"),
    ("鋪", "铺"), ("織", "织"), ("絲", "丝"), ("線", "线"), ("統", "统"),
    ("經", "经"), ("給", "给"), ("結", "结"), ("網", "网"), ("緣", "缘"),
    ("紀", "纪"), ("紙", "纸"), ("級", "级"), ("紛", "纷"), ("終", "终"),
    ("馬", "马"), ("鳥", "鸟"), ("魚", "鱼"), ("龍", "龙"), ("鳳", "凤"),
    ("風", "风"), ("雲", "云"), ("電", "电"), ("氣", "气"), ("體", "体"),
    ("頭", "头"), ("臉", "脸"), ("腳", "脚"), ("長", "长"), ("東", "东"),
    ("車", "车"), ("萬", "万"), ("億", "亿"), ("歲", "岁"), ("縣", "县"),
    ("鄉", "乡"), ("親", "亲"), ("愛", "爱"), ("歡", "欢"), ("樂", "乐"),
    ("藥", "药"), ("醫", "医"), ("農", "农"), ("無", "无"), ("與", "与"),
    ("於", "于"), ("為", "为"), ("從", "从"), ("過", "过"), ("還", "还"),
    ("進", "进"), ("遠", "远"), ("邊", "边"), ("裡", "里"), ("麼", "么"),
    ("華", "华"), ("漢", "汉"), ("舊", "旧"), ("雖", "虽"), ("隨", "随"),
    ("願", "愿"), ("類", "类"), ("體", "体"), ("寶", "宝"), ("詩", "诗"),
    ("詞", "词"), ("畫", "画"), ("聲", "声"), ("葉", "叶"), ("樹", "树"),
    ("橋", "桥"), ("鄉", "乡"), ("豐", "丰"), ("燈", "灯"), ("陽", "阳"),
)

_TRAD_TO_SIMP = dict(TRAD_TO_SIMP_PAIRS)


def normalize_cjk(text: str) -> str:
    """把常见繁体字折叠成简体，让「红楼梦」和「紅樓夢」能被当成同一个词。"""
    if not text:
        return text
    if not any(ch in _TRAD_TO_SIMP for ch in text):
        return text
    return "".join(_TRAD_TO_SIMP.get(ch, ch) for ch in text)


def language_label(code: str) -> str:
    if not code:
        return "未知"
    if code in LANGUAGE_NAMES:
        return LANGUAGE_NAMES[code]
    base = code.split("-")[0].lower()
    return LANGUAGE_NAMES.get(base, code)


@dataclass
class Book:
    """一条检索结果（此时尚未下载正文）。"""

    source: str                     # 书源 id，如 "gutenberg"
    uid: str                        # 书源内唯一 id
    title: str
    authors: list[str] = field(default_factory=list)
    language: str = ""
    year: str = ""
    description: str = ""
    subjects: list[str] = field(default_factory=list)
    url: str = ""                   # 详情页地址
    #: 可直接下载的成品文件，形如 {"epub": "https://...", "txt": "https://..."}
    download_urls: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    source_name: str = ""           # 书源显示名（由注册表回填）

    @property
    def key(self) -> str:
        return "%s:%s" % (self.source, self.uid)

    @property
    def author_text(self) -> str:
        return ", ".join(self.authors) if self.authors else "不详"

    def to_row(self) -> tuple:
        return (
            self.title or "(无标题)",
            self.author_text,
            language_label(self.language),
            self.source_name or self.source,
            self.year or "",
        )


@dataclass
class Chapter:
    """一章 / 一节，``html`` 为已清洗的 XHTML 片段。"""

    title: str
    html: str = ""


@dataclass
class BookContent:
    """已下载的书籍正文。"""

    title: str
    authors: list[str] = field(default_factory=list)
    language: str = ""
    chapters: list[Chapter] = field(default_factory=list)
    identifier: str = ""
    source_name: str = ""
    source_url: str = ""
    description: str = ""
    subjects: list[str] = field(default_factory=list)
    year: str = ""
    publisher: str = ""
    rights: str = ""
    cover: bytes | None = None
    cover_media_type: str = "image/jpeg"

    @property
    def author_text(self) -> str:
        return ", ".join(self.authors) if self.authors else ""

    @property
    def word_count(self) -> int:
        text = re.sub(r"<[^>]+>", "", "\n".join(c.html for c in self.chapters))
        return len(re.sub(r"\s+", "", text))

    def plain_text(self) -> str:
        from .convert.htmlutil import html_to_text

        parts = []
        for ch in self.chapters:
            parts.append(html_to_text(ch.html))
        return "\n\n".join(parts)

    def guess_language(self) -> str:
        if self.language:
            return self.language.split("-")[0]
        sample = re.sub(r"<[^>]+>", "", "".join(c.html for c in self.chapters[:3]))[:4000]
        if not sample.strip():
            return "zh"
        kana = sum(1 for ch in sample if "\u3040" <= ch <= "\u30ff")
        cjk = sum(1 for ch in sample if "\u4e00" <= ch <= "\u9fff")
        if kana > max(5, len(sample) * 0.01):
            return "ja"
        if cjk > len(sample) * 0.15:
            return "zh"
        return "en"


def safe_filename(name: str, fallback: str = "book", max_len: int = 100) -> str:
    """把书名转成安全的文件名。"""
    name = (name or "").strip()
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = fallback
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name