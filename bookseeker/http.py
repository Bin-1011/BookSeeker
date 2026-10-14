"""HTTP 客户端：纯标准库实现，带重试、编码嗅探、磁盘缓存与同域限速。

所有内置书源和插件都应当通过本模块访问网络，这样可以统一享受：
自动重试、gzip 解压、BOM / meta 编码嗅探、离线缓存，以及对目标站点的礼貌限速。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import random
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import config

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "application/json;q=0.9,application/atom+xml;q=0.8,image/*;q=0.7,*/*;q=0.5",
    "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en-US;q=0.7,en;q=0.6",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "close",
}

#: ``hard_retries`` 客户端被限流（429 / 503）时最多再等的秒数。
#: 检索路径用得上：维基媒体这类站点经常回 429 + Retry-After，等一两秒再试
#: 往往就成功了，但绝不能为了一个源在后台等上几十秒。
HARD_RETRY_GRACE = 4.0


class HttpError(Exception):
    """网络请求最终失败。"""

    def __init__(self, message: str, url: str = "", status: int = 0):
        super().__init__(message)
        self.url = url
        self.status = status


# --------------------------------------------------------------------------
# 编码嗅探
# --------------------------------------------------------------------------

_BOMS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)

_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]{0,400}?charset\s*=\s*["']?\s*([a-zA-Z0-9_\-:.]+)""", re.I
)
_XML_ENCODING_RE = re.compile(
    rb"""<\?xml[^>]{0,200}?encoding\s*=\s*["']([a-zA-Z0-9_\-:.]+)["']""", re.I
)
_CT_CHARSET_RE = re.compile(r"""charset\s*=\s*["']?\s*([a-zA-Z0-9_\-:.]+)""", re.I)

#: 无任何声明时的候选编码，按「先utf-8、再中文、再日文、最后西文」的顺序打分
_CANDIDATES = (
    "utf-8", "gb18030", "big5", "shift_jis", "euc-jp", "euc-kr",
    "cp1251", "cp1252", "latin-1",
)

_ALIASES = {
    "gbk": "gb18030", "gb2312": "gb18030", "gb_2312-80": "gb18030",
    "csgb2312": "gb18030", "x-gbk": "gb18030", "ms936": "gb18030",
    "sjis": "shift_jis", "shift-jis": "shift_jis", "xsjis": "shift_jis",
    "ms_kanji": "shift_jis", "cp932": "shift_jis", "windows-31j": "shift_jis",
    "euc_jp": "euc-jp", "x-euc-jp": "euc-jp", "ujis": "euc-jp",
    "big-5": "big5", "big5-hkscs": "big5", "cp950": "big5",
    "utf8": "utf-8", "utf-8n": "utf-8", "unicode-1-1-utf-8": "utf-8",
    "ascii": "utf-8", "us-ascii": "utf-8", "iso-8859-1": "cp1252",
    "ks_c_5601-1987": "euc-kr", "cp949": "euc-kr",
}


def _norm_codec(name: str) -> str:
    name = (name or "").strip().strip("\"';").lower()
    return _ALIASES.get(name, name)


def _score_text(text: str) -> float:
    """粗略评估一段文本"像不像正常文字"，用于无声明时的编码猜测。"""
    sample = text[:6000]
    if not sample:
        return 0.0
    good = 0
    for ch in sample:
        o = ord(ch)
        if (
            o < 0x80
            or 0x2000 <= o <= 0x206F          # 通用标点
            or 0x2E80 <= o <= 0x2EFF          # 康熙部首
            or 0x3000 <= o <= 0x303F          # CJK 标点
            or 0x3040 <= o <= 0x30FF          # 平假名 / 片假名
            or 0x31F0 <= o <= 0x31FF          # 片假名扩展
            or 0x3400 <= o <= 0x4DBF          # CJK 扩展 A
            or 0x4E00 <= o <= 0x9FFF          # CJK 基本区
            or 0xAC00 <= o <= 0xD7AF          # 韩文音节
            or 0xF900 <= o <= 0xFAFF          # CJK 兼容
            or 0xFE30 <= o <= 0xFE4F
            or 0xFF00 <= o <= 0xFFEF          # 全角字符
        ):
            good += 1
    return good / len(sample)


def detect_encoding(data: bytes, content_type: str = "") -> str:
    """按 BOM → HTTP 头 → XML 声明 → HTML meta → 猜测 的顺序判断编码。"""
    for bom, enc in _BOMS:
        if data.startswith(bom):
            return enc

    if content_type:
        m = _CT_CHARSET_RE.search(content_type)
        if m:
            enc = _norm_codec(m.group(1))
            if enc:
                return enc

    head = data[:8192]
    m = _XML_ENCODING_RE.search(head)
    if m:
        enc = _norm_codec(m.group(1).decode("ascii", "ignore"))
        if enc:
            return enc

    m = _META_CHARSET_RE.search(head)
    if m:
        enc = _norm_codec(m.group(1).decode("ascii", "ignore"))
        if enc:
            return enc

    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass

    best, best_score = "utf-8", -1.0
    for cand in _CANDIDATES:
        try:
            text = data.decode(cand)
        except (UnicodeDecodeError, LookupError):
            continue
        score = _score_text(text)
        # 日文编码解出假名是强信号，给一点加权
        if cand in ("shift_jis", "euc-jp") and any(
            "\u3040" <= ch <= "\u30ff" for ch in text[:6000]
        ):
            score += 0.08
        if score > best_score:
            best, best_score = cand, score
    return best


def decode(data: bytes, encoding: str | None = None, content_type: str = "") -> str:
    """把字节解码成文本，失败时退化为替换字符而不是抛异常。"""
    enc = encoding or detect_encoding(data, content_type)
    try:
        return data.decode(enc, "replace")
    except LookupError:
        return data.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# 响应对象
# --------------------------------------------------------------------------


@dataclass
class Response:
    url: str
    status: int
    headers: dict = field(default_factory=dict)
    data: bytes = b""
    content_type: str = ""
    from_cache: bool = False

    def text(self, encoding: str | None = None) -> str:
        return decode(self.data, encoding, self.content_type)

    def json(self) -> Any:
        return json.loads(self.text())


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


def _ssl_context(relaxed: bool = False) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if relaxed:
        # 少数站点 / 中间设备对 TLS 握手比较挑剔，退一步重试
        try:
            ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        except Exception:
            pass
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        except Exception:
            pass
    return ctx


class HttpClient:
    """带重试与缓存的极简 HTTP 客户端（线程安全）。"""

    def __init__(
        self,
        cache_dir: Path | None = None,
        timeout: float = 30,
        retries: int = 3,
        proxy: str = "",
        min_interval: float = 0.4,
        cache_enabled: bool = True,
        cache_ttl: float = 7 * 86400,
        hard_retries: bool = False,
    ) -> None:
        self.timeout = timeout
        self.retries = max(1, int(retries))
        #: ``retries`` 是否是硬上限：为 True 时被限流（429 / 503）最多只再试一次，
        #: 而且等待不超过 :data:`HARD_RETRY_GRACE` 秒。检索路径会打开它——
        #: 「失败一次就放弃」正是检索想要的行为，否则一个被限流的源能在后台
        #: 按 Retry-After 等上好几分钟。
        self.hard_retries = bool(hard_retries)
        self.proxy = proxy or ""
        self.min_interval = max(0.0, float(min_interval))
        self.cache_enabled = cache_enabled
        self.cache_ttl = cache_ttl
        self.cache_dir = Path(cache_dir) if cache_dir else (config.CACHE_DIR / "http")
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stats = {"requests": 0, "cache_hits": 0, "errors": 0}

    # -- 内部工具 ---------------------------------------------------------

    def _opener(self) -> urllib.request.OpenerDirector:
        handlers: list[Any] = []
        if self.proxy:
            handlers.append(
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
            )
        handlers.append(urllib.request.HTTPSHandler(context=_ssl_context()))
        handlers.append(urllib.request.HTTPRedirectHandler())
        return urllib.request.build_opener(*handlers)

    def _throttle(self, url: str, interval: float | None = None) -> None:
        """同一域名的两次请求之间至少间隔 ``interval`` 秒。

        ``interval`` 为 ``None`` 时用客户端的默认值；书源可以对限流严格的站点
        （比如维基媒体）单次调大这个值。
        """
        interval = self.min_interval if interval is None else interval
        if interval <= 0:
            return
        host = urllib.parse.urlsplit(url).netloc
        with self._lock:
            now = time.monotonic()
            last = self._last_hit.get(host, 0.0)
            wait = interval - (now - last)
            if wait > 0:
                time.sleep(wait)
            self._last_hit[host] = time.monotonic()

    def _cache_file(self, url: str) -> Path:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self.cache_dir / digest[:2] / (digest + ".bin")

    def _cache_meta(self, url: str) -> Path:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self.cache_dir / digest[:2] / (digest + ".json")

    def _cache_read(self, url: str) -> Response | None:
        if not self.cache_enabled:
            return None
        f, meta_f = self._cache_file(url), self._cache_meta(url)
        try:
            if not f.is_file() or not meta_f.is_file():
                return None
            if self.cache_ttl > 0 and time.time() - f.stat().st_mtime > self.cache_ttl:
                return None
            meta = json.loads(meta_f.read_text(encoding="utf-8"))
            return Response(
                url=url,
                status=int(meta.get("status", 200)),
                headers={},
                data=f.read_bytes(),
                content_type=meta.get("content_type", ""),
                from_cache=True,
            )
        except Exception:
            return None

    def _cache_write(self, url: str, resp: Response) -> None:
        if not self.cache_enabled:
            return
        try:
            f, meta_f = self._cache_file(url), self._cache_meta(url)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(resp.data)
            meta_f.write_text(
                json.dumps(
                    {"url": url, "status": resp.status, "content_type": resp.content_type,
                     "ts": time.time()},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    @staticmethod
    def _decompress(raw: bytes, encoding: str) -> bytes:
        encoding = (encoding or "").lower()
        try:
            if encoding == "gzip":
                return gzip.decompress(raw)
            if encoding == "deflate":
                try:
                    return zlib.decompress(raw)
                except zlib.error:
                    return zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            return raw
        return raw

    @staticmethod
    def _retry_after(exc: Exception) -> float | None:
        """读 429 / 503 响应里的 Retry-After 头（秒数或 HTTP 日期），上限 60 秒。"""
        headers = getattr(exc, "headers", None)
        if not headers:
            return None
        raw = headers.get("Retry-After")
        if not raw:
            return None
        raw = raw.strip()
        try:
            return max(0.0, min(60.0, float(raw)))
        except ValueError:
            pass
        try:
            from email.utils import parsedate_to_datetime

            when = parsedate_to_datetime(raw)
            delta = when.timestamp() - time.time()
            return max(0.0, min(60.0, delta))
        except Exception:
            return None

    # -- 公共 API ---------------------------------------------------------

    def request(
        self,
        url: str,
        headers: dict | None = None,
        use_cache: bool | None = None,
        timeout: float | None = None,
        method: str = "GET",
        data: bytes | None = None,
        progress: Callable[[int, int], None] | None = None,
        interval: float | None = None,
    ) -> Response:
        """发起请求。失败会重试，超过次数抛 :class:`HttpError`。

        :param interval: 本次请求之前的最小间隔（秒），用来对限流严格的站点放慢速度。
        """
        use_cache = self.cache_enabled if use_cache is None else use_cache
        if method == "GET" and use_cache:
            hit = self._cache_read(url)
            if hit is not None:
                self._stats["cache_hits"] += 1
                return hit

        hdrs = dict(DEFAULT_HEADERS)
        if headers:
            hdrs.update({k: v for k, v in headers.items() if v is not None})
        # 书源明确指定的超时（例如古腾堡 120 秒的 Gutendex、青空文庫 180 秒的
        # 索引下载）一律照办——那些是有理由的慢操作；只有没指定时用客户端的默认值。
        timeout = float(timeout) if timeout else float(self.timeout)
        last_err: Exception | None = None

        retries = self.retries
        # 被限流时额外给几次机会（下载正文很有用）；检索路径（hard_retries）
        # 最多只多给一次，而且下面的等待时间也会被压到几秒以内。
        extra_attempts = 1 if self.hard_retries else 3

        attempt = 0
        max_attempts = retries
        while attempt < max_attempts:
            self._throttle(url, interval)
            delay: float | None = None
            try:
                req = urllib.request.Request(url, headers=hdrs, method=method, data=data)
                self._stats["requests"] += 1
                with self._opener().open(req, timeout=timeout) as raw:
                    body = self._decompress(raw.read(), raw.headers.get("Content-Encoding", ""))
                    resp = Response(
                        url=raw.geturl(), status=getattr(raw, "status", 200),
                        headers=dict(raw.headers), data=body,
                        content_type=raw.headers.get("Content-Type", "") or "",
                    )
                if method == "GET" and use_cache:
                    self._cache_write(url, resp)
                return resp

            except urllib.error.HTTPError as exc:
                last_err = exc
                status = exc.code
                if status in (429, 503):
                    # 被限流：优先听服务器给的 Retry-After，其次指数退避，
                    # 并且多给几次机会——限流本来就是「等一下再来」的意思。
                    self._stats["throttled"] = self._stats.get("throttled", 0) + 1
                    delay = self._retry_after(exc) or min(30.0, 1.5 * (2 ** attempt))
                    if self.hard_retries:
                        # 检索路径：只等一小会儿再试一次，不能为一个源在后台干等
                        delay = min(delay, HARD_RETRY_GRACE)
                    if attempt >= max_attempts - 1 and max_attempts < retries + extra_attempts:
                        max_attempts += 1
                elif status < 500:
                    # 其它 4xx 重试没有意义
                    self._stats["errors"] += 1
                    raise HttpError(
                        "HTTP %d %s" % (status, exc.reason), url, status
                    ) from None
                else:
                    delay = min(8.0, 0.6 * (2 ** attempt))

            except ssl.SSLError as exc:
                last_err = exc
                try:
                    req = urllib.request.Request(url, headers=hdrs, method=method, data=data)
                    opener = urllib.request.build_opener(
                        urllib.request.HTTPSHandler(context=_ssl_context(relaxed=True))
                    )
                    with opener.open(req, timeout=timeout) as raw:
                        body = self._decompress(
                            raw.read(), raw.headers.get("Content-Encoding", "")
                        )
                        resp = Response(
                            url=raw.geturl(), status=getattr(raw, "status", 200),
                            headers=dict(raw.headers), data=body,
                            content_type=raw.headers.get("Content-Type", "") or "",
                        )
                    if method == "GET" and use_cache:
                        self._cache_write(url, resp)
                    return resp
                except Exception as exc2:
                    last_err = exc2

            except Exception as exc:  # URLError / timeout / socket
                last_err = exc

            attempt += 1
            if attempt < max_attempts:
                time.sleep(
                    (delay if delay is not None else min(6.0, 0.6 * (2 ** (attempt - 1))))
                    + random.uniform(0, 0.3)
                )

        self._stats["errors"] += 1
        raise HttpError(
            "请求失败（重试 %d 次）：%s" % (attempt, last_err), url
        ) from last_err

    def get(self, url: str, **kw: Any) -> Response:
        return self.request(url, **kw)

    def get_text(self, url: str, encoding: str | None = None, **kw: Any) -> str:
        return self.request(url, **kw).text(encoding)

    def get_json(self, url: str, **kw: Any) -> Any:
        return self.request(url, **kw).json()

    def download(
        self,
        url: str,
        dest: Path,
        progress: Callable[[int, int], None] | None = None,
        headers: dict | None = None,
        use_cache: bool | None = None,
    ) -> Path:
        """把 URL 下载到本地文件并返回路径。"""
        resp = self.request(url, headers=headers, use_cache=use_cache)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.data)
        if progress:
            progress(len(resp.data), len(resp.data))
        return dest

    def clear_cache(self) -> int:
        """清空 HTTP 缓存，返回删除的文件数。"""
        n = 0
        try:
            for p in self.cache_dir.rglob("*"):
                if p.is_file():
                    p.unlink()
                    n += 1
        except Exception:
            pass
        return n

    def cache_size(self) -> int:
        total = 0
        try:
            for p in self.cache_dir.rglob("*"):
                if p.is_file():
                    total += p.stat().st_size
        except Exception:
            pass
        return total

    @property
    def stats(self) -> dict:
        return dict(self._stats)


#: 全局默认客户端
default = HttpClient()

_client_lock = threading.Lock()


def configure_from_config(cfg: dict | None = None) -> HttpClient:
    """按配置重建全局客户端（修改设置后调用）。"""
    global default
    cfg = cfg or config.CONFIG
    with _client_lock:
        default = HttpClient(
            timeout=cfg.get("timeout", 30),
            retries=cfg.get("retries", 3),
            proxy=cfg.get("proxy", ""),
            min_interval=cfg.get("request_interval", 0.4),
            cache_enabled=bool(cfg.get("cache_enabled", True)),
            cache_ttl=float(cfg.get("cache_ttl_days", 7)) * 86400,
        )
    return default


def get(url: str, **kw: Any) -> Response:
    return default.request(url, **kw)


def get_text(url: str, encoding: str | None = None, **kw: Any) -> str:
    return default.get_text(url, encoding=encoding, **kw)


def get_json(url: str, **kw: Any) -> Any:
    return default.get_json(url, **kw)