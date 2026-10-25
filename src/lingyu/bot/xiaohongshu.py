"""小红书笔记解析：短链展开 + 页面内嵌 ``__INITIAL_STATE__`` + 浏览器兜底。

数据从哪来
----------
小红书笔记页是 **SSR** 的：首次 HTML 里就带一份 ``window.__INITIAL_STATE__``，
所以**不需要签名、也不需要调接口**，一次普通 GET 就能拿到（小红书没有给 Web
接口配抖音那种 ``a_bogus`` 级的强制签名）。

**移动端 UA 才是免登录的那把钥匙**（2026-10-24 实测）：同一条笔记链接，

- 桌面 UA → 302 到 ``/login``（真地址被塞进 ``redirectPath``）
- iPhone / Android UA → 直接返回完整页面

两端的页面结构还不一样，都要认：

- 桌面版：``note.noteDetailMap[<noteId>].note``，图片取 ``WB_DFT``
- 移动 H5：``noteData.data.noteData``，图片取 ``H5_DTL``（1080 宽）

所以 HTTP 路线固定用移动 UA，见 :func:`fetch_note`；:func:`note_from_state`
两套结构都解析。

必须带上的东西
--------------
**``xsec_token``**：从 App/网页「分享」出来的链接里带（``?xsec_token=…``）。
手工拼 ``explore/<id>`` 而不带 token 时，小红书会 302 到
``/404?error_code=300031&error_msg=当前笔记暂时无法浏览``——**这不是「笔记不存在」**，
只是没有访问凭证，错误信息要如实区分。短链展开时**必须保留 query**，
token 就在里面。

Cookie 只在少数「要求登录态」的笔记上才需要，复用配置里的 ``link_cookies_file``
（Netscape 格式），只挑 ``.xiaohongshu.com`` 域下的键。

浏览器兜底
----------
HTML 里拿不到数据（被重定向到 /404、/login，或状态里是空壳）时，用浏览器桥
（:mod:`lingyu.bot.browser`）再打开一次页面。注意 ``--headless=new`` 的 UA 里
带着 ``HeadlessChrome``，小红书一眼识破并甩「IP存在风险」的风控页——所以这里
必须换正常 UA 并关掉 ``AutomationControlled``。仍然不行时，把 ``/404`` 地址里的
``error_msg`` 原样带回来，而不是笼统报「解析失败」。

分享文案里的裸短链
------------------
小红书 App 复制出来的分享文案常常**不带 ``http://``**（``xhslink.com/a/xxx``、
``xhslink.cn/o/xxx``），故 ``links.extract_urls`` 额外认几个已知短链主机的裸域名
（见 ``_BARE_HOSTS``）。
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# **移动端 UA 才是免登录的那把钥匙**（2026-10-24 实测）：同一个笔记链接，
# 桌面 UA 一律 302 到 ``/login``；iPhone/Android UA 直接返回完整页面，
# 数据在 ``__INITIAL_STATE__.noteData.data.noteData`` 里（结构见
# :func:`note_from_state`）。所以 HTTP 路线固定用移动 UA。
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# --------------------------------------------------------------------------- #
# URL
# --------------------------------------------------------------------------- #

# 短链有 .com 和 .cn 两种（实测 App 分享出来的是 ``xhslink.cn``）。
_XHS_HOSTS = ("xiaohongshu.com", "xhslink.com", "xhslink.cn")
# 笔记 ID 是 24 位十六进制（历史上有 16 位的老笔记）。三种路径都见过：
#   /explore/<id>
#   /discovery/item/<id>
#   /user/profile/<user_id>/<id>
_NOTE_PATH_RE = re.compile(
    r"/(?:explore|discovery/item)/(?P<id>[0-9a-zA-Z]{16,32})"
    r"|/user/profile/[0-9a-zA-Z]+/(?P<id2>[0-9a-zA-Z]{16,32})"
)
_QUERY_NOTE_RE = re.compile(r"[?&]noteId=([0-9a-zA-Z]{16,32})")
# /login?redirectPath=<urlencoded 原地址>、/404?...&redirectPath=...
_REDIRECT_RE = re.compile(r"[?&]redirectPath=([^&]+)")
# /404?...&error_code=300031&error_msg=当前笔记暂时无法浏览
_ERROR_MSG_RE = re.compile(r"[?&]error_msg=([^&]+)")


def is_xhs_url(url: str) -> bool:
    """是否小红书链接（含 ``xhslink.com`` / ``xhslink.cn`` 短链）。"""
    try:
        host = urllib.parse.urlsplit((url or "").strip()).netloc.lower()
    except ValueError:
        return False
    if "@" in host:  # 去掉 userinfo
        host = host.rsplit("@", 1)[-1]
    host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return any(host == item or host.endswith("." + item) for item in _XHS_HOSTS)


def extract_note_id(url: str) -> str:
    """从链接里取笔记 ID；不是笔记链接（或只是短链）时返回空串。"""
    text = (url or "").strip()
    if not text:
        return ""
    match = _NOTE_PATH_RE.search(urllib.parse.urlsplit(text).path or "")
    if match:
        return match.group("id") or match.group("id2") or ""
    match = _QUERY_NOTE_RE.search(text)
    return match.group(1) if match else ""


def _error_from_url(url: str) -> str:
    """从 ``/404`` 跳转地址里取小红书自己的错误说明。"""
    match = _ERROR_MSG_RE.search(url or "")
    if not match:
        return ""
    return urllib.parse.unquote(match.group(1)).strip()


def unwrap_redirect(url: str) -> str:
    """把 ``/login?redirectPath=<urlencoded 原地址>`` 还原成原地址。

    桌面 UA 打开分享短链时，小红书不直接跳笔记页，而是跳到登录页，把真正的
    笔记地址（连同 ``xsec_token``）塞进 ``redirectPath`` 参数里。短链展开拿到
    的若就是这个登录页，得先把原地址解出来，否则连笔记 ID 都提取不到。
    """
    match = _REDIRECT_RE.search(url or "")
    if not match:
        return url
    target = urllib.parse.unquote(match.group(1)).strip()
    if target.lower().startswith("http") and is_xhs_url(target):
        return target
    return url


def user_agent() -> str:
    """小红书请求该用的 UA（移动端；桌面端会被要求登录）。"""
    return _MOBILE_UA


# --------------------------------------------------------------------------- #
# 结果模型
# --------------------------------------------------------------------------- #


@dataclass
class XhsNote:
    """一条小红书笔记的解析结果。"""

    note_id: str = ""
    ok: bool = False
    title: str = ""
    uploader: str = ""
    # 作者头像（卡片头部用；取不到就只画昵称，头部自动收窄）。
    avatar: str = ""
    description: str = ""
    images: list[str] = field(default_factory=list)
    video_url: str = ""
    likes: int = 0
    comments: int = 0
    kind: str = ""  # "video" / "normal"
    error: str = ""

    @property
    def is_video(self) -> bool:
        return bool(self.video_url)

    @property
    def is_images(self) -> bool:
        return bool(self.images) and not self.video_url

    @property
    def kind_label(self) -> str:
        if self.is_video:
            return "视频"
        if self.images:
            return f"图文（{len(self.images)} 张）"
        return "笔记"


def _pick(mapping: Any, *keys: str) -> Any:
    """按顺序取第一个非空字段（小红书同一份数据有 camelCase / snake_case 两套）。"""
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def parse_likes(raw: Any) -> int:
    """``"1.2万"`` / ``"3.4亿"`` / ``1234`` → 整数。"""
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw or "").strip()
    if not text:
        return 0
    multiplier = 1
    if text.endswith("万"):
        multiplier, text = 10_000, text[:-1]
    elif text.endswith("亿"):
        multiplier, text = 100_000_000, text[:-1]
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return 0
    try:
        return int(float(match.group(0)) * multiplier)
    except ValueError:  # pragma: no cover - 正则已经保证是数字
        return 0


def _image_urls(note: dict[str, Any]) -> list[str]:
    """笔记配图（去重保序）。

    同一张图在不同版本/端上的字段不一样：

    - 桌面状态：``infoList[imageScene=WB_DFT]`` / ``urlDefault`` / ``urlPre``
    - 移动 H5：``infoList[imageScene=H5_DTL]``（1080 宽）/ ``H5_PRV``，外加顶层 ``url``

    按「越清晰越优先」的顺序挑，都取不到才退回顶层 ``url``。
    """
    urls: list[str] = []
    entries = _pick(note, "imageList", "image_list") or []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = ""
        for scene in ("H5_DTL", "WB_DFT", "H5_PRV", "WB_PRV"):
            for info in _pick(entry, "infoList", "info_list") or []:
                if not isinstance(info, dict):
                    continue
                if str(info.get("imageScene") or info.get("image_scene")) == scene:
                    url = str(info.get("url") or "")
                    break
            if url:
                break
        if not url:
            url = str(
                _pick(
                    entry,
                    "url",
                    "urlDefault",
                    "url_default",
                    "urlPre",
                    "url_pre",
                )
                or ""
            )
        if url and url not in urls:
            urls.append(url)
    return urls


def _video_url(note: dict[str, Any]) -> str:
    """笔记视频的直链（优先 h264 masterUrl）。"""
    video = _pick(note, "video") or {}
    media = _pick(video, "media") or {}
    stream = _pick(media, "stream") or {}
    for codec in ("h264", "h265", "av1"):
        items = stream.get(codec) or []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(_pick(item, "masterUrl", "master_url") or "")
            if url:
                return url
            backups = _pick(item, "backupUrls", "backup_urls") or []
            for candidate in backups:
                if candidate:
                    return str(candidate)
    return ""


def parse_note(note: dict[str, Any], note_id: str = "") -> XhsNote:
    """把 ``noteDetailMap[...].note``（或接口里的 ``note_card``）转成 :class:`XhsNote`。"""
    if not isinstance(note, dict) or not note:
        return XhsNote(note_id=note_id, error="笔记数据为空")
    user = _pick(note, "user") or {}
    interact = _pick(note, "interactInfo", "interact_info") or {}
    title = str(_pick(note, "title") or "").strip()
    desc = str(_pick(note, "desc", "description") or "").strip()
    kind = str(_pick(note, "type") or "").strip().lower()
    video_url = _video_url(note)
    images = _image_urls(note)
    if not kind:
        kind = "video" if video_url else "normal"
    return XhsNote(
        note_id=str(_pick(note, "noteId", "note_id", "id") or note_id),
        ok=True,
        title=title or desc.split("\n", 1)[0][:60],
        uploader=str(
            _pick(user, "nickname", "nickName", "nick_name", "name") or ""
        ).strip(),
        avatar=str(
            _pick(user, "avatar", "avatarUrl", "avatar_url", "image") or ""
        ).strip(),
        description=desc,
        images=images,
        video_url=video_url,
        likes=parse_likes(_pick(interact, "likedCount", "liked_count")),
        comments=parse_likes(
            _pick(interact, "commentCount", "comment_count")
        ),
        kind=kind,
    )


_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*")
# 状态是 JS 字面量、不是严格 JSON：里面会出现 ``undefined``。
# 只替换「值位置」上的 undefined（``:``/``,``/``[`` 之后、``,``/``}``/``]`` 之前），
# 不去动字符串里的同名词。
_UNDEFINED_RE = re.compile(r"(?<=[:,\[])\s*undefined\s*(?=[,}\]])")


def extract_state(html: str) -> dict[str, Any] | None:
    """从页面 HTML 里抠出 ``window.__INITIAL_STATE__``。"""
    match = _STATE_RE.search(html or "")
    if not match:
        return None
    start = match.end()
    end = html.find("</script>", start)
    raw = html[start : end if end > 0 else len(html)]
    raw = raw.strip().rstrip(";")
    raw = _UNDEFINED_RE.sub("null", raw)
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return state if isinstance(state, dict) else None


def note_from_state(state: dict[str, Any] | None, note_id: str = "") -> XhsNote | None:
    """从状态里取笔记；没有笔记数据时返回 ``None``。

    两种页面结构都要认：

    - **桌面版**：``note.noteDetailMap[<noteId>].note``
    - **移动 H5**：``noteData.data.noteData``（免登录走的就是这条）
    """
    root = (state or {}).get("note") or {}
    detail_map = root.get("noteDetailMap") or root.get("note_detail_map") or {}
    entry = detail_map.get(note_id) if note_id else None
    if not isinstance(entry, dict) or not entry.get("note"):
        # 有时键不是 note_id（或被改写），退而取第一个有数据的
        entry = None
        for value in detail_map.values():
            if isinstance(value, dict) and value.get("note"):
                entry = value
                break
    if entry:
        return parse_note(entry.get("note") or {}, note_id)

    # 移动 H5：noteData.data.noteData
    mobile = ((state or {}).get("noteData") or {}).get("data") or {}
    card = mobile.get("noteData") if isinstance(mobile, dict) else None
    if isinstance(card, dict) and card:
        return parse_note(card, note_id)
    return None


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #

_XHS_DOMAINS = (".xiaohongshu.com", "xiaohongshu.com", ".xhscdn.com")


def _cookie_header(cookies_file: str) -> str:
    """从 cookies.txt 里挑出小红书域的 Cookie（Netscape 格式）。"""
    path = str(cookies_file or "").strip().strip('"').strip("'")
    if not path:
        return ""
    pairs: list[str] = []
    seen: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                # Netscape 格式：domain / include_subdomains / path / secure /
                # expires / name / value
                domain, name, value = parts[0], parts[5], parts[6]
                if not any(domain.endswith(item) for item in _XHS_DOMAINS):
                    continue
                if name in seen:
                    continue
                seen.add(name)
                pairs.append(f"{name}={value}")
    except OSError:
        return ""
    return "; ".join(pairs)


def fetch_note(
    url: str,
    *,
    cookies_file: str = "",
    timeout: float = 20.0,
    logger: Callable[[str, str], None] | None = None,
    allow_browser: bool = True,
) -> XhsNote:
    """解析一条小红书笔记；失败时在 ``error`` 里说明原因。

    先用**移动端 UA** 走普通 GET（桌面 UA 会 302 到 ``/login``，移动 H5 不会），
    页面里没有笔记数据时才退到浏览器桥。
    """
    log = logger or (lambda level, message: None)
    note_id = extract_note_id(url)
    if not note_id:
        return XhsNote(error="未能从链接中识别笔记 ID")

    headers = {
        "User-Agent": _MOBILE_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.xiaohongshu.com/",
    }
    cookie = _cookie_header(cookies_file)
    if cookie:
        headers["Cookie"] = cookie

    reason = ""
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final = response.geturl()
            html = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        reason = f"HTTP {exc.code}"
        final, html = url, ""
    except Exception as exc:
        reason = f"请求失败：{exc}"
        final, html = url, ""

    if html:
        note = note_from_state(extract_state(html), note_id)
        if note is not None:
            return note
        message = _error_from_url(final)
        if message:
            reason = message
        elif "/login" in final:
            reason = "小红书要求登录后才能查看（请在「链接解析」页配置 cookies.txt）"
        else:
            reason = reason or "页面里没有笔记数据（可能缺少 xsec_token）"

    if not allow_browser:
        return XhsNote(note_id=note_id, error=reason or "解析失败")

    log("info", f"小红书 HTML 解析未取到数据（{reason}），改用浏览器：{url}")
    return fetch_note_via_browser(
        url, note_id=note_id, cookies_file=cookies_file, timeout=timeout, logger=log
    )


#: 浏览器实例较重，同时只跑一个。
_BROWSER_LOCK = threading.Lock()
_BROWSER_LOCK_TIMEOUT = 60.0

#: 页面里取数据的脚本：状态优先，DOM 兜底。
_PAGE_SCRIPT = """
(() => {
  const st = window.__INITIAL_STATE__ || {};
  const desk = ((st.note || {}).noteDetailMap) || ((st.note || {}).note_detail_map) || {};
  const out = {note: null, dom: {}};
  for (const key of Object.keys(desk)) {
    const item = desk[key];
    if (item && item.note) { out.note = item.note; break; }
  }
  if (!out.note) {
    const mob = ((st.noteData || {}).data || {}).noteData;
    if (mob) out.note = mob;
  }
  const pick = (...sels) => {
    for (const sel of sels) {
      const el = document.querySelector(sel);
      const text = el ? (el.innerText || '').trim() : '';
      if (text) return text;
    }
    return '';
  };
  const imgs = Array.from(
    document.querySelectorAll(
      '#noteContainer .swiper-slide img, .swiper-slide img, .note-slider img, .media-container img'
    )
  ).map((el) => el.currentSrc || el.src || '').filter((s) => s.startsWith('http'));
  const video = document.querySelector('#noteContainer video, video');
  out.dom = {
    title: pick('#detail-title', '.note-content .title', '.note-scroller .title'),
    desc: pick('#detail-desc', '.note-content .desc', '.note-scroller .desc'),
    author: pick('.author-wrapper .username', '.author-container .name', '.username'),
    images: imgs,
    og: (document.querySelector('meta[property="og:image"]') || {}).content || '',
    video: (video ? (video.currentSrc || video.src || '') : ''),
  };
  return JSON.stringify(out);
})()
"""


def fetch_note_via_browser(
    url: str,
    *,
    note_id: str = "",
    cookies_file: str = "",
    timeout: float = 30.0,
    logger: Callable[[str, str], None] | None = None,
) -> XhsNote:
    """用浏览器桥打开笔记页取数据（真实浏览器执行 JS，能过掉大部分拦截）。"""
    from . import browser  # 延迟导入：没有浏览器时不必付这个开销

    log = logger or (lambda level, message: None)
    note_id = note_id or extract_note_id(url)
    if not browser.browser_available():
        return XhsNote(
            note_id=note_id,
            error="未找到 Chrome/Edge，无法用浏览器解析小红书链接",
        )
    if not _BROWSER_LOCK.acquire(timeout=_BROWSER_LOCK_TIMEOUT):
        return XhsNote(note_id=note_id, error="上一次浏览器解析还没结束，本次跳过")

    try:
        # ``--headless=new`` 的 UA 里带着 ``HeadlessChrome``，小红书一眼就看出来，
        # 直接甩「IP存在风险」的风控页。换成正常 UA 并关掉自动化特征再试。
        page = browser.BrowserSession(
            logger=log,
            timeout=timeout,
            extra_args=(
                f"--user-agent={_MOBILE_UA}",
                "--disable-blink-features=AutomationControlled",
                "--lang=zh-CN",
            ),
        )
        try:
            page.start()
            _apply_cookies(page, cookies_file)
            page.navigate(url)
            page.wait_ready(timeout=min(timeout, 20.0))
            raw = page.evaluate(_PAGE_SCRIPT)
            data = json.loads(raw) if isinstance(raw, str) and raw else {}
            note = data.get("note")
            if isinstance(note, dict) and note:
                parsed = parse_note(note, note_id)
                if parsed.ok:
                    return parsed
            # 先看页面是不是被踢到了 /404 或 /login：这时候 DOM 里只剩站点的
            # 占位内容（``og:image`` 是 logo），照 DOM 抓会抓出一个「空笔记」，
            # 比报错更糟——用户会收到一张只有「1 张图」字样的卡片。
            final = str(page.evaluate("location.href") or "")
            message = _error_from_url(final)
            if "/404" in final or "/login" in final:
                return XhsNote(
                    note_id=note_id,
                    error=message
                    or (
                        "小红书要求登录后才能查看（请在「链接解析」页配置 cookies.txt）"
                        if "/login" in final
                        else "笔记暂时无法浏览（链接可能缺少 xsec_token 或已删除）"
                    ),
                )
            dom = data.get("dom") or {}
            title = str(dom.get("title") or "").strip()
            desc = str(dom.get("desc") or "").strip()
            images = [str(u) for u in (dom.get("images") or []) if u]
            if not (title or desc or images):
                return XhsNote(
                    note_id=note_id,
                    error=message or "页面没有渲染出笔记内容（可能需要登录或链接缺少 xsec_token）",
                )
            return XhsNote(
                note_id=note_id,
                ok=True,
                title=title,
                uploader=str(dom.get("author") or "").strip(),
                description=desc,
                images=images,
                video_url=str(dom.get("video") or "").strip(),
                kind="video" if dom.get("video") else "normal",
            )
        finally:
            page.close(blocking=False)
    except Exception as exc:
        return XhsNote(note_id=note_id, error=f"浏览器解析失败：{exc}")
    finally:
        _BROWSER_LOCK.release()


def _apply_cookies(page: Any, cookies_file: str) -> None:
    """把 cookies.txt 里的小红书 Cookie 注入浏览器（登录态才看得到部分笔记）。

    失败只记日志：没有 Cookie 时带 ``xsec_token`` 的链接通常仍能打开。
    """
    header = _cookie_header(cookies_file)
    if not header:
        return
    pairs = []
    for chunk in header.split(";"):
        name, _, value = chunk.strip().partition("=")
        if name:
            pairs.append((name, value))
    for name, value in pairs:
        try:
            page._call(  # noqa: SLF001 - 浏览器桥没有暴露 setCookie，直接走 CDP
                "Network.setCookie",
                {
                    "name": name,
                    "value": value,
                    "domain": ".xiaohongshu.com",
                    "path": "/",
                },
            )
        except Exception:
            return


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #

_TTL = 600.0
_CACHE_SIZE = 32
_CACHE: "OrderedDict[str, tuple[float, XhsNote]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _cache_lookup(note_id: str) -> XhsNote | None:
    now = time.time()
    with _CACHE_LOCK:
        entry = _CACHE.get(note_id)
        if entry is None:
            return None
        ts, note = entry
        if now - ts > _TTL:
            _CACHE.pop(note_id, None)
            return None
        _CACHE.move_to_end(note_id)
        return note


def _cache_store(note_id: str, note: XhsNote) -> None:
    with _CACHE_LOCK:
        _CACHE[note_id] = (time.time(), note)
        _CACHE.move_to_end(note_id)
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)


def forget_cached(note_id: str = "") -> None:
    """清缓存；不传 ID 则全清。"""
    with _CACHE_LOCK:
        if note_id:
            _CACHE.pop(note_id, None)
        else:
            _CACHE.clear()


def note_for(
    url: str,
    *,
    cookies_file: str = "",
    timeout: float = 20.0,
    logger: Callable[[str, str], None] | None = None,
    force: bool = False,
) -> XhsNote:
    """带缓存的 :func:`fetch_note`（只缓存成功结果）。"""
    note_id = extract_note_id(url)
    if not force and note_id:
        hit = _cache_lookup(note_id)
        if hit is not None:
            return hit
    note = fetch_note(
        url, cookies_file=cookies_file, timeout=timeout, logger=logger
    )
    if note.ok and note_id:
        _cache_store(note_id, note)
    return note


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #


def download_video(
    note: XhsNote,
    outdir: str,
    *,
    timeout: float = 60.0,
    max_mb: int = 0,
    logger: Callable[[str, str], None] | None = None,
) -> str | None:
    """把笔记视频下到 ``outdir``，返回本地路径；失败返回 ``None``。"""
    log = logger or (lambda level, message: None)
    url = note.video_url
    if not url:
        return None
    from pathlib import Path

    outdir_path = Path(outdir)
    try:
        outdir_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log("warning", f"创建下载目录失败：{exc}")
        return None

    safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", note.title or "").strip(" ._")[:60]
    target = outdir_path / (f"{safe or 'xhs'}-{note.note_id or int(time.time())}.mp4")
    limit = max_mb * 1024 * 1024 if max_mb and max_mb > 0 else 0
    request = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Referer": "https://www.xiaohongshu.com/"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            written = 0
            with target.open("wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if limit and written > limit:
                        raise ValueError("视频超过体积上限")
                    handle.write(chunk)
    except Exception as exc:
        log("warning", f"小红书视频下载失败：{exc}")
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    if target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        log("warning", "小红书视频下载为空文件")
        return None
    return str(target)
