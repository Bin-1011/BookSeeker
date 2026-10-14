"""全局配置与路径管理。

所有可写数据（缓存、插件、配置、默认下载目录）都放在一个"家目录"下，
可以用环境变量 ``BOOKSEEKER_HOME`` 覆盖。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_NAME = "BookSeeker"
APP_TITLE = "BookSeeker \u2014 跨书源书籍检索与导出"


def _default_home() -> Path:
    env = os.environ.get("BOOKSEEKER_HOME")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / (".%s" % APP_NAME.lower())


HOME: Path = _default_home()
CACHE_DIR: Path = HOME / "cache"
PLUGIN_DIR: Path = HOME / "plugins"
CONFIG_FILE: Path = HOME / "config.json"
LOG_FILE: Path = HOME / "bookseeker.log"


def _default_download_dir() -> str:
    for candidate in (Path.home() / "Documents", Path.home() / "文档", Path.home()):
        try:
            if candidate.is_dir():
                return str(candidate / APP_NAME)
        except OSError:
            continue
    return str(Path.home() / APP_NAME)


DEFAULTS: dict = {
    # ---- 导出 ----
    "download_dir": _default_download_dir(),
    "formats": ["epub", "txt"],
    "txt_encoding": "utf-8",          # utf-8 / utf-8-sig / gb18030
    "txt_wrap_width": 0,              # 0 = 不自动换行（交给阅读器排版）
    "txt_chapter_separator": True,
    "epub_include_cover": True,
    "epub_split_chapters": True,
    "epub_include_source_link": True,
    # ---- 网络 ----
    "timeout": 30,
    "retries": 3,
    "proxy": "",                      # 形如 http://127.0.0.1:7890，留空则读环境变量
    "cache_ttl_days": 7,
    "cache_enabled": True,
    "request_interval": 0.7,          # 同一域名两次请求的最小间隔（秒）
    # ---- 书源 ----
    "disabled_sources": [],           # 想临时关掉某个书源就把它加进来
    "max_results_per_source": 30,
    # ---- 界面 ----
    "window_geometry": "1220x780",
    "theme": "vista",                 # vista / winnative / clam / default
}


def ensure_dirs() -> None:
    for d in (HOME, CACHE_DIR, PLUGIN_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        if CONFIG_FILE.is_file():
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k in DEFAULTS or k.startswith("_"):
                        cfg[k] = v
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    ensure_dirs()
    data = {k: v for k, v in cfg.items() if not k.startswith("_")}
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


def setup_console() -> None:
    """让 Windows 控制台按 UTF-8 输出，避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


#: 进程内共享的配置对象（由 bootstrap() 填充）
CONFIG: dict = dict(DEFAULTS)


def bootstrap() -> dict:
    """初始化目录 + 载入配置，返回配置字典。"""
    global CONFIG
    ensure_dirs()
    CONFIG = load_config()
    return CONFIG