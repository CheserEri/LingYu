"""卡片渲染的数据结构：推文与小红书笔记共用同一套版式。

为什么单独抽一层
----------------
``tweet.render_card`` 原本直接吃 :class:`lingyu.bot.tweet.Tweet`。小红书笔记要
用**同一套版式**（头像 / 昵称 / 正文 / 竖排长图 / 页脚），字段却对不上：
它没有 ``@handle``、点赞叫「赞」、评论叫「评论」，正文还分成标题与描述两段。

与其在渲染器里塞一堆 ``if 是小红书``，不如把「一张社交帖子卡片需要什么」定义成
:class:`CardData`，两边各自转换（``Tweet.to_card()`` / :func:`note_to_card`），
渲染器只认这一种结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只用于类型标注，避免运行期循环导入
    from .xiaohongshu import XhsNote


@dataclass
class CardData:
    """一张卡片所需的全部字段。"""

    id: str
    url: str = ""
    text: str = ""
    # 译文（通常是简体中文）；有值时正文画译文、原文进灰色「原文」引用块。
    translation: str = ""
    author_name: str = ""
    # 有值时渲染成 ``@handle``；空则退回 :attr:`subtitle`。
    author_handle: str = ""
    subtitle: str = ""
    avatar_url: str = ""
    # 认证："" / "blue" / "business" / "government"
    verified: str = ""
    created_at: str = ""
    photos: list[str] = field(default_factory=list)
    video_poster: str = ""
    has_video: bool = False
    likes: int = 0
    likes_label: str = "喜欢"
    replies: int = 0
    replies_label: str = "回复"
    # 作品**总图数**。渲染器只画前几张（长图不能无限长），超出的在页脚标一句
    # 「共 N 张图」，让用户知道后面还有。0 表示不提示。
    photo_total: int = 0
    # 落盘文件名前缀，如 ``tweet-123.png`` / ``xhs-abc.png``。
    prefix: str = "tweet"

    @property
    def byline(self) -> str:
        """昵称下面那行：``@handle`` 或副标题（如「小红书」）。"""
        return f"@{self.author_handle}" if self.author_handle else self.subtitle


def note_to_card(note: "XhsNote") -> CardData:
    """把一条小红书笔记转成卡片数据（与推文共用同一套渲染器）。

    差异只有三处，都在这里抹平：

    - **没有 @用户名**：副标题改用「小红书 · 图文（5 张）」这类说明。
    - **正文分两段**：标题单独一行、空一行后接描述；描述已含标题时不重复。
    - **互动口径**：赞 / 评论，而不是喜欢 / 回复。
    """
    title = (note.title or "").strip()
    description = (note.description or "").strip()
    if title and description and not description.startswith(title):
        text = f"{title}\n\n{description}"
    else:
        text = description or title

    subtitle = "小红书"
    if note.kind_label:
        subtitle = f"小红书 · {note.kind_label}"

    photos = [url for url in note.images if url]
    return CardData(
        id=note.note_id,
        text=text,
        author_name=(note.uploader or "小红书用户").strip(),
        subtitle=subtitle,
        avatar_url=note.avatar,
        # 视频笔记的封面也在 imageList 里，画成带播放角标的第一张。
        photos=photos,
        video_poster=photos[0] if (note.is_video and photos) else "",
        has_video=note.is_video,
        likes=note.likes,
        likes_label="赞",
        replies=note.comments,
        replies_label="评论",
        photo_total=len(photos),
        prefix="xhs",
    )
