"""青空文庫（Aozora Bunko）书源 —— 日本公有领域文学作品。

青空文庫官方提供整库索引 CSV（zip 包，UTF-8）：首次使用下载索引并缓存到
``config.HOME / "aozora_index.csv"``（30 天有效），之后检索都在本地索引里完成；
正文是打包在 zip 里的 Shift_JIS 纯文本，解析时切掉头尾信息块与「符号说明」块、
清理 ``［＃…］`` 注记与ルビ，再按 ``［＃…は中見出し］`` 注记切章。只依赖标准库。
"""

from __future__ import annotations

import csv, io, re, threading, time, unicodedata, zipfile
from dataclasses import dataclass
from pathlib import Path

from .. import config as config_mod
from .. import http as http_mod
from ..convert import htmlutil
from ..models import Book, BookContent, Chapter
from .base import Source, SourceError

#: 整库索引（已实测可下载，约 2MB；解压后约 16MB CSV）
INDEX_URL = "https://www.aozora.gr.jp/index_pages/list_person_all_extended_utf8.zip"
#: 本地缓存文件名（放在 config.HOME 下）与有效期（秒）
INDEX_FILENAME = "aozora_index.csv"
INDEX_TTL = 30 * 86400

# 下面这些列名全部抄自真实 CSV 表头。人物 6 列按「姓 名 姓読み 名読み 姓ローマ字 名ローマ字」
# 顺序写在 _PERSON 里（用｜分隔；不能用逗号，姓氏里可能有逗号）。
COL_ID = "作品ID"
COL_TITLE, COL_TITLE_KANA, COL_SUBTITLE = "作品名", "作品名読み", "副題"
COL_CARD_URL, COL_ROLE, COL_PUBDATE, COL_CHARS = "図書カードURL", "役割フラグ", "公開日", "文字数"
COL_TXT_URL, COL_HTML_URL = "テキストファイルURL", "XHTML/HTMLファイルURL"
COL_BASEBOOK = "底本名1"
COL_PUBLISHER, COL_PUBYEAR = "底本出版社名1", "底本初版発行年1"
#: 人物列名（顺序：姓／名／姓読み／名読み／姓ローマ字／名ローマ字）
_PERSON = "姓｜名｜姓読み｜名読み｜姓ローマ字｜名ローマ字"
#: 角色列里代表「作者」的值
ROLE_AUTHOR = "著者"

# 正文相关正则：見出し注记 / 青空注记 / ルビ / 分隔线 / 符号说明 / 页脚
_HEADING_RE = re.compile(r"［＃[^］]{0,80}は[^］]{0,8}見出し］")           # ［＃…は中見出し］
_HEADING_NAME_RE = re.compile(r"［＃「([^」]{1,60})」は[^］]{0,8}見出し］")  # 标题文字
_ANNOT_RE = re.compile(r"［[＃]?[^］]{0,200}］")                          # ［＃…］及其它引用
_RUBY_RE = re.compile(r"《[^》]{0,40}》")                                 # ルビ
_BAR_RE = re.compile(r"[｜|](?=[\u4e00-\u9fff\u3005])")                  # ルビ起始符
_RULE_RE = re.compile(r"^\s*-{20,}\s*$")                                 # 分隔线
_LEGEND_RE = re.compile(r"テキスト中に現れる記号|記号について")            # 符号说明块
_FOOTER_RE = re.compile(                                                 # 底本／入力信息块
    r"^(?:底本|初出|入力|校正|青空文庫作成ファイル|ファイル作成|このファイル|"
    r"底本の親本|公開日|校正者|入力者|※|＊)"
)
#: 繁简／异体字归一化表：每组用 ``｜`` 分隔，组内固定 2 个字符
#: （``异体字或简体字`` + ``日文常用字形``），让「广／廣／広」「鐵／铁／鉄」等
#: 字形差异能互相命中。用显式分隔符而不是纯定长，避免以后加字时错位。
_VARIANTS = (
    "广広｜国国｜实実｜学学｜变変｜边辺｜医医｜艺芸｜驿駅｜应応｜"
    "欧欧｜樱桜｜横横｜黄黄｜觉覚｜乐乐｜两両｜猎猟｜龙竜｜劳労｜"
    "楼楼｜录録｜湾湾｜万万｜与与｜战戦｜关関｜观観｜归帰｜气気｜"
    "岁歳｜号号｜据拠｜旧旧｜峡峡｜狭狭｜献献｜读読｜卖売｜麦麦｜"
    "来来｜发発｜废廃｜图図｜会会｜绘絵｜怀懐｜续続｜独独｜乱乱｜"
    "览覧｜说説｜剩剰｜剑剣｜齐斉｜齿歯｜儿児｜丰豊｜译訳｜驱駆｜"
    "俭倹｜转転｜恶悪｜压圧｜为為｜真真｜内内｜静静｜增増｜德徳｜"
    "默默｜歩歩｜毎毎｜泪涙｜舍舎｜装装｜藏蔵｜证証｜赞賛｜辞辞｜"
    "迟遅｜歷歴｜铁鉄｜点点｜党党｜盗盗｜乡郷｜稲稲｜恼悩｜拝拝｜"
    "抜抜｜黒黒｜争争｜届届｜带帯｜対対｜湿湿｜兽獣｜状状｜勉勉｜"
    "龍竜｜廣広｜藝芸｜譯訳｜鐵鉄｜戰戦｜遲遅｜靑青｜迹跡｜勲勲｜"
    "韵韻｜呉呉｜姫姫｜壌壌｜婬淫｜双双｜叙叙｜唇唇｜嘱嘱｜慎慎｜"
    "勅勅｜勧勧｜歓歓｜歯歯｜渋渋｜獣獣｜畳畳｜穂穂｜繋繋｜翻翻｜"
    "詰詰｜謡謡｜訳訳｜誉誉｜譲譲｜踪踪｜恥恥"
)
#: 片假名 -> 平假名（检索时统一假名形态）
_KATA = "ァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソゾタダチヂッツヅテデトドナニヌネノハバパヒビピフブプヘベペホボポマミムメモャヤュユョヨラリルレロヮワヰヱヲンヴヵヶ"
_HIRA = "ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞただちぢっつづてでとどなにぬねのはばぱひびぴふぶぷへべぺほぼぽまみむめもゃやゅゆょよらりるれろゎわゐゑをんゔゕゖ"
_KATA_TO_HIRA = str.maketrans(_KATA, _HIRA)
#: 检索时忽略的标点与空白
_PUNCT_RE = re.compile(
    r"[\s\u3000・･、。,.!?！？「」『』（）()\[\]{}〈〉《》【】―—‐\-–~〜:：;；/／\\|｜'\"’”“”]+"
)
#: 对话／引用开头的字符，这些行不当作見出し
_DIALOG_STARTS = "「『（(“\"'［"

#: 繁简归一化表：{异体字: 日文常用字}；先去掉换行，再按｜分组，长度不是 2 的组跳过
_VARIANT_MAP = str.maketrans(
    {g[0]: g[1] for g in _VARIANTS.replace("\n", "").split("｜")
     if len(g) == 2 and g[0] != g[1]}
)
#: 人物列名（顺序与 _PERSON 一致）
_PERSON_COLS = tuple(_PERSON.split("｜"))


def _normalize(text: str) -> str:
    """归一化检索文本：NFKC + 繁简／异体字统一 + 片假名转平假名 + 去标点 + 小写。"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    return _PUNCT_RE.sub("", s.translate(_VARIANT_MAP).translate(_KATA_TO_HIRA)).lower()


def _to_hira(text: str) -> str:
    """NFKC + 片假名转平假名（读音列本身就是平假名，这里做一层保险）。"""
    return unicodedata.normalize("NFKC", text or "").translate(_KATA_TO_HIRA)


@dataclass
class _Work:
    """索引里聚合后的一个作品（同一「作品ID」的多行合并成一条）。"""

    uid: str
    title: str
    title_kana: str = ""
    subtitle: str = ""
    card_url: str = ""
    txt_url: str = ""
    html_url: str = ""
    pubdate: str = ""
    chars: int = 0
    basebook: str = ""
    publisher: str = ""
    pubyear: str = ""
    authors: list[str] = None            # 作者（CSV 里 役割フラグ == 著者 的行）
    authors_kana: list[str] = None       # 作者读音
    authors_romaji: list[str] = None     # 作者罗马字
    contributors: list[str] = None       # 译者／校阅者等贡献者
    n_title: str = ""                    # 下面几个是预先算好的归一化检索字段
    n_kana: str = ""
    n_author: str = ""
    n_author_kana: str = ""
    n_romaji: str = ""

    def __post_init__(self) -> None:
        self.authors = []
        self.authors_kana = []
        self.authors_romaji = []
        self.contributors = []

    def finalize(self) -> "_Work":
        """预先归一化各检索字段，避免每次检索重复计算。"""
        self.n_title = _normalize(self.title)
        self.n_kana = _normalize(_to_hira(self.title_kana))
        self.n_author = _normalize("".join(self.authors))
        self.n_author_kana = _normalize(_to_hira("".join(self.authors_kana)))
        self.n_romaji = _normalize(" ".join(self.authors_romaji))
        return self


def _parse_works(rows: list[list[str]]) -> dict[str, _Work]:
    """把 CSV 行按「作品ID」聚合成 ``{作品ID: _Work}``。"""
    if not rows:
        return {}
    idx = {h.strip(): i for i, h in enumerate(rows[0])}

    def cell(row: list[str], col: str) -> str:
        i = idx.get(col)
        return (row[i] or "").strip() if i is not None and i < len(row) else ""

    def person(row: list[str], *cols: str) -> str:
        return "".join(cell(row, c) for c in cols).strip()

    works: dict[str, _Work] = {}
    for row in rows[1:]:
        uid = cell(row, COL_ID)
        if not row or not uid:
            continue
        work = works.get(uid)
        if work is None:
            # 注意：官方 extended CSV 共 55 列，实测「没有」文字数列，
            # 所以这里做存在性判断，有就取、没有就保持 0（不猜、不报错）。
            chars = idx.get(COL_CHARS)
            chars = cell(row, COL_CHARS) if chars is not None else ""
            work = _Work(
                uid=uid, title=cell(row, COL_TITLE),
                title_kana=cell(row, COL_TITLE_KANA), subtitle=cell(row, COL_SUBTITLE),
                card_url=cell(row, COL_CARD_URL),
                # 优先「テキストファイルURL」（实测 _txt_ 结尾的是无ルビ版），
                # 没有则退化为 XHTML/HTML 版
                txt_url=cell(row, COL_TXT_URL) or cell(row, COL_HTML_URL),
                html_url=cell(row, COL_HTML_URL), pubdate=cell(row, COL_PUBDATE),
                chars=int(chars) if chars.isdigit() else 0,
                basebook=cell(row, COL_BASEBOOK), publisher=cell(row, COL_PUBLISHER),
                pubyear=cell(row, COL_PUBYEAR),
            )
            works[uid] = work
        # 只有「著者」行算作者，其余（翻訳者／校閲者／入力者…）算贡献者
        name, role = person(row, *_PERSON_COLS[:2]), cell(row, COL_ROLE)
        if role == ROLE_AUTHOR:
            if name and name not in work.authors:
                work.authors.append(name)
                kana = person(row, *_PERSON_COLS[2:4])
                if kana:
                    work.authors_kana.append(kana)
                # 注意用 (cell(row, p) for p in ...) 取值：写成
                # "for p in cols if cell(row, p)" 会 join 出列名本身
                romaji = " ".join(v for v in (cell(row, p) for p in _PERSON_COLS[4:]) if v)
                if not romaji:
                    # 个别版本的索引会把罗马字列留空，退而用 姓/名 拼一个占位
                    romaji = name
                work.authors_romaji.append(romaji)
        elif name and role and name not in work.contributors:
            work.contributors.append("%s（%s）" % (name, role))
    return works


def _extract_main_text(raw: str) -> str:
    """切掉头尾信息块，只留作品正文。

    青空纯文本的骨架是「分隔线夹住正文」，但带ルビ的正文前面还会多一段
    「テキスト中に現れる記号について」符号说明，实测（坊っちやん／こころ／三四郎）
    分隔线通常只有 **2 条**：

    * ``作品名／作者 ─── 符号说明 ─── 正文 …（一直到文件末尾）``
    * 没有符号说明时：``作品名／作者 ─── 正文 …``

    也就是说正文是从「最后一条分隔线」之后一直延伸到文件末尾，末尾的
    「底本／入力／校正」信息块再由页脚规则切掉。绝对不能用
    「第一条与最后一条分隔线之间」——那样只会取到符号说明。
    """
    lines = (raw or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rules = [i for i, ln in enumerate(lines) if _RULE_RE.match(ln)]
    if rules:
        start, end = rules[0] + 1, len(lines)
        # 跳过符号说明块：它夹在头两条分隔线之间
        if len(rules) >= 2 and _LEGEND_RE.search("\n".join(lines[start:rules[1]])):
            start = rules[1] + 1
    else:
        # 没有分隔线（短篇／诗歌）：跳过「作品名／作者／译者」头部，从第一个空行后开始
        start, end = 0, len(lines)
        for i, ln in enumerate(lines):
            if not ln.strip():
                start = i + 1
                break
    body = lines[start:end]
    # 再从前往后砍掉页脚（底本／入力／校正／青空文庫作成ファイル…）
    for i, ln in enumerate(body):
        if _FOOTER_RE.match(ln.strip()):
            return "\n".join(body[:i])
    return "\n".join(body)


def _clean_body(text: str) -> str:
    """去掉青空注记、ルビ记号、全角行首缩进与多余空行。"""
    out = []
    for line in _ANNOT_RE.sub("", text or "").split("\n"):
        line = _RUBY_RE.sub("", line)
        line = _BAR_RE.sub("", line)
        out.append(line.replace("\u3000", " ").rstrip())
    text = re.sub(r"[ \t]+\n", "\n", "\n".join(out))
    return re.sub(r"\n{3,}", "\n\n", text).strip("\n")


def _is_heading(line: str, lines: list[str], i: int) -> bool:
    """启发式判断某行是不是「見出し」（章节标题）。

    只作为**兜底**：青空官方文本里的标题都带 ``［＃…は中見出し］`` 注记，
    优先用注记判定；没有注记的老文本才用这个启发式
    （行首 1~4 个空白、长度较短、上下有空行、不是对话开头）。
    """
    stripped = line.strip()
    if not stripped or len(stripped) >= 30 or stripped[0] in _DIALOG_STARTS:
        return False
    if stripped.startswith(("-", "*", "＊", "※", "#", "＃", "〔")):
        return False
    indent = len(line) - len(line.lstrip(" \u3000"))
    if indent < 1 or indent > 4:
        return False
    prev_blank = i == 0 or not lines[i - 1].strip()
    next_blank = i + 1 >= len(lines) or not lines[i + 1].strip()
    return prev_blank and next_blank


def _heading_name(line: str) -> str:
    """取見出し行的显示标题：优先注记里引用的名字，其次去掉注记后的整行。"""
    m = _HEADING_NAME_RE.search(line)
    if m:
        return m.group(1).replace("\u3000", " ").strip()
    return _BRACKET_RE.sub("", _ANNOT_RE.sub("", line)).strip()


def _split_chapters(body: str, title: str) -> list[tuple[str, str]]:
    """按「見出し」切章，返回 ``[(章节标题, 正文), ...]``；切不出来就退化成单章。

    第一优先是青空官方注记 ``［＃…は中見出し］``（大／中／小見出し都认），
    这样能真正把「坊っちやん」切成十二章、「こころ」切成一〇九章；
    没有注记的文本才退回行首缩进启发式。
    """
    lines = body.split("\n")
    marks = [i for i, ln in enumerate(lines) if _HEADING_RE.search(ln)]
    annotated = bool(marks)
    if not marks:
        marks = [i for i, ln in enumerate(lines) if _is_heading(ln, lines, i)]
    # 没有标题，或标题过密（平均每 25 行一个以上）多半是启发式误判，退化成单章。
    # 注记是青空官方标注，天然可信，不受「过密」这条限制
    # （こころ 实测 113 个見出し / 1566 行，属于正常密度）。
    if not marks or len(lines) < 12:
        return [(title, body)]
    if not annotated and len(marks) > max(3, len(lines) // 25):
        return [(title, body)]

    chapters: list[tuple[str, str]] = []
    head = "\n".join(lines[: marks[0]]).strip()
    if head:
        chapters.append((title, head))
    for n, pos in enumerate(marks):
        stop = marks[n + 1] if n + 1 < len(marks) else len(lines)
        name = _heading_name(lines[pos]) or title
        content = "\n".join(lines[pos + 1 : stop]).strip()
        if content:
            chapters.append((name, content))
        elif chapters:
            # 空章节（如大見出し后面紧跟中見出し）：并入上一章并改名
            chapters[-1] = (name, chapters[-1][1])
    return chapters or [(title, body)]


class AozoraSource(Source):
    """青空文庫：本地整库索引检索 + 单篇正文下载。"""

    id = "aozora"
    name = "青空文库 青空文庫"
    homepage = "https://www.aozora.gr.jp"
    description = "日本公有领域文学（青空文庫）整库索引，支持中日文检索"
    languages = ("ja",)
    supports = ("epub", "txt", "html")
    priority = 25
    enabled_by_default = True
    requires_network = True

    #: 进程内索引缓存：``(文件 mtime, {作品ID: _Work})``
    _index_cache: tuple[float, dict[str, _Work]] | None = None
    _index_lock = threading.Lock()

    @property
    def index_path(self) -> Path:
        """本地索引 CSV 的路径：``config.HOME / aozora_index.csv``。"""
        home = getattr(self.cfg, "HOME", None) or config_mod.HOME
        try:
            return Path(home) / INDEX_FILENAME
        except Exception:
            return config_mod.HOME / INDEX_FILENAME

    # ------------------------------------------------------------------
    # 索引：下载 / 缓存 / 聚合
    # ------------------------------------------------------------------

    def _download_index(self, progress=None) -> str:
        """下载索引 zip、解压成 CSV 文本并写入本地缓存（供 30 天内复用）。"""
        self.report(progress, 0, 1, "正在下载青空文庫索引（约 2MB，首次较慢）…")
        try:
            # 本模块自己按 30 天 TTL 管缓存，故不复用 http 层缓存
            resp = self.get(INDEX_URL, use_cache=False, timeout=180)
        except Exception as exc:
            raise SourceError(
                "无法下载青空文庫索引 %s：%s（也可手动下载 list_person_all_extended"
                "_utf8.zip 解压后另存为 %s）" % (INDEX_URL, exc, self.index_path)
            ) from exc
        try:
            zf = zipfile.ZipFile(io.BytesIO(resp.data or b""))
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")] or zf.namelist()
            if not names:
                raise ValueError("压缩包里没有文件")
            text = zf.read(names[0]).decode("utf-8-sig", "replace")
        except Exception as exc:
            raise SourceError("青空文庫索引不是有效的 zip 包：%s" % exc) from exc
        try:
            path = self.index_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError:
            pass  # 缓存写不进去不影响本次检索，只是下次还要重下
        return text

    def _index_text(self, progress=None) -> str:
        """拿索引文本：本地缓存 30 天内直接用，否则重新下载（过期文件兜底）。"""
        path = self.index_path
        try:
            if (time.time() - path.stat().st_mtime) < INDEX_TTL:
                return path.read_text(encoding="utf-8")
        except OSError:
            pass
        try:
            return self._download_index(progress)
        except SourceError:
            return path.read_text(encoding="utf-8")  # 下载失败就退回过期缓存

    def _load_index(self, progress=None) -> dict[str, _Work]:
        """加载并缓存已聚合的作品索引（线程安全，按文件 mtime 失效）。"""
        path = self.index_path
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        hit = AozoraSource._index_cache
        if hit is not None and mtime > 0 and hit[0] == mtime:
            return hit[1]
        with AozoraSource._index_lock:
            hit = AozoraSource._index_cache
            if hit is not None and mtime > 0 and hit[0] == mtime:
                return hit[1]
            works = _parse_works(list(csv.reader(io.StringIO(self._index_text(progress)))))
            for work in works.values():
                work.finalize()
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            AozoraSource._index_cache = (mtime, works)
            return works

    def warmup(self, progress=None) -> None:
        """预热：下载（或读取）索引，让首次检索不再卡顿。"""
        self._load_index(progress)

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    @staticmethod
    def _score(work: _Work, q: str) -> int:
        """给单个作品与查询词打分；0 表示不匹配。

        档次：作品名／作者名（精确 1000／前缀 900／包含 800）
        > 作品名読み（600/500/400）> 作者读音 > 作者罗马字。
        把「作者名」放在与「作品名」同一档，是为了让搜「夏目漱石」时
        他本人写的作品（如 坊っちゃん）优先于《夏目漱石論》这类评论文章。
        """
        for field, base in (
            (work.n_title, 4), (work.n_author, 4), (work.n_kana, 2),
            (work.n_author_kana, 1), (work.n_romaji, 1),
        ):
            if not field or q not in field:
                continue
            rank = 2 if field == q else (1 if field.startswith(q) else 0)
            return 200 * base + 100 * rank
        return 0

    @staticmethod
    def _to_book(work: _Work) -> Book:
        """把索引记录转成 :class:`Book`。"""
        base = work.basebook
        if work.publisher:
            base = ("%s（%s）" % (base, work.publisher)).strip("（）")
        book = Book(
            source=AozoraSource.id, uid=work.uid, title=work.title,
            authors=list(work.authors), language="ja",
            year=work.pubyear[:4] if work.pubyear else "",
            description=" / ".join(b for b in (base, work.pubyear) if b),
            # 卡片页；CSV 里没有就退化到索引页
            url=work.card_url or "https://www.aozora.gr.jp/index_pages/",
        )
        book.source_name = AozoraSource.name
        book.extra.update({
            "txt_url": work.txt_url, "html_url": work.html_url, "chars": work.chars,
            "title_kana": work.title_kana, "subtitle": work.subtitle, "pubdate": work.pubdate,
            "contributors": list(work.contributors), "basebook": work.basebook,
            "publisher": work.publisher, "pubyear": work.pubyear,
        })
        return book

    def search(self, query: str, limit: int = 20) -> list[Book]:
        """在本地索引里检索。任何异常都不外抛，失败时返回 ``[]``。"""
        try:
            q = _normalize(query or "")
            if not q:
                return []
            hits: list[tuple[int, _Work]] = []
            for work in self._load_index().values():
                if not (work.txt_url or work.html_url):
                    continue  # 没有正文的作品（未公開／仅卡片）跳过
                score = self._score(work, q)
                if score:
                    hits.append((score, work))
            # 排序：精确 > 前缀 > 包含；同档次按文字数多的优先，其次公开日新的优先
            hits.sort(key=lambda it: (-it[0], -it[1].chars, _date_key(it[1].pubdate)))
            return [self._to_book(w) for _, w in hits[: max(1, int(limit or 20))]]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # 正文下载
    # ------------------------------------------------------------------

    def _download_text(self, url: str) -> str:
        """下载正文。青空把 ``.txt`` 打包成 zip（Shift_JIS），也兼容直接给 txt。"""
        try:
            resp = self.get(url, use_cache=True, timeout=120)
        except Exception as exc:
            raise SourceError("下载正文失败：%s（%s）" % (url, exc)) from exc
        data = resp.data or b""
        if data[:2] == b"PK":
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
                names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
                names = names or zf.namelist()
                if not names:
                    raise ValueError("压缩包里没有文件")
                names.sort(key=lambda n: ("_ruby_" in n, len(n)))  # 优先无ルビ版
                data = zf.read(names[0])
            except Exception as exc:
                raise SourceError("正文压缩包无法解压：%s" % exc) from exc
        # 交给 http 层嗅探编码（青空是 Shift_JIS）
        return http_mod.decode(data, None, resp.content_type)

    def fetch(self, book: Book, progress=None) -> BookContent:
        """下载并解析正文，按見出し启发式切章。"""
        extra = book.extra or {}
        url = extra.get("txt_url") or extra.get("html_url") or ""
        if not url:
            raise SourceError("这条记录没有可下载的正文地址：%s" % (book.title or book.uid))
        self.report(progress, 0, 3, "正在下载《%s》…" % (book.title or book.uid))
        raw = self._download_text(url)
        if not raw.strip():
            raise SourceError("下载到的正文为空：%s" % url)
        self.report(progress, 1, 3, "正在解析正文…")
        body = _extract_main_text(raw) or raw
        if not body.strip():
            raise SourceError("正文解析后为空：%s" % url)

        title = book.title or ""
        subtitle = extra.get("subtitle") or ""
        if subtitle and subtitle not in title:
            title = ("%s %s" % (title, subtitle)).strip()
        # 先按原始正文（注记还在）切章，再逐章清理：
        # ［＃…は中見出し］这类注记一旦被清理掉就再也认不出标题了
        parts = _split_chapters(body, title)
        self.report(progress, 2, 3, "正在生成章节（%d 章）…" % len(parts))
        chapters = [Chapter(title=name or title, html=htmlutil.text_to_xhtml(_clean_body(c)))
                    for name, c in parts]
        chapters = [ch for ch in chapters if ch.html]
        if not chapters:
            chapters = [Chapter(title=title, html=htmlutil.text_to_xhtml(_clean_body(body)))]

        # 简介用底本／出版社／公开日／贡献者拼出来
        bits = ["底本：%s" % extra["basebook"]] if extra.get("basebook") else []
        bits += [str(extra[k]) for k in ("publisher", "pubyear") if extra.get(k)]
        if extra.get("pubdate"):
            bits.append("青空文庫公開日：%s" % extra["pubdate"])
        if extra.get("contributors"):
            bits.append("贡献者：" + "、".join(extra["contributors"]))

        content = BookContent(
            title=title, authors=list(book.authors), language="ja", chapters=chapters,
            identifier="aozora-%s" % book.uid, source_name=self.name,
            source_url=book.url, description="／".join(bits),
            year=(extra.get("pubyear") or "")[:4], publisher=extra.get("publisher") or "",
            rights="青空文庫（日本公有领域 / Public Domain）",
        )
        self.report(progress, 3, 3, "完成")
        return content


def _date_key(pubdate: str) -> tuple:
    """公开日「越新越靠前」的排序键；空值排最后。"""
    d = (pubdate or "").strip()[:10]
    return (1, "") if not d else (0, "".join(chr(0x10FFFF - ord(c)) for c in d))


#: 便于注册表收集（``_collect_sources`` 也会自动发现 AozoraSource）
SOURCES = (AozoraSource,)
