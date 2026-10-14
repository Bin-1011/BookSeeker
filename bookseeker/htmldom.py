"""轻量 HTML DOM 与选择器引擎。

目的是让 BookSeeker 在「零第三方依赖」的前提下依然能舒服地解析网页，
相当于内置了一个小号 BeautifulSoup。支持的语法：

    doc.find_all("div.item")
    doc.find_all("ul.list > li")
    doc.find("a.title[href]")
    doc.find_all("div#main .content p")

即：标签名、``.类名``、``#id``、``[属性]``、``[属性=值]``，以及空格（后代）
和 ``>``（直接子元素）组合。
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser
from typing import Iterable, Iterator

#: 自闭合 / 空元素
VOID_TAGS = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)

#: 提取文本时应当跳过的标签
SKIP_TEXT_TAGS = frozenset(
    "script style noscript template head title svg iframe canvas".split()
)

#: 遇到这些标签开始，就先把已打开的同类标签自动闭合
_AUTO_CLOSE = {
    "li": {"li"},
    "p": {"p"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    "option": {"option"},
    "optgroup": {"optgroup", "option"},
    "tr": {"tr", "td", "th"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "thead": {"tbody", "tfoot", "tr"},
    "tbody": {"thead", "tfoot"},
    "tfoot": {"thead", "tbody"},
    "colgroup": {"colgroup"},
}

#: 这些块级标签会把尚未闭合的 <p> 顶掉
_BLOCK_CLOSES_P = frozenset(
    """address article aside blockquote details div dl fieldset figcaption figure
    footer form h1 h2 h3 h4 h5 h6 header hgroup hr main nav ol p pre section
    table ul""".split()
)


class Node:
    """一个 HTML 元素节点。文本子节点以 ``str`` 形式混在 ``children`` 里。"""

    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict | None = None, parent: "Node | None" = None):
        self.tag = (tag or "").lower()
        self.attrs: dict[str, str] = dict(attrs or {})
        self.children: list[Node | str] = []
        self.parent = parent

    # -- 属性 -------------------------------------------------------------

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    @property
    def id(self) -> str:
        return self.attrs.get("id", "") or ""

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.attrs.get(name.lower(), default)

    def has_class(self, name: str) -> bool:
        return name in self.classes

    def append(self, child: "Node | str") -> None:
        if isinstance(child, Node):
            child.parent = self
        self.children.append(child)

    # -- 遍历 -------------------------------------------------------------

    def iter_descendants(self) -> Iterator["Node"]:
        """深度优先遍历所有后代元素（不含自身）。"""
        stack = list(reversed(self.children))
        while stack:
            item = stack.pop()
            if isinstance(item, Node):
                yield item
                stack.extend(reversed(item.children))

    def iter_nodes(self) -> Iterator["Node"]:
        """包含自身在内的所有元素。"""
        yield self
        yield from self.iter_descendants()

    def parents(self) -> Iterator["Node"]:
        p = self.parent
        while p is not None:
            yield p
            p = p.parent

    # -- 文本 -------------------------------------------------------------

    def _collect_text(self, skip: frozenset[str], out: list[str]) -> None:
        for child in self.children:
            if isinstance(child, str):
                out.append(child)
            elif child.tag not in skip:
                child._collect_text(skip, out)

    def text(self, strip: bool = False, skip: frozenset[str] = SKIP_TEXT_TAGS) -> str:
        """节点内所有可见文本（已做 HTML 实体解码）。"""
        out: list[str] = []
        self._collect_text(skip, out)
        value = "".join(out)
        if strip:
            value = re.sub(r"\s+", " ", value).strip()
        return value

    # -- 选择器 -----------------------------------------------------------

    def find_all(self, selector: str) -> list["Node"]:
        return select_all(self, selector)

    def find(self, selector: str) -> "Node | None":
        found = select_all(self, selector, limit=1)
        return found[0] if found else None

    def find_tag(self, tag: str) -> "Node | None":
        for node in self.iter_descendants():
            if node.tag == tag:
                return node
        return None

    def find_all_tags(self, tag: str) -> list["Node"]:
        return [n for n in self.iter_descendants() if n.tag == tag]

    # -- 序列化 -----------------------------------------------------------

    def _open_tag(self) -> str:
        parts = ["<", self.tag]
        for k, v in self.attrs.items():
            if v is None or v == "":
                parts.append(" %s" % k)
            else:
                parts.append(' %s="%s"' % (k, escape(str(v), quote=True)))
        parts.append(">")
        return "".join(parts)

    def inner_html(self) -> str:
        out: list[str] = []
        for child in self.children:
            if isinstance(child, str):
                out.append(child)
            else:
                out.append(child.outer_html())
        return "".join(out)

    def outer_html(self) -> str:
        # 空元素输出成自闭合形式（<br/>、<img .../>），这样既是合法 XML/XHTML，
        # 也是合法 HTML5 —— 直接拿去写 EPUB 才不会出现标签不匹配。
        if self.tag in VOID_TAGS:
            return self._open_tag()[:-1] + "/>"
        return "%s%s</%s>" % (self._open_tag(), self.inner_html(), self.tag)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        cls = ".".join(sorted(self.classes))
        return "<Node %s%s%s>" % (
            self.tag, ("#" + self.id) if self.id else "", ("." + cls) if cls else ""
        )


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack: list[Node] = [self.root]

    # -- 内部 -------------------------------------------------------------

    def _close_implied(self, tag: str) -> None:
        if tag == "p" or tag in _BLOCK_CLOSES_P:
            for idx in range(len(self.stack) - 1, 0, -1):
                if self.stack[idx].tag == "p":
                    del self.stack[idx:]
                    break
        closing = _AUTO_CLOSE.get(tag)
        if closing:
            while len(self.stack) > 1 and self.stack[-1].tag in closing:
                self.stack.pop()

    # -- HTMLParser 回调 --------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        self._close_implied(tag)
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs})
        self.stack[-1].append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs})
        self.stack[-1].append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for idx in range(len(self.stack) - 1, 0, -1):
            if self.stack[idx].tag == tag:
                del self.stack[idx:]
                return
        # 没有匹配的开标签：忽略

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)

    def handle_comment(self, data: str) -> None:  # 注释直接丢弃
        pass

    def handle_decl(self, decl: str) -> None:
        pass

    def handle_pi(self, data: str) -> None:
        pass


def parse(html: str) -> Node:
    """把 HTML 字符串解析成 DOM，返回 ``#document`` 根节点。"""
    builder = _TreeBuilder()
    try:
        builder.feed(html)
        builder.close()
    except Exception:
        pass
    return builder.root


def parse_fragment(html: str) -> Node:
    """解析一段 HTML 片段（同样返回文档根）。"""
    return parse(html)


# --------------------------------------------------------------------------
# 选择器
# --------------------------------------------------------------------------

_STEP_TOKEN = re.compile(r"([a-zA-Z][\w\-]*)|\.([\w\-]+)|#([\w\-]+)|\[([^\]]*)\]|(\*)")


class Step:
    __slots__ = ("tag", "classes", "id", "attrs", "child")

    def __init__(self, tag="*", classes=None, eid=None, attrs=None, child=False):
        self.tag = tag
        self.classes = classes or set()
        self.id = eid
        self.attrs = attrs or {}
        self.child = child


def _parse_step(raw: str) -> Step:
    raw = raw.strip()
    child = False
    if raw.startswith(">"):
        child = True
        raw = raw[1:].strip()
    if not raw:
        raise ValueError("选择器片段为空")
    step = Step(child=child)
    pos = 0
    while pos < len(raw):
        ch = raw[pos]
        if ch in " \t":
            pos += 1
            continue
        if ch == ".":
            m = re.match(r"\.([\w\-]+)", raw[pos:])
            if not m:
                raise ValueError("非法的类选择器：%r" % raw)
            step.classes.add(m.group(1))
            pos += m.end()
        elif ch == "#":
            m = re.match(r"#([\w\-]+)", raw[pos:])
            if not m:
                raise ValueError("非法的 id 选择器：%r" % raw)
            step.id = m.group(1)
            pos += m.end()
        elif ch == "[":
            end = raw.find("]", pos)
            if end < 0:
                raise ValueError("非法的属性选择器：%r" % raw)
            body = raw[pos + 1 : end].strip()
            if "=" in body:
                key, val = body.split("=", 1)
                step.attrs[key.strip().lower()] = val.strip().strip("\"'")
            else:
                step.attrs[body.lower()] = None
            pos = end + 1
        elif ch == "*":
            step.tag = "*"
            pos += 1
        else:
            m = re.match(r"([a-zA-Z][\w\-]*)", raw[pos:])
            if not m:
                raise ValueError("无法解析的选择器片段：%r" % raw)
            step.tag = m.group(1).lower()
            pos += m.end()
    return step


def parse_selector(selector: str) -> list[list[Step]]:
    """把选择器解析成「组 -> 步骤」的结构（逗号分隔多组）。"""
    groups: list[list[Step]] = []
    for group in selector.split(","):
        group = group.strip()
        if not group:
            continue
        tokens = [t for t in re.split(r"\s+", group) if t]
        merged: list[str] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok == ">":
                if i + 1 < len(tokens):
                    merged.append(">" + tokens[i + 1])
                    i += 2
                    continue
                i += 1
                continue
            if tok.startswith(">"):
                merged.append(tok)
            else:
                merged.append(tok)
            i += 1
        groups.append([_parse_step(t) for t in merged])
    if not groups:
        raise ValueError("选择器为空：%r" % selector)
    return groups


def _matches(node: Node, step: Step) -> bool:
    if step.tag != "*" and node.tag != step.tag:
        return False
    if step.classes and not step.classes.issubset(node.classes):
        return False
    if step.id and node.attrs.get("id") != step.id:
        return False
    for key, val in step.attrs.items():
        if key not in node.attrs:
            return False
        if val is not None and node.attrs.get(key) != val:
            return False
    return True


def _select_group(root: Node, steps: list[Step], limit: int) -> list[Node]:
    bases = [root]
    for step in steps:
        found: list[Node] = []
        seen: set[int] = set()
        for base in bases:
            pool: Iterable[Node] = base.children if step.child else base.iter_descendants()
            for node in pool:
                if not isinstance(node, Node):
                    continue
                if id(node) in seen:
                    continue
                if _matches(node, step):
                    seen.add(id(node))
                    found.append(node)
                    if limit and len(found) >= limit:
                        break
            if limit and len(found) >= limit:
                break
        bases = found
        if not bases:
            return []
    return bases


def select_all(root: Node, selector: str, limit: int = 0) -> list[Node]:
    """在 ``root`` 的后代中执行选择器，返回按文档顺序去重后的结果。"""
    results: list[Node] = []
    seen: set[int] = set()
    for steps in parse_selector(selector):
        for node in _select_group(root, steps, limit):
            if id(node) not in seen:
                seen.add(id(node))
                results.append(node)
                if limit and len(results) >= limit:
                    return results
    return results