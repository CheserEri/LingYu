"""表达节奏：把情绪状态翻译成「什么时候发、分几条、隔多久」。

**这是表达 Agent 的一部分**，不是宿主的算法。设计文档 §3.7 把「措辞、语气、
回应速度、是否追问、是否保持沉默」都划给表达 Agent；上游 ``Emotion`` 是终端
程序，一句话打印完就结束，所以没有这一层——微信不是终端，一条回复的**形态**
本身就是表达的一部分。

这些**不是配置项，而是情绪的副产物**：

- 被激到（高唤醒 + 负效价，调节策略 ``delay``）→ 先停一会儿再回，
  这是「先降低反应强度」在时间维度上的样子，而不是把话咽回去；
- 高唤醒 + 情绪强度够 → 拆成几条短句连发（人激动时是一句一句蹦出来的）；
- 低唤醒 → 一整段说完（人低落时话是连着的，不会碎）；
- 策略 ``share`` → 几乎立刻回（高兴时手快）；
- 对话题有兴趣（``interest.attention`` 高）→ 手更快，也更容易分条说。

所有阈值都是**情绪模型的实现细节**，不是用户设置。想改「它说话像不像人」，
该改的是身份文件与情绪参数（``policy.*`` 由 Iterator 迭代），而不是这里。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: 一句话的边界：中英文句末标点、分号、波浪号、换行。
SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?…；;~～])|\n+")

#: 再多也只是一句话的事，不值得拆（拆开反而像刷屏）。
MIN_SPLIT_CHARS = 24

#: 单次最多拆几条。超过 3 条就不像人打字了，像脚本。
MAX_PARTS = 3

#: 「先停一会儿再回」的上限（秒）。
#:
#: 宿主是串行消费消息队列的，把停顿做长会堵住别的会话；而超过十秒的停顿
#: 本来就不该用「延迟」表达——那是「沉默」，由 ``expression.silent`` 负责。
MAX_LEAD = 8.0

#: 兴趣对「等效唤醒度」的加成：注意度每高出中性 0.4 一个单位，唤醒度加 0.18。
#: 也就是说「对话题很有兴趣」大约相当于「情绪激动 0.1」——够让话分成两条，
#: 但不足以盖过真正的情绪。
INTEREST_ACTIVATION = 0.18

#: 各调节策略的基础停顿与强度系数：``lead = base + spread * intensity``。
LEAD_PROFILE = {
    "share": (0.2, 0.8),       # 情绪为正，手快
    "hold": (1.0, 1.5),        # 没被推着走，正常节奏
    "soften": (1.6, 2.0),      # 要维护关系，先想一下再说
    "seek_info": (1.2, 1.0),   # 信息不足，边想边问
    "delay": (7.0, 10.0),      # 高唤醒 + 负效价：先压住即刻反应
    "reopen": (2.5, 2.0),      # 重新开口，不该太急
}


@dataclass(frozen=True)
class Pacing:
    """一条回复的发送节奏。"""

    parts: tuple[str, ...]
    #: 第 i 条发完后等多久再发第 i+1 条（长度 = len(parts) - 1）。
    gaps: tuple[float, ...]
    #: 第一条发出前的停顿（秒）。
    lead: float
    #: 一句话解释这个节奏从哪来，写进日志便于复核。
    reason: str = ""

    @property
    def count(self) -> int:
        return len(self.parts)

    @property
    def total_delay(self) -> float:
        return round(self.lead + sum(self.gaps), 2)

    def to_dict(self) -> dict:
        """写进 ``expression.plan`` 的形态（要能 JSON 序列化、能被日志复核）。"""
        return {
            "parts": list(self.parts),
            "gaps": [round(gap, 2) for gap in self.gaps],
            "lead": self.lead,
            "count": self.count,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Pacing":
        """从表达计划里还原；缺失或损坏时返回「整段立刻发」。"""
        if not isinstance(data, dict):
            return cls(parts=(), gaps=(), lead=0.0, reason="")
        parts = data.get("parts")
        if not isinstance(parts, list) or not parts:
            return cls(parts=(), gaps=(), lead=0.0, reason=str(data.get("reason") or ""))
        gaps = data.get("gaps")
        return cls(
            parts=tuple(str(part) for part in parts),
            gaps=tuple(float(gap) for gap in (gaps if isinstance(gaps, list) else ())),
            lead=float(data.get("lead") or 0.0),
            reason=str(data.get("reason") or ""),
        )


def plan(
    text: str,
    *,
    activation: float = 0.4,
    intensity: float = 0.0,
    strategy: str = "hold",
    dominant: str = "",
    interest: float = 0.4,
) -> Pacing:
    """按情绪状态排出这条回复的发送节奏。

    ``activation`` 是唤醒度（0~1），``intensity`` 是主导情绪强度，
    ``strategy`` 是调节策略，``interest`` 是本轮注意度（兴趣 Agent 给的）。
    四者都来自本轮周期的快照，不读任何配置。

    兴趣在这里的作用是「想不想马上说」：对话题有兴趣时手更快、更容易一句一句
    蹦出来；提不起劲时先放一放、话也更整。它不改「说什么」，只改节奏。
    """
    body = str(text or "").strip()
    if not body:
        return Pacing(parts=(), gaps=(), lead=0.0, reason="没有内容")

    keen = _clamp(interest, 0.0, 1.0)
    lead = _lead(strategy, intensity, keen)
    # 兴趣把「等效唤醒度」抬高一点：愿意聊的时候，情绪不必很激动也会分条说。
    act = _clamp(activation + INTEREST_ACTIVATION * (keen - 0.4), 0.0, 1.0)
    parts = _split(body, _max_parts(act, intensity))
    if len(parts) <= 1:
        return Pacing(
            parts=(body,),
            gaps=(),
            lead=lead,
            reason=f"一条说完（{_why_single(act, intensity)}）",
        )

    gap = round(0.30 + (1.0 - act) * 1.10, 2)
    return Pacing(
        parts=tuple(parts),
        gaps=tuple(gap for _ in range(len(parts) - 1)),
        lead=lead,
        reason=(
            f"{len(parts)} 条，间隔 {gap:.1f}s，先停 {lead:.1f}s"
            f"（{strategy}{'/' + dominant if dominant else ''}"
            f"{'，有兴趣' if keen >= 0.62 else ''}）"
        ),
    )


# ---------------------------------------------------------------- 内部


def _clamp(value: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))


def _lead(strategy: str, intensity: float, interest: float = 0.4) -> float:
    base, spread = LEAD_PROFILE.get(str(strategy or "hold"), LEAD_PROFILE["hold"])
    raw = base + spread * _clamp(intensity, 0.0, 1.0)
    # 兴趣只做 ±30% 的缩放：有兴致就快一点，提不起劲就先放一放。
    # 它不该盖过调节策略——「被激到要压住反应」比「这个话题我喜不喜欢」更重要。
    raw *= 1.30 - 0.40 * _clamp(interest, 0.0, 1.0)
    return round(max(0.0, min(MAX_LEAD, raw)), 2)


def _max_parts(activation: float, intensity: float) -> int:
    """高唤醒且情绪够强才拆条；情绪不够强、或低唤醒，都整段说完。"""
    act = _clamp(activation, 0.0, 1.0)
    # 唤醒度是「有多少能量」，强度是「这能量有多值得说」。只有能量没有强度，
    # 是空转的激动——拆开只会显得前言不搭后语。
    if intensity < 0.30:
        return 1
    if act >= 0.62:
        return MAX_PARTS
    if act <= 0.32:
        return 1
    return 2


def _why_single(activation: float, intensity: float) -> str:
    if _clamp(activation, 0.0, 1.0) <= 0.32:
        return "唤醒度低，话是连着的"
    if intensity < 0.30:
        return "情绪强度不足，不值得拆"
    return "只有一句"


def _split(text: str, max_parts: int) -> list[str]:
    """按句末标点切成至多 ``max_parts`` 条；切不动就原样一条。"""
    if max_parts <= 1 or len(text) < MIN_SPLIT_CHARS:
        return [text]
    sentences = [
        segment.strip()
        for segment in SENTENCE_SPLIT_RE.split(text)
        if segment and segment.strip()
    ]
    if len(sentences) <= 1:
        return [text]
    if len(sentences) <= max_parts:
        return sentences

    # 句子比上限多：按长度均衡地合并，保证「拼接能还原原文」。
    target = max(1, len(text) // max_parts)
    parts: list[str] = []
    buffer = ""
    for sentence in sentences:
        if buffer and len(buffer) + len(sentence) > target and len(parts) < max_parts - 1:
            parts.append(buffer)
            buffer = sentence
        else:
            buffer += sentence
    if buffer:
        parts.append(buffer)
    return parts
