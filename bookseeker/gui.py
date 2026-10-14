"""BookSeeker 桌面界面（tkinter，标准库自带，零依赖）。

界面结构::

    ┌───────────────────────────────────────────────────────────┐
    │ 搜索框 [            ] [搜索]  来源[全部] 每源[30]          │
    ├──────────────────────────┬────────────────────────────────┤
    │ 结果列表                 │ 书籍详情 / 导出设置            │
    ├──────────────────────────┴────────────────────────────────┤
    │ 运行日志                                                  │
    ├───────────────────────────────────────────────────────────┤
    │ 状态栏                                                    │
    └───────────────────────────────────────────────────────────┘

所有网络操作都在后台线程里跑，通过 ``queue.Queue`` 把消息回传到主线程，
再用 ``after()`` 轮询消费，避免 tkinter 的跨线程问题。
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__, config, exporter
from .models import Book, BookContent, language_label
from .sources import Registry, Source, get_registry

LOG_LIMIT = 400


def _enable_dpi_awareness() -> None:
    """让 Windows 高分屏下的界面不发虚。"""
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


class BookSeekerApp(tk.Tk):
    """主窗口。"""

    def __init__(self, registry: Registry | None = None, cfg: dict | None = None) -> None:
        super().__init__()
        _enable_dpi_awareness()

        self.cfg = cfg if cfg is not None else config.CONFIG
        self.registry = registry if registry is not None else get_registry()

        self.queue: queue.Queue = queue.Queue()
        self.results: list[Book] = []
        self.books_by_iid: dict[str, Book] = {}
        self.searching = False
        self.stop_flag = threading.Event()
        self.last_export_dir: Path | None = None

        self.title("BookSeeker %s \u2014 \u8de8\u4e66\u6e90\u4e66\u7c4d\u68c0\u7d22\u4e0e\u5bfc\u51fa" % __version__)
        self.geometry(self.cfg.get("window_geometry", "1220x780"))
        self.minsize(940, 600)

        self.var_query = tk.StringVar()
        self.var_source = tk.StringVar(value="\u5168\u90e8\u4e66\u6e90")
        self.var_limit = tk.StringVar(value=str(self.cfg.get("max_results_per_source", 30)))
        self.var_outdir = tk.StringVar(value=str(self.cfg.get("download_dir", "")))
        self.var_status = tk.StringVar(value="\u5c31\u7eea")
        self.var_detail = tk.StringVar(value="\u5728\u5de6\u4fa7\u9009\u4e2d\u4e00\u672c\u4e66\u67e5\u770b\u8be6\u60c5")
        self.fmt_vars = {
            fmt: tk.BooleanVar(value=fmt in (self.cfg.get("formats") or ["epub", "txt"]))
            for fmt in exporter.FORMATS
        }

        self._build_style()
        self._build_menu()
        self._build_widgets()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_queue)
        self._log("\u6b22\u8fce\u4f7f\u7528 BookSeeker\uff01\u8f93\u5165\u5173\u952e\u5b57\u540e\u6309\u56de\u8f66\u6216\u70b9\u300c\u641c\u7d22\u300d\u3002")
        self._report_sources()

    # ==================================================================
    # 界面构建
    # ==================================================================

    def _build_style(self) -> None:
        style = ttk.Style(self)
        for theme in (self.cfg.get("theme"), "vista", "winnative", "clam", "default"):
            if not theme:
                continue
            try:
                if theme in style.theme_names():
                    style.theme_use(theme)
                    break
            except Exception:
                continue
        try:
            style.configure("Treeview", rowheight=24)
            style.configure("Title.TLabel", font=("Microsoft YaHei UI", 13, "bold"))
            style.configure("Hint.TLabel", foreground="#666666")
            style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"))
        except Exception:
            pass

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="\u4e0b\u8f7d\u5e76\u5bfc\u51fa\u9009\u4e2d\u4e66\u7c4d", command=self._download_selected)
        file_menu.add_command(label="\u6253\u5f00\u5bfc\u51fa\u76ee\u5f55", command=self._open_output_dir)
        file_menu.add_separator()
        file_menu.add_command(label="\u9000\u51fa", command=self._on_close)
        menubar.add_cascade(label="\u6587\u4ef6", menu=file_menu)

        tool_menu = tk.Menu(menubar, tearoff=0)
        tool_menu.add_command(label="\u8bbe\u7f6e\u2026", command=self._open_settings)
        tool_menu.add_command(label="\u4e66\u6e90\u7ba1\u7406\u2026", command=self._open_sources)
        tool_menu.add_separator()
        tool_menu.add_command(label="\u6e05\u7a7a\u7f51\u7edc\u7f13\u5b58", command=self._clear_cache)
        menubar.add_cascade(label="\u5de5\u5177", menu=tool_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="\u4f7f\u7528\u8bf4\u660e", command=self._show_help)
        help_menu.add_command(label="\u5f00\u53d1\u63d2\u4ef6\uff1a\u6253\u5f00\u63d2\u4ef6\u76ee\u5f55", command=self._open_plugin_dir)
        help_menu.add_separator()
        help_menu.add_command(label="\u5173\u4e8e", command=self._show_about)
        menubar.add_cascade(label="\u5e2e\u52a9", menu=help_menu)

        self.configure(menu=menubar)

    def _build_widgets(self) -> None:
        # ---- 顶部搜索栏 ----
        top = ttk.Frame(self, padding=(10, 10, 10, 6))
        top.pack(fill="x")

        ttk.Label(top, text="\u641c\u7d22\uff1a").pack(side="left")
        entry = ttk.Entry(top, textvariable=self.var_query, font=("Microsoft YaHei UI", 11))
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        entry.bind("<Return>", lambda _e: self._start_search())
        self.entry = entry

        self.btn_search = ttk.Button(top, text="\u641c\u7d22", command=self._start_search, style="Accent.TButton")
        self.btn_search.pack(side="left")

        ttk.Label(top, text="  \u6765\u6e90\uff1a").pack(side="left")
        self.cmb_source = ttk.Combobox(top, textvariable=self.var_source, state="readonly", width=16)
        self.cmb_source.pack(side="left")
        self.cmb_source.bind("<<ComboboxSelected>>", lambda _e: None)

        ttk.Label(top, text="  \u6bcf\u6e90\u6761\u6570\uff1a").pack(side="left")
        ttk.Spinbox(top, from_=5, to=200, increment=5, textvariable=self.var_limit, width=5).pack(side="left")

        # ---- 中部：结果 + 详情 ----
        middle = ttk.Panedwindow(self, orient="horizontal")
        middle.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        left = ttk.Frame(middle)
        columns = ("title", "author", "lang", "source", "year")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="extended")
        # 列宽总和要能塞进默认窗口，否则「年份」会被挤到可视区域外面
        headings = {
            "title": ("书名", 240), "author": ("作者", 140),
            "lang": ("语言", 56), "source": ("来源", 122),
            "year": ("年份", 74),
        }
        for key, (text, width) in headings.items():
            self.tree.heading(key, text=text, command=lambda k=key: self._sort_by(k))
            self.tree.column(key, width=width, anchor="w", stretch=(key == "title"))
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda _e: self._download_selected())
        middle.add(left, weight=3)

        right = ttk.Frame(middle, padding=(12, 4))
        ttk.Label(right, textvariable=self.var_detail, style="Title.TLabel", wraplength=340).pack(anchor="w")
        self.lbl_meta = ttk.Label(right, text="", style="Hint.TLabel", wraplength=340, justify="left")
        self.lbl_meta.pack(anchor="w", pady=(2, 8))

        ttk.Label(right, text="\u7b80\u4ecb\uff1a").pack(anchor="w")
        desc_box = tk.Text(right, height=9, wrap="word", relief="solid", borderwidth=1,
                           font=("Microsoft YaHei UI", 9))
        desc_box.pack(fill="both", expand=True)
        desc_box.configure(state="disabled")
        self.desc_box = desc_box

        ttk.Separator(right).pack(fill="x", pady=8)

        ttk.Label(right, text="\u5bfc\u51fa\u76ee\u5f55\uff1a").pack(anchor="w")
        row = ttk.Frame(right)
        row.pack(fill="x", pady=(2, 8))
        ttk.Entry(row, textvariable=self.var_outdir).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="\u6d4f\u89c8\u2026", width=7, command=self._choose_dir).pack(side="left", padx=(4, 0))

        ttk.Label(right, text="\u5bfc\u51fa\u683c\u5f0f\uff1a").pack(anchor="w")
        fmt_row = ttk.Frame(right)
        fmt_row.pack(fill="x", pady=(2, 10))
        for fmt, label in exporter.FORMATS.items():
            ttk.Checkbutton(fmt_row, text=label, variable=self.fmt_vars[fmt]).pack(side="left", padx=(0, 10))

        btns = ttk.Frame(right)
        btns.pack(fill="x")
        ttk.Button(btns, text="\u4e0b\u8f7d\u5e76\u5bfc\u51fa", style="Accent.TButton",
                   command=self._download_selected).pack(fill="x", pady=2)
        ttk.Button(btns, text="\u9884\u89c8\u6b63\u6587", command=self._preview_selected).pack(fill="x", pady=2)
        ttk.Button(btns, text="\u6253\u5f00\u5bfc\u51fa\u76ee\u5f55", command=self._open_output_dir).pack(fill="x", pady=2)
        middle.add(right, weight=2)

        # ---- 底部日志 ----
        bottom = ttk.LabelFrame(self, text="\u8fd0\u884c\u65e5\u5fd7", padding=6)
        bottom.pack(fill="both", padx=10, pady=(0, 4))
        self.log_box = tk.Text(bottom, height=8, wrap="word", relief="flat",
                               font=("Consolas", 9), background="#f7f7f7")
        log_sb = ttk.Scrollbar(bottom, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=log_sb.set, state="disabled")
        self.log_box.pack(side="left", fill="both", expand=True)
        log_sb.pack(side="right", fill="y")

        self.progress = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=10)

        status = ttk.Frame(self, padding=(10, 3))
        status.pack(fill="x")
        ttk.Label(status, textvariable=self.var_status, style="Hint.TLabel").pack(side="left")

        self._refresh_source_combo()

    # ==================================================================
    # 消息泵
    # ==================================================================

    def _drain_queue(self) -> None:
        try:
            while True:
                msg = self.queue.get_nowait()
                self._handle_message(msg)
        except queue.Empty:
            pass
        except Exception:
            traceback.print_exc()
        finally:
            self.after(100, self._drain_queue)

    def _handle_message(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "log":
            self._log(msg[1])
        elif kind == "status":
            self.var_status.set(msg[1])
        elif kind == "results":
            self._add_results(msg[1])
        elif kind == "source_error":
            src, exc = msg[1], msg[2]
            self._log("  \u26a0 %s \u68c0\u7d22\u5931\u8d25\uff1a%s" % (src.name or src.id, exc))
        elif kind == "search_done":
            total, elapsed = msg[1], msg[2]
            self.searching = False
            self.btn_search.configure(text="\u641c\u7d22", state="normal")
            self.progress.configure(mode="determinate", value=0)
            self.var_status.set("\u68c0\u7d22\u5b8c\u6210\uff1a%d \u6761\u7ed3\u679c\uff0c\u8017\u65f6 %.1f \u79d2" % (total, elapsed))
            self._log("\u68c0\u7d22\u5b8c\u6210\uff0c\u5171 %d \u6761\u7ed3\u679c\uff0c\u8017\u65f6 %.1f \u79d2\u3002" % (total, elapsed))
        elif kind == "progress_reset":
            self.progress.configure(mode="determinate", value=0)
        elif kind == "progress":
            done, total, text = msg[1], msg[2], msg[3]
            if total:
                self.progress.configure(mode="determinate", maximum=total, value=done)
            # 只有真正有话说时才更新状态栏，否则会把「任务结束」冲掉
            if text:
                self.var_status.set(text)
        elif kind == "exported":
            content, written = msg[1], msg[2]
            self._on_exported(content, written)
        elif kind == "error":
            self._log("  \u2717 %s" % msg[1])
            messagebox.showerror("\u51fa\u9519", msg[1], parent=self)
        elif kind == "preview":
            self._show_preview(*msg[1:])
        elif kind == "reload_sources":
            self._reload_sources()

    # ==================================================================
    # 检索
    # ==================================================================

    def _selected_source_ids(self) -> list[str] | None:
        value = self.var_source.get()
        if not value or value.startswith("\u5168\u90e8"):
            return None
        for src in self.registry.all():
            if (src.name or src.id) == value:
                return [src.id]
        return None

    def _refresh_source_combo(self) -> None:
        names = ["\u5168\u90e8\u4e66\u6e90"] + [s.name or s.id for s in self.registry.enabled()]
        self.cmb_source.configure(values=names)
        if self.var_source.get() not in names:
            self.var_source.set(names[0])

    def _start_search(self) -> None:
        query = self.var_query.get().strip()
        if not query:
            messagebox.showinfo("\u63d0\u793a", "\u8bf7\u5148\u8f93\u5165\u5173\u952e\u5b57\u3002", parent=self)
            return
        if self.searching:
            return
        self.tree.delete(*self.tree.get_children())
        self.results.clear()
        self.books_by_iid.clear()
        self.searching = True
        self.stop_flag.clear()
        self.btn_search.configure(text="\u68c0\u7d22\u4e2d\u2026", state="disabled")
        self.var_status.set("\u6b63\u5728\u68c0\u7d22\u2026")
        self.progress.configure(mode="indeterminate")
        self.progress.start(60)
        self._log("\u5f00\u59cb\u68c0\u7d22\uff1a%s" % query)

        source_ids = self._selected_source_ids()
        try:
            limit = int(self.var_limit.get())
        except ValueError:
            limit = int(self.cfg.get("max_results_per_source", 30))
        threading.Thread(target=self._search_worker, args=(query, source_ids, limit), daemon=True).start()

    def _search_worker(self, query: str, source_ids: list[str] | None, limit: int) -> None:
        started = time.time()
        try:
            books = self.registry.search_all(
                query,
                source_ids=source_ids,
                limit_per_source=limit,
                on_batch=lambda src, items: self.queue.put(("results", items)),
                on_error=lambda src, exc: self.queue.put(("source_error", src, exc)),
                should_stop=self.stop_flag.is_set,
            )
            self.queue.put(("search_done", len(books), time.time() - started))
        except Exception as exc:
            self.queue.put(("error", "\u68c0\u7d22\u51fa\u9519\uff1a%s" % exc))
            self.queue.put(("search_done", 0, time.time() - started))

    def _add_results(self, books: list[Book]) -> None:
        for book in books:
            self.results.append(book)
            iid = self.tree.insert("", "end", values=book.to_row())
            self.books_by_iid[iid] = book
        count = len(self.results)
        self.var_status.set("\u5df2\u6536\u5230 %d \u6761\u7ed3\u679c\u2026" % count)

    def _sort_by(self, key: str) -> None:
        index = {"title": 1, "author": 2, "lang": 3, "source": 4, "year": 5}.get(key)
        if not index:
            return
        items = [(self.tree.set(iid, key), iid) for iid in self.tree.get_children("")]
        items.sort(key=lambda kv: kv[0])
        for pos, (_val, iid) in enumerate(items):
            self.tree.move(iid, "", pos)

    # ==================================================================
    # 选中与详情
    # ==================================================================

    def _selected_books(self) -> list[Book]:
        return [self.books_by_iid[i] for i in self.tree.selection() if i in self.books_by_iid]

    def _on_select(self, _event=None) -> None:
        books = self._selected_books()
        if not books:
            return
        book = books[0]
        if len(books) > 1:
            self.var_detail.set("\u5df2\u9009\u4e2d %d \u672c\u4e66" % len(books))
        else:
            self.var_detail.set(book.title or "(\u65e0\u6807\u9898)")
        meta = []
        if book.author_text:
            meta.append("\u4f5c\u8005\uff1a%s" % book.author_text)
        meta.append("\u8bed\u8a00\uff1a%s" % language_label(book.language))
        meta.append("\u6765\u6e90\uff1a%s" % (book.source_name or book.source))
        if book.year:
            meta.append("\u5e74\u4efd\uff1a%s" % book.year)
        self.lbl_meta.configure(text="\u3000".join(meta))

        text = (book.description or "").strip() or "\uff08\u8be5\u4e66\u6e90\u672a\u63d0\u4f9b\u7b80\u4ecb\uff09"
        if book.subjects:
            text += "\n\n\u4e3b\u9898\uff1a" + "\u3001".join(book.subjects[:20])
        if book.url:
            text += "\n\n\u8be6\u60c5\u9875\uff1a" + book.url
        self.desc_box.configure(state="normal")
        self.desc_box.delete("1.0", "end")
        self.desc_box.insert("1.0", text)
        self.desc_box.configure(state="disabled")

    # ==================================================================
    # 下载 / 导出
    # ==================================================================

    def _chosen_formats(self) -> list[str]:
        formats = [f for f, var in self.fmt_vars.items() if var.get()]
        return formats or ["epub"]

    def _choose_dir(self) -> None:
        current = self.var_outdir.get() or str(Path.home())
        chosen = filedialog.askdirectory(parent=self, title="\u9009\u62e9\u5bfc\u51fa\u76ee\u5f55",
                                         initialdir=current if Path(current).is_dir() else None)
        if chosen:
            self.var_outdir.set(chosen)

    def _download_selected(self) -> None:
        books = self._selected_books()
        if not books:
            messagebox.showinfo("\u63d0\u793a", "\u8bf7\u5148\u5728\u5217\u8868\u91cc\u9009\u4e2d\u81f3\u5c11\u4e00\u672c\u4e66\u3002", parent=self)
            return
        out_dir = self.var_outdir.get().strip()
        if not out_dir:
            self._choose_dir()
            out_dir = self.var_outdir.get().strip()
        if not out_dir:
            return
        formats = self._chosen_formats()
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        self.last_export_dir = Path(out_dir)
        threading.Thread(target=self._download_worker, args=(books, out_dir, formats), daemon=True).start()

    def _download_worker(self, books: list[Book], out_dir: str, formats: list[str]) -> None:
        for index, book in enumerate(books, 1):
            self.queue.put(("log", "[%d/%d] \u4e0b\u8f7d\uff1a%s" % (index, len(books), book.title)))
            self.queue.put(("status", "\u6b63\u5728\u4e0b\u8f7d %s\u2026" % book.title))
            try:
                content = self.registry.fetch(
                    book,
                    progress=lambda done, total, text: self.queue.put(("progress", done, total, text)),
                )
                self.queue.put(("log", "    \u5df2\u83b7\u53d6 %d \u7ae0\uff0c\u7ea6 %s \u5b57"
                                % (len(content.chapters), format(content.word_count, ","))))
                written = exporter.export(content, out_dir, formats, options=self.cfg)
                self.queue.put(("exported", content, written))
            except Exception as exc:
                self.queue.put(("log", "    \u2717 \u5931\u8d25\uff1a%s" % exc))
        self.queue.put(("progress_reset",))
        self.queue.put(("status", "任务结束"))

    def _on_exported(self, content: BookContent, written: dict) -> None:
        for fmt, path in written.items():
            if str(path).startswith("!ERROR"):
                self._log("    \u2717 [%s] %s" % (fmt, str(path)[7:]))
            else:
                self._log("    \u2713 [%s] %s" % (fmt, path))

    def _preview_selected(self) -> None:
        books = self._selected_books()
        if not books:
            messagebox.showinfo("\u63d0\u793a", "\u8bf7\u5148\u9009\u4e2d\u4e00\u672c\u4e66\u3002", parent=self)
            return
        book = books[0]
        self._log("\u6b63\u5728\u62c9\u53d6\u9884\u89c8\uff1a%s" % book.title)
        self.var_status.set("\u6b63\u5728\u83b7\u53d6\u6b63\u6587\u2026")

        def worker() -> None:
            try:
                content = self.registry.fetch(book)
                self.queue.put(("preview", content))
            except Exception as exc:
                self.queue.put(("error", "\u9884\u89c8\u5931\u8d25\uff1a%s" % exc))

        threading.Thread(target=worker, daemon=True).start()

    def _show_preview(self, content: BookContent) -> None:
        win = tk.Toplevel(self)
        win.title("\u9884\u89c8\uff1a%s" % (content.title or ""))
        win.geometry("860x680")
        win.transient(self)

        head = ttk.Frame(win, padding=8)
        head.pack(fill="x")
        ttk.Label(head, text=content.title or "(\u65e0\u6807\u9898)", style="Title.TLabel").pack(anchor="w")
        info = "%s\uff5c%s \u7ae0\uff5c\u7ea6 %s \u5b57\uff5c%s" % (
            content.author_text or "\u4f5c\u8005\u4e0d\u8be6",
            len(content.chapters),
            format(content.word_count, ","),
            content.source_name or "",
        )
        ttk.Label(head, text=info, style="Hint.TLabel").pack(anchor="w")

        body = ttk.Frame(win, padding=(8, 0, 8, 8))
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="\u7ae0\u8282").pack(anchor="w")
        listbox = tk.Listbox(left, width=26, activestyle="dotbox")
        for idx, ch in enumerate(content.chapters, 1):
            listbox.insert("end", "%d. %s" % (idx, ch.title or "\u7b2c%d\u7ae0" % idx))
        listbox.pack(side="left", fill="y", expand=False)

        text_frame = ttk.Frame(body)
        text_frame.pack(side="left", fill="both", expand=True, padx=(10, 0))
        text = tk.Text(text_frame, wrap="word", font=("Microsoft YaHei UI", 10))
        sb = ttk.Scrollbar(text_frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=sb.set)
        text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        from .convert.htmlutil import html_to_text

        def show_chapter(_event=None) -> None:
            sel = listbox.curselection()
            if not sel:
                return
            chapter = content.chapters[sel[0]]
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", "%s\n\n%s" % (chapter.title or "", html_to_text(chapter.html)))
            text.configure(state="disabled")

        listbox.bind("<<ListboxSelect>>", show_chapter)
        if content.chapters:
            listbox.selection_set(0)
            show_chapter()
        ttk.Button(win, text="\u5173\u95ed", command=win.destroy).pack(pady=6)

    # ==================================================================
    # 菜单动作
    # ==================================================================

    def _open_output_dir(self) -> None:
        target = self.last_export_dir or Path(self.var_outdir.get() or ".")
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._open_path(target)

    def _open_plugin_dir(self) -> None:
        config.ensure_dirs()
        self._open_path(config.PLUGIN_DIR)

    def _open_path(self, path: Path) -> None:
        try:
            if sys.platform == "win32":
                import os

                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                webbrowser.open(path.as_uri())
        except Exception as exc:
            messagebox.showerror("\u65e0\u6cd5\u6253\u5f00", "%s\n%s" % (path, exc), parent=self)

    def _clear_cache(self) -> None:
        from . import http as http_mod

        if not messagebox.askyesno("\u786e\u8ba4", "\u786e\u5b9a\u8981\u6e05\u7a7a\u7f51\u7edc\u7f13\u5b58\u5417\uff1f", parent=self):
            return
        count = http_mod.default.clear_cache()
        self._log("\u5df2\u6e05\u7a7a\u7f13\u5b58\uff0c\u5220\u9664 %d \u4e2a\u6587\u4ef6\u3002" % count)

    def _report_sources(self) -> None:
        sources = self.registry.all()
        enabled = self.registry.enabled()
        self._log("\u5df2\u52a0\u8f7d\u4e66\u6e90 %d \u4e2a\uff08\u542f\u7528 %d \u4e2a\uff09\uff1a%s"
                  % (len(sources), len(enabled), "\u3001".join(s.name or s.id for s in enabled)))
        for report in self.registry.load_reports:
            if report.get("file") and not report.get("ok"):
                self._log("  \u26a0 \u63d2\u4ef6 %s \u52a0\u8f7d\u5931\u8d25\uff1a%s"
                          % (Path(report["file"]).name, report.get("message", "")))

    def _reload_sources(self) -> None:
        from . import http as http_mod
        from .sources import build_registry

        http_mod.configure_from_config(self.cfg)
        self.registry = build_registry(self.cfg)
        self._refresh_source_combo()
        self._log("\u5df2\u91cd\u65b0\u52a0\u8f7d\u4e66\u6e90\u3002")
        self._report_sources()

    # ==================================================================
    # 对话框
    # ==================================================================

    def _open_settings(self) -> None:
        win = tk.Toplevel(self)
        win.title("\u8bbe\u7f6e")
        win.geometry("560x460")
        win.transient(self)
        win.grab_set()

        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)

        def add_row(label: str, widget_factory, row: int):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5)
            widget = widget_factory(frame)
            widget.grid(row=row, column=1, sticky="ew", pady=5, padx=(8, 0))
            return widget

        frame.columnconfigure(1, weight=1)

        dir_var = tk.StringVar(value=self.cfg.get("download_dir", ""))
        def dir_factory(parent):
            box = ttk.Frame(parent)
            ttk.Entry(box, textvariable=dir_var).pack(side="left", fill="x", expand=True)
            ttk.Button(box, text="\u6d4f\u89c8\u2026", width=7, command=lambda: dir_var.set(
                filedialog.askdirectory(parent=win) or dir_var.get())).pack(side="left", padx=(4, 0))
            return box
        add_row("\u5bfc\u51fa\u76ee\u5f55", dir_factory, 0)

        enc_var = tk.StringVar(value=str(self.cfg.get("txt_encoding", "utf-8")))
        add_row("TXT \u7f16\u7801", lambda p: ttk.Combobox(
            p, textvariable=enc_var, state="readonly",
            values=["utf-8", "utf-8-sig", "gb18030"]), 1)

        wrap_var = tk.StringVar(value=str(self.cfg.get("txt_wrap_width", 0)))
        add_row("TXT \u6298\u884c\u5bbd\u5ea6\uff08\u5b57\u7b26\uff0c0 = \u4e0d\u6298\uff09",
                lambda p: ttk.Spinbox(p, from_=0, to=200, increment=10, textvariable=wrap_var), 2)

        max_var = tk.StringVar(value=str(self.cfg.get("max_results_per_source", 30)))
        add_row("\u6bcf\u4e2a\u4e66\u6e90\u6700\u5927\u7ed3\u679c\u6570",
                lambda p: ttk.Spinbox(p, from_=5, to=200, increment=5, textvariable=max_var), 3)

        timeout_var = tk.StringVar(value=str(self.cfg.get("timeout", 30)))
        add_row("\u8bf7\u6c42\u8d85\u65f6\uff08\u79d2\uff09",
                lambda p: ttk.Spinbox(p, from_=5, to=180, increment=5, textvariable=timeout_var), 4)

        retry_var = tk.StringVar(value=str(self.cfg.get("retries", 3)))
        add_row("\u91cd\u8bd5\u6b21\u6570",
                lambda p: ttk.Spinbox(p, from_=1, to=8, increment=1, textvariable=retry_var), 5)

        proxy_var = tk.StringVar(value=str(self.cfg.get("proxy", "")))
        add_row("\u4ee3\u7406\uff08\u7559\u7a7a\u5219\u4e0d\u7528\uff09", lambda p: ttk.Entry(p, textvariable=proxy_var), 6)

        cache_var = tk.BooleanVar(value=bool(self.cfg.get("cache_enabled", True)))
        ttk.Checkbutton(frame, text="\u542f\u7528\u7f51\u7edc\u7f13\u5b58\uff08\u52a0\u5feb\u91cd\u590d\u68c0\u7d22\uff09",
                        variable=cache_var).grid(row=7, column=0, columnspan=2, sticky="w", pady=5)

        cover_var = tk.BooleanVar(value=bool(self.cfg.get("epub_include_cover", True)))
        ttk.Checkbutton(frame, text="EPUB \u5305\u542b\u5c01\u9762",
                        variable=cover_var).grid(row=8, column=0, columnspan=2, sticky="w")
        split_var = tk.BooleanVar(value=bool(self.cfg.get("epub_split_chapters", True)))
        ttk.Checkbutton(frame, text="EPUB \u6bcf\u7ae0\u4e00\u4e2a\u6587\u4ef6\uff08\u517c\u5bb9\u6027\u66f4\u597d\uff09",
                        variable=split_var).grid(row=9, column=0, columnspan=2, sticky="w")

        def save() -> None:
            try:
                self.cfg["download_dir"] = dir_var.get()
                self.cfg["txt_encoding"] = enc_var.get()
                self.cfg["txt_wrap_width"] = int(wrap_var.get() or 0)
                self.cfg["max_results_per_source"] = int(max_var.get() or 30)
                self.cfg["timeout"] = int(timeout_var.get() or 30)
                self.cfg["retries"] = int(retry_var.get() or 3)
                self.cfg["proxy"] = proxy_var.get().strip()
                self.cfg["cache_enabled"] = bool(cache_var.get())
                self.cfg["epub_include_cover"] = bool(cover_var.get())
                self.cfg["epub_split_chapters"] = bool(split_var.get())
                self.cfg["formats"] = self._chosen_formats()
                config.save_config(self.cfg)

                from . import http as http_mod

                http_mod.configure_from_config(self.cfg)
                self.var_outdir.set(self.cfg["download_dir"])
                self.var_limit.set(str(self.cfg["max_results_per_source"]))
                self._log("\u8bbe\u7f6e\u5df2\u4fdd\u5b58\u3002")
                win.destroy()
            except Exception as exc:
                messagebox.showerror("\u4fdd\u5b58\u5931\u8d25", str(exc), parent=win)

        bar = ttk.Frame(win, padding=(14, 0, 14, 12))
        bar.pack(fill="x")
        ttk.Button(bar, text="\u4fdd\u5b58", style="Accent.TButton", command=save).pack(side="right")
        ttk.Button(bar, text="\u53d6\u6d88", command=win.destroy).pack(side="right", padx=(0, 8))

    def _open_sources(self) -> None:
        win = tk.Toplevel(self)
        win.title("\u4e66\u6e90\u7ba1\u7406")
        win.geometry("720x520")
        win.transient(self)

        ttk.Label(win, text="\u53cc\u51fb\u4e00\u884c\u53ef\u5207\u6362\u542f\u7528 / \u7981\u7528\uff08\u6539\u5b8c\u540e\u70b9\u300c\u4fdd\u5b58\u5e76\u91cd\u8f7d\u300d\uff09",
                  style="Hint.TLabel", padding=8).pack(anchor="w")

        columns = ("enabled", "name", "id", "lang", "desc")
        frame = ttk.Frame(win, padding=(8, 0))
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=columns, show="headings")
        for key, text, width in (
            ("enabled", "\u542f\u7528", 60), ("name", "\u540d\u79f0", 170),
            ("id", "ID", 120), ("lang", "\u8bed\u8a00", 90), ("desc", "\u8bf4\u660e", 260),
        ):
            tree.heading(key, text=text)
            tree.column(key, width=width, anchor="w", stretch=(key == "desc"))
        tree.pack(fill="both", expand=True)

        disabled = self.registry.disabled_ids()

        def refresh() -> None:
            tree.delete(*tree.get_children())
            for src in self.registry.all():
                tree.insert("", "end", iid=src.id, values=(
                    "\u2713" if src.id not in disabled else "",
                    src.name or src.id,
                    src.id,
                    "/".join(src.languages) or "-",
                    src.description,
                ))

        def toggle(_event=None) -> None:
            for iid in tree.selection():
                if iid in disabled:
                    disabled.discard(iid)
                else:
                    disabled.add(iid)
            refresh()

        tree.bind("<Double-1>", toggle)
        refresh()

        plugins_frame = ttk.LabelFrame(win, text="\u63d2\u4ef6\u52a0\u8f7d\u60c5\u51b5", padding=6)
        plugins_frame.pack(fill="x", padx=8, pady=8)
        plugin_text = tk.Text(plugins_frame, height=6, wrap="word", relief="flat",
                              font=("Consolas", 9), background="#f7f7f7")
        plugin_text.pack(fill="x")
        lines = []
        for report in self.registry.load_reports:
            if report.get("file"):
                lines.append("%s  %s  %s" % (
                    "\u2713" if report.get("ok") else "\u2717",
                    Path(report["file"]).name, report.get("message", "")))
            elif not report.get("ok"):
                lines.append("\u2717 \u5185\u7f6e\u6a21\u5757 %s: %s" % (report.get("module"), report.get("message", "")))
        plugin_text.insert("1.0", "\n".join(lines) or "\u6ca1\u6709\u63d2\u4ef6")
        plugin_text.configure(state="disabled")

        bar = ttk.Frame(win, padding=(8, 0, 8, 10))
        bar.pack(fill="x")
        ttk.Button(bar, text="\u6253\u5f00\u63d2\u4ef6\u76ee\u5f55", command=self._open_plugin_dir).pack(side="left")
        ttk.Button(bar, text="\u5b89\u88c5\u793a\u4f8b\u63d2\u4ef6", command=self._install_example_plugin).pack(side="left", padx=6)

        def save() -> None:
            self.cfg["disabled_sources"] = sorted(disabled)
            config.save_config(self.cfg)
            win.destroy()
            self._reload_sources()

        ttk.Button(bar, text="\u4fdd\u5b58\u5e76\u91cd\u8f7d", style="Accent.TButton", command=save).pack(side="right")

    def _install_example_plugin(self) -> None:
        from .sources import write_example_plugin

        path = write_example_plugin()
        if path:
            self._log("\u5df2\u5b89\u88c5\u793a\u4f8b\u63d2\u4ef6\uff1a%s" % path)
            messagebox.showinfo("\u5b8c\u6210", "\u793a\u4f8b\u63d2\u4ef6\u5df2\u5199\u5165\uff1a\n%s" % path, parent=self)
        else:
            messagebox.showwarning("\u5931\u8d25", "\u672a\u627e\u5230\u793a\u4f8b\u63d2\u4ef6\u6e90\u6587\u4ef6\u3002", parent=self)

    def _show_help(self) -> None:
        text = (
            "\u4f7f\u7528\u8bf4\u660e\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "1. \u5728\u9876\u90e8\u8f93\u5165\u5173\u952e\u5b57\uff08\u4e2d\u6587 / \u65e5\u6587 / \u82f1\u6587\u90fd\u884c\uff09\uff0c\u6309\u56de\u8f66\u6216\u70b9\u300c\u641c\u7d22\u300d\u3002\n"
            "2. \u7ed3\u679c\u4f1a\u968f\u7740\u5404\u4e2a\u4e66\u6e90\u8fd4\u56de\u800c\u9010\u6b65\u51fa\u73b0\uff0c\u53cc\u51fb\u4e00\u884c\u53ef\u76f4\u63a5\u4e0b\u8f7d\u3002\n"
            "3. \u53ef\u4ee5\u591a\u9009\uff08Ctrl / Shift \u70b9\u51fb\uff09\u540e\u6279\u91cf\u4e0b\u8f7d\u3002\n"
            "4. \u5728\u53f3\u4fa7\u52fe\u9009\u8981\u5bfc\u51fa\u7684\u683c\u5f0f\uff08EPUB / TXT / HTML\uff09\uff0c\u518d\u70b9\u300c\u4e0b\u8f7d\u5e76\u5bfc\u51fa\u300d\u3002\n"
            "5. \u300c\u9884\u89c8\u6b63\u6587\u300d\u53ef\u4ee5\u5148\u770b\u770b\u5185\u5bb9\u518d\u51b3\u5b9a\u8981\u4e0d\u8981\u3002\n\n"
            "\u5173\u4e8e\u4e66\u6e90\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "\u5185\u7f6e\u4e66\u6e90\u90fd\u662f\u5408\u6cd5\u514d\u8d39\u8d44\u6e90\uff08\u516c\u6709\u9886\u57df\u3001CC \u6388\u6743\u6216\u660e\u786e\u5f00\u653e\u53d6\u7528\uff09\uff1a\n"
            "  \u00b7 Project Gutenberg\uff08\u53e4\u817e\u5821\uff0c7 \u4e07+\u516c\u7248\u4e66\uff09\n"
            "  \u00b7 Standard Ebooks\uff08\u7cbe\u6821\u516c\u7248\u82f1\u6587\u4e66\uff09\n"
            "  \u00b7 \u7ef4\u57fa\u6587\u5e93\uff08\u4e2d / \u65e5 / \u82f1\u516c\u7248\u539f\u6587\uff09\n"
            "  \u00b7 \u9752\u7a7a\u6587\u5e93\uff08\u65e5\u672c\u516c\u7248\u6587\u5b66\uff09\n"
            "  \u00b7 Internet Archive / Open Library\n"
            "  \u00b7 \u672c\u5730\u4e66\u5e93\uff08\u793a\u4f8b\u63d2\u4ef6\uff0c\u628a\u4f60\u81ea\u5df1\u7684 epub/txt \u5f53\u4e66\u6e90\uff09\n\n"
            "\u60f3\u652f\u6301\u5176\u5b83\u7f51\u7ad9\uff0c\u5199\u4e00\u4e2a Python \u63d2\u4ef6\u653e\u5230\u63d2\u4ef6\u76ee\u5f55\u5373\u53ef\uff0c\n"
            "\u53c2\u800b examples/example_source.py\u3002\u8bf7\u81ea\u884c\u786e\u8ba4\u76ee\u6807\u7ad9\u70b9\u7684\u7248\u6743\u4e0e robots \u7ea6\u5b9a\u3002\n"
        )
        messagebox.showinfo("\u4f7f\u7528\u8bf4\u660e", text, parent=self)

    def _show_about(self) -> None:
        messagebox.showinfo(
            "\u5173\u4e8e BookSeeker",
            "BookSeeker %s\n\n"
            "\u8de8\u4e66\u6e90\u4e66\u7c4d\u68c0\u7d22\u4e0e\u5bfc\u51fa\u5de5\u5177\n"
            "\u7eaf Python \u6807\u51c6\u5e93\u5b9e\u73b0\uff0c\u96f6\u7b2c\u4e09\u65b9\u4f9d\u8d56\n\n"
            "\u914d\u7f6e\u4e0e\u7f13\u5b58\u76ee\u5f55\uff1a\n%s\n\n"
            "\u63d2\u4ef6\u76ee\u5f55\uff1a\n%s" % (__version__, config.HOME, config.PLUGIN_DIR),
            parent=self,
        )

    # ==================================================================
    # 工具
    # ==================================================================

    def _log(self, text: str) -> None:
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

    def _on_close(self) -> None:
        self.stop_flag.set()
        try:
            self.cfg["window_geometry"] = self.geometry()
            self.cfg["formats"] = self._chosen_formats()
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
