"""TikTok 评论区抓取：借本机浏览器读评论侧栏 DOM。

为什么不走接口
--------------
``/api/comment/list/`` 对**没有登录态**的无头浏览器返回 ``200`` 但**响应体为空**
（实测），拿它当数据源只能得到 0 条。评论侧栏本身会正常渲染，且顺序就是平台
的热度顺序，故改为「点开评论图标 → 读 DOM」。

DOM 结构（2026-11 实测）
------------------------
``[data-e2e="comment-level-1"]`` 是**正文**那个 ``<span>``，不是整条评论。往上
一层是 ``DivCommentContentWrapper``（含 ``comment-username-1`` / 正文 / 时间 /
「回复」/ 点赞数），再往上一层才是整条评论（含「查看 N 条回复」）。点赞数与
时间都没有 ``data-e2e``，只能从容器 ``innerText`` 的行里读：

::

    😇
    球が多くて分からない
    4 天前
    回复
    9086
    查看 12 条回复

即「时间行之后：先遇到的是点赞数，带『查看…条回复』的是回复数」。

另一个坑：``--headless=new`` 的 UA 里带 ``HeadlessChrome``，TikTok 会让评论
侧栏**渲染成空白**（接口也是 200 空体），必须伪装成桌面 Chrome 的 UA。

模块只负责「拿数据」，截图见 :mod:`lingyu.bot.comments`。
"""

from __future__ import annotations

import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # 只用于类型标注，避免与 comments.py 循环导入
    from .comments import Comment

_TIKTOK_HOSTS = {"tiktok.com"}
_VIDEO_ID_RE = re.compile(r"/@[^/?#]+/(?:video|photo)/(\d{6,})", re.IGNORECASE)
_USER_RE = re.compile(r"/@([^/?#]+)/", re.IGNORECASE)

#: 一级评论的正文 ``<span>``（实测稳定的 data-e2e 属性）。
_BODY_SELECTOR = '[data-e2e="comment-level-1"]'
#: 打开评论侧栏的按钮。
_COMMENT_ICON_SELECTOR = '[data-e2e="comment-icon"]'
#: 侧栏是否已经打开：不能只看正文数量——刚点开时列表还在加载，正文还是 0。
_PANEL_OPEN_JS = (
    "!!document.querySelector('[data-e2e=\"comment-username-1\"],"
    " [data-e2e=\"comment-thumbnail\"],"
    " [class*=\"DivCommentListContainer\"]')"
)
_BODY_COUNT_JS = f"document.querySelectorAll('{_BODY_SELECTOR}').length"
_ICON_RECT_JS = (
    f"(() => {{ const el = document.querySelector('{_COMMENT_ICON_SELECTOR}');"
    " if (!el) return null; const r = el.getBoundingClientRect();"
    " return {x: r.left + r.width / 2, y: r.top + r.height / 2}; })()"
)

_EXTRACT_JS = """(() => {
  const bodies = Array.from(document.querySelectorAll('[data-e2e="comment-level-1"]'));
  const lines = (node) => (node ? (node.innerText || '').split('\\n')
    .map((s) => s.trim()).filter(Boolean) : []);
  return bodies.slice(0, 40).map((body) => {
    let wrap = body.parentElement;
    while (wrap && !wrap.querySelector('[data-e2e^="comment-username"]')) {
      wrap = wrap.parentElement;
    }
    const item = wrap ? wrap.parentElement : body;
    const block = (item && item.parentElement) || item || body;
    const user = wrap
      ? lines(wrap.querySelector('[data-e2e^="comment-username"]')).join(' ')
      : '';
    const holder = (wrap && wrap.parentElement) || body;
    const thumb = holder.querySelector('[data-e2e="comment-thumbnail"] img')
      || holder.querySelector('img');
    const rows = lines(block);
    return {
      user: user,
      text: lines(body).join(' '),
      avatar: thumb ? (thumb.currentSrc || thumb.src || '') : '',
      // 结构变了导致爬到了列表容器时，行数会暴增，退回单条评论那一层。
      rows: rows.length > 40 ? lines(item) : rows,
    };
  });
})()"""

#: 视频页评论按钮旁边的总数（``212`` / ``1.2K``），用于卡片头部的「评论 212」。
_TOTAL_JS = (
    "(() => { const el = document.querySelector('[data-e2e=\"comment-count\"]');"
    " return el ? (el.innerText || '').trim() : ''; })()"
)

#: 伪装成桌面 Chrome：默认 headless UA 会被 TikTok 判成机器人，评论区留白。
_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_LIKE_UNITS = {
    "": 1,
    "w": 10_000,
    "万": 10_000,
    "萬": 10_000,
    "k": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
}

#: 「刚刚 / 4 天前 / 23 分钟前 / 4d ago / 2024-5-5」这些时间行。
_TIME_RE = re.compile(
    r"^(?:\d+\s*(?:秒|分钟|小时|天|周|个月|年)前"
    r"|\d+\s*(?:s|m|h|d|w|mo|y)\s+ago"
    r"|刚刚|Just now|昨天|Yesterday"
    r"|\d{4}-\d{1,2}-\d{1,2})$",
    re.IGNORECASE,
)
#: 点赞数那一行（可能只有 ``-``，表示 0 赞）。
_LIKE_RE = re.compile(r"^[\d.,]+\s*[万萬wWkKmMbB]?$")
#: 「查看 12 条回复 / View 12 replies」。
_REPLIES_RE = re.compile(
    r"(?:查看|View|See)\s*(\d+)\s*(?:条)?\s*(?:回复|repl)", re.IGNORECASE
)


@dataclass
class TikTokComments:
    """一次评论抓取的结果。"""

    video_id: str = ""
    comments: list["Comment"] = field(default_factory=list)
    #: 评论区总条数（页面上「212 条评论」那个数）。
    total: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def is_tiktok_url(url: str) -> bool:
    """是否 TikTok 地址（``vm.tiktok.com`` 短链需先展开）。"""
    try:
        host = urllib.parse.urlsplit(url or "").netloc.lower().split(":", 1)[0]
    except ValueError:
        return False
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    return any(host == item or host.endswith("." + item) for item in _TIKTOK_HOSTS)


def extract_video_id(url: str) -> str:
    """从 ``/@user/video/123`` 里取视频 ID；取不到返回空串。"""
    match = _VIDEO_ID_RE.search(url or "")
    return match.group(1) if match else ""


def extract_username(url: str) -> str:
    match = _USER_RE.search(url or "")
    return match.group(1) if match else ""


def parse_likes(text: str) -> int:
    """把 ``8989`` / ``1.2K`` / ``1.2万`` 这类点赞文案转成整数。"""
    raw = str(text or "").strip().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([万萬wWkKmMbB]?)", raw)
    if not match:
        return 0
    try:
        value = float(match.group(1))
    except ValueError:
        return 0
    unit = match.group(2).lower()
    return int(value * _LIKE_UNITS.get(unit, 1))


def _parse_rows(rows: Any) -> tuple[str, int, int]:
    """从一条评论的行里取「时间 / 点赞数 / 回复数」。

    时间行是分界线：它前面是昵称与正文，后面依次是「回复」按钮、点赞数；
    有二级回复时再跟一行「查看 N 条回复」。
    """
    items = [str(row).strip() for row in (rows or []) if str(row).strip()]
    time_text = ""
    start = -1
    for index, row in enumerate(items):
        if _TIME_RE.match(row):
            time_text = row
            start = index
            break
    rest = items[start + 1 :] if start >= 0 else []
    likes = 0
    replies = 0
    for row in rest:
        match = _REPLIES_RE.search(row)
        if match:
            replies = int(match.group(1))
            break
    for row in rest:
        if _REPLIES_RE.search(row):
            continue
        if _LIKE_RE.match(row):
            likes = parse_likes(row)
            break
    return time_text, likes, replies


#: 昵称里可能出现不可见字符（实测有纯「￼ ￼」的昵称），画到卡片上就是一个豆腐块。
_BLANK_CHARS = {"\ufffc", "\u2800"}


def _clean_name(name: str) -> str:
    """去掉不可见字符；剩下的若没有可读字符（纯 emoji / 纯空白）就按匿名处理。"""
    text = "".join(
        char
        for char in (name or "")
        if char not in _BLANK_CHARS and unicodedata.category(char)[0] != "C"
    )
    text = " ".join(text.split())
    return text if any(char.isalnum() for char in text) else ""


def _to_comment(item: Any) -> "Comment | None":
    from .comments import Comment

    if not isinstance(item, dict):
        return None
    time_text, likes, replies = _parse_rows(item.get("rows"))
    author = _clean_name(str(item.get("user") or "").lstrip("@"))
    text = " ".join(str(item.get("text") or "").split())
    if not text:
        return None
    return Comment(
        author=author or "TikTok 用户",
        text=text,
        likes=likes,
        time_text=time_text,
        replies=replies,
        avatar_url=str(item.get("avatar") or "").strip(),
    )


def _click_at(page: Any, x: float, y: float) -> None:
    """按坐标发一次**可信**鼠标点击（``el.click()`` 打不开侧栏）。"""
    base = {"x": float(x), "y": float(y), "button": "left", "clickCount": 1}
    page._call("Input.dispatchMouseEvent", {**base, "type": "mouseMoved"})
    time.sleep(0.2)
    page._call("Input.dispatchMouseEvent", {**base, "type": "mousePressed"})
    page._call("Input.dispatchMouseEvent", {**base, "type": "mouseReleased"})


def _open_comment_panel(page: Any, attempts: int = 3) -> bool:
    """点开评论侧栏。

    无头模式下**第一次点击经常被吞掉**（页面还没拿到焦点），实测要点第二次
    才开；而重复点击又会把它关回去。所以按「点一次 → 看侧栏有没有出现」的
    节奏来，已经开着就不再点。
    """
    try:
        if page.evaluate(_PANEL_OPEN_JS):
            return True
    except Exception:
        return False
    rect = page.evaluate(_ICON_RECT_JS)
    if not rect:
        return False
    for _ in range(max(1, attempts)):
        _click_at(page, rect["x"], rect["y"])
        deadline = time.time() + 6.0
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                if page.evaluate(_PANEL_OPEN_JS):
                    return True
            except Exception:
                return False
    return False


def fetch_comments_via_browser(
    url: str,
    *,
    limit: int = 20,
    logger: Callable[[str, str], None] | None = None,
    timeout: float = 45.0,
    wait_seconds: float = 8.0,
) -> TikTokComments:
    """打开视频页、点开评论区、读 DOM；返回按点赞倒序的前 ``limit`` 条。

    页面加载与评论渲染都要等（TikTok 是重度 SPA）：先用 ``wait_for`` 等关键
    元素出现，再给 ``wait_seconds`` 秒让评论列表补数据——这两个等待是实测
    能稳定拿到 10 条评论的最小代价。
    """
    log = logger or (lambda level, message: None)
    video_id = extract_video_id(url)
    if not video_id:
        return TikTokComments(
            error="未能从 TikTok 链接中识别视频 ID（短链请先展开）"
        )

    from . import browser  # 延迟导入：没装浏览器时不必付这个开销

    if not browser.browser_available():
        return TikTokComments(
            video_id=video_id,
            error="未找到 Chrome/Edge，无法读取 TikTok 评论",
        )

    username = extract_username(url)
    target = url
    if username and "tiktok.com" in (url or "") and not url.startswith("https://www."):
        target = f"https://www.tiktok.com/@{username}/video/{video_id}"

    reason = ""
    page = browser.BrowserSession(
        logger=log,
        timeout=timeout,
        extra_args=[f"--user-agent={_DESKTOP_UA}"],
    )
    try:
        page.start()
        page.navigate(target)
        page.wait_for(
            "location.host.indexOf('tiktok.com') >= 0", timeout=min(timeout, 25.0)
        )
        if not page.wait_for(
            f"!!document.querySelector('{_COMMENT_ICON_SELECTOR}')", timeout=20.0
        ):
            reason = "页面没有加载出评论按钮（可能被风控或地区限制）"
        else:
            if not _open_comment_panel(page):
                reason = "点击评论按钮后侧栏没有打开"
            elif not page.wait_for(f"{_BODY_COUNT_JS} > 0", timeout=20.0):
                reason = "评论侧栏没有渲染出内容"
            else:
                time.sleep(max(0.0, wait_seconds))
                # 评论区是滚动的：等一会儿让前几条先画出来，再一次性读。
                items = page.evaluate(_EXTRACT_JS) or []
                comments = [
                    comment for comment in (_to_comment(item) for item in items) if comment
                ]
                comments.sort(key=lambda item: item.likes, reverse=True)
                if comments:
                    return TikTokComments(
                        video_id=video_id,
                        comments=comments[: max(1, int(limit or 20))],
                        total=parse_likes(str(page.evaluate(_TOTAL_JS) or "")),
                    )
                reason = "评论区没有取到文字内容"
    except Exception as exc:
        reason = f"浏览器读取 TikTok 评论失败：{exc}"
    finally:
        page.close(blocking=False)
    return TikTokComments(video_id=video_id, error=reason or "TikTok 评论获取失败")
