"""BookSeeker 的各个对话框：设置、书源管理、正文预览、帮助。

全部用自绘控件 + 暗色配色，和主界面保持一致。
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

from .. import __version__, config, exporter
from ..models import BookContent, language_label
from ..sources import write_example_plugin
from . import theme as T
from .widgets import Chip, LabeledEntry, PillButton, ScrollFrame, SectionTitle, Select, Switch


def make_dialog(app: tk.Misc, title: str, width: int, height: int) -> tk.Toplevel:
    """建一个暗色 Toplevel，居中于主窗口。"""
    win = tk.Toplevel(app)
    win.title(title)
    win.configure(bg=T.BG)
    win.transient(app)
    try:
        app.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - T.px(width)) // 2
        y = app.winfo_rooty() + (app.winfo_height() - T.px(height)) // 3
        win.geometry("%dx%d+%d+%d" % (T.px(width), T.px(height), max(0, x), max(0, y)))
    except Exception:
        win.geometry("%dx%d" % (T.px(width), T.px(height)))
    win.minsize(T.px(width) // 2, T.px(240))
    return win


def heading(win: tk.Misc, text: str, sub: str = "") -> tk.Frame:
    box = tk.Frame(win, bg=T.BG)
    box.pack(fill="x", padx=T.px(20), pady=(T.px(16), T.px(4)))
    tk.Label(box, text=text, bg=T.BG, fg=T.TEXT,
             font=T.font(15, "bold")).pack(anchor="w")
    if sub:
        tk.Label(box, text=sub, bg=T.BG, fg=T.TEXT_MUTED,
                 font=T.font(9)).pack(anchor="w", pady=(T.px(2), 0))
    return box


def dark_text(master: tk.Misc, **kw) -> tk.Text:
    return tk.Text(master, bg=T.SURFACE_2, fg=T.TEXT, insertbackground=T.ACCENT,
                   relief="flat", font=T.font(9), wrap="word",
                   highlightthickness=1, highlightbackground=T.BORDER,
                   padx=T.px(8), pady=T.px(6), **kw)


# ==========================================================================
# 设置
# ==========================================================================


def open_settings(app) -> None:
    cfg = app.cfg
    win = make_dialog(app, "设置", 640, 620)
    win.grab_set()

    heading(win, "设置", "改动会立刻保存到 config.json")

    body = ScrollFrame(win, bg=T.BG)
    body.pack(fill="both", expand=True, padx=T.px(20), pady=T.px(8))
    box = body.body

    # ---- 导出 ----
    SectionTitle(box, "导出").pack(anchor="w", pady=(T.px(10), T.px(8)))

    dir_entry = LabeledEntry(box, "导出目录", cfg.get("download_dir", ""), width=32,
                             button="浏览…",
                             on_button=lambda: _choose_dir(app, dir_entry, win))
    dir_entry.pack(anchor="w", pady=T.px(4))

    fmt_row = tk.Frame(box, bg=T.BG)
    fmt_row.pack(anchor="w", pady=T.px(4))
    tk.Label(fmt_row, text="默认格式", bg=T.BG, fg=T.TEXT_MUTED,
             font=T.font(9), width=16, anchor="w").pack(side="left")
    fmt_vars = {}
    for fmt, label in exporter.FORMATS.items():
        var = tk.BooleanVar(value=fmt in (cfg.get("formats") or ["epub", "txt"]))
        fmt_vars[fmt] = var
        tk.Checkbutton(fmt_row, text=label, variable=var, bg=T.BG, fg=T.TEXT,
                       selectcolor=T.SURFACE_2, activebackground=T.BG,
                       activeforeground=T.TEXT, font=T.font(9), bd=0,
                       highlightthickness=0).pack(side="left", padx=(0, T.px(10)))

    enc = Select(box, "TXT 编码", ["utf-8", "utf-8-sig", "gb18030"],
                 str(cfg.get("txt_encoding", "utf-8")))
    enc.pack(anchor="w", pady=T.px(4))
    wrap = LabeledEntry(box, "TXT 折行宽度", str(cfg.get("txt_wrap_width", 0)), width=8,
                        hint="0 = 不自动折行")
    wrap.pack(anchor="w", pady=T.px(4))

    epub_row = tk.Frame(box, bg=T.BG)
    epub_row.pack(anchor="w", pady=T.px(4))
    tk.Label(epub_row, text="EPUB 选项", bg=T.BG, fg=T.TEXT_MUTED,
             font=T.font(9), width=16, anchor="w").pack(side="left")
    cover_sw = Switch(epub_row, bool(cfg.get("epub_include_cover", True)))
    cover_sw.pack(side="left")
    tk.Label(epub_row, text="包含封面", bg=T.BG, fg=T.TEXT,
             font=T.font(9)).pack(side="left", padx=(T.px(4), T.px(14)))
    split_sw = Switch(epub_row, bool(cfg.get("epub_split_chapters", True)))
    split_sw.pack(side="left")
    tk.Label(epub_row, text="每章一个文件", bg=T.BG, fg=T.TEXT,
             font=T.font(9)).pack(side="left", padx=T.px(4))

    # ---- 检索 ----
    SectionTitle(box, "检索").pack(anchor="w", pady=(T.px(18), T.px(8)))
    limit = LabeledEntry(box, "每源最大结果", str(cfg.get("max_results_per_source", 30)), width=8)
    limit.pack(anchor="w", pady=T.px(4))
    deadline = LabeledEntry(box, "检索总超时（秒）", str(cfg.get("search_deadline", 12)), width=8,
                            hint="到点就用已拿到的结果，不等慢源")
    deadline.pack(anchor="w", pady=T.px(4))

    # ---- 网络 ----
    SectionTitle(box, "网络").pack(anchor="w", pady=(T.px(18), T.px(8)))
    timeout = LabeledEntry(box, "请求超时（秒）", str(cfg.get("timeout", 30)), width=8)
    timeout.pack(anchor="w", pady=T.px(4))
    retries = LabeledEntry(box, "重试次数", str(cfg.get("retries", 3)), width=8)
    retries.pack(anchor="w", pady=T.px(4))
    proxy = LabeledEntry(box, "代理", str(cfg.get("proxy", "")), width=24,
                         hint="例如 http://127.0.0.1:7890")
    proxy.pack(anchor="w", pady=T.px(4))

    net_row = tk.Frame(box, bg=T.BG)
    net_row.pack(anchor="w", pady=T.px(4))
    tk.Label(net_row, text="缓存", bg=T.BG, fg=T.TEXT_MUTED,
             font=T.font(9), width=16, anchor="w").pack(side="left")
    cache_sw = Switch(net_row, bool(cfg.get("cache_enabled", True)))
    cache_sw.pack(side="left")
    tk.Label(net_row, text="启用网络缓存", bg=T.BG, fg=T.TEXT,
             font=T.font(9)).pack(side="left", padx=T.px(4))
    PillButton(net_row, "清空缓存", variant="ghost", width=84, height=28, font_size=9,
               command=lambda: app.clear_cache()).pack(side="left", padx=T.px(12))

    # ---- 底部按钮 ----
    bar = tk.Frame(win, bg=T.BG)
    bar.pack(fill="x", padx=T.px(20), pady=T.px(14))

    def save() -> None:
        try:
            cfg["download_dir"] = dir_entry.get()
            cfg["txt_encoding"] = enc.get()
            cfg["txt_wrap_width"] = int(wrap.get() or 0)
            cfg["max_results_per_source"] = int(limit.get() or 30)
            cfg["search_deadline"] = float(deadline.get() or 12)
            cfg["timeout"] = int(timeout.get() or 30)
            cfg["retries"] = int(retries.get() or 3)
            cfg["proxy"] = proxy.get()
            cfg["cache_enabled"] = bool(cache_sw.value)
            cfg["epub_include_cover"] = bool(cover_sw.value)
            cfg["epub_split_chapters"] = bool(split_sw.value)
            cfg["formats"] = [f for f, v in fmt_vars.items() if v.get()] or ["epub"]
            config.save_config(cfg)

            from .. import http as http_mod

            http_mod.configure_from_config(cfg)
            app.var_outdir.set(cfg["download_dir"])
            app.var_limit.set(str(cfg["max_results_per_source"]))
            app.var_deadline.set(str(cfg["search_deadline"]))
            app.log("设置已保存。")
            win.destroy()
        except Exception as exc:
            app.log("保存设置失败：%s" % exc)

    PillButton(bar, "保存", command=save, width=100).pack(side="right")
    PillButton(bar, "取消", command=win.destroy, variant="ghost", width=88).pack(
        side="right", padx=T.px(8))


def _choose_dir(app, entry: LabeledEntry, parent) -> None:
    current = entry.get() or str(Path.home())
    chosen = tk.filedialog.askdirectory(
        parent=parent, title="选择导出目录",
        initialdir=current if Path(current).is_dir() else None)
    if chosen:
        entry.set(chosen)


# ==========================================================================
# 书源管理
# ==========================================================================


def open_sources(app) -> None:
    win = make_dialog(app, "书源管理", 720, 620)
    heading(win, "书源管理", "关掉不想用的书源可以加快检索；插件的加载情况也在这里看")

    body = ScrollFrame(win, bg=T.BG)
    body.pack(fill="both", expand=True, padx=T.px(20), pady=T.px(6))
    box = body.body

    disabled = set(app.registry.disabled_ids())
    switches: dict[str, Switch] = {}

    for src in app.registry.all():
        row = tk.Frame(box, bg=T.SURFACE, highlightthickness=1,
                       highlightbackground=T.BORDER)
        row.pack(fill="x", pady=T.px(4), ipady=T.px(8))

        left = tk.Frame(row, bg=T.SURFACE)
        left.pack(side="left", fill="x", expand=True, padx=T.px(12))
        tk.Label(left, text=src.name or src.id, bg=T.SURFACE,
                 fg=T.TEXT, font=T.font(10, "bold")).pack(anchor="w")
        desc = src.description or ""
        langs = "/".join(src.languages) or "-"
        tk.Label(left, text="%s   语言：%s" % (desc, langs), bg=T.SURFACE,
                 fg=T.TEXT_MUTED, font=T.font(8), anchor="w",
                 justify="left", wraplength=T.px(430)).pack(anchor="w", pady=(T.px(2), 0))

        sw = Switch(row, src.id not in disabled)
        sw.pack(side="right", padx=T.px(14))
        switches[src.id] = sw

    # ---- 插件情况 ----
    SectionTitle(box, "插件加载情况").pack(anchor="w", pady=(T.px(16), T.px(6)))
    plugins = [r for r in app.registry.load_reports if r.get("file")]
    if not plugins:
        tk.Label(box, text="还没有装任何插件。", bg=T.BG, fg=T.TEXT_DIM,
                 font=T.font(9)).pack(anchor="w")
    for rep in plugins:
        ok = rep.get("ok")
        line = "%s  %s  %s" % ("✓" if ok else "✗", Path(rep["file"]).name,
                               rep.get("message", ""))
        tk.Label(box, text=line, bg=T.BG, fg=T.SUCCESS if ok else T.DANGER,
                 font=T.font(9), anchor="w", justify="left",
                 wraplength=T.px(620)).pack(anchor="w", pady=T.px(1))

    failed = [r for r in app.registry.load_reports if not r.get("file") and not r.get("ok")]
    for rep in failed:
        tk.Label(box, text="✗ 内置模块 %s：%s" % (rep.get("module"), rep.get("message", "")),
                 bg=T.BG, fg=T.DANGER, font=T.font(9), anchor="w",
                 justify="left", wraplength=T.px(620)).pack(anchor="w", pady=T.px(1))

    bar = tk.Frame(win, bg=T.BG)
    bar.pack(fill="x", padx=T.px(20), pady=T.px(14))
    PillButton(bar, "打开插件目录", variant="ghost", width=120,
               command=app.open_plugin_dir).pack(side="left")
    PillButton(bar, "安装示例插件", variant="ghost", width=120,
               command=app.install_example_plugin).pack(side="left", padx=T.px(8))

    def save() -> None:
        cfg = app.cfg
        cfg["disabled_sources"] = sorted(sid for sid, sw in switches.items() if not sw.value)
        config.save_config(cfg)
        win.destroy()
        app.log("已更新书源启用状态。")
        app.reload_sources()

    PillButton(bar, "保存并重载", command=save, width=120).pack(side="right")


# ==========================================================================
# 正文预览
# ==========================================================================


def open_preview(app, content: BookContent) -> None:
    win = make_dialog(app, "预览：%s" % (content.title or ""), 900, 700)

    top = tk.Frame(win, bg=T.BG)
    top.pack(fill="x", padx=T.px(20), pady=(T.px(14), T.px(6)))
    tk.Label(top, text=content.title or "(无标题)", bg=T.BG, fg=T.TEXT,
             font=T.font(14, "bold")).pack(anchor="w")
    info = "%s ｜ %d 章 ｜ 约 %s 字 ｜ %s" % (
        content.author_text or "作者不详", len(content.chapters),
        format(content.word_count, ","), content.source_name or "")
    tk.Label(top, text=info, bg=T.BG, fg=T.TEXT_MUTED,
             font=T.font(9)).pack(anchor="w", pady=(T.px(2), 0))

    missing = getattr(content, "missing_chapters", 0)
    if missing:
        warn = tk.Frame(win, bg=T.mix(T.WARN, T.BG, 0.82), highlightthickness=1,
                        highlightbackground=T.WARN)
        warn.pack(fill="x", padx=T.px(20), pady=T.px(4))
        tk.Label(warn, text="⚠ 有 %d 章本次没抓到，正文里已用占位符标出。"
                            "重新下载时会走缓存，已成功的章节不会重复下载。" % missing,
                 bg=warn["bg"], fg=T.WARN, font=T.font(9), anchor="w",
                 justify="left", wraplength=T.px(820)).pack(anchor="w", padx=T.px(10),
                                                             pady=T.px(6))

    split = tk.Frame(win, bg=T.BG)
    split.pack(fill="both", expand=True, padx=T.px(20), pady=(T.px(4), T.px(14)))

    # 左侧章节列表
    left = tk.Frame(split, bg=T.BG)
    left.pack(side="left", fill="y")
    tk.Label(left, text="章节", bg=T.BG, fg=T.TEXT_MUTED,
             font=T.font(8)).pack(anchor="w")
    listbox = tk.Listbox(left, width=24, bg=T.SURFACE, fg=T.TEXT,
                         selectbackground=T.ACCENT, selectforeground="#1A1A28",
                         relief="flat", font=T.font(9), bd=0,
                         highlightthickness=1, highlightbackground=T.BORDER,
                         activestyle="none")
    lsb = tk.Scrollbar(left, orient="vertical", command=listbox.yview,
                       bg=T.SURFACE, troughcolor=T.BG, relief="flat",
                       bd=0, width=T.px(10))
    listbox.configure(yscrollcommand=lsb.set)
    listbox.pack(side="left", fill="y", expand=True)
    lsb.pack(side="right", fill="y")
    for idx, ch in enumerate(content.chapters, 1):
        listbox.insert("end", "%d. %s" % (idx, ch.title or "第%d章" % idx))

    # 右侧正文
    right = tk.Frame(split, bg=T.BG)
    right.pack(side="left", fill="both", expand=True, padx=(T.px(12), 0))
    text = dark_text(right)
    tsb = tk.Scrollbar(right, orient="vertical", command=text.yview,
                       bg=T.SURFACE, troughcolor=T.BG, relief="flat",
                       bd=0, width=T.px(10))
    text.configure(yscrollcommand=tsb.set)
    text.pack(side="left", fill="both", expand=True)
    tsb.pack(side="right", fill="y")

    from ..convert.htmlutil import html_to_text

    def show_chapter(_event=None) -> None:
        sel = listbox.curselection()
        if not sel:
            return
        chapter = content.chapters[sel[0]]
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("1.0", "%s\n\n%s" % (chapter.title or "", html_to_text(chapter.html)))
        text.configure(state="disabled")
        text.yview_moveto(0)

    listbox.bind("<<ListboxSelect>>", show_chapter)
    listbox.bind("<MouseWheel>", lambda e: listbox.yview_scroll(int(-e.delta / 120), "units"))
    if content.chapters:
        listbox.selection_set(0)
        show_chapter()


# ==========================================================================
# 帮助 / 关于
# ==========================================================================


HELP_TEXT = """使用说明
──────────────────────────────────────────────
1. 顶部搜索框输入关键字（中文 / 日文 / 英文都行），回车即可。
   检索是并发的：快的书源一两秒就出结果，慢的会在「检索总超时」后放弃，
   不会一直等下去。
2. 首页「热门书籍」是精选书单，点封面直接定位到那本书。
3. 结果以卡片显示，单击选中；Ctrl+单击可以多选，然后一次批量下载。
4. 右侧面板选好导出格式（EPUB / TXT / HTML）和目录，点「下载并导出」。
5. 「预览正文」可以先看内容再决定要不要。

关于书源
──────────────────────────────────────────────
内置书源都是合法免费资源（公有领域、CC 授权或明确开放取用）：
  · Project Gutenberg（古腾堡，7 万余本公版书）
  · Standard Ebooks（精校公版英文书）
  · 维基文库（中 / 日 / 英公版原文，长篇小说自动按子页面分章）
  · 青空文庫（日本公有领域文学）
  · Internet Archive（只取开放取用的扫描件全文）
  · Open Library（书目元数据，默认关闭）
  · 本地书库（示例插件，把你自己的 epub/txt 当书源）

想支持其它网站，写一个 Python 插件放进插件目录即可，
参考 examples/example_source.py。请自行确认目标站点的版权与 robots 约定。

关于「章节不全」
──────────────────────────────────────────────
维基文库这类站点会限流，抓 100 多章的长篇时可能有个别章失败。
遇到这种情况：程序不会静默丢章，而是在正文里放一个明确的占位段落，
并在导出文件的头部标注「有 N 章抓取失败」。
重新下载同一本书时，已成功的章节会走本地缓存，只会重试缺的那几章。
"""


def show_help(app) -> None:
    win = make_dialog(app, "使用说明", 680, 620)
    heading(win, "使用说明")
    box = tk.Frame(win, bg=T.BG)
    box.pack(fill="both", expand=True, padx=T.px(20), pady=T.px(8))
    text = dark_text(box, font=T.font(9))
    sb = tk.Scrollbar(box, orient="vertical", command=text.yview, bg=T.SURFACE,
                      troughcolor=T.BG, relief="flat", bd=0, width=T.px(10))
    text.configure(yscrollcommand=sb.set)
    text.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    text.insert("1.0", HELP_TEXT)
    text.configure(state="disabled")
    PillButton(win, "知道了", command=win.destroy, width=100).pack(pady=T.px(12))


def show_about(app) -> None:
    win = make_dialog(app, "关于", 460, 340)
    heading(win, "BookSeeker %s" % __version__, "跨书源书籍检索与导出工具")
    box = tk.Frame(win, bg=T.BG)
    box.pack(fill="both", expand=True, padx=T.px(24), pady=T.px(8))
    for line in (
        "纯 Python 标准库实现，零第三方依赖。",
        "",
        "配置与缓存目录：",
        str(config.HOME),
        "",
        "插件目录：",
        str(config.PLUGIN_DIR),
        "",
        "书源只包含公有领域与明确开放取用的资源。",
    ):
        tk.Label(box, text=line, bg=T.BG,
                 fg=T.TEXT_MUTED if line.endswith("：") else T.TEXT_DIM,
                 font=T.font(9), anchor="w", justify="left",
                 wraplength=T.px(400)).pack(anchor="w")
    PillButton(win, "关闭", command=win.destroy, width=100).pack(pady=T.px(12))
