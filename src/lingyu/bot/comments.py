"""抖音 / TikTok 评论区「前十条热评」截图。

数据从哪来
----------
- **抖音**：评论区接口 ``aweme/v1/web/comment/list`` 同样要求页面签名，借
  浏览器桥在作品页里发一次 XHR 即可（见 :mod:`lingyu.bot.douyin`）。接口返回的
  顺序**不是**按点赞排的，这里统一按 ``digg_count`` 倒序，前 N 条才是「热评」。
- **TikTok**：评论接口对无登录态的请求返回 200 + 空体（风控），改读评论区
  DOM（``data-e2e=comment-level-1``），DOM 本身已按热度排序
  （见 :mod:`lingyu.bot.tiktok`）。

版式
----
按用户给的样式图复刻抖音原生评论区：顶部「评论 2.4万」，下面每条是
「圆形头像 + 灰色昵称 + 大号正文 + 时间·地区·回复 + 右侧心形点赞」，
有回复的再补一行「展开 N 条回复」，最底部是「分享你此刻的想法」输入条。

本地没有头像图时，用「首字 + 柔和底色」的圆形占位；平台给了头像地址
（抖音 ``avatar_thumb`` / TikTok ``comment-thumbnail``）就下载后裁成圆形贴上，
下载失败再退回占位。其余尺寸/配色都以样式图为准。
"""

from __future__ import annotations

import io
import math
import re
import time
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageOps

from . import douyin, tiktok
from .tweet import (
    CARD_WIDTH,
    _MUTED_COLOR,
    _PAD,
    _TEXT_COLOR,
    _draw_glyphs,
    _ellipsize,
    _glyphs,
    _line_advance,
    _metrics,
    _width,
    _wrap,
)

_BG_COLOR = (255, 255, 255)
#: 底部输入条 / 浅灰分隔用的底色（同抖音浅灰）。
_STRIP_COLOR = (246, 246, 248)
#: 昵称灰。
_NAME_COLOR = (124, 126, 132)
#: 时间、地区、点赞数灰。
_META_COLOR = (143, 145, 151)
#: 「回复」比时间略深一点。
_REPLY_COLOR = (96, 98, 104)
#: 头部「评论」旁的红色点缀（抖音主色）。
_ACCENT_COLOR = (254, 44, 85)
_RULE_COLOR = (238, 238, 240)

_HEADER_SIZE = 32
_NAME_SIZE = 21
_BODY_SIZE = 27
_META_SIZE = 19
_HINT_SIZE = 19
_AVATAR_SIZE = 60
_AVATAR_GAP = 16
_ENTRY_GAP = 26
_STRIP_HEIGHT = 64
#: 单条评论最多画这么多字：评论区常见整段复制来的长文，不截会让卡片无限长。
_MAX_COMMENT_CHARS = 300

#: 首字头像的底色（按昵称哈希挑一个，保证同一个人每次颜色一致）。
_AVATAR_COLORS = (
    (255, 190, 160),
    (166, 203, 255),
    (178, 222, 186),
    (255, 199, 205),
    (208, 196, 255),
    (250, 219, 150),
    (170, 220, 226),
    (255, 181, 190),
)


@dataclass
class Comment:
    """一条评论（两个平台共用）。"""

    author: str = ""
    text: str = ""
    likes: int = 0
    #: 相对时间（「7小时前」）或原始日期，平台没给就留空。
    time_text: str = ""
    #: IP 属地（抖音给 ``ip_label``，TikTok 没有）。
    region: str = ""
    #: 回复条数；大于 0 才画「展开 N 条回复」。
    replies: int = 0
    #: 头像地址；取不到或下载失败时退回「首字头像」。
    avatar_url: str = ""


@dataclass
class CommentDigest:
    """一个作品的评论区摘要。"""

    platform: str = ""
    url: str = ""
    title: str = ""
    author: str = ""
    #: 落盘文件名用的稳定标识（抖音 aweme_id / TikTok 视频 ID）。
    card_id: str = ""
    #: 评论区总条数（接口的 ``total``）。
    total: int = 0
    comments: list[Comment] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def supports(url: str) -> bool:
    """该链接是否有评论区可截（目前只有抖音与 TikTok）。"""
    return douyin.is_douyin_url(url) or tiktok.is_tiktok_url(url)


def relative_time(timestamp: int) -> str:
    """把 unix 时间转成「刚刚 / 7小时前 / 3天前 / 5月6日」。"""
    try:
        value = int(timestamp or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    delta = max(0, time.time() - value)
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)}分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)}小时前"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)}天前"
    local = time.localtime(value)
    return f"{local.tm_mon}月{local.tm_mday}日"


def for_url(
    url: str,
    *,
    limit: int = 10,
    logger: Callable[[str, str], None] | None = None,
    timeout: float = 30.0,
    force: bool = False,
) -> CommentDigest:
    """取一个视频的评论区热评；失败原因写在 ``error`` 里。

    ``force`` 只影响抖音的作品详情缓存（直链/评论有变动时强制重取），
    评论缓存由 ``douyin.comments_for`` 自己按 TTL 过期。
    """
    log = logger or (lambda level, message: None)
    count = max(1, min(20, int(limit or 10)))

    if douyin.is_douyin_url(url):
        aweme_id = douyin.extract_aweme_id(url)
        if not aweme_id:
            return CommentDigest(
                platform="抖音", url=url, error="未能从抖音链接中识别作品 ID"
            )
        # 详情与评论是同一次浏览器会话里取的（见 douyin.fetch_video_via_browser），
        # 正常情况下这里两处都是缓存命中，不会再开浏览器。
        video = douyin.video_for(aweme_id, logger=log, timeout=timeout, force=force)
        batch = douyin.comments_for(
            aweme_id, limit=count, logger=log, timeout=timeout
        )
        if not batch.ok:
            return CommentDigest(
                platform="抖音",
                url=url,
                card_id=aweme_id,
                title=video.title,
                author=video.uploader,
                error=batch.error or "抖音评论获取失败",
            )
        return CommentDigest(
            platform="抖音",
            url=url,
            card_id=aweme_id,
            title=video.title,
            author=video.uploader,
            total=batch.total or len(batch.comments),
            comments=batch.comments[:count],
        )

    if tiktok.is_tiktok_url(url):
        batch = tiktok.fetch_comments_via_browser(
            url, limit=count, logger=log, timeout=max(timeout, 45.0)
        )
        if not batch.ok:
            return CommentDigest(
                platform="TikTok",
                url=url,
                card_id=batch.video_id,
                error=batch.error or "TikTok 评论获取失败",
            )
        return CommentDigest(
            platform="TikTok",
            url=url,
            card_id=batch.video_id,
            total=batch.total or len(batch.comments),
            comments=batch.comments[:count],
        )

    return CommentDigest(url=url, error="该链接不支持评论区截图")


# --------------------------------------------------------------------------- #
# 绘制小工具
# --------------------------------------------------------------------------- #


def _fmt_count(value: int) -> str:
    """一万以上折成「2.4万」，和平台自己显示的写法一致。"""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    if number <= 0:
        return "0"
    if number < 10000:
        return str(number)
    text = f"{number / 10000:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return f"{text}万"


def _shorten(text: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")


def _avatar_color(name: str) -> tuple[int, int, int]:
    return _AVATAR_COLORS[sum(ord(char) for char in name or "?") % len(_AVATAR_COLORS)]


def _avatar_initial(name: str) -> str:
    """取首字做头像；昵称以 emoji 开头时跳到第一个可显示字符，避免豆腐块。"""
    for char in name or "":
        if char.isalnum():
            return char.upper()
    return "友"


@lru_cache(maxsize=1)
def _avatar_mask() -> Image.Image:
    """圆形遮罩（先放大 4 倍画再缩回来，边缘才不毛糙）。"""
    scale = 4
    size = _AVATAR_SIZE * scale
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    return mask.resize((_AVATAR_SIZE, _AVATAR_SIZE), Image.LANCZOS)


#: 头像缓存：下载失败也缓存成 ``None``，同一张卡片不会对死链反复重试。
_AVATAR_CACHE: dict[str, Image.Image | None] = {}
_AVATAR_CACHE_LIMIT = 200
_AVATAR_TIMEOUT = 5.0
_AVATAR_MAX_BYTES = 2_000_000
_AVATAR_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "image/*,*/*;q=0.8",
}


def _load_avatar(url: str) -> Image.Image | None:
    """下载头像并裁成正方形；任何失败都返回 ``None``（退回首字头像）。"""
    if not url:
        return None
    if url in _AVATAR_CACHE:
        return _AVATAR_CACHE[url]
    image: Image.Image | None = None
    try:
        request = urllib.request.Request(url, headers=_AVATAR_HEADERS)
        with urllib.request.urlopen(request, timeout=_AVATAR_TIMEOUT) as response:
            data = response.read(_AVATAR_MAX_BYTES)
        with Image.open(io.BytesIO(data)) as raw:
            image = ImageOps.fit(
                raw.convert("RGB"), (_AVATAR_SIZE, _AVATAR_SIZE), Image.LANCZOS
            )
    except Exception:
        image = None
    if len(_AVATAR_CACHE) >= _AVATAR_CACHE_LIMIT:
        _AVATAR_CACHE.clear()
    _AVATAR_CACHE[url] = image
    return image


def _draw_avatar(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    left: int,
    top: int,
    name: str,
    avatar: Image.Image | None = None,
) -> None:
    """圆形头像：有图贴图，没有就用底色 + 首字占位。"""
    if avatar is not None:
        canvas.paste(avatar, (left, top), _avatar_mask())
        return
    right = left + _AVATAR_SIZE
    bottom = top + _AVATAR_SIZE
    draw.ellipse((left, top, right, bottom), fill=_avatar_color(name))
    glyphs = _glyphs(_avatar_initial(name), 26, True)
    ascent, descent = _metrics(26, True)
    baseline = top + (_AVATAR_SIZE + ascent - descent) / 2
    _draw_glyphs(
        draw,
        left + (_AVATAR_SIZE - _width(glyphs)) / 2,
        baseline,
        glyphs,
        (255, 255, 255),
    )


def _draw_heart(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    size: float,
    color: tuple[int, int, int],
) -> None:
    """空心心形（点赞图标）。没有可靠的字形，直接按参数方程画。"""
    points: list[tuple[float, float]] = []
    for step in range(0, 361, 8):
        angle = math.radians(step)
        x = 16 * math.sin(angle) ** 3
        y = (
            13 * math.cos(angle)
            - 5 * math.cos(2 * angle)
            - 2 * math.cos(3 * angle)
            - math.cos(4 * angle)
        )
        points.append((cx + x * size / 34, cy - y * size / 34))
    draw.line(points, fill=color, width=2, joint="curve")


def _draw_chevron(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    size: float,
    color: tuple[int, int, int],
    up: bool = True,
) -> None:
    half = size / 2
    direction = -1 if up else 1
    draw.line(
        (
            (cx - half, cy + direction * half / 3),
            (cx, cy - direction * half / 2),
            (cx + half, cy + direction * half / 3),
        ),
        fill=color,
        width=2,
        joint="curve",
    )


def _draw_image_icon(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    size: float,
    color: tuple[int, int, int],
) -> None:
    half = size / 2
    draw.rounded_rectangle(
        (cx - half, cy - half, cx + half, cy + half),
        radius=5,
        outline=color,
        width=2,
    )
    draw.ellipse(
        (cx - half * 0.55, cy - half * 0.6, cx - half * 0.05, cy - half * 0.1),
        outline=color,
        width=2,
    )
    draw.line(
        (
            (cx - half * 0.85, cy + half * 0.6),
            (cx - half * 0.1, cy - half * 0.15),
            (cx + half * 0.3, cy + half * 0.25),
            (cx + half * 0.55, cy),
            (cx + half * 0.85, cy + half * 0.6),
        ),
        fill=color,
        width=2,
        joint="curve",
    )


def _draw_smiley(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    size: float,
    color: tuple[int, int, int],
) -> None:
    half = size / 2
    draw.ellipse((cx - half, cy - half, cx + half, cy + half), outline=color, width=2)
    dot = max(1.6, size * 0.07)
    draw.ellipse((cx - half * 0.5 - dot, cy - half * 0.25 - dot,
                  cx - half * 0.5 + dot, cy - half * 0.25 + dot), fill=color)
    draw.ellipse((cx + half * 0.5 - dot, cy - half * 0.25 - dot,
                  cx + half * 0.5 + dot, cy - half * 0.25 + dot), fill=color)
    draw.arc(
        (cx - half * 0.55, cy - half * 0.2, cx + half * 0.55, cy + half * 0.6),
        start=20,
        end=160,
        fill=color,
        width=2,
    )


def _draw_bottom_bar(draw: ImageDraw.ImageDraw, top: int) -> None:
    """底部「分享你此刻的想法」输入条：浅灰底 + 白色圆角输入框 + 三个图标。"""
    bottom = top + _STRIP_HEIGHT
    draw.rectangle((0, top, CARD_WIDTH, bottom), fill=_STRIP_COLOR)

    pill_left = _PAD
    pill_right = CARD_WIDTH - _PAD - 132
    pill_top = top + 14
    pill_bottom = pill_top + 36
    draw.rounded_rectangle(
        (pill_left, pill_top, pill_right, pill_bottom),
        radius=18,
        fill=(255, 255, 255),
    )
    ascent, descent = _metrics(_META_SIZE)
    baseline = pill_top + (36 + ascent - descent) / 2
    _draw_glyphs(
        draw,
        pill_left + 16,
        baseline,
        _glyphs("分享你此刻的想法", _META_SIZE),
        _META_COLOR,
    )

    icon_y = top + _STRIP_HEIGHT / 2
    icon_size = 24
    x = pill_right + 22
    _draw_image_icon(draw, x, icon_y, icon_size, _MUTED_COLOR)
    x += 46
    at_glyphs = _glyphs("@", 26)
    at_ascent, at_descent = _metrics(26)
    _draw_glyphs(
        draw, x - _width(at_glyphs) / 2, icon_y + (at_ascent - at_descent) / 2,
        at_glyphs, _MUTED_COLOR,
    )
    x += 46
    _draw_smiley(draw, x, icon_y, icon_size, _MUTED_COLOR)


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


def render_card(
    digest: CommentDigest,
    outdir: str | Path,
    timeout: float = 20.0,
    logger: Callable[[str, str], None] | None = None,
) -> Path | None:
    """把评论区摘要渲染成 PNG，返回文件路径；失败返回 ``None``。

    ``timeout`` 与推文卡片保持同一签名；头像下载用 :data:`_AVATAR_TIMEOUT`，
    失败就退回首字头像，不会让整张卡片失败。
    """
    log = logger or (lambda level, message: None)
    outdir = Path(outdir)
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log("warning", f"创建卡片目录失败：{exc}")
        return None

    content_x = _PAD + _AVATAR_SIZE + _AVATAR_GAP
    content_width = CARD_WIDTH - _PAD - content_x

    head_ascent, head_descent = _metrics(_HEADER_SIZE, True)
    name_ascent, _ = _metrics(_NAME_SIZE)
    body_ascent, _ = _metrics(_BODY_SIZE)
    meta_ascent, meta_descent = _metrics(_META_SIZE)
    hint_ascent, _ = _metrics(_HINT_SIZE)

    name_adv = _line_advance(_NAME_SIZE)
    body_adv = _line_advance(_BODY_SIZE)
    hint_adv = _line_advance(_HINT_SIZE)

    # ---- 竖向排版：先把每块的高度算清楚，再统一落笔 -------------------- #
    header_baseline = _PAD + head_ascent
    divider_y = header_baseline + head_descent + 16
    y = divider_y + 22

    entries: list[dict] = []
    for comment in digest.comments:
        lines = _wrap(
            _shorten(comment.text, _MAX_COMMENT_CHARS),
            _BODY_SIZE,
            False,
            content_width,
        )
        avatar_top = y
        name_baseline = y + name_ascent
        body_top = y + name_adv + 2
        body_bottom = body_top + len(lines) * body_adv
        meta_baseline = body_bottom + 14 + meta_ascent
        meta_bottom = meta_baseline + meta_descent
        hint_baseline = 0.0
        hint_bottom = meta_bottom
        if comment.replies > 0:
            hint_baseline = meta_bottom + 10 + hint_ascent
            hint_bottom = meta_bottom + 10 + hint_adv
        bottom = max(avatar_top + _AVATAR_SIZE, hint_bottom)
        entries.append(
            {
                "comment": comment,
                "lines": lines,
                "avatar_top": avatar_top,
                "name_baseline": name_baseline,
                "body_top": body_top,
                "meta_baseline": meta_baseline,
                "hint_baseline": hint_baseline,
            }
        )
        y = bottom + _ENTRY_GAP

    strip_top = y + 2
    height = int(strip_top + _STRIP_HEIGHT)

    canvas = Image.new("RGB", (CARD_WIDTH, max(height, 260)), _BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    # ---- 头部：评论 2.4万 ……………………… 前 10 条热评 -------------------- #
    label_glyphs = _glyphs("评论", _HEADER_SIZE, True)
    _draw_glyphs(draw, _PAD, header_baseline, label_glyphs, _TEXT_COLOR)
    total_text = _fmt_count(digest.total or len(digest.comments))
    _draw_glyphs(
        draw,
        _PAD + _width(label_glyphs) + 10,
        header_baseline,
        _glyphs(total_text, _HEADER_SIZE, True),
        _TEXT_COLOR,
    )
    hint = f"前 {len(digest.comments)} 条热评"
    hint_glyphs = _glyphs(hint, _META_SIZE)
    _draw_glyphs(
        draw,
        CARD_WIDTH - _PAD - _width(hint_glyphs),
        header_baseline - (head_ascent - meta_ascent),
        hint_glyphs,
        _META_COLOR,
    )
    draw.line((_PAD, divider_y, CARD_WIDTH - _PAD, divider_y), fill=_RULE_COLOR, width=1)

    # ---- 每条热评 ------------------------------------------------------- #
    for entry in entries:
        comment: Comment = entry["comment"]
        _draw_avatar(
            canvas,
            draw,
            _PAD,
            entry["avatar_top"],
            comment.author,
            _load_avatar(comment.avatar_url),
        )
        _draw_glyphs(
            draw,
            content_x,
            entry["name_baseline"],
            _ellipsize(
                _glyphs(comment.author or "网友", _NAME_SIZE),
                content_width,
                _NAME_SIZE,
            ),
            _NAME_COLOR,
        )

        baseline = entry["body_top"] + body_ascent
        for line in entry["lines"]:
            if line:
                _draw_glyphs(draw, content_x, baseline, line, _TEXT_COLOR)
            baseline += body_adv

        # 时间 · 地区   回复 …………………… ♡ 2024
        meta_baseline = entry["meta_baseline"]
        meta_text = " · ".join(
            part for part in (comment.time_text, comment.region) if part
        )
        x = content_x
        if meta_text:
            meta_glyphs = _glyphs(meta_text, _META_SIZE)
            _draw_glyphs(draw, x, meta_baseline, meta_glyphs, _META_COLOR)
            x += _width(meta_glyphs) + 18
        _draw_glyphs(
            draw,
            x,
            meta_baseline,
            _glyphs("回复", _META_SIZE),
            _REPLY_COLOR,
        )

        like_glyphs = _glyphs(_fmt_count(comment.likes), _META_SIZE)
        like_x = CARD_WIDTH - _PAD - _width(like_glyphs)
        _draw_glyphs(draw, like_x, meta_baseline, like_glyphs, _META_COLOR)
        _draw_heart(
            draw,
            like_x - 16,
            meta_baseline - meta_ascent * 0.45,
            20,
            _META_COLOR,
        )

        if comment.replies > 0:
            hint_baseline = entry["hint_baseline"]
            _draw_chevron(
                draw,
                content_x + 8,
                hint_baseline - meta_ascent * 0.45,
                10,
                _META_COLOR,
            )
            _draw_glyphs(
                draw,
                content_x + 24,
                hint_baseline,
                _glyphs(f"展开 {_fmt_count(comment.replies)} 条回复", _HINT_SIZE),
                _MUTED_COLOR,
            )

    _draw_bottom_bar(draw, strip_top)

    path = outdir / f"comments-{digest.card_id or 'card'}.png"
    try:
        canvas.save(path, "PNG")
    except OSError as exc:
        log("warning", f"保存评论区卡片失败：{exc}")
        return None
    return path
