"""措辞闸门：把"他人可能被我误解"从禁词表改成断言预算 + 确定性修复。

原来的做法是直接禁用"你就是/故意/你总是"。这有两个问题：

1. **与人不符**：人是有情绪的。明明很在意，却只能说"我这边先等你的消息"，
   等于把表达压成永远理智的客服腔；情绪只在内部，外面看不见。
2. **判错了对象**：真正的问题不是"某个词"，而是"把对他人的猜测说成事实"。
   同一句话，在有证据时是诚实表达，在没证据时才是误解。禁词表分不开两者。

因此这里按**断言类型**分别设条件，条件全部由系统里已有的量推导：

| 类型 | 例子 | 允许直说的条件 | 依据的是 |
|---|---|---|---|
| 读心（mind） | 你就是 / 故意 / 你不在乎 | `other.confidence ≥ 0.62`，或句中已有明确限定词 | 他人情绪评分的**证据强度** |
| 绝对（absolute） | 总是 / 从来 / 每次都 | 历史重复样本 ≥ 2 | 记忆里的**重复证据** |
| 施压（coercive） | 必须 / 马上 / 凭什么 | 自己情绪 ≥ 0.55 且对方可接纳度 ≥ 0.45 且对方评分可信度 ≥ 0.45 且不处于 seek_info | **心情**与关系情境 |

关键分离：**读心类只看证据，不看我的情绪**——生气不能成为断定别人动机的许可证；
**施压类才由心情决定**，但也必须有一份可信的对方评分：没有对方状态估计时，
再强的情绪也只能表达"我在意/我着急"，不能变成命令。心情对措辞的影响还体现在
修复方式上：情绪强时保留"我觉得/我在意"的自我表达，情绪弱时只做降级。

不满足条件时先做确定性修复（加限定、降绝对化、去施压与感叹号），再复核；
修复只降低断言强度，不新增任何事实。判定结果全部写进 `expression.plan["wording"]`。
"""
from __future__ import annotations

from dataclasses import dataclass

# 读心类：对他人心理状态/动机下结论
MIND_READING = (
    "你就是", "你总是", "你从来", "你根本", "你不想", "你不在乎",
    "你故意", "故意", "你肯定", "你一定",
)
# 绝对化：对频率下结论，需要重复证据
ABSOLUTE = ("总是", "从来", "每次都", "永远", "根本")
# 施压：要求对方立刻行动
COERCIVE = ("凭什么", "必须", "马上", "立刻")

# 断言预算（全部可在测试中直接引用）
MIND_READING_MIN_CONFIDENCE = 0.62
ABSOLUTE_MIN_REPETITION = 2
COERCIVE_MIN_INTENSITY = 0.55
COERCIVE_MIN_RECEPTIVITY = 0.45
COERCIVE_MIN_READ_CONFIDENCE = 0.45

# 修复规则：长的先替换，避免"你就是"被拆成两个短词
REPAIRS = (
    ("你就是不想", "我觉得你好像不太想"),
    ("你就是", "我觉得你"),
    ("你根本", "我有点觉得你"),
    ("你故意", "我有点觉得你像是"),
    ("你肯定", "我猜你"),
    ("你一定", "我猜你"),
    ("你总是", "这次你"),
    ("你从来", "这次你"),
    ("每次都", "这次"),
    ("总是", "这次"),
    ("从来", "这次"),
    ("永远", "这次"),
    ("根本", "其实"),
    ("故意", "像是"),
    ("凭什么", "为什么"),
    ("必须", "能不能"),
    ("马上", "方便的时候"),
    ("立刻", "方便的时候"),
)

DEFAULT_HEDGES = ("我觉得", "我感觉", "可能", "也许", "是不是", "看起来",
                  "听起来", "说不定", "大概", "像是", "我猜")


@dataclass(frozen=True)
class WordingCheck:
    allowed: bool
    reason: str
    hedged: bool
    risky: tuple[str, ...]
    repaired: str = ""

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "hedged": self.hedged,
            "risky": list(self.risky),
            "repaired": self.repaired,
        }


def soften(text: str, reasons: tuple[str, ...] = ()) -> str:
    """确定性修复：加限定词、降绝对化、去施压。只降低断言强度。"""
    out = text
    for old, new in REPAIRS:
        if old in out:
            out = out.replace(old, new)
    if any(r.startswith(("mind_reading", "absolute")) for r in reasons):
        prefix = "我觉得"
        if not any(hedge in out for hedge in DEFAULT_HEDGES):
            out = prefix + out
    if any("coercive" in r for r in reasons):
        # 施压不成立时也一并去掉感叹号：标点是最直接的强度信号
        out = out.replace("！", "。").replace("!", "。")
    return out


def check_text(text: str, *, read: dict | None = None,
               own_intensity: float = 0.0, strategy: str = "hold",
               repetition: int = 0, receptivity: float = 0.5,
               hedges: tuple[str, ...] = DEFAULT_HEDGES,
               high_risk: tuple[str, ...] = ()) -> WordingCheck:
    """判断一段措辞是否可以直说；不允许时给出确定性修复候选。

    `read` 是他人情绪评分（`other.*`）的快照；缺省表示"没有估计"，
    此时读心类一律不允许直说——没有证据的断言就是误解。
    """
    if not text:
        return WordingCheck(True, "empty", False, ())

    read = read or {}
    confidence = float(read.get("confidence", 0.0) or 0.0)
    hedged = any(hedge in text for hedge in hedges)

    mind_hits = [w for w in MIND_READING if w in text]
    absolute_hits = [w for w in ABSOLUTE if w in text]
    coercive_hits = [w for w in COERCIVE if w in text]
    extra_hits = [w for w in high_risk
                  if w in text and w not in mind_hits
                  and w not in absolute_hits and w not in coercive_hits]
    risky = tuple(dict.fromkeys(mind_hits + absolute_hits + coercive_hits + extra_hits))

    reasons: list[str] = []
    if mind_hits and not hedged and confidence < MIND_READING_MIN_CONFIDENCE:
        reasons.append("mind_reading_needs_evidence")
    if absolute_hits and not hedged and repetition < ABSOLUTE_MIN_REPETITION:
        reasons.append("absolute_needs_repetition")
    if coercive_hits:
        if not (own_intensity >= COERCIVE_MIN_INTENSITY
                and confidence >= COERCIVE_MIN_READ_CONFIDENCE
                and receptivity >= COERCIVE_MIN_RECEPTIVITY
                and strategy != "seek_info"):
            reasons.append("coercive_needs_intensity_and_receptivity")
    if extra_hits:
        reasons.append("high_risk_word")

    if not reasons:
        classes = []
        if mind_hits:
            classes.append("mind_reading")
        if absolute_hits:
            classes.append("absolute")
        if coercive_hits:
            classes.append("coercive")
        if extra_hits:
            classes.append("high_risk_word")
        reason = "allowed:" + "+".join(classes) if classes else "clean"
        return WordingCheck(True, reason, hedged, risky)

    reasons_t = tuple(reasons)
    return WordingCheck(False, "+".join(reasons_t), hedged, risky,
                        soften(text, reasons_t))


def why(text: str, **kwargs) -> str:
    """便于 CLI / 测试输出的一行解释。"""
    verdict = check_text(text, **kwargs)
    if verdict.allowed:
        return "通过"
    return f"需改写（{verdict.reason}）"
