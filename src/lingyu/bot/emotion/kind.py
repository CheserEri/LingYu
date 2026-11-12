"""微信消息 → 情绪内核的事件类型（``kind``）。

上游由调用方直接指定 kind（``chat.py --kind rejection``），但微信里没有这个字段。
不补这一层，所有消息都会按 ``message`` 的先验走：被称赞不会带来正向情绪、
被拒绝和闲聊的情绪曲线一样——系统能跑，但「情绪有记忆」这件事就消失了。

分类只用确定性词表 + 上一条事件类型，不调模型：它要便宜、可复核、可回滚，
而且必须在情绪周期开始**之前**就得出结果（评估先验依赖它）。
"""
from __future__ import annotations

# 顺序即优先级：先判「明确的负面信号」，再判安抚/称赞。
# 「谢谢你，不用了」应该算拒绝，而不是称赞。
REJECTION_CUES = (
    "算了", "不想聊", "不想说", "不想讲", "别说了", "别问了", "别烦", "别找我",
    "滚", "拉黑", "删了", "没空理", "不想理", "不想管", "不需要", "不用了",
    "没必要", "不感兴趣", "别再发", "闭嘴", "烦死了",
)

CRITICISM_CUES = (
    "你怎么", "你为什么", "你总是", "你从来", "你根本", "你就是", "凭什么",
    "不应该", "不应该这样", "有毛病", "讨厌你", "烦人", "过分了", "说话注意",
)

REASSURANCE_CUES = (
    "没事", "别担心", "不用担心", "我很好", "放心", "抱歉", "对不起",
    "不好意思", "是我不好", "我理解", "别生气", "没生气",
)

PRAISE_CUES = (
    "谢谢", "谢啦", "感谢", "辛苦了", "太好了", "真好", "真棒", "厉害",
    "牛啊", "好厉害", "喜欢你", "爱你", "么么", "感动", "有你在真好",
    "写得真好", "说得对",
)

#: 「事后解释」线索：只有在上一条是负面事件时才算 ``outcome``。
#: 内核的 ``memory._settle`` 靠这类事件回填「当时那样理解，现在还成立吗」，
#: 没有它，被误读成拒绝的经历永远不会被修正。
OUTCOME_CUES = (
    "刚看到", "才看到", "手机没电", "在忙", "加班", "睡着了", "开会",
    "没带手机", "刚忙完", "出差", "信号不好", "没注意", "忘记了", "来晚了",
)

NEGATIVE_KINDS = frozenset({"rejection", "criticism", "message_silence"})

#: 一次消息最多按多少字参与匹配（长文里的引用/转发不该左右判断）。
_MATCH_WINDOW = 120


def classify(
    text: str,
    *,
    prev_kind: str | None = None,
    unanswered_hours: float = 0.0,
) -> str:
    """返回 ``message`` / ``praise`` / ``criticism`` / ``rejection`` /
    ``reassurance`` / ``outcome`` 之一。

    ``prev_kind`` 是该会话上一条事件类型：只用于识别「事后解释」。
    ``unanswered_hours`` 暂不参与判断（沉默事件由引擎的定时器单独产生），
    保留参数是为了将来把「久未回复后的第一句」单独建模。
    """
    body = str(text or "").strip()
    if not body:
        return "message"
    window = body[:_MATCH_WINDOW]

    if prev_kind in NEGATIVE_KINDS and _hit(window, OUTCOME_CUES):
        return "outcome"
    if _hit(window, REJECTION_CUES):
        return "rejection"
    if _hit(window, CRITICISM_CUES):
        return "criticism"
    if _hit(window, REASSURANCE_CUES):
        return "reassurance"
    if _hit(window, PRAISE_CUES):
        return "praise"
    return "message"


def _hit(text: str, cues: tuple[str, ...]) -> bool:
    return any(cue in text for cue in cues)


def is_negative(kind: str) -> bool:
    return kind in NEGATIVE_KINDS
