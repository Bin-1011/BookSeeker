"""BookSeeker 主界面：暗色卡片式布局，视觉参考 kazumi（看番项目）。

设计要点
--------
* 左侧竖向导航 + 顶部搜索条 + 内容区，内容区在「首页 / 搜索结果」之间切换。
* 书卡是自绘 Canvas（tkinter 原生控件做不出圆角），封面用按书名生成的稳定渐变，
  因为 tkinter 只支持 PNG，而各书源封面几乎都是 JPEG。
* 所有网络操作都在后台线程，通过 ``queue`` + ``after()`` 回到主线程。
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk

from .. import __version__, config, exporter, hotlist
from ..models import Book, BookContent, language_label
from ..sources import Registry, Source, get_registry
from . import dialogs
from . import theme as T
from .widgets import BookCard, Chip, PillButton, ScrollFrame, draw_cover

NAV_ITEMS = (
    ("home", "🏠", "首页"),
    ("search", "🔍", "搜索"),
)

LOG_LIMIT = 500


def _enable_dpi_awareness() -> None:
    """高分屏下不发虚。必须在创建 Tk 之前调用。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ==========================================================================
# 圆角搜索框
# ==========================================================================


class SearchBox(tk.Frame):
    """圆角搜索框：Canvas 画圆角底，Entry 叠在上面用同色，视觉上就是圆角。"""

    H = 38

    def __init__(self, master: tk.Misc, placeholder: str = "",
                 on_submit=None, width: int = 420, **kw) -> None:
        super().__init__(master, bg=master["bg"], **kw)
        self.on_submit = on_submit
        self._focused = False
        self.canvas = tk.Canvas(self, height=T.px(self.H), bg=master["bg"],
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.var = tk.StringVar()
        self.entry = tk.Entry(self.canvas, textvariable=self.var, bd=0,
                              relief="flat", bg=T.SURFACE_2, fg=T.TEXT,
                              insertbackground=T.ACCENT, font=T.font(10),
                              highlightthickness=0)
        self.placeholder = placeholder
        self._ph_shown = False
        self.canvas.bind("<Configure>", lambda _e: self._redraw())
        self.canvas.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self.entry.bind("<Return>", lambda _e: self._submit())
        self.entry.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.entry.bind("<FocusOut>", lambda _e: self._set_focus(False))
        self._show_placeholder()

    def _show_placeholder(self) -> None:
        if not self.var.get() and self.placeholder:
            self.entry.configure(fg=T.TEXT_DIM)
            self.var.set(self.placeholder)
            self._ph_shown = True

    def _clear_placeholder(self) -> None:
        if self._ph_shown:
            self.var.set("")
            self.entry.configure(fg=T.TEXT)
            self._ph_shown = False

    def _set_focus(self, value: bool) -> None:
        self._focused = value
        if value:
            self._clear_placeholder()
        else:
            self._show_placeholder()
        self._redraw()

    def _submit(self) -> None:
        self._clear_placeholder()
        text = self.var.get().strip()
        if text and self.on_submit:
            self.on_submit(text)

    def set(self, text: str) -> None:
        self._ph_shown = False
        self.entry.configure(fg=T.TEXT)
        self.var.set(text)

    def _redraw(self) -> None:
        self.canvas.delete("all")
        w = self.canvas.winfo_width() or T.px(300)
        h = T.px(self.H)
        T.round_rect(self.canvas, 0.5, 0.5, w - 0.5, h - 0.5, h / 2,
                     fill=T.SURFACE_2,
                     outline=T.ACCENT if self._focused else T.BORDER, width=1)
        self.canvas.create_text(T.px(16), h / 2, text="🔍", anchor="w",
                                fill=T.TEXT_DIM, font=T.font(10))
        self.canvas.create_window(T.px(34), h / 2, anchor="w", window=self.entry,
                                  width=max(T.px(60), w - T.px(90)), height=h - T.px(12))


# ==========================================================================
# 主窗口
# ==========================================================================


class BookSeekerApp(tk.Tk):
    def __init__(self, registry: Registry | None = None, cfg: dict | None = None) -> None:
        super().__init__()
        _enable_dpi_awareness()
        T.init_scaling(self)

        self.cfg = cfg if cfg is not None else config.CONFIG
        self.registry = registry if registry is not None else get_registry()
        self.queue: queue.Queue = queue.Queue()
        self.results: list[Book] = []
        self.selected: list[Book] = []
        self.searching = False
        self.stop_flag = threading.Event()
        self.hot_books: dict[str, Book] = {}
        self.hot_category = "全部"
        self._result_cards: list[BookCard] = []
        self._empty_label = None
        self._hot_cards: list[BookCard] = []
        self._preview_win = None
        self._log_open = False
        self._last_grid_width = 0

        self.title("BookSeeker %s — 跨书源书籍检索与导出" % __version__)
        self.geometry(self.cfg.get("window_geometry", "1280x800"))
        self.minsize(T.px(900), T.px(600))
        self.configure(bg=T.BG)

        self.var_outdir = tk.StringVar(value=str(self.cfg.get("download_dir", "")))
        self.var_limit = tk.StringVar(value=str(self.cfg.get("max_results_per_source", 30)))
        self.var_deadline = tk.StringVar(value=str(self.cfg.get("search_deadline", 12)))
        self.var_status = tk.StringVar(value="就绪")
        self.var_source = tk.StringVar(value="全部书源")
        self.fmt_vars = {
            fmt: tk.BooleanVar(value=fmt in (self.cfg.get("formats") or ["epub", "txt"]))
            for fmt in exporter.FORMATS
        }

        self._style_ttk()
        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._drain_queue)

        self.log("欢迎使用 BookSeeker！在顶部输入关键字，或者从首页的热门书籍里挑一本。")
        self._report_sources()
        self._refresh_source_menu()
        self.show_page("home")
        self.hot_books.update(hotlist.load_cache())
        self._render_hot()

    # ==================================================================
    # 外观
    # ==================================================================

    def _style_ttk(self) -> None:
        from tkinter import ttk

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("BS.Horizontal.TProgressbar", troughcolor=T.SURFACE,
                        background=T.ACCENT, bordercolor=T.SURFACE,
                        lightcolor=T.ACCENT, darkcolor=T.ACCENT, thickness=T.px(4))

    def _build_layout(self) -> None:
        # ---------------- 左侧导航 ----------------
        self.side = tk.Frame(self, bg=T.BG_SOFT, width=T.px(168))
        self.side.pack(side="left", fill="y")
        self.side.pack_propagate(False)

        logo = tk.Canvas(self.side, height=T.px(72), bg=T.BG_SOFT, highlightthickness=0)
        logo.pack(fill="x")
        logo.create_text(T.px(20), T.px(34), anchor="w", text="◆", fill=T.ACCENT,
                         font=T.font(15, "bold"))
        logo.create_text(T.px(40), T.px(34), anchor="w", text="BookSeeker",
                         fill=T.TEXT, font=T.font(13, "bold"))
        logo.create_text(T.px(40), T.px(52), anchor="w", text="v" + __version__,
                         fill=T.TEXT_DIM, font=T.font(8))

        self.nav_items: dict[str, object] = {}
        for key, icon, label in NAV_ITEMS:
            item = type(self)._nav_factory(self.side, label, icon,
                                           lambda _w, k=key: self.show_page(k))
            item.pack(fill="x", pady=T.px(2))
            self.nav_items[key] = item

        # 书源 / 设置是弹窗，也用导航项触发
        for key, icon, label, handler in (
            ("sources", "📚", "书源", lambda: dialogs.open_sources(self)),
            ("settings", "⚙", "设置", lambda: dialogs.open_settings(self)),
        ):
            item = type(self)._nav_factory(self.side, label, icon, lambda _w, h=handler: h())
            item.pack(fill="x", pady=T.px(2))

        tk.Frame(self.side, bg=T.BG_SOFT).pack(fill="both", expand=True)
        self.lbl_cache = tk.Label(self.side, text="", bg=T.BG_SOFT, fg=T.TEXT_DIM,
                                  font=T.font(8), justify="left", anchor="w")
        self.lbl_cache.pack(fill="x", padx=T.px(18), pady=T.px(12))

        # ---------------- 右侧主区 ----------------
        right = tk.Frame(self, bg=T.BG)
        right.pack(side="left", fill="both", expand=True)

        top = tk.Frame(right, bg=T.BG, height=T.px(64))
        top.pack(fill="x", padx=T.px(18), pady=(T.px(14), T.px(4)))
        top.pack_propagate(False)
        self.search_box = SearchBox(top, "搜索书名、作者…", self.start_search, width=420)
        self.search_box.pack(side="left", fill="x", expand=True, pady=T.px(12))
        PillButton(top, "搜索", command=lambda: self.start_search(self.search_box.var.get()),
                   width=88).pack(side="left", padx=T.px(10), pady=T.px(12))

        self.content = tk.Frame(right, bg=T.BG)
        self.content.pack(fill="both", expand=True)

        self.pages: dict[str, tk.Frame] = {}
        self._build_home(self.pages.setdefault("home", tk.Frame(self.content, bg=T.BG)))
        self._build_search(self.pages.setdefault("search", tk.Frame(self.content, bg=T.BG)))

        # ---------------- 日志抽屉 ----------------
        self.log_frame = tk.Frame(right, bg=T.BG_SOFT, height=T.px(150))
        self.log_box = tk.Text(self.log_frame, bg=T.BG_SOFT, fg=T.TEXT_MUTED,
                               relief="flat", font=T.font(8, mono=True),
                               wrap="word", height=8, bd=0,
                               highlightthickness=0, padx=T.px(14), pady=T.px(6))
        log_sb = tk.Scrollbar(self.log_frame, orient="vertical", command=self.log_box.yview,
                              bg=T.SURFACE, troughcolor=T.BG_SOFT, relief="flat",
                              bd=0, width=T.px(10))
        self.log_box.configure(yscrollcommand=log_sb.set, state="disabled")
        self.log_box.pack(side="left", fill="both", expand=True)
        log_sb.pack(side="right", fill="y")

        # ---------------- 底部状态栏 ----------------
        bottom = tk.Frame(right, bg=T.BG, height=T.px(34))
        bottom.pack(fill="x", padx=T.px(18))
        bottom.pack_propagate(False)
        tk.Label(bottom, textvariable=self.var_status, bg=T.BG, fg=T.TEXT_MUTED,
                 font=T.font(8)).pack(side="left")
        self.btn_log = PillButton(bottom, "日志", variant="ghost", width=64, height=24,
                                  font_size=8, command=self.toggle_log)
        self.btn_log.pack(side="right", pady=T.px(4))

        from tkinter import ttk

        self.progress = ttk.Progressbar(right, style="BS.Horizontal.TProgressbar",
                                        mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=T.px(18), pady=(0, T.px(6)))

    @staticmethod
    def _nav_factory(parent, label, icon, command):
        from .widgets import NavItem

        return NavItem(parent, label, icon, command)

    # ==================================================================
    # 首页
    # ==================================================================

    def _build_home(self, page: tk.Frame) -> None:
        head = tk.Frame(page, bg=T.BG)
        head.pack(fill="x", padx=T.px(18), pady=(T.px(10), T.px(2)))
        tk.Label(head, text="热门书籍", bg=T.BG, fg=T.TEXT,
                 font=T.font(17, "bold")).pack(side="left")
        self.chip_hot = Chip(head, "%d 本" % len(hotlist.HOT_BOOKS))
        self.chip_hot.pack(side="left", padx=T.px(10))
        PillButton(head, "换一批", variant="ghost", width=78, height=30, font_size=9,
                   command=self._shuffle_hot).pack(side="right")

        cats = tk.Frame(page, bg=T.BG)
        cats.pack(fill="x", padx=T.px(18), pady=(T.px(6), T.px(2)))
        self._cat_buttons: dict[str, PillButton] = {}
        for cat in hotlist.CATEGORIES:
            btn = PillButton(cats, cat, variant="ghost", width=max(56, len(cat) * 14 + 22),
                             height=28, font_size=9,
                             command=lambda c=cat: self._filter_hot(c))
            btn.pack(side="left", padx=(0, T.px(6)))
            self._cat_buttons[cat] = btn

        self.hot_scroll = ScrollFrame(page, bg=T.BG)
        self.hot_scroll.pack(fill="both", expand=True, padx=T.px(12), pady=T.px(6))
        self.hot_scroll.body.bind("<Configure>", lambda _e: self._maybe_relayout("hot"))
        self._render_hot()

    def _render_hot(self) -> None:
        body = self.hot_scroll.body
        self.hot_scroll.clear()
        self._hot_cards = []
        entries = hotlist.books_in(self.hot_category)
        for hot in entries:
            book = self.hot_books.get(hot.title)
            card = BookCard(
                body,
                title=hot.title,
                subtitle=book.author_text if book else hot.author,
                badge=(book.source_name or hot.source) if book else "",
                meta=hot.category,
                on_click=lambda c, h=hot: self._open_hot(h),
            )
            self._hot_cards.append(card)
        self._layout_cards(body, self._hot_cards)
        for cat, btn in self._cat_buttons.items():
            btn.set_active(cat == self.hot_category)

    def _filter_hot(self, category: str) -> None:
        self.hot_category = category
        self._render_hot()

    def _shuffle_hot(self) -> None:
        import random

        order = hotlist.books_in(self.hot_category)
        random.shuffle(order)
        hotlist_order = list(order)
        body = self.hot_scroll.body
        self.hot_scroll.clear()
        self._hot_cards = []
        for hot in hotlist_order:
            book = self.hot_books.get(hot.title)
            card = BookCard(body, title=hot.title,
                            subtitle=book.author_text if book else hot.author,
                            badge=(book.source_name or hot.source) if book else "",
                            meta=hot.category,
                            on_click=lambda c, h=hot: self._open_hot(h))
            self._hot_cards.append(card)
        self._layout_cards(body, self._hot_cards)
        self.log("已重新排列首页书单。")

    def _resolve_hot(self, hot: hotlist.HotBook, then=None) -> None:
        """在后台解析单本热门书，成功后写缓存并刷新卡片。

        首页刻意**不预解析整个书单**：33 本书就是 33 次检索请求，会把 HTTP
        限速额度吃光，用户真正发起的搜索就被拖慢了。
        """
        if hot.title in self.hot_books:
            if then:
                then(self.hot_books[hot.title])
            return

        def worker() -> None:
            book = hotlist.resolve(self.registry, hot)
            self.queue.put(("hot", hot, book, then))

        threading.Thread(target=worker, daemon=True, name="bs-hot-one").start()

    def _open_hot(self, hot: hotlist.HotBook) -> None:
        """点首页书卡：解析到具体那本书并选中。"""
        book = self.hot_books.get(hot.title)
        if book is not None:
            self._focus_book(book)
            return
        self.var_status.set("正在定位《%s》…" % hot.title)
        self._resolve_hot(hot, then=self._focus_book)

    def _focus_book(self, book: Book | None) -> None:
        if book is None:
            self.var_status.set("没能定位到这本书，请手动搜索")
            return
        self.show_page("search")
        self.results = [book]
        self._result_cards = []
        self._render_results()
        self._select_only(book)
        self.var_status.set("已定位：%s" % book.title)

    # ==================================================================
    # 搜索页
    # ==================================================================

    def _build_search(self, page: tk.Frame) -> None:
        bar = tk.Frame(page, bg=T.BG)
        bar.pack(fill="x", padx=T.px(18), pady=(T.px(6), T.px(2)))
        self.lbl_result = tk.Label(bar, text="还没有搜索结果", bg=T.BG, fg=T.TEXT,
                                   font=T.font(12, "bold"))
        self.lbl_result.pack(side="left")

        # 书源筛选 + 每源条数
        self.cmb_source = tk.OptionMenu(bar, self.var_source, "全部书源")
        self.cmb_source.configure(bg=T.SURFACE, fg=T.TEXT_MUTED, relief="flat", bd=0,
                                  highlightthickness=0, font=T.font(8), width=14,
                                  activebackground=T.SURFACE_HI,
                                  activeforeground=T.TEXT)
        self.cmb_source["menu"].configure(bg=T.SURFACE, fg=T.TEXT, font=T.font(8),
                                          activebackground=T.ACCENT,
                                          activeforeground="#1A1A28", bd=0)
        self.cmb_source.pack(side="right")
        tk.Label(bar, text="书源", bg=T.BG, fg=T.TEXT_DIM,
                 font=T.font(8)).pack(side="right", padx=T.px(6))

        # side="right" 是从右往左堆，所以要按「视觉上的从右到左」来 pack
        tk.Label(bar, text="秒", bg=T.BG, fg=T.TEXT_DIM,
                 font=T.font(8)).pack(side="right", padx=(T.px(2), 0))
        tk.Label(bar, textvariable=self.var_deadline, bg=T.BG, fg=T.TEXT_MUTED,
                 font=T.font(8, "bold")).pack(side="right", padx=(T.px(4), 0))
        tk.Label(bar, text="检索最多等", bg=T.BG, fg=T.TEXT_DIM,
                 font=T.font(8)).pack(side="right", padx=(T.px(12), 0))

        split = tk.Frame(page, bg=T.BG)
        split.pack(fill="both", expand=True, padx=T.px(18), pady=T.px(4))

        self.result_scroll = ScrollFrame(split, bg=T.BG)
        self.result_scroll.pack(side="left", fill="both", expand=True)
        self.result_scroll.body.bind("<Configure>", lambda _e: self._maybe_relayout("result"))

        self._build_detail(split)

    def _build_detail(self, parent: tk.Frame) -> None:
        panel = tk.Frame(parent, bg=T.SURFACE, width=T.px(318))
        panel.pack(side="right", fill="y", padx=(T.px(14), 0))
        panel.pack_propagate(False)
        self.detail_panel = panel

        inner = tk.Frame(panel, bg=T.SURFACE)
        inner.pack(fill="both", expand=True, padx=T.px(16), pady=T.px(14))

        self.detail_title = tk.Label(inner, text="选一本书看详情", bg=T.SURFACE, fg=T.TEXT,
                                     font=T.font(13, "bold"), anchor="w", justify="left",
                                     wraplength=T.px(286))
        self.detail_title.pack(anchor="w")

        self.detail_meta = tk.Label(inner, text="", bg=T.SURFACE, fg=T.TEXT_MUTED,
                                    font=T.font(9), anchor="w", justify="left",
                                    wraplength=T.px(286))
        self.detail_meta.configure(
            text="单击选中，Ctrl+单击可多选后批量下载\n检索结果会随各书源陆续返回")
        self.detail_meta.pack(anchor="w", pady=(T.px(4), T.px(10)))

        self.detail_cover = tk.Canvas(inner, height=T.px(140), bg=T.SURFACE,
                                      highlightthickness=0, bd=0)
        self.detail_cover.pack(fill="x")
        self.detail_cover.bind("<Configure>", lambda _e: self._reset_cover()
                               if not self.selected else None)
        self._reset_cover()

        tk.Label(inner, text="简介", bg=T.SURFACE, fg=T.TEXT_DIM,
                 font=T.font(8)).pack(anchor="w", pady=(T.px(10), T.px(2)))
        self.detail_desc = tk.Text(inner, height=8, bg=T.SURFACE_2, fg=T.TEXT_MUTED,
                                   relief="flat", font=T.font(9), wrap="word", bd=0,
                                   highlightthickness=1, highlightbackground=T.BORDER,
                                   padx=T.px(8), pady=T.px(6))
        self.detail_desc.pack(fill="both", expand=True)
        self.detail_desc.configure(state="disabled")

        tk.Label(inner, text="导出目录", bg=T.SURFACE, fg=T.TEXT_DIM,
                 font=T.font(8)).pack(anchor="w", pady=(T.px(10), T.px(2)))
        dir_row = tk.Frame(inner, bg=T.SURFACE)
        dir_row.pack(fill="x")
        tk.Entry(dir_row, textvariable=self.var_outdir, bg=T.SURFACE_2, fg=T.TEXT,
                 relief="flat", font=T.font(8), insertbackground=T.ACCENT,
                 highlightthickness=1, highlightbackground=T.BORDER,
                 highlightcolor=T.ACCENT).pack(side="left", fill="x", expand=True,
                                               ipady=T.px(4))
        PillButton(dir_row, "…", variant="ghost", width=32, height=26, font_size=9,
                   command=self.choose_dir).pack(side="left", padx=(T.px(4), 0))

        fmt_row = tk.Frame(inner, bg=T.SURFACE)
        fmt_row.pack(fill="x", pady=(T.px(8), T.px(6)))
        tk.Label(fmt_row, text="格式", bg=T.SURFACE, fg=T.TEXT_DIM,
                 font=T.font(8)).pack(side="left", padx=(0, T.px(8)))
        for fmt in exporter.FORMATS:
            tk.Checkbutton(fmt_row, text=fmt.upper(), variable=self.fmt_vars[fmt],
                           bg=T.SURFACE, fg=T.TEXT, selectcolor=T.SURFACE_2,
                           activebackground=T.SURFACE, activeforeground=T.TEXT,
                           font=T.font(8), bd=0, highlightthickness=0).pack(side="left",
                                                                            padx=(0, T.px(6)))

        self.btn_download = PillButton(inner, "下载并导出", command=self.download_selected,
                                       width=280, height=38)
        self.btn_download.pack(fill="x", pady=(T.px(4), T.px(6)))
        PillButton(inner, "预览正文", command=self.preview_selected, variant="ghost",
                   width=280, height=32, font_size=9).pack(fill="x", pady=T.px(3))
        PillButton(inner, "打开导出目录", command=self.open_output_dir, variant="ghost",
                   width=280, height=32, font_size=9).pack(fill="x", pady=T.px(3))

    # ==================================================================
    # 网格布局
    # ==================================================================

    def _layout_cards(self, body: tk.Frame, cards: list[BookCard], key: str = "") -> None:
        if not cards:
            return
        width = body.winfo_width()
        if width <= 1:
            width = self.content.winfo_width() or T.px(900)
            if key == "result":
                width -= T.px(350)
        card_w = cards[0].winfo_reqwidth()
        gap = T.px(6)
        cols = max(1, int((width - gap) // (card_w + gap * 2)))
        for i, card in enumerate(cards):
            card.grid(row=i // cols, column=i % cols, padx=gap, pady=T.px(8))

    def _maybe_relayout(self, which: str) -> None:
        """窗口宽度变了才重排，避免 Configure 事件风暴。"""
        width = (self.result_scroll.body.winfo_width() if which == "result"
                 else self.hot_scroll.body.winfo_width())
        if abs(width - self._last_grid_width) < T.px(20):
            return
        self._last_grid_width = width
        if which == "hot":
            self._layout_cards(self.hot_scroll.body, self._hot_cards, "hot")
        else:
            self._layout_cards(self.result_scroll.body, self._result_cards, "result")

    # ==================================================================
    # 检索
    # ==================================================================

    def _selected_source_ids(self) -> list[str] | None:
        value = self.var_source.get()
        if not value or value.startswith("全部"):
            return None
        for src in self.registry.all():
            if (src.name or src.id) == value:
                return [src.id]
        return None

    def _refresh_source_menu(self) -> None:
        names = ["全部书源"] + [s.name or s.id for s in self.registry.enabled()]
        menu = self.cmb_source["menu"]
        menu.delete(0, "end")
        for name in names:
            menu.add_command(label=name, command=lambda n=name: self.var_source.set(n))
        if self.var_source.get() not in names:
            self.var_source.set(names[0])

    def start_search(self, query: str, prefer: str = "") -> None:
        query = (query or "").strip()
        if not query or query == "搜索书名、作者…":
            return
        if self.searching:
            return
        self.stop_flag.clear()
        self.show_page("search")
        self.results = []
        self.selected = []
        self._render_results()
        self.searching = True
        self._prefer = prefer
        self.btn_download.set_state("disabled")
        self.var_status.set("正在检索…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(50)
        self.lbl_result.configure(text="检索中…")
        self.log("开始检索：%s" % query)
        try:
            limit = int(self.var_limit.get())
        except ValueError:
            limit = 30
        try:
            deadline = float(self.var_deadline.get())
        except ValueError:
            deadline = 12.0
        threading.Thread(target=self._search_worker,
                         args=(query, self._selected_source_ids(), limit, deadline),
                         daemon=True, name="bs-search").start()

    def _search_worker(self, query: str, source_ids, limit: int, deadline: float) -> None:
        started = time.time()
        try:
            import inspect

            kwargs = {}
            params = inspect.signature(self.registry.search_all).parameters
            if "deadline" in params:
                kwargs["deadline"] = deadline
            books = self.registry.search_all(
                query,
                source_ids=source_ids,
                limit_per_source=limit,
                on_batch=lambda src, items: self.queue.put(("results", src, items, time.time() - started)),
                on_error=lambda src, exc: self.queue.put(("source_error", src, exc)),
                should_stop=self.stop_flag.is_set,
                **kwargs,
            )
            self.queue.put(("search_done", len(books), time.time() - started))
        except Exception as exc:
            self.queue.put(("error", "检索出错：%s" % exc))
            self.queue.put(("search_done", len(self.results), time.time() - started))

    # ==================================================================
    # 结果渲染
    # ==================================================================

    def _render_results(self) -> None:
        body = self.result_scroll.body

        if not self.results:
            self.result_scroll.clear()
            self._result_cards = []
            self._empty_label = tk.Label(
                body, text="检索中…" if self.searching else "还没有结果",
                bg=T.BG, fg=T.TEXT_DIM, font=T.font(10))
            self._empty_label.grid(row=0, column=0, padx=T.px(20), pady=T.px(30))
            self.lbl_result.configure(text="检索中…" if self.searching else "还没有搜索结果")
            return

        if self._empty_label is not None:
            self._empty_label.destroy()
            self._empty_label = None

        # 结果被重置过（数量比卡片少）就全部重建，否则只补新增的
        if len(self._result_cards) > len(self.results):
            self.result_scroll.clear()
            self._result_cards = []

        for book in self.results[len(self._result_cards):]:
            card = BookCard(
                body,
                title=book.title or "(无标题)",
                subtitle=book.author_text,
                badge=book.source_name or book.source,
                meta="%s%s" % (language_label(book.language),
                               ("  ·  " + book.year) if book.year else ""),
                on_click=self._on_card_click,
            )
            card.book = book  # type: ignore[attr-defined]
            self._result_cards.append(card)

        self._last_grid_width = 0
        self._layout_cards(body, self._result_cards, "result")
        self.lbl_result.configure(text="搜索结果  %d 本" % len(self.results))
        self._sync_card_selection()

    def _card_for(self, book: Book) -> BookCard | None:
        return next((c for c in self._result_cards if getattr(c, "book", None) is book), None)

    def _on_card_click(self, card: BookCard, event=None) -> None:
        book = getattr(card, "book", None)
        if book is None:
            return
        # Windows 下 Control 的修饰位是 0x0004
        additive = bool(event is not None and (event.state & 0x0004))
        if additive and book in self.selected:
            self.selected.remove(book)
        elif additive:
            self.selected.append(book)
        else:
            self.selected = [book]
        self._sync_card_selection()
        self._show_detail(book)

    def _sync_card_selection(self) -> None:
        for card in self._result_cards:
            card.set_selected(getattr(card, "book", None) in self.selected)

    def _select_only(self, book: Book) -> None:
        self.selected = [book]
        self._sync_card_selection()
        self._show_detail(book)

    def _reset_cover(self) -> None:
        cv = self.detail_cover
        cv.delete("all")
        w = cv.winfo_width() or T.px(286)
        T.round_rect(cv, 0, 0, w, T.px(140), T.px(10),
                     fill=T.SURFACE_2, outline=T.BORDER, width=1)
        cv.create_text(w / 2, T.px(70), text="封面预览", fill=T.TEXT_DIM,
                       font=T.font(10))

    def _show_detail(self, book: Book) -> None:
        self.detail_title.configure(text=book.title or "(无标题)")
        meta = " · ".join(x for x in (
            book.author_text,
            language_label(book.language),
            book.source_name or book.source,
            book.year or "",
        ) if x)
        self.detail_meta.configure(text=meta)

        cv = self.detail_cover
        cv.delete("all")
        w = cv.winfo_width() or T.px(286)
        h = T.px(140)
        draw_cover(cv, 0, 0, w, h, book.title or "?", radius=T.px(10),
                   bg=T.SURFACE, badge=book.source_name or book.source)

        text = (book.description or "").strip() or "（该书源未提供简介）"
        if book.subjects:
            text += "\n\n主题：" + "、".join(book.subjects[:16])
        if book.url:
            text += "\n\n详情页：" + book.url
        self.detail_desc.configure(state="normal")
        self.detail_desc.delete("1.0", "end")
        self.detail_desc.insert("1.0", text)
        self.detail_desc.configure(state="disabled")
        self.btn_download.set_state("normal")

    # ==================================================================
    # 下载 / 导出
    # ==================================================================

    def chosen_formats(self) -> list[str]:
        return [f for f, v in self.fmt_vars.items() if v.get()] or ["epub"]

    def choose_dir(self) -> None:
        current = self.var_outdir.get() or str(Path.home())
        chosen = tk.filedialog.askdirectory(
            parent=self, title="选择导出目录",
            initialdir=current if Path(current).is_dir() else None)
        if chosen:
            self.var_outdir.set(chosen)

    def download_selected(self) -> None:
        if not self.selected:
            self.var_status.set("请先在左侧选一本书")
            return
        out_dir = self.var_outdir.get().strip()
        if not out_dir:
            self.choose_dir()
            out_dir = self.var_outdir.get().strip()
        if not out_dir:
            return
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        self.last_export_dir = Path(out_dir)
        self.var_status.set("正在下载…")
        self.btn_download.set_state("disabled")
        threading.Thread(target=self._download_worker,
                         args=(list(self.selected), out_dir, self.chosen_formats()),
                         daemon=True, name="bs-download").start()

    def _download_worker(self, books: list[Book], out_dir: str, formats: list[str]) -> None:
        for index, book in enumerate(books, 1):
            self.queue.put(("log", "[%d/%d] 下载：%s" % (index, len(books), book.title)))
            self.queue.put(("status", "正在下载 %s…" % book.title))
            try:
                content = self.registry.fetch(
                    book,
                    progress=lambda d, t, m: self.queue.put(("progress", d, t, m)))
                self.queue.put(("log", "    已获取 %d 章，约 %s 字"
                                % (len(content.chapters), format(content.word_count, ","))))
                written = exporter.export(content, out_dir, formats, options=self.cfg)
                self.queue.put(("exported", content, written))
            except Exception as exc:
                self.queue.put(("log", "    ✗ 失败：%s" % exc))
        self.queue.put(("progress_reset",))
        self.queue.put(("download_done",))

    def preview_selected(self) -> None:
        if not self.selected:
            return
        book = self.selected[0]
        self.var_status.set("正在获取正文…")
        self.log("正在拉取预览：%s" % book.title)

        def worker() -> None:
            try:
                content = self.registry.fetch(book)
                self.queue.put(("preview", content))
            except Exception as exc:
                self.queue.put(("error", "预览失败：%s" % exc))

        threading.Thread(target=worker, daemon=True, name="bs-preview").start()

    # ==================================================================
    # 书源 / 设置相关动作
    # ==================================================================

    def open_output_dir(self) -> None:
        target = getattr(self, "last_export_dir", None) or Path(self.var_outdir.get() or ".")
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.open_path(target)

    def open_plugin_dir(self) -> None:
        config.ensure_dirs()
        self.open_path(config.PLUGIN_DIR)

    def open_path(self, path: Path) -> None:
        try:
            if sys.platform == "win32":
                import os

                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                import webbrowser

                webbrowser.open(path.as_uri())
        except Exception as exc:
            self.log("无法打开 %s：%s" % (path, exc))

    def install_example_plugin(self) -> None:
        path = write_example_plugin()
        self.log("示例插件已写入：%s" % path if path else "找不到示例插件源文件。")

    def clear_cache(self) -> None:
        from .. import http as http_mod

        count = http_mod.default.clear_cache()
        self.log("已清空缓存，删除 %d 个文件。" % count)
        self._update_cache_label()

    def reload_sources(self) -> None:
        from .. import http as http_mod
        from ..sources import build_registry

        http_mod.configure_from_config(self.cfg)
        self.registry = build_registry(self.cfg)
        self._refresh_source_menu()
        self._report_sources()

    def _report_sources(self) -> None:
        enabled = self.registry.enabled()
        self.log("已加载书源 %d 个（启用 %d 个）：%s"
                 % (len(self.registry.all()), len(enabled),
                    "、".join(s.name or s.id for s in enabled)))
        for report in self.registry.load_reports:
            if report.get("file") and not report.get("ok"):
                self.log("⚠ 插件 %s 加载失败：%s"
                         % (Path(report["file"]).name, report.get("message", "")))
        self._update_cache_label()

    def _update_cache_label(self) -> None:
        try:
            from .. import http as http_mod

            size = http_mod.default.cache_size() / 1048576.0
            self.lbl_cache.configure(text="缓存 %.1f MB\n书源 %d 个"
                                          % (size, len(self.registry.enabled())))
        except Exception:
            pass

    # ==================================================================
    # 页面切换 / 日志 / 消息泵
    # ==================================================================

    def show_page(self, key: str) -> None:
        for name, page in self.pages.items():
            page.pack_forget()
        page = self.pages.get(key)
        if page is not None:
            page.pack(fill="both", expand=True)
        for name, item in self.nav_items.items():
            item.set_active(name == key)

    def toggle_log(self) -> None:
        self._log_open = not self._log_open
        if self._log_open:
            self.log_frame.pack(fill="x", side="bottom", before=self.progress)
        else:
            self.log_frame.pack_forget()
        self.btn_log.set_active(self._log_open)

    def log(self, text: str) -> None:
        try:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", text.rstrip() + "\n")
            lines = int(self.log_box.index("end-1c").split(".")[0])
            if lines > LOG_LIMIT:
                self.log_box.delete("1.0", "%d.0" % (lines - LOG_LIMIT))
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        except Exception:
            print(text)

    def _drain_queue(self) -> None:
        try:
            while True:
                self._handle(self.queue.get_nowait())
        except queue.Empty:
            pass
        except Exception:
            traceback.print_exc()
        finally:
            self.after(80, self._drain_queue)

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "log":
            self.log(msg[1])
        elif kind == "status":
            self.var_status.set(msg[1])
        elif kind == "results":
            src, items, elapsed = msg[1], msg[2], msg[3]
            self.results.extend(items)
            self.log("  [%5.1fs] %-22s %d 条" % (elapsed, src.name or src.id, len(items)))
            self._render_results()
        elif kind == "source_error":
            src, exc = msg[1], msg[2]
            self.log("  ⚠ %s 检索失败：%s" % (src.name or src.id, exc))
        elif kind == "search_done":
            total, elapsed = msg[1], msg[2]
            self.searching = False
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.var_status.set("检索完成：%d 条结果，耗时 %.1f 秒" % (total, elapsed))
            self.lbl_result.configure(text="搜索结果  %d 本" % len(self.results))
            self.log("检索完成，共 %d 条结果，耗时 %.1f 秒。" % (total, elapsed))
            self._prefer_first()
        elif kind == "progress":
            done, total, text = msg[1], msg[2], msg[3]
            if total:
                self.progress.configure(mode="determinate", maximum=total, value=done)
            if text:
                self.var_status.set(text)
        elif kind == "progress_reset":
            self.progress.configure(mode="determinate", value=0)
        elif kind == "exported":
            self._on_exported(msg[1], msg[2])
        elif kind == "download_done":
            self.btn_download.set_state("normal")
            self.var_status.set("任务结束")
        elif kind == "hot":
            hot, book, then = msg[1], msg[2], msg[3]
            if book is not None:
                self.hot_books[hot.title] = book
                hotlist.save_cache(self.hot_books)
                self._render_hot()
            if then:
                then(book)
        elif kind == "hot_done":
            self.log("首页书单已就绪（%d 本已解析）。" % len(self.hot_books))
            self.chip_hot.set_text("%d 本" % len(hotlist.HOT_BOOKS))
        elif kind == "preview":
            dialogs.open_preview(self, msg[1])
        elif kind == "error":
            self.log("  ✗ %s" % msg[1])

    def _prefer_first(self) -> None:
        """首页点进来的书：自动选中匹配的那一本。"""
        prefer = getattr(self, "_prefer", "")
        if not prefer or not self.results:
            return
        from ..models import normalize_cjk

        want = normalize_cjk(prefer).lower()
        best = next((b for b in self.results if normalize_cjk(b.title or "").lower() == want),
                    None)
        if best is None:
            best = next((b for b in self.results
                         if want in normalize_cjk(b.title or "").lower()), None)
        if best is not None:
            self._select_only(best)
            self._scroll_card_into_view(best)
        self._prefer = ""

    def _scroll_card_into_view(self, book: Book) -> None:
        card = self._card_for(book)
        if card is None:
            return
        try:
            self.result_scroll.canvas.update_idletasks()
            total = max(1, self.result_scroll.body.winfo_height())
            y = card.winfo_y()
            self.result_scroll.canvas.yview_moveto(max(0.0, (y - T.px(20)) / total))
        except Exception:
            pass

    def _on_exported(self, content: BookContent, written: dict) -> None:
        for fmt, path in written.items():
            if str(path).startswith("!ERROR"):
                self.log("    ✗ [%s] %s" % (fmt, str(path)[7:]))
            else:
                self.log("    ✓ [%s] %s" % (fmt, path))
        missing = getattr(content, "missing_chapters", 0)
        if missing:
            self.log("    ⚠ 有 %d 章本次没抓到，正文里已用占位符标出。" % missing)
            self.var_status.set("完成，但有 %d 章没抓到" % missing)

    # ==================================================================
    # 收尾
    # ==================================================================

    def _on_close(self) -> None:
        self.stop_flag.set()
        try:
            self.cfg["window_geometry"] = self.geometry()
            self.cfg["formats"] = self.chosen_formats()
            config.save_config(self.cfg)
        except Exception:
            pass
        self.destroy()


def run_gui(registry: Registry | None = None, cfg: dict | None = None) -> int:
    """启动图形界面。"""
    config.ensure_dirs()
    app = BookSeekerApp(registry=registry, cfg=cfg)
    app.mainloop()
    return 0
