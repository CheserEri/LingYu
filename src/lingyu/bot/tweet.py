"""X（Twitter）推文卡片：抓取推文数据，并用 PIL 本地渲染成一张图片。

数据源
------
X 的 **syndication** 接口（公开、无需登录、无需 API Key）::

    https://cdn.syndication.twimg.com/tweet-result?id=<推文ID>&token=<token>

``token`` 由推文 ID 算出（算法同 react-tweet，见 ``_token``）。实测该接口对
token 只要求**非空**（给错值也返回数据），但仍按正确算法生成，避免哪天收紧。

渲染
----
完全用 PIL 本地绘制（**不依赖浏览器**，离线可用），产出接近 X 推文卡片的图：
头像、昵称、认证徽标、@handle、正文、配图、底部时间与互动数。

排版的高度**全部由字号度量算出**（ascent/descent），不靠逐行墨迹高度累加：
后者会让行距忽松忽紧，且遇到段落空行时低估总高，正文就压到配图上了。

配图：**按原比例竖排成一条长图**，一条消息发完。不拼固定格子、不做覆盖裁切
（裁切必然切掉画面），几张图就是几段；单张过高时限高并同步收窄、居中。

字体：**按字符**沿候选链选字体（见 ``_REGULAR_CHAIN``）——雅黑覆盖中/英/俄/希
和假名，韩文、泰文、阿拉伯文、印度诸语各由专门字体补，emoji 最后落到
Segoe UI Emoji。是否「有这个字形」用位图与 ``.notdef`` 比对来判断，因为
PIL 不暴露 cmap，而硬编码「哪个字体覆盖哪些语种」在不同 Windows 版本上并不可靠。

翻译：卡片正文优先用 ``Tweet.translation``（由引擎调大模型翻好再传进来），
原文缩成灰色小字附在「原文」引用块里。翻不翻由 :func:`needs_translation` 判定。

视频：``parse_tweet`` 会顺带取回**最佳 mp4 直链**（``Tweet.video_url``），
由 ``download_video`` 直接流式下载——不依赖 yt-dlp 的 X 提取器（其默认走
GraphQL，未登录时容易 403）。yt-dlp 仅作为拿不到直链时的兜底。
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------- #
# 推文 URL
# --------------------------------------------------------------------------- #

# 形如 https://x.com/<user>/status/<id>（也兼容 twitter.com / mobile.x.com 等）。
_TWEET_RE = re.compile(
    r"https?://(?:www\.|mobile\.|m\.)?(?:x|twitter)\.com/"
    r"(?P<user>[A-Za-z0-9_]{1,20})/status(?:es)?/(?P<id>\d+)",
    re.IGNORECASE,
)

_API = "https://cdn.syndication.twimg.com/tweet-result"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def is_tweet_url(url: str) -> bool:
    """是否 X/Twitter 推文链接。"""
    return _TWEET_RE.match(url or "") is not None


def tweet_id(url: str) -> str | None:
    """取推文数字 ID；不是推文链接时返回 ``None``。"""
    match = _TWEET_RE.match(url or "")
    return match.group("id") if match else None


def _base36(value: float) -> str:
    """浮点转 36 进制（模拟 JS 的 ``Number.prototype.toString(36)``）。"""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    whole = int(value)
    frac = value - whole
    out = "0" if whole == 0 else ""
    while whole:
        out = digits[whole % 36] + out
        whole //= 36
    if frac:
        out += "."
        for _ in range(25):
            frac *= 36
            digit = int(frac)
            out += digits[digit]
            frac -= digit
            if frac == 0:
                break
    return out


def _token(tid: str) -> str:
    """syndication 接口的 token：``((id / 1e15) * PI).toString(36)`` 去掉 0 与点。"""
    return _base36((int(tid) / 1e15) * math.pi).replace("0", "").replace(".", "")


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


@dataclass
class Tweet:
    """一条推文（卡片渲染所需的全部字段）。"""

    id: str
    url: str
    text: str = ""
    author_name: str = ""
    author_handle: str = ""
    avatar_url: str = ""
    # 认证："" / "blue" / "business" / "government"
    verified: str = ""
    created_at: str = ""
    photos: list[str] = field(default_factory=list)
    has_video: bool = False
    video_url: str = ""  # 最佳 mp4 直链（有视频时才有）
    video_poster: str = ""  # 视频/动图的封面帧（卡片上用它 + 播放角标）
    likes: int = 0
    replies: int = 0
    # 译文（通常是简体中文）。空串 = 未翻译，卡片直接画正文；
    # 非空时卡片以译文为正文、原文缩成灰色小字附在下面。
    translation: str = ""

    def summary(self) -> str:
        """压成一行注入模型上下文。"""
        parts: list[str] = []
        if self.author_name:
            handle = f"（@{self.author_handle}）" if self.author_handle else ""
            parts.append(f"作者：{self.author_name}{handle}")
        if self.created_at:
            parts.append(f"时间：{self.created_at}")
        if self.has_video:
            parts.append("含视频")
        elif self.photos:
            parts.append(f"含 {len(self.photos)} 张图")
        if self.likes:
            parts.append(f"点赞：{self.likes}")
        body = re.sub(r"\s+", " ", self.translation or self.text).strip()
        head = f"[推文] {self.url}"
        if body:
            head += f"｜正文：{body[:200]}"
        return head + ("｜" + "；".join(parts) if parts else "")


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #


def _get(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read()


def _fmt_time(raw: str) -> str:
    """``2014-09-03T15:18:45.000Z`` → ``2014-09-03 15:18``（转本地时区）。"""
    if not raw:
        return ""
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone().strftime("%Y-%m-%d %H:%M")


def _best_video_url(media: list[dict]) -> str:
    """从 ``mediaDetails`` 里挑最高码率的 mp4 直链。

    ``animated_gif``（动图）本质也是 mp4，同样可用；只要没有 mp4 变体就返回空串。
    """
    best_url = ""
    best_bitrate = -1
    for item in media:
        if not isinstance(item, dict):
            continue
        variants = ((item.get("video_info") or {}).get("variants")) or []
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            url = str(variant.get("url") or "")
            if not url or variant.get("content_type") != "video/mp4":
                continue
            try:
                bitrate = int(variant.get("bitrate") or 0)
            except (TypeError, ValueError):
                bitrate = 0
            if bitrate > best_bitrate:
                best_bitrate, best_url = bitrate, url
    return best_url


def parse_tweet(data: dict, url: str) -> Tweet | None:
    """把 syndication 的 JSON 转成 :class:`Tweet`。"""
    if not isinstance(data, dict) or not data.get("id_str"):
        return None
    user = data.get("user") or {}
    photos = [
        str(item["url"])
        for item in (data.get("photos") or [])
        if isinstance(item, dict) and item.get("url")
    ]
    media = [item for item in (data.get("mediaDetails") or []) if isinstance(item, dict)]
    verified = ""
    if user.get("is_blue_verified"):
        verified = str(user.get("verified_type") or "blue").lower()
    elif user.get("verified"):
        verified = "blue"
    has_video = any(m.get("type") in ("video", "animated_gif") for m in media)
    # 视频/动图没有 photos 条目，但 syndication 给了封面帧（media_url_https）；
    # 把它补进配图列表，卡片才有缩略图可画。
    poster = ""
    for item in media:
        if item.get("type") in ("video", "animated_gif"):
            poster = str(item.get("media_url_https") or "")
            if poster:
                break
    if poster and poster not in photos:
        photos.append(poster)
    return Tweet(
        id=str(data["id_str"]),
        url=url,
        text=str(data.get("text") or ""),
        author_name=str(user.get("name") or ""),
        author_handle=str(user.get("screen_name") or ""),
        avatar_url=str(user.get("profile_image_url_https") or ""),
        verified=verified,
        created_at=_fmt_time(str(data.get("created_at") or "")),
        photos=photos,
        has_video=has_video,
        video_url=_best_video_url(media) if has_video else "",
        video_poster=poster if has_video else "",
        likes=int(data.get("favorite_count") or 0),
        replies=int(data.get("conversation_count") or 0),
    )


def fetch_tweet(
    url: str,
    timeout: float = 20.0,
    logger: Callable[[str, str], None] | None = None,
) -> Tweet | None:
    """抓取推文；失败返回 ``None``。"""
    log = logger or (lambda level, message: None)
    tid = tweet_id(url)
    if not tid:
        return None
    api = f"{_API}?id={tid}&token={_token(tid)}&lang=en"
    try:
        payload = json.loads(_get(api, timeout))
    except urllib.error.HTTPError as exc:
        log("warning", f"推文抓取失败（HTTP {exc.code}）：{url}")
        return None
    except Exception as exc:  # 网络/解析异常统一兜住
        log("warning", f"推文抓取失败：{exc}")
        return None
    tweet = parse_tweet(payload, url)
    if tweet is None:
        log("warning", f"推文不存在或已删除：{url}")
    return tweet


_CACHE: dict[str, Tweet] = {}
_CACHE_LOCK = threading.Lock()


def fetch_tweet_cached(
    url: str,
    timeout: float = 20.0,
    logger: Callable[[str, str], None] | None = None,
) -> Tweet | None:
    """带缓存的抓取：同一条推文在一次运行里只请求一次（成功才缓存）。

    解析上下文（``links.LinkResolver``）与发送卡片都会用到推文数据，
    缓存一下避免同一条推文被抓两遍。
    """
    with _CACHE_LOCK:
        hit = _CACHE.get(url)
    if hit is not None:
        return hit
    info = fetch_tweet(url, timeout, logger)
    if info is not None:
        with _CACHE_LOCK:
            _CACHE[url] = info
    return info


def download_video(
    info: Tweet,
    outdir: str | Path,
    timeout: float = 60.0,
    logger: Callable[[str, str], None] | None = None,
    max_mb: int = 0,
) -> Path | None:
    """把推文里的视频下载到 ``outdir``，返回文件路径；失败返回 ``None``。

    直接用 ``Tweet.video_url``（syndication 给的 mp4 直链）流式下载，
    因此不依赖 yt-dlp 的 X 提取器。``max_mb`` > 0 时超限即中断并删除残文件。
    """
    log = logger or (lambda level, message: None)
    url = info.video_url
    if not url:
        log("warning", f"推文未提供视频直链：{info.url}")
        return None
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / f"tweet-{info.id}.mp4"
    limit = max_mb * 1024 * 1024 if max_mb and max_mb > 0 else 0
    request = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Referer": "https://x.com/"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            declared = resp.headers.get("Content-Length")
            if limit and declared and int(declared) > limit:
                log("warning", f"推文视频超过体积上限（{declared} 字节），已跳过")
                return None
            written = 0
            with target.open("wb") as handle:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if limit and written > limit:
                        raise ValueError("视频超过体积上限")
                    handle.write(chunk)
    except Exception as exc:
        log("warning", f"推文视频下载失败：{exc}")
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    if target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        log("warning", "推文视频下载为空文件")
        return None
    return target


# 推文配图地址里的尺寸参数（syndication 给的是小图，``name=small``）。
_PHOTO_SIZE_RE = re.compile(r"([?&])name=[A-Za-z0-9_]+")


def best_photo_url(url: str) -> str:
    """把配图地址换成大图。

    syndication 返回的多是 ``...?format=jpg&name=small``，直接发回聊天会糊。
    统一改成 ``name=large``（约 1200px，体积可控）——``orig`` 动辄几 MB，
    微信发图会先转码，没必要。
    """
    url = (url or "").strip()
    if not url:
        return ""
    if "pbs.twimg.com" not in url:
        return url
    if _PHOTO_SIZE_RE.search(url):
        return _PHOTO_SIZE_RE.sub(r"\1name=large", url)
    return url + ("&" if "?" in url else "?") + "name=large"


def photo_urls(info: Tweet, limit: int = 9) -> list[str]:
    """要画进卡片的配图地址。

    视频推文的 ``photos`` 里那张是封面帧，视频本身会另发，故排除；
    其余按原顺序返回，最多 ``limit`` 张。
    """
    return [
        url
        for url in info.photos
        if url and not (info.has_video and url == info.video_poster)
    ][: max(0, int(limit))]


# --------------------------------------------------------------------------- #
# 翻译判定
# --------------------------------------------------------------------------- #

# 汉字 / 假名 / 谚文：用来判断「这段文字是不是已经不需要翻译」。
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_HANGUL_RE = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_URL_ONLY_RE = re.compile(r"https?://\S+")


def needs_translation(text: str, han_ratio: float = 0.3) -> bool:
    """这段正文是否需要翻成中文。

    判定刻意保守——**只在明显是外文时**才翻，避免把中文推文翻成
    「中文的中文」白烧 token：

    - 含假名或谚文 → 日文/韩文，要翻
    - 汉字占「字母类字符」比例 ≥ ``han_ratio`` → 视为中文原文，不翻
    - 纯链接 / 纯数字 / 纯符号 → 不翻
    - 其余（英、俄、阿、泰、印地…）→ 要翻
    """
    body = _URL_ONLY_RE.sub(" ", text or "")
    if _KANA_RE.search(body) or _HANGUL_RE.search(body):
        return True
    letters = _LETTER_RE.findall(body)
    if len(letters) < 2:
        return False
    han = len(_HAN_RE.findall(body))
    return han / len(letters) < han_ratio


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #

CARD_WIDTH = 760
_PAD = 28
_AVATAR = 76
_NAME_SIZE = 27
_HANDLE_SIZE = 21
_BODY_SIZE = 25
_QUOTE_SIZE = 21  # 「原文」块（有译文时）
_FOOT_SIZE = 20
_LINE_GAP = 9  # 行内额外间距（行高 = ascent + descent + 该值）
_HEAD_GAP = 16  # 头像块 → 正文
_QUOTE_GAP = 16  # 正文 → 原文块
_BLOCK_GAP = 20  # 文字 → 配图 / 配图 → 页脚
_MEDIA_RADIUS = 14
# 单张配图的最大高度：超过就等比收窄并居中，免得一张长截图把整条卡片
# 撑到几千像素高。
_MAX_MEDIA_HEIGHT = 900
# 多图**竖排成长图**：每张铺满宽度、按原比例往下摞，图与图间距 8px。
_MEDIA_GAP = 8

# 候选字体链，**按字符**取第一个真正含该字符字形的字体。
# 单用雅黑必然出豆腐块：它没有韩文/阿拉伯文/泰文/天城文；
# Segoe UI 反过来没有汉字。顺序即优先级：
#   雅黑（中/英/俄/希/假名）→ 游ゴシック（日文补充）→ 韩文 →
#   Segoe UI（阿拉伯/希伯来）→ Nirmala（印度诸语）→ 泰文 →
#   符号 → emoji
_REGULAR_CHAIN = (
    "msyh.ttc",
    "YuGothR.ttc",
    "malgun.ttf",
    "segoeui.ttf",
    "Nirmala.ttf",
    "LeelawUI.ttf",
    "seguisym.ttf",
    "seguiemj.ttf",
)
_BOLD_CHAIN = (
    "msyhbd.ttc",
    "YuGothB.ttc",
    "malgunbd.ttf",
    "segoeuib.ttf",
    "NirmalaB.ttf",
    "LeelaUIb.ttf",
    "seguisym.ttf",
    "seguiemj.ttf",
)
# 确定「字体里没有这个字」用的探针码位：Unicode 第 16 平面的未分配码位，
# 任何字体都没有它的字形，画出来就是 .notdef（豆腐块）本身。
_NOTDEF_PROBE = "\U0010FFFD"
# 零宽字符直接丢掉，否则会多算宽度、把行撑开。
_ZERO_WIDTH = {"\u200d", "\ufe0e", "\ufe0f", "\u200b"}


def _font_dir() -> Path:
    return Path(os.environ.get("WINDIR") or r"C:\Windows") / "Fonts"


@lru_cache(maxsize=64)
def _font_file(name: str, size: int) -> ImageFont.FreeTypeFont | None:
    """按文件名加载系统字体；缺失时返回 ``None``（不抛异常）。"""
    try:
        return ImageFont.truetype(str(_font_dir() / name), size)
    except OSError:
        return None


@lru_cache(maxsize=4096)
def _glyph_mask(name: str, size: int, ch: str) -> bytes:
    font = _font_file(name, size)
    if font is None:
        return b""
    mask = font.getmask(ch)
    try:  # Pillow 10+：getmask 直接返回可 tobytes 的对象
        return mask.tobytes()
    except AttributeError:  # 旧版本返回 ImagingCore，得先包成 Image
        return Image.Image()._new(mask).tobytes()


@lru_cache(maxsize=64)
def _notdef_mask(name: str, size: int) -> bytes:
    return _glyph_mask(name, size, _NOTDEF_PROBE)


@lru_cache(maxsize=8192)
def _has_glyph(name: str, size: int, ch: str) -> bool:
    """字体里有没有 ``ch`` 的字形。

    PIL 不暴露 cmap，所以用位图比对：缺字形时 FreeType 一律返回同一个
    .notdef 方块，与「必然缺失」的探针码位画出来的位图完全一致。
    这比硬编码「哪个字体覆盖哪些语种」可靠——Windows 各版本装的字体并不一致。
    """
    if ch.isspace():
        return True
    return _glyph_mask(name, size, ch) != _notdef_mask(name, size)


@lru_cache(maxsize=8192)
def _pick_font(ch: str, size: int, bold: bool) -> ImageFont.FreeTypeFont:
    """按候选链取第一个含该字符的字体；全都没有则退回链首（画成豆腐块）。"""
    chain = _BOLD_CHAIN if bold else _REGULAR_CHAIN
    for name in chain:
        if _has_glyph(name, size, ch):
            font = _font_file(name, size)
            if font is not None:
                return font
    fallback = _font_file(chain[0], size)
    return fallback if fallback is not None else ImageFont.load_default(size)


@lru_cache(maxsize=64)
def _metrics(size: int, bold: bool = False) -> tuple[int, int]:
    """同字号的 ``(ascent, descent)``。

    行高必须**由字号决定、而不是由行内字符的墨迹高度决定**：早先按
    ``getbbox()`` 逐行累加，同一段文字里「只有逗号的一行」和「有 g 的一行」
    高度不同，行距忽松忽紧，整块正文还会比预估矮——于是压到下面的配图上。
    """
    font = _font_file(_BOLD_CHAIN[0] if bold else _REGULAR_CHAIN[0], size)
    if font is None:
        return int(size * 0.78), int(size * 0.26)
    ascent, descent = font.getmetrics()
    return ascent, descent


def _line_advance(size: int, bold: bool = False) -> int:
    ascent, descent = _metrics(size, bold)
    return ascent + descent + _LINE_GAP


# RTL 区段（希伯来 / 阿拉伯 / 叙利亚 / 它拿 / 阿拉伯呈现形式）。
_RTL_RANGES = (
    (0x0590, 0x05FF),
    (0x0600, 0x06FF),
    (0x0700, 0x074F),
    (0x0750, 0x077F),
    (0x0780, 0x07BF),
    (0x07C0, 0x07FF),
    (0x0800, 0x085F),
    (0x08A0, 0x08FF),
    (0xFB1D, 0xFB4F),
    (0xFB50, 0xFDFF),
    (0xFE70, 0xFEFF),
)
# RTL 段里允许跟着一起反转的中性字符（标点/空格）。
_RTL_NEUTRAL = set(" \t.,!?:;-_/\\()[]{}<>\"'«»…،؛؟٬٫")
# 折行时「可以断在后面」的字符：长 URL / 长英文串靠它们找到断点。
_BREAK_AFTER = set(" /-&?=+_,;")


def _is_rtl(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _RTL_RANGES)


def _visual_order(text: str) -> str:
    """把 RTL 连续段整体反转，让阿拉伯/希伯来文按从右到左的视觉顺序落笔。

    这只是**够用级别**的 bidi：不实现完整的 UBA，也不做阿拉伯文连写塑形
    （那需要 arabic_reshaper + python-bidi，本项目坚持零外部依赖）。
    效果是字母按孤立形呈现、但顺序正确——比整段左右颠倒好读得多。
    数字与拉丁字母不参与反转，避免把「2026」画成「6202」。
    """
    if not any(_is_rtl(ch) for ch in text):
        return text
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            out.extend(reversed(run))
            run.clear()

    for ch in text:
        if _is_rtl(ch) or (_is_rtl_neutral(ch) and run):
            run.append(ch)
        else:
            flush()
            out.append(ch)
    flush()
    return "".join(out)


def _is_rtl_neutral(ch: str) -> bool:
    return ch in _RTL_NEUTRAL


def _glyphs(
    text: str, size: int, bold: bool = False
) -> list[tuple[str, ImageFont.FreeTypeFont]]:
    """拆成 ``(字符, 字体)``，零宽字符丢弃。"""
    return [(ch, _pick_font(ch, size, bold)) for ch in text if ch not in _ZERO_WIDTH]


def _width(glyphs: list[tuple[str, ImageFont.FreeTypeFont]]) -> float:
    return sum(font.getlength(ch) for ch, font in glyphs)


def _ellipsize(
    glyphs: list[tuple[str, ImageFont.FreeTypeFont]], max_width: float, size: int
) -> list[tuple[str, ImageFont.FreeTypeFont]]:
    """超宽就截断并补省略号（昵称/用户名过长时不再溢出到卡片外）。"""
    if _width(glyphs) <= max_width:
        return glyphs
    dots = _glyphs("…", size)
    budget = max_width - _width(dots)
    out: list[tuple[str, ImageFont.FreeTypeFont]] = []
    width = 0.0
    for ch, font in glyphs:
        char_width = font.getlength(ch)
        if width + char_width > budget:
            break
        out.append((ch, font))
        width += char_width
    return out + dots


def _wrap(
    text: str, size: int, bold: bool, max_width: float
) -> list[list[tuple[str, ImageFont.FreeTypeFont]]]:
    """按宽度折行：西文尽量在空格处断，中日韩逐字断。

    空行（``\\n\\n``）会保留成一个空列表——渲染时它同样占一行高度，
    否则「正文里的段落空行」会让文字整块下移、压到配图上。
    """
    lines: list[list[tuple[str, ImageFont.FreeTypeFont]]] = []
    for raw_line in (text or "").split("\n"):
        glyphs = _glyphs(_visual_order(raw_line), size, bold)
        current: list[tuple[str, ImageFont.FreeTypeFont]] = []
        width = 0.0
        for ch, font in glyphs:
            char_width = font.getlength(ch)
            if current and width + char_width > max_width:
                # 断点优先级：空格 > 斜杠/连字符这类「天然可断」的位置；
                # 都没有才逐字硬断（长 URL 不硬断就会溢出卡片）。
                space_cut = -1
                for index in range(len(current) - 1, -1, -1):
                    if current[index][0] == " ":
                        space_cut = index
                        break
                if space_cut > 0:
                    lines.append(current[:space_cut])
                    current = current[space_cut + 1 :]
                else:
                    sym_cut = -1
                    for index in range(len(current) - 1, -1, -1):
                        if current[index][0] in _BREAK_AFTER:
                            sym_cut = index + 1
                            break
                    if sym_cut > 0:
                        lines.append(current[:sym_cut])
                        current = current[sym_cut:]
                    else:
                        lines.append(current)
                        current = []
                width = _width(current)
            current.append((ch, font))
            width += char_width
        lines.append(current)
    return lines


def _draw_glyphs(
    draw: ImageDraw.ImageDraw,
    x: float,
    baseline: float,
    glyphs: list[tuple[str, ImageFont.FreeTypeFont]],
    fill,
) -> float:
    """按基线绘制（``anchor="ls"``）：不同字体的字形都坐在同一条基线上。"""
    for ch, font in glyphs:
        draw.text((x, baseline), ch, font=font, fill=fill, anchor="ls")
        x += font.getlength(ch)
    return x


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius, fill=255)
    return mask


def _circle(img: Image.Image, size: int) -> Image.Image:
    img = img.convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _media_boxes(
    media: list[tuple[Image.Image, bool]], inner: int, left: int = _PAD
) -> list[tuple[Image.Image, tuple[int, int, int, int], bool]]:
    """算每张配图的落位框 ``(图, (left, top, width, height), 是否视频封面)``。

    ``left`` 是配图区的左边界——调用方传**正文左边界**（``text_x``），
    让配图和文字左对齐。

    **竖排成长图，不裁切**：多图不拼成固定格子的网格（那必然要切掉画面），
    而是一张张按原始比例铺满宽度、从上往下摞，整张卡片就是一条长图——
    用户在微信里看到的就是这一条。

    单张过高（竖图/长截图）时按 ``_MAX_MEDIA_HEIGHT`` 限高，并**同步收窄
    宽度、居中**——只截高度不缩宽度会把图横向压扁。
    """
    boxes: list[tuple[Image.Image, tuple[int, int, int, int], bool]] = []
    top = 0
    for img, play in media:
        ratio = max(1, img.width) / max(1, img.height)
        width = inner
        height = int(round(width / ratio))
        x = left
        if height > _MAX_MEDIA_HEIGHT:
            height = _MAX_MEDIA_HEIGHT
            width = max(1, int(round(height * ratio)))
            x = left + (inner - width) // 2
        boxes.append((img, (x, top, width, height), play))
        top += height + _MEDIA_GAP
    return boxes


def _load_image(url: str, timeout: float) -> Image.Image | None:
    try:
        raw = _get(url, timeout)
        img = Image.open(io.BytesIO(raw))
        img.load()
        return img.convert("RGB")
    except Exception:
        return None


def _draw_badge(draw: ImageDraw.ImageDraw, x: float, y: float, size: float, kind: str) -> None:
    """认证徽标：蓝底白勾（政府/企业也统一画蓝，避免配色歧义）。"""
    color = (29, 155, 240) if kind in ("", "blue", "business") else (120, 86, 255)
    draw.ellipse((x, y, x + size, y + size), fill=color)
    cx, cy = x + size / 2, y + size / 2
    draw.line(
        [
            (cx - size * 0.22, cy + size * 0.02),
            (cx - size * 0.06, cy + size * 0.18),
            (cx + size * 0.24, cy - size * 0.18),
        ],
        fill="white",
        width=max(2, int(size * 0.13)),
        joint="curve",
    )


def _draw_play_badge(card: Image.Image, left: int, top: int, width: int, height: int) -> None:
    """在视频封面正中画一个半透明播放角标（卡片本身是 RGB，故叠一层 RGBA 再贴回）。"""
    overlay = Image.new("RGBA", card.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    cx, cy = left + width / 2, top + height / 2
    radius = max(22, min(width, height) * 0.14)
    odraw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius), fill=(0, 0, 0, 140)
    )
    tri = radius * 0.46
    odraw.polygon(
        [
            (cx - tri * 0.55, cy - tri),
            (cx - tri * 0.55, cy + tri),
            (cx + tri * 0.85, cy),
        ],
        fill=(255, 255, 255, 235),
    )
    merged = Image.alpha_composite(card.convert("RGBA"), overlay).convert("RGB")
    card.paste(merged, (0, 0))


_TEXT_COLOR = (15, 20, 25)
_MUTED_COLOR = (83, 100, 113)
_RULE_COLOR = (207, 217, 222)


def render_card(
    tweet: Tweet,
    outdir: str | Path,
    timeout: float = 20.0,
    logger: Callable[[str, str], None] | None = None,
    include_media: bool = True,
) -> Path | None:
    """把推文渲染成 PNG，返回文件路径；失败返回 ``None``。

    排版自上而下依次是：头像/昵称块 → 正文（有译文时用译文）→ 原文块
    （仅在有译文时）→ 配图 → 页脚。每个块的高度都由**字号度量**算出，
    块与块之间留固定间距，因此正文再长也不会压到配图上。

    配图**竖排成一条长图**：几张图各自按原比例铺满宽度、从上往下摞，
    不裁切也不拉伸，整张卡片就是一条长图，一条消息发完。
    ``include_media=False`` 时只画文字（供只需要文字卡的场景）。
    """
    log = logger or (lambda level, message: None)
    outdir = Path(outdir)
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log("warning", f"创建卡片目录失败：{exc}")
        return None

    text_x = _PAD + _AVATAR + 20
    text_width = CARD_WIDTH - _PAD - text_x

    # 头像先取：有没有头像会影响「昵称块」到底占多高（没头像时不该空出 76px）。
    avatar_img = _load_image(tweet.avatar_url, timeout) if tweet.avatar_url else None

    # 正文：有译文就画译文，原文另起一块缩成灰色小字。
    translation = (tweet.translation or "").strip()
    original = (tweet.text or "").strip()
    body_text = translation or original
    body_lines = _wrap(body_text, _BODY_SIZE, False, text_width) if body_text else []
    quote_lines = (
        _wrap(original, _QUOTE_SIZE, False, text_width - 16)
        if translation and original and original != translation
        else []
    )

    # 配图：与正文左对齐、同宽，多图**竖排**摞成一条长图（不裁切）。
    # 取 `name=large` 而不是 syndication 默认的小图，长图放大后才不糊。
    media: list[tuple[Image.Image, bool]] = []
    if include_media:
        for url in tweet.photos[:4]:
            img = _load_image(best_photo_url(url), timeout)
            if img is not None:
                media.append((img, bool(tweet.video_poster) and url == tweet.video_poster))
    media_boxes = _media_boxes(media, text_width, text_x)
    media_height = max(
        (top + height for _, (_, top, _, height), _ in media_boxes), default=0
    )

    name_ascent, name_descent = _metrics(_NAME_SIZE, True)
    handle_ascent, handle_descent = _metrics(_HANDLE_SIZE)
    body_ascent, _ = _metrics(_BODY_SIZE)
    quote_ascent, _ = _metrics(_QUOTE_SIZE)
    foot_ascent, foot_descent = _metrics(_FOOT_SIZE)

    # ---- 竖向排版：先算好每块的顶边，再统一落笔 -------------------------- #
    name_baseline = _PAD + 2 + name_ascent
    handle_baseline = name_baseline + name_descent + 8 + handle_ascent
    # 头像高度只在**真的取到头像**时才算进头部；没有 @用户名 时也不留那一行。
    head_bottom = name_baseline + name_descent
    if tweet.author_handle:
        head_bottom = handle_baseline + handle_descent
    if avatar_img is not None:
        head_bottom = max(head_bottom, _PAD + _AVATAR)

    adv_body = _line_advance(_BODY_SIZE)
    adv_quote = _line_advance(_QUOTE_SIZE)

    y = head_bottom
    if body_lines:
        body_top = y + _HEAD_GAP
        y = body_top + len(body_lines) * adv_body - _LINE_GAP
    else:
        body_top = y

    quote_top = 0
    quote_label_baseline = 0
    if quote_lines:
        label_top = y + _QUOTE_GAP
        quote_label_baseline = label_top + foot_ascent
        quote_top = label_top + _line_advance(_FOOT_SIZE)
        y = quote_top + len(quote_lines) * adv_quote - _LINE_GAP

    media_top = y + (_BLOCK_GAP if media_boxes else 0)
    if media_boxes:
        y = media_top + media_height

    # 页脚：多图走「原图单独发」时在这里标一句「N 张图」，让用户知道后面还有图。
    footer_parts = [tweet.created_at]
    if not include_media:
        count = len(photo_urls(tweet))
        if count:
            footer_parts.append(f"{count} 张图")
    footer_parts.extend(
        [
            f"{tweet.likes} 喜欢" if tweet.likes else "",
            f"{tweet.replies} 回复" if tweet.replies else "",
        ]
    )
    footer = " · ".join(part for part in footer_parts if part)
    foot_baseline = 0
    if footer:
        foot_baseline = y + (_BLOCK_GAP if y > head_bottom else _HEAD_GAP) + foot_ascent
        y = foot_baseline + foot_descent
    height = int(y + _PAD)

    card = Image.new("RGB", (CARD_WIDTH, max(height, 160)), (255, 255, 255))
    draw = ImageDraw.Draw(card)

    # 头像（圆角裁切；同一张圆图既当蒙版又当内容，只算一次）
    if avatar_img is not None:
        circle = _circle(avatar_img, _AVATAR)
        card.paste(circle, (_PAD, _PAD), circle)

    # 昵称 + 认证（昵称留 40px 给认证徽标，超长截断）
    name_glyphs = _ellipsize(
        _glyphs(tweet.author_name or "未知用户", _NAME_SIZE, True),
        text_width - 40,
        _NAME_SIZE,
    )
    name_x = _draw_glyphs(draw, text_x, name_baseline, name_glyphs, _TEXT_COLOR)
    if tweet.verified:
        badge_size = _NAME_SIZE - 7
        _draw_badge(
            draw,
            name_x + 8,
            name_baseline - name_ascent + max(0, (_NAME_SIZE - badge_size) / 2),
            badge_size,
            tweet.verified,
        )

    # @handle
    if tweet.author_handle:
        _draw_glyphs(
            draw,
            text_x,
            handle_baseline,
            _ellipsize(
                _glyphs(f"@{tweet.author_handle}", _HANDLE_SIZE),
                text_width,
                _HANDLE_SIZE,
            ),
            _MUTED_COLOR,
        )

    # 正文
    baseline = body_top + body_ascent
    for line in body_lines:
        if line:
            _draw_glyphs(draw, text_x, baseline, line, _TEXT_COLOR)
        baseline += adv_body

    # 原文块（引用样式：左侧细线 + 灰色小字）
    if quote_lines:
        _draw_glyphs(
            draw, text_x, quote_label_baseline, _glyphs("原文", _FOOT_SIZE), _MUTED_COLOR
        )
        draw.rounded_rectangle(
            (
                text_x,
                quote_top - 6,
                text_x + 3,
                quote_top + len(quote_lines) * adv_quote - _LINE_GAP,
            ),
            radius=1,
            fill=_RULE_COLOR,
        )
        baseline = quote_top + quote_ascent
        for line in quote_lines:
            if line:
                _draw_glyphs(draw, text_x + 16, baseline, line, _MUTED_COLOR)
            baseline += adv_quote

    # 配图（竖排长图：按原比例缩放，不裁切不拉伸；圆角；视频封面加播放角标）
    for img, (left, top, width, height_), play in media_boxes:
        mask = _rounded_mask((width, height_), _MEDIA_RADIUS)
        card.paste(img.resize((width, height_), Image.LANCZOS), (left, int(media_top) + top), mask)
        if play:
            _draw_play_badge(card, left, int(media_top) + top, width, height_)

    # 底部时间 / 互动
    if footer:
        _draw_glyphs(draw, text_x, foot_baseline, _glyphs(footer, _FOOT_SIZE), _MUTED_COLOR)

    path = outdir / f"tweet-{tweet.id}.png"
    try:
        card.save(path, "PNG")
    except OSError as exc:
        log("warning", f"保存推文卡片失败：{exc}")
        return None
    return path
