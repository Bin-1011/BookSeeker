"""BookSeeker 界面主题：配色、字体、DPI 缩放与绘制小工具。

视觉参考 kazumi（看番项目）那种暗色 + 圆角卡片 + 高饱和强调色的风格。
纯 tkinter 实现，不引入任何第三方库。
"""

from __future__ import annotations

import hashlib
import tkinter as tk
from tkinter import font as tkfont

# --------------------------------------------------------------------------
# 配色（暗色）
# --------------------------------------------------------------------------

BG = "#141420"          # 窗口底色
BG_SOFT = "#191926"     # 次级背景（侧边栏）
SURFACE = "#1F1F30"     # 卡片 / 面板
SURFACE_HI = "#2A2A40"  # 悬停
SURFACE_2 = "#252538"   # 输入框等
BORDER = "#31314A"
BORDER_HI = "#4A4A6A"

TEXT = "#ECEDF7"
TEXT_MUTED = "#9A9AB8"
TEXT_DIM = "#6C6C8C"

ACCENT = "#FF6B9D"      # 主强调色（玫红，kazumi 风）
ACCENT_DARK = "#E04E80"
ACCENT_2 = "#8B6BFF"    # 次强调色（紫）
SUCCESS = "#4ADE80"
WARN = "#FBBF24"
DANGER = "#F87171"

#: 封面占位图的渐变色对（标题哈希决定用哪一组，保证同一本书颜色稳定）
GRADIENTS: tuple[tuple[str, str], ...] = (
    ("#FF6B9D", "#8B6BFF"),
    ("#4FACFE", "#00F2FE"),
    ("#43E97B", "#38F9D7"),
    ("#FA709A", "#FEE140"),
    ("#A18CD1", "#FBC2EB"),
    ("#F6D365", "#FDA085"),
    ("#84FAB0", "#8FD3F4"),
    ("#F093FB", "#F5576C"),
    ("#5EE7DF", "#B490CA"),
    ("#667EEA", "#764BA2"),
    ("#FF9A9E", "#FECFEF"),
    ("#30CFD0", "#5B6FF5"),
    ("#FDCB6E", "#E17055"),
    ("#74B9FF", "#0984E3"),
    ("#A29BFE", "#6C5CE7"),
    ("#55EFC4", "#00B894"),
)

# --------------------------------------------------------------------------
# DPI 缩放
# --------------------------------------------------------------------------

_SCALE = 1.0
_SCALE_READY = False
_FAMILY_CACHE: dict[str, str] = {}

_PREFERRED_SANS = (
    "Microsoft YaHei UI", "Microsoft YaHei", "HarmonyOS Sans SC",
    "Noto Sans SC", "Source Han Sans SC", "PingFang SC",
    "Segoe UI", "Yu Gothic UI", "Meiryo UI", "Arial",
)
_PREFERRED_MONO = ("Cascadia Mono", "Cascadia Code", "Consolas", "Courier New")


def init_scaling(root: tk.Misc) -> float:
    """按屏幕 DPI 计算缩放系数。高分屏下所有像素尺寸都要乘它。"""
    global _SCALE, _SCALE_READY
    try:
        dpi = float(root.winfo_fpixels("1i"))
        _SCALE = max(1.0, min(3.0, dpi / 96.0))
    except Exception:
        _SCALE = 1.0
    _SCALE_READY = True
    return _SCALE


def scale() -> float:
    return _SCALE if _SCALE_READY else 1.0


def px(value: float) -> int:
    """把设计稿像素换算成当前 DPI 下的实际像素。"""
    return max(1, int(round(value * scale())))


def font(size: int = 10, weight: str = "normal", mono: bool = False) -> tuple:
    """取一个可用的字体元组（自动挑系统里存在的字族）。"""
    key = "mono" if mono else "sans"
    family = _FAMILY_CACHE.get(key)
    if family is None:
        candidates = _PREFERRED_MONO if mono else _PREFERRED_SANS
        try:
            available = set(tkfont.families())
        except Exception:
            available = set()
        family = next((c for c in candidates if c in available), candidates[-1])
        _FAMILY_CACHE[key] = family
    return (family, px(size), "bold" if weight == "bold" else "normal")


def fonts_ready() -> bool:
    return bool(_FAMILY_CACHE)


# --------------------------------------------------------------------------
# 绘制工具
# --------------------------------------------------------------------------


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(v))) for v in rgb)


def mix(color_a: str, color_b: str, t: float) -> str:
    """在两个颜色之间线性插值，``t`` 取 0~1。"""
    a, b = hex_to_rgb(color_a), hex_to_rgb(color_b)
    return rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))


def lighten(color: str, amount: float = 0.12) -> str:
    return mix(color, "#FFFFFF", amount)


def darken(color: str, amount: float = 0.12) -> str:
    return mix(color, "#000000", amount)


def gradient_for(text: str) -> tuple[str, str]:
    """按标题取一组稳定的渐变色（同一本书每次都是同一个颜色）。"""
    digest = hashlib.sha1((text or "?").encode("utf-8")).digest()
    return GRADIENTS[digest[0] % len(GRADIENTS)]


def round_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
               radius: float, **kwargs) -> int:
    """在 Canvas 上画一个圆角矩形（用平滑多边形实现，四角都是真圆角）。"""
    r = max(0.0, min(radius, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=12, **kwargs)


def draw_gradient(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
                  color_a: str, color_b: str, vertical: bool = True) -> None:
    """逐行/逐列画线性渐变（Canvas 没有原生渐变，这样最简单可靠）。"""
    steps = int(abs((y2 - y1) if vertical else (x2 - x1)))
    steps = max(1, min(steps, 400))
    for i in range(steps):
        t = i / max(1, steps - 1)
        color = mix(color_a, color_b, t)
        if vertical:
            yy = y1 + (y2 - y1) * i / steps
            canvas.create_line(x1, yy, x2, yy, fill=color)
        else:
            xx = x1 + (x2 - x1) * i / steps
            canvas.create_line(xx, y1, xx, y2, fill=color)


def gradient_tile(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
                  text: str, radius: float = 10, title: str = "",
                  subtitle: str = "") -> None:
    """画一块圆角渐变封面占位图，中间放书名首字。

    tkinter 只支持 PNG，而各书源的封面几乎都是 JPEG，所以与其显示破图，
    不如统一用这套「按书名生成稳定配色」的占位封面——视觉上也更整齐。
    """
    color_a, color_b = gradient_for(title or text)
    tile = tk.Canvas(canvas, highlightthickness=0, bd=0, bg=canvas["bg"])
    tile.place(x=x1, y=y1, width=x2 - x1, height=y2 - y1)

    # 渐变底 + 圆角遮罩（先画整块渐变，再用圆角多边形裁出形状）
    w, h = x2 - x1, y2 - y1
    body = tile.create_rectangle(0, 0, w, h, outline="", fill=color_a)
    draw_gradient(tile, 0, 0, w, h, color_a, color_b, vertical=True)
    # 右下角加一点深色，避免整体太平
    tile.create_oval(w * 0.25, h * 0.55, w * 1.25, h * 1.35,
                     fill=darken(color_b, 0.25), outline="")
    # 书名首字
    head = (text or title or "书").strip()[:2]
    tile.create_text(w / 2, h / 2 - (px(6) if subtitle else 0), text=head,
                     fill="#FFFFFF", font=font(20, "bold"))
    if subtitle:
        tile.create_text(w / 2, h - px(16), text=subtitle[:12],
                         fill="#FFFFFF", font=font(8))
    # 用页面底色把四角"切"成圆角
    r = radius
    for cx, cy, start in ((0, 0, 90), (w, 0, 180), (w, h, 270), (0, h, 0)):
        tile.create_arc(cx - r, cy - r, cx + r, cy + r, start=start, extent=90,
                        fill=canvas["bg"], outline="")
    tile.body = body  # type: ignore[attr-defined]
    return tile

# --------------------------------------------------------------------------
# 文字测量（tkinter 的 Canvas 不会告诉我们折了几行，只好自己量）
# --------------------------------------------------------------------------


def measure_lines(text: str, font_tuple, width: int) -> int:
    """一段文字在给定像素宽度下会折成几行（逐字测量，中日韩同样准确）。"""
    if width <= 0 or not text:
        return 1
    try:
        f = tkfont.Font(font=font_tuple)
    except Exception:
        return 1
    lines, current = 1, 0
    for ch in text:
        if ch == "\n":
            lines += 1
            current = 0
            continue
        cw = f.measure(ch)
        if current + cw > width:
            lines += 1
            current = cw
        else:
            current += cw
    return lines


def line_height(font_tuple) -> int:
    try:
        return tkfont.Font(font=font_tuple).metrics("linespace")
    except Exception:
        return px(16)


def text_height(text: str, font_tuple, width: int) -> int:
    return measure_lines(text, font_tuple, width) * line_height(font_tuple)


def fit_lines(text: str, font_tuple, width: int, max_lines: int = 2) -> str:
    """把文字截断到最多 ``max_lines`` 行，超出部分用省略号。"""
    text = (text or "").strip()
    if not text or width <= 0 or max_lines <= 0:
        return text
    try:
        f = tkfont.Font(font=font_tuple)
    except Exception:
        return text
    lines, current, used = 1, 0, 0
    for i, ch in enumerate(text):
        cw = f.measure(ch)
        if current + cw > width:
            lines += 1
            if lines > max_lines:
                return text[:max(0, used)].rstrip() + "…"
            current = cw
        else:
            current += cw
        used = i + 1
    return text


def ellipsize(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
