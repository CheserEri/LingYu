"""撤回：记录机器人最近发出的消息，并识别「@机器人 撤回」这类应急指令。

为什么需要它
------------
机器人偶尔会发出不该发的东西（违规视频、解析错的素材）。等群里有人发现时，
最有效的处置是**立刻撤回**，而不是等管理员去点鼠标。底层微信只提供「撤回
当前会话最新一条自己发的消息」，所以这里要做两件事：

1. **记账**：每发出一条消息（文本或附件）就在 :class:`RecallBook` 里记一笔，
   撤回时才知道撤掉的是哪一条、值不值得撤（媒体优先）。
2. **认指令**：把「@灵语 撤回」「撤回一下」「/撤回」这类话识别成指令，
   同时**不能**把「别撤回」「撤回的消息看不到」这种日常对话误判成指令。

微信的撤回时限是 2 分钟（发出去超过就只剩「删除」，只对自己生效），
故 :class:`RecallBook` 的时间窗默认留了余量（110 秒）。
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

# 消息类型：只有 media 才是「违规内容」的主要载体，text 仅作为顺带撤掉的对象。
KIND_MEDIA = "media"
KIND_TEXT = "text"

# 指令允许的后缀：``撤回`` + 这些后缀仍算指令，其它组合一律不当指令，
# 以免把「撤回的消息看不到」这类正常聊天误伤成指令。
_ALLOWED_SUFFIXES = (
    "",
    "一下",
    "一下下",
    "吧",
    "掉",
    "了",
    "啊",
    "呀",
    "呢",
    "它",
    "这个",
    "那个",
    "这条",
    "那条",
    "上一条",
    "刚才的",
    "刚发的",
    "这条消息",
    "那条消息",
    "消息",
    "视频",
    "图片",
    "文件",
    "链接",
)

# @某人：微信的 @ 后面可能跟一个细空白（\u2005）。
_AT_RE = re.compile(r"@[^\s\u2005]+[\s\u2005]*")
# 指令里的装饰性字符。
_TRIM_CHARS = " \t\r\n\u2005\u3000，。！？!?,.:：；;、~～…/／"


@dataclass
class SentItem:
    """机器人发出的一条消息（用于撤回定位）。"""

    kind: str = KIND_TEXT
    label: str = ""
    path: str = ""
    text: str = ""
    ts: float = field(default_factory=time.time)

    def describe(self) -> str:
        """一句话说明「撤回的是什么东西」，用于日志与回执。"""
        if self.kind == KIND_MEDIA:
            label = self.label or "文件"
            name = Path(self.path).name if self.path else ""
            return f"{label}（{name}）" if name else label
        preview = re.sub(r"\s+", " ", self.text or "").strip()
        return f"文字（{preview[:12]}…）" if len(preview) > 12 else f"文字（{preview}）"


class RecallBook:
    """按会话记录机器人最近发出的消息，并给出「撤回计划」。

    只保留每个会话最近 ``per_chat`` 条：撤回只关心最新的几条，留太多没意义。
    """

    def __init__(self, per_chat: int = 20) -> None:
        self._per_chat = max(1, int(per_chat))
        self._items: dict[str, deque[SentItem]] = {}

    def _bucket(self, chat: str) -> deque[SentItem]:
        return self._items.setdefault(chat, deque(maxlen=self._per_chat))

    def record(self, chat: str, item: SentItem) -> SentItem:
        self._bucket(chat).append(item)
        return item

    def record_text(self, chat: str, text: str) -> SentItem:
        return self.record(chat, SentItem(kind=KIND_TEXT, text=text or ""))

    def record_media(
        self, chat: str, path: str | Path, label: str = ""
    ) -> SentItem:
        return self.record(
            chat,
            SentItem(kind=KIND_MEDIA, label=label, path=str(path or "")),
        )

    def recent(self, chat: str) -> list[SentItem]:
        """该会话的记录（旧 → 新）。"""
        return list(self._items.get(chat) or ())

    def plan(
        self,
        chat: str,
        now: float | None = None,
        window: float = 110.0,
        max_items: int = 2,
        require_media: bool = True,
    ) -> list[SentItem]:
        """算出「要撤回哪几条」，**新 → 旧**排列。

        底层一次只能撤最新的一条，所以要撤到某条媒体，就得把它之后发的消息
        一起撤掉（那些多半是「正在解析链接」之类的提示，撤掉也不可惜）。
        规则：

        - 只在 ``window`` 秒内找（微信 2 分钟时限，留余量）；
        - 从最新往回走，**碰到第一条媒体就停**；
        - 最多 ``max_items`` 条，避免一口气撤掉一堆正常发言；
        - ``require_media=True`` 且窗口内没有媒体记录时返回空列表，交给上层
          决定要不要「兜底撤最新一条」。
        """
        moment = time.time() if now is None else now
        items = self.recent(chat)
        picked: list[SentItem] = []
        found_media = False
        for item in reversed(items):
            if window > 0 and moment - float(item.ts or 0.0) > window:
                break
            picked.append(item)
            if item.kind == KIND_MEDIA:
                found_media = True
                break
            if len(picked) >= max(1, int(max_items)):
                break
        if require_media and not found_media:
            return []
        return picked[: max(1, int(max_items))]

    def forget(self, chat: str, count: int) -> None:
        """撤掉成功后把这些记录从账本上抹掉，避免重复撤回同一条。"""
        bucket = self._items.get(chat)
        if not bucket:
            return
        for _ in range(max(0, int(count))):
            if not bucket:
                break
            bucket.pop()

    def clear(self, chat: str | None = None) -> None:
        if chat is None:
            self._items.clear()
        else:
            self._items.pop(chat, None)


def parse_keywords(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """把配置里的指令词（逗号/换行/空格分隔）拆成列表，去空去重保序。"""
    if isinstance(raw, (list, tuple)):
        parts = [str(item) for item in raw]
    else:
        parts = re.split(r"[,，、;；\s]+", str(raw or ""))
    result: list[str] = []
    for part in parts:
        word = part.strip(_TRIM_CHARS)
        if word and word not in result:
            result.append(word)
    return result


def normalize_command(content: str) -> str:
    """去掉 @提及、空白与标点，得到「纯指令文本」。"""
    text = _AT_RE.sub(" ", content or "")
    text = re.sub(r"[\s\u2005\u3000]+", "", text)
    return text.strip(_TRIM_CHARS)


def is_recall_command(
    content: str, keywords: list[str] | tuple[str, ...] | None = None
) -> bool:
    """内容是否是撤回指令（只认「指令词 + 允许的后缀」的短句）。"""
    words = list(keywords) if keywords else ["撤回"]
    text = normalize_command(content)
    if not text or len(text) > 12:
        return False
    for word in words:
        if not word:
            continue
        if text == word:
            return True
        if not text.startswith(word):
            continue
        suffix = text[len(word) :]
        if suffix in _ALLOWED_SUFFIXES:
            return True
    return False
