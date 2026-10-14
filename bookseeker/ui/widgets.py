"""BookSeeker 界面控件：圆角卡片、渐变封面、胶囊按钮、滚动容器。

tkinter 原生控件做不出圆角和渐变，所以这些"现代"控件都是用 ``tk.Canvas``
手绘出来的。好处是零依赖、外观完全可控。
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

from . import theme as T


# ==========================================================================
# 滚动容器
# ==========================================================================


class ScrollFrame(tk.Frame):
    """一个可以纵向滚动、内容宽度自动跟随的容器。

    把子控件放进 ``.body``。
    """

    def __init__(self, master: tk.Misc, bg: str = T.BG, **kw) -> None:
        super().__init__(master, bg=bg, **kw)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.vsb = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                bg=T.SURFACE, troughcolor=bg, activebackground=T.BORDER_HI,
                                relief="flat", bd=0, width=T.px(10),
                                highlightthickness=0)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")

        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", lambda _e: self._bind_wheel())
        self.canvas.bind("<Leave>", lambda _e: self._unbind_wheel())

    def _on_body_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    # -- 滚轮 --------------------------------------------------------------

    def _bind_wheel(self) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event) -> None:
        try:
            self.canvas.yview_scroll(int(-event.delta / 120) * 3, "units")
        except Exception:
            pass

    def scroll_to_top(self) -> None:
        self.canvas.yview_moveto(0.0)

    def clear(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()


# ==========================================================================
# 封面占位图
# ==========================================================================


def draw_cover(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
               title: str, radius: float = 10, bg: str = T.SURFACE,
               badge: str = "") -> None:
    """在给定矩形里画一块圆角渐变封面（书名首字 + 可选角标）。

    tkinter 只认 PNG，而各书源封面几乎清一色 JPEG，所以统一用这套
    按书名生成稳定配色的占位封面，比显示破图好看得多。
    """
    w, h = x2 - x1, y2 - y1
    if w <= 1 or h <= 1:
        return
    color_a, color_b = T.gradient_for(title)

    steps = max(8, min(int(h), 200))
    for i in range(steps):
        t = i / max(1, steps - 1)
        yy = y1 + h * i / steps
        canvas.create_line(x1, yy, x2, yy, fill=T.mix(color_a, color_b, t))

    # 右下角做一个柔和的暗角。Canvas 没有透明度，所以用多层同心椭圆叠加
    # 来模拟渐变，否则就是一个边缘很硬的圆片。
    # 所有椭圆的包围盒都锚在封面右下角向内收，保证不越界。
    dark = T.darken(color_b, 0.34)
    layers = 6
    for i in range(layers):
        t = i / (layers - 1)
        shrink = 0.30 * (1.0 - t)
        canvas.create_oval(
            x1 + w * (0.40 + shrink * 0.55), y1 + h * (0.42 + shrink * 0.55),
            x2 - w * 0.02 * t, y2 - h * 0.02 * t,
            fill=T.mix(color_b, dark, 0.10 + 0.16 * i), outline="")

    head = (title or "书").strip()[:2]
    canvas.create_text((x1 + x2) / 2, y1 + h * 0.42, text=head,
                       fill="#FFFFFF", font=T.font(22, "bold"))

    # 四角用背景色"切"成圆角
    r = max(0, min(radius, w / 2, h / 2))
    if r:
        for cx, cy, start in ((x1, y1, 90), (x2, y1, 180), (x2, y2, 270), (x1, y2, 0)):
            canvas.create_arc(cx - r, cy - r, cx + r, cy + r, start=start,
                              extent=90, fill=bg, outline="")

    if badge:
        _draw_badge(canvas, x1 + T.px(8), y2 - T.px(24), badge)


def _draw_badge(canvas: tk.Canvas, x: float, y: float, text: str) -> None:
    """封面左下角的小圆角标签（显示来源）。"""
    f = T.font(8)
    width = max(T.px(30), len(text) * T.px(9) + T.px(14))
    height = T.px(18)
    T.round_rect(canvas, x, y, x + width, y + height, height / 2,
                 fill=T.mix("#000000", "#FFFFFF", 0.08), outline="")
    canvas.create_text(x + width / 2, y + height / 2, text=text,
                       fill="#FFFFFF", font=f)


# ==========================================================================
# 书卡
# ==========================================================================


class BookCard(tk.Canvas):
    """一张书卡：封面 + 书名 + 副标题。

    整张卡画在一个 Canvas 上，这样才能有真正的圆角。
    """

    W = 166
    H = 262
    COVER_H = 168

    def __init__(self, master: tk.Misc, title: str, subtitle: str = "",
                 badge: str = "", meta: str = "", on_click: Callable | None = None,
                 selected: bool = False, **kw) -> None:
        self._cw = T.px(self.W)
        self._ch = T.px(self.H)
        super().__init__(master, width=self._cw, height=self._ch, bg=T.BG,
                         highlightthickness=0, bd=0, **kw)
        self.title = title or ""
        self.subtitle = subtitle or ""
        self.badge = badge or ""
        self.meta = meta or ""
        self.on_click = on_click
        self.selected = selected
        self._hover = False

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self.draw()

    # -- 状态 --------------------------------------------------------------

    def set_selected(self, value: bool) -> None:
        if self.selected != value:
            self.selected = value
            self.draw()

    def _on_enter(self, _event=None) -> None:
        self._hover = True
        self.draw()
        self.configure(cursor="hand2")

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self.draw()

    def _on_click(self, event=None) -> None:
        # 把事件一起传出去，调用方才能判断 Ctrl / Shift 多选
        if self.on_click:
            self.on_click(self, event)

    # -- 绘制 --------------------------------------------------------------

    def draw(self) -> None:
        self.delete("all")
        w, h = self._cw, self._ch
        pad = T.px(8)

        if self.selected:
            bg, border = T.SURFACE_HI, T.ACCENT
        elif self._hover:
            bg, border = T.SURFACE_HI, T.BORDER_HI
        else:
            bg, border = T.SURFACE, T.BORDER

        T.round_rect(self, 0.5, 0.5, w - 0.5, h - 0.5, T.px(12),
                     fill=bg, outline=border, width=1)

        cover_h = T.px(self.COVER_H)
        draw_cover(self, pad, pad, w - pad, pad + cover_h, self.title,
                   radius=T.px(9), bg=bg, badge=self.badge)

        # 文字排版靠真实测量，不靠猜行数
        text_w = w - pad * 2 - T.px(4)
        title_font = T.font(10, "bold")
        sub_font = T.font(9)
        meta_font = T.font(8)

        y = pad + cover_h + T.px(8)
        title = T.fit_lines(self.title, title_font, text_w, 2)
        self.create_text(pad + T.px(2), y, anchor="nw", text=title,
                         fill=T.TEXT, font=title_font, width=text_w)
        y += T.text_height(title, title_font, text_w) + T.px(2)

        if self.subtitle:
            sub = T.fit_lines(self.subtitle, sub_font, text_w, 1)
            self.create_text(pad + T.px(2), y, anchor="nw", text=sub,
                             fill=T.TEXT_MUTED, font=sub_font, width=text_w)
            y += T.text_height(sub, sub_font, text_w) + T.px(1)

        if self.meta:
            self.create_text(pad + T.px(2), min(y, h - T.px(15)), anchor="nw",
                             text=T.fit_lines(self.meta, meta_font, text_w, 1),
                             fill=T.TEXT_DIM, font=meta_font, width=text_w)


# ==========================================================================
# 按钮
# ==========================================================================


class PillButton(tk.Canvas):
    """圆角（胶囊）按钮。``variant`` 可选 ``primary`` / ``ghost`` / ``danger``。"""

    def __init__(self, master: tk.Misc, text: str, command: Callable | None = None,
                 variant: str = "primary", width: int | None = None,
                 height: int = 36, font_size: int = 10, **kw) -> None:
        self._text = text
        self._variant = variant
        self._ch = T.px(height)
        self._cw = T.px(width) if width else max(T.px(88), len(text) * T.px(13) + T.px(28))
        super().__init__(master, width=self._cw, height=self._ch, bg=master["bg"],
                         highlightthickness=0, bd=0, **kw)
        self.command = command
        self._state = "normal"
        self._hover = False
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.draw()

    # -- 对外 --------------------------------------------------------------

    def set_text(self, text: str) -> None:
        self._text = text
        self.draw()

    def set_state(self, state: str) -> None:
        self._state = state
        self.draw()

    def set_active(self, active: bool) -> None:
        self._variant = "primary" if active else "ghost"
        self.draw()

    # -- 交互 --------------------------------------------------------------

    def _enter(self, _e=None) -> None:
        self._hover = True
        self.configure(cursor="hand2" if self._state == "normal" else "arrow")
        self.draw()

    def _leave(self, _e=None) -> None:
        self._hover = False
        self.draw()

    def _press(self, _e=None) -> None:
        if self._state == "normal":
            self._state = "pressed"
            self.draw()

    def _release(self, _e=None) -> None:
        if self._state == "pressed":
            self._state = "normal"
            self.draw()
            if self.command:
                self.command()

    # -- 绘制 --------------------------------------------------------------

    def draw(self) -> None:
        self.delete("all")
        w, h = self._cw, self._ch
        disabled = self._state == "disabled"

        if self._variant == "primary":
            fill = T.ACCENT_DARK if self._state == "pressed" else (
                T.ACCENT if not self._hover else T.lighten(T.ACCENT, 0.12))
            fg = "#1A1A28"
            outline = ""
        elif self._variant == "danger":
            fill = T.DANGER if not disabled else T.mix(T.DANGER, T.BG, 0.6)
            fg = "#1A1A28"
            outline = ""
        else:  # ghost
            fill = T.SURFACE_HI if self._hover else T.SURFACE
            fg = T.TEXT if not disabled else T.TEXT_DIM
            outline = T.BORDER_HI if self._hover else T.BORDER

        if disabled:
            fill = T.mix(fill, T.BG, 0.55)

        T.round_rect(self, 0.5, 0.5, w - 0.5, h - 0.5, h / 2,
                     fill=fill, outline=outline, width=1)
        self.create_text(w / 2, h / 2, text=self._text, fill=fg,
                         font=T.font(10, "bold"))


class NavItem(tk.Canvas):
    """左侧导航栏的一项。"""

    H = 42

    def __init__(self, master: tk.Misc, text: str, icon: str = "",
                 command: Callable | None = None, **kw) -> None:
        self._cw = T.px(150)
        self._ch = T.px(self.H)
        super().__init__(master, width=self._cw, height=self._ch, bg=T.BG_SOFT,
                         highlightthickness=0, bd=0, **kw)
        self._text = text
        self._icon = icon
        self._active = False
        self._hover = False
        self.command = command
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", lambda _e: self.command and self.command(self))
        self.draw()

    def _set_hover(self, value: bool) -> None:
        self._hover = value
        self.configure(cursor="hand2")
        self.draw()

    def set_active(self, value: bool) -> None:
        self._active = value
        self.draw()

    def draw(self) -> None:
        self.delete("all")
        w, h = self._cw, self._ch
        pad = T.px(8)
        if self._active:
            T.round_rect(self, pad, T.px(3), w - pad, h - T.px(3), T.px(10),
                         fill=T.SURFACE_HI, outline="")
        elif self._hover:
            T.round_rect(self, pad, T.px(3), w - pad, h - T.px(3), T.px(10),
                         fill=T.mix(T.SURFACE, T.BG_SOFT, 0.5), outline="")

        color = T.ACCENT if self._active else (T.TEXT if self._hover else T.TEXT_MUTED)
        x = pad + T.px(14)
        if self._icon:
            self.create_text(x, h / 2, text=self._icon, fill=color, font=T.font(11))
            x += T.px(22)
        self.create_text(x, h / 2, anchor="w", text=self._text, fill=color,
                         font=T.font(10, "bold" if self._active else "normal"))


class Chip(tk.Canvas):
    """小圆角标签，用来显示来源 / 分类 / 状态。``set_text`` 可以改字。"""

    def __init__(self, master: tk.Misc, text: str, color: str = T.ACCENT_2,
                 bg: str | None = None, **kw) -> None:
        self._text = text
        self._color = color
        self._cw = max(T.px(38), len(text) * T.px(9) + T.px(16))
        self._ch = T.px(20)
        super().__init__(master, width=self._cw, height=self._ch,
                         bg=bg or master["bg"], highlightthickness=0, bd=0, **kw)
        self.draw()

    def set_text(self, text: str) -> None:
        self._text = text
        width = max(T.px(38), len(text) * T.px(9) + T.px(16))
        if width != self._cw:
            self._cw = width
            self.configure(width=width)
        self.draw()

    def draw(self) -> None:
        self.delete("all")
        T.round_rect(self, 0, 0, self._cw, self._ch, self._ch / 2,
                     fill=T.mix(self._color, self["bg"], 0.78), outline="")
        self.create_text(self._cw / 2, self._ch / 2, text=self._text,
                         fill=self._color, font=T.font(8))

class Switch(tk.Canvas):
    """圆角滑动开关（比原生 Checkbutton 好看，且颜色完全可控）。"""

    W = 44
    H = 24

    def __init__(self, master: tk.Misc, value: bool = False,
                 command: Callable | None = None, **kw) -> None:
        self._cw = T.px(self.W)
        self._ch = T.px(self.H)
        super().__init__(master, width=self._cw, height=self._ch,
                         bg=master["bg"], highlightthickness=0, bd=0, **kw)
        self.value = bool(value)
        self.command = command
        self.bind("<Button-1>", self._toggle)
        self.bind("<Enter>", lambda _e: self.configure(cursor="hand2"))
        self.draw()

    def _toggle(self, _event=None) -> None:
        self.value = not self.value
        self.draw()
        if self.command:
            self.command(self.value)

    def set(self, value: bool) -> None:
        self.value = bool(value)
        self.draw()

    def draw(self) -> None:
        self.delete("all")
        w, h = self._cw, self._ch
        track = T.ACCENT if self.value else T.SURFACE_HI
        T.round_rect(self, 0.5, 0.5, w - 0.5, h - 0.5, h / 2,
                     fill=track, outline=T.BORDER if not self.value else "", width=1)
        r = h / 2 - T.px(3)
        cx = (w - r - T.px(3)) if self.value else (r + T.px(3))
        self.create_oval(cx - r, h / 2 - r, cx + r, h / 2 + r,
                         fill="#FFFFFF", outline="")


class LabeledEntry(tk.Frame):
    """一行表单：左边标签，右边输入框（可选后缀按钮）。"""

    def __init__(self, master: tk.Misc, label: str, value: str = "",
                 width: int = 26, hint: str = "", button: str = "",
                 on_button: Callable | None = None, **kw) -> None:
        super().__init__(master, bg=master["bg"], **kw)
        tk.Label(self, text=label, bg=master["bg"], fg=T.TEXT_MUTED,
                 font=T.font(9), width=16, anchor="w").pack(side="left")
        self.var = tk.StringVar(value=value)
        self.entry = tk.Entry(self, textvariable=self.var, width=width,
                              bg=T.SURFACE_2, fg=T.TEXT, insertbackground=T.ACCENT,
                              relief="flat", font=T.font(9),
                              highlightthickness=1, highlightbackground=T.BORDER,
                              highlightcolor=T.ACCENT)
        self.entry.pack(side="left", ipady=T.px(5), padx=(0, T.px(6)))
        if button:
            PillButton(self, button, command=on_button, variant="ghost",
                       width=70, height=28, font_size=9).pack(side="left")
        if hint:
            tk.Label(self, text=hint, bg=master["bg"], fg=T.TEXT_DIM,
                     font=T.font(8)).pack(side="left", padx=T.px(6))

    def get(self) -> str:
        return self.var.get().strip()

    def set(self, value: str) -> None:
        self.var.set(value)


class Select(tk.Frame):
    """一行表单：左边标签，右边下拉框（用 OptionMenu 保证暗色可读）。"""

    def __init__(self, master: tk.Misc, label: str, values: list[str],
                 value: str = "", width: int = 22, **kw) -> None:
        super().__init__(master, bg=master["bg"], **kw)
        tk.Label(self, text=label, bg=master["bg"], fg=T.TEXT_MUTED,
                 font=T.font(9), width=16, anchor="w").pack(side="left")
        self.var = tk.StringVar(value=value or (values[0] if values else ""))
        menu = tk.OptionMenu(self, self.var, *(values or [""]))
        menu.configure(bg=T.SURFACE_2, fg=T.TEXT, activebackground=T.SURFACE_HI,
                       activeforeground=T.TEXT, relief="flat", bd=0,
                       highlightthickness=0, font=T.font(9), width=width,
                       anchor="w", indicatoron=True)
        menu["menu"].configure(bg=T.SURFACE_2, fg=T.TEXT,
                               activebackground=T.ACCENT, activeforeground="#1A1A28",
                               font=T.font(9), bd=0)
        menu.pack(side="left")

    def get(self) -> str:
        return self.var.get()


class SectionTitle(tk.Frame):
    """设置页里的小节标题（左侧一条强调色竖线）。"""

    def __init__(self, master: tk.Misc, text: str, **kw) -> None:
        super().__init__(master, bg=master["bg"], **kw)
        bar = tk.Canvas(self, width=T.px(3), height=T.px(16), bg=master["bg"],
                        highlightthickness=0)
        bar.create_rectangle(0, 0, T.px(3), T.px(16), fill=T.ACCENT, outline="")
        bar.pack(side="left", padx=(0, T.px(8)))
        tk.Label(self, text=text, bg=master["bg"], fg=T.TEXT,
                 font=T.font(11, "bold")).pack(side="left")
