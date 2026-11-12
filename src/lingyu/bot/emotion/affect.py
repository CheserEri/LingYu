"""L1 心境层：连续情绪数值、衰减与事件评估。

这一层只回答「此刻心里是什么状态」，不决定说什么。三个边界：

- 数值永远 clamp 在合法区间，不允许 NaN 进入持久化；
- 情绪只能改变表达与显著性，不能改写身份与人格札记；
- 每次更新都要能由「事件类型 + 文本线索 + 时间」重放，不依赖模型。
"""
from __future__ import annotations

import time
from typing import Any

from .core.util import clamp

#: 心境字段与初始值。数值都归一到 [0,1]，效价是 [-1,1]。
DEFAULT_AFFECT: dict[str, float] = {
    "valence": 0.10,
    "arousal": 0.35,
    "longing": 0.20,
    "irritation": 0.00,
    "warmth": 0.45,
    "sharing": 0.40,
    "security": 0.50,
    "energy": 0.60,
}

DEFAULT_RELATION: dict[str, float] = {
    "familiarity": 0.20,
    "trust": 0.35,
    "closeness": 0.25,
    "tension": 0.00,
}

#: 这些事件对关系与情绪的权重明显高于普通闲聊。
STRONG_KINDS = frozenset({"criticism", "rejection", "praise", "reassurance", "outcome"})

POSITIVE_CUES = (
    "开心", "高兴", "谢谢", "谢啦", "喜欢", "爱你", "太好了", "真好", "真棒",
    "厉害", "顺利", "成功", "可爱", "安心", "抱抱", "想你", "感动", "舒服",
    "轻松", "放心", "不错", "好耶", "哈哈",
)
NEGATIVE_CUES = (
    "难过", "累", "烦", "生气", "委屈", "失望", "焦虑", "害怕", "崩溃", "难受",
    "吵架", "讨厌", "孤独", "压力", "失眠", "想哭", "糟糕", "倒霉", "无语",
)
URGENT_CUES = ("急", "马上", "现在", "快点", "赶紧", "救命")

NEGATIVE_DOMINANTS = frozenset({"烦躁", "低落", "警惕", "想念"})
POSITIVE_DOMINANTS = frozenset({"愉快", "安心"})


def default_state() -> dict[str, Any]:
    """新会话的完整 state。"""
    return {
        "schema": 2,
        "affect": dict(DEFAULT_AFFECT),
        "relation": dict(DEFAULT_RELATION),
        "episodes": [],
        "episode_count": 0,
        "interest": {"values": {}, "topic": "", "attention": 0.4, "note": ""},
        "regulation": {"silence_hours": 0.0, "reopen_done": False},
        "meta": {"turn": 1, "last_kind": "", "last_at": 0.0, "recent_topics": []},
    }


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def _section(raw: Any, defaults: dict[str, float], lo: float, hi: float) -> dict[str, float]:
    source = raw if isinstance(raw, dict) else {}
    out: dict[str, float] = {}
    for key, default in defaults.items():
        out[key] = clamp(_num(source.get(key), default), lo, hi)
    return out


def normalize_state(raw: Any) -> dict[str, Any]:
    """把磁盘上的 state 收拢成当前 schema；缺字段补默认值。"""
    base = default_state()
    if not isinstance(raw, dict):
        return base
    base["affect"] = _section(raw.get("affect"), DEFAULT_AFFECT, -1.0, 1.0)
    # 唤醒度、想念、烦躁等本来就是 [0,1]，效价单独保持 [-1,1]。
    for key in DEFAULT_AFFECT:
        if key == "valence":
            continue
        base["affect"][key] = clamp(base["affect"][key], 0.0, 1.0)
    base["relation"] = _section(raw.get("relation"), DEFAULT_RELATION, 0.0, 1.0)

    episodes = raw.get("episodes")
    if isinstance(episodes, list):
        base["episodes"] = [item for item in episodes[-60:] if isinstance(item, dict)]
    base["episode_count"] = max(
        int(_num(raw.get("episode_count"), 0.0)),
        len(base["episodes"]),
    )

    interest = raw.get("interest")
    if isinstance(interest, dict):
        values = interest.get("values")
        base["interest"]["values"] = {
            str(k): clamp(_num(v, 0.0), 0.0, 1.0)
            for k, v in (values or {}).items()
        } if isinstance(values, dict) else {}
        base["interest"]["topic"] = str(interest.get("topic") or "")
        base["interest"]["attention"] = clamp(_num(interest.get("attention"), 0.4), 0.0, 1.0)
        base["interest"]["note"] = str(interest.get("note") or "")

    regulation = raw.get("regulation")
    if isinstance(regulation, dict):
        base["regulation"]["silence_hours"] = max(
            0.0, _num(regulation.get("silence_hours"), 0.0)
        )
        base["regulation"]["reopen_done"] = bool(regulation.get("reopen_done"))

    meta = raw.get("meta")
    if isinstance(meta, dict):
        base["meta"]["turn"] = max(1, int(_num(meta.get("turn"), 1.0)))
        base["meta"]["last_kind"] = str(meta.get("last_kind") or "")
        base["meta"]["last_at"] = max(0.0, _num(meta.get("last_at"), 0.0))
        topics = meta.get("recent_topics")
        base["meta"]["recent_topics"] = (
            [str(item) for item in topics[-6:]] if isinstance(topics, list) else []
        )
    return base


def decay(state: dict[str, Any], elapsed_hours: float, local_hour: float | None) -> None:
    """时间流逝：唤醒与烦躁先退，想念慢慢涨，深夜压低精力。"""
    affect = state["affect"]
    relation = state["relation"]
    hours = clamp(_num(elapsed_hours, 0.0), 0.0, 168.0)

    slow = 0.5 ** (hours / 6.0)
    affect["arousal"] = clamp(affect["arousal"] * slow, 0.0, 1.0)
    affect["irritation"] = clamp(affect["irritation"] * slow, 0.0, 1.0)
    relation["tension"] = clamp(relation["tension"] * slow, 0.0, 1.0)

    valence_slow = 0.5 ** (hours / 12.0)
    affect["valence"] = clamp(0.10 + (affect["valence"] - 0.10) * valence_slow, -1.0, 1.0)

    # 想念不是「没消息就线性上涨」：前几小时几乎不变，之后才慢慢显出来。
    if hours >= 3.0:
        affect["longing"] = clamp(affect["longing"] + min(0.35, (hours - 3.0) * 0.012), 0.0, 1.0)

    # 精力向常态回归；深夜额外压低，白天慢慢恢复。
    # 深夜是「上限」而不是每次调用都扣一次：连发几条消息不该把精力扣到零。
    affect["energy"] = clamp(0.45 + (affect["energy"] - 0.45) * slow, 0.0, 1.0)
    if local_hour is not None and (local_hour < 6.0 or local_hour >= 23.0):
        affect["energy"] = clamp(min(affect["energy"], 0.42), 0.0, 1.0)


def _hits(text: str, cues: tuple[str, ...]) -> int:
    return sum(1 for cue in cues if cue in text)


def appraise(
    *,
    kind: str,
    text: str,
    state: dict[str, Any],
    interest_attention: float = 0.4,
) -> dict[str, Any]:
    """把一条事件翻译成数值增量。纯规则、无模型、可重放。"""
    body = str(text or "").strip()
    positive = _hits(body, POSITIVE_CUES)
    negative = _hits(body, NEGATIVE_CUES)
    urgent = _hits(body, URGENT_CUES)

    delta = {
        "valence": 0.0,
        "arousal": 0.0,
        "longing": 0.0,
        "irritation": 0.0,
        "warmth": 0.0,
        "sharing": 0.0,
        "security": 0.0,
        "energy": 0.0,
    }

    if kind == "praise":
        delta.update(valence=0.18, warmth=0.12, security=0.08, arousal=0.05)
    elif kind == "reassurance":
        delta.update(valence=0.12, warmth=0.08, security=0.16, irritation=-0.10)
    elif kind == "outcome":
        # 事后解释：先前的负面读法被修正，关系安全感回升。
        delta.update(valence=0.06, security=0.14, irritation=-0.12, tension=-0.10)
    elif kind == "criticism":
        delta.update(valence=-0.20, irritation=0.18, arousal=0.12, security=-0.10, warmth=-0.06)
    elif kind == "rejection":
        delta.update(valence=-0.24, irritation=0.12, arousal=0.08, security=-0.16, warmth=-0.10)
    elif kind == "message_silence":
        delta.update(longing=0.12, security=-0.08, arousal=0.04)
    elif kind == "self_expression":
        delta.update(sharing=0.12, longing=-0.08, arousal=0.03)

    delta["valence"] += 0.10 * positive - 0.12 * negative
    delta["arousal"] += 0.04 * (positive + negative) + 0.03 * urgent
    delta["warmth"] += 0.04 * positive
    delta["security"] += 0.03 * positive - 0.04 * negative
    if interest_attention >= 0.55:
        delta["sharing"] += 0.05
        delta["arousal"] += 0.03
    if interest_attention <= 0.25:
        delta["sharing"] -= 0.03

    # 词表之外再补一层「具体感受」：只靠正负计数会让「好累、有点烦」显得太平。
    if any(word in body for word in ("烦", "生气", "讨厌", "无语", "上火")):
        delta["irritation"] += 0.18
        delta["valence"] -= 0.04
    if any(word in body for word in ("累", "困", "疲惫", "失眠")):
        delta["energy"] -= 0.15
        delta["valence"] -= 0.03
    if any(word in body for word in ("难过", "委屈", "想哭", "崩溃")):
        delta["valence"] -= 0.10
        delta["security"] -= 0.05
    if any(word in body for word in ("害怕", "焦虑", "紧张")):
        delta["arousal"] += 0.15
        delta["security"] -= 0.08
    if any(word in body for word in ("太好了", "开心", "高兴", "好耶")):
        delta["valence"] += 0.06

    strength = abs(delta["valence"]) + abs(delta["irritation"]) + abs(delta["security"])
    importance = clamp(
        0.20
        + 0.35 * strength
        + (0.12 if kind in STRONG_KINDS else 0.0)
        + (0.08 if urgent else 0.0),
        0.05,
        0.95,
    )
    return {"kind": kind, "delta": delta, "importance": round(importance, 3)}


def apply(
    state: dict[str, Any],
    appraisal: dict[str, Any],
    *,
    now: float | None = None,
    sender: str = "",
) -> None:
    """把评估结果落到 state；同时更新关系与调节状态。"""
    affect = state["affect"]
    relation = state["relation"]
    delta = appraisal.get("delta") or {}
    kind = str(appraisal.get("kind") or "message")

    for key in DEFAULT_AFFECT:
        affect[key] = clamp(
            _num(affect.get(key), DEFAULT_AFFECT[key]) + _num(delta.get(key), 0.0),
            -1.0 if key == "valence" else 0.0,
            1.0,
        )

    # 关系变化必须慢：一次对话不该把「刚认识」变成「很熟」。
    relation["familiarity"] = clamp(relation["familiarity"] + 0.004, 0.0, 0.95)
    if kind in ("praise", "reassurance", "outcome"):
        relation["trust"] = clamp(relation["trust"] + 0.010, 0.0, 1.0)
        relation["closeness"] = clamp(relation["closeness"] + 0.005, 0.0, 1.0)
        relation["tension"] = clamp(relation["tension"] - 0.06, 0.0, 1.0)
    elif kind in ("criticism", "rejection"):
        relation["trust"] = clamp(relation["trust"] - 0.012, 0.0, 1.0)
        relation["closeness"] = clamp(relation["closeness"] - 0.004, 0.0, 1.0)
        relation["tension"] = clamp(relation["tension"] + 0.10, 0.0, 1.0)

    stamp = time.time() if now is None else float(now)
    affect["updated_at"] = stamp
    state["meta"]["last_kind"] = kind
    state["meta"]["last_at"] = stamp
    if sender:
        state["meta"]["sender"] = sender


def dominant_of(state: dict[str, Any]) -> str:
    """把连续数值压成一个可读标签；只用于日志、display 与兜底模板。"""
    affect = state["affect"]
    relation = state["relation"]
    valence = affect["valence"]
    arousal = affect["arousal"]
    irritation = affect["irritation"]
    longing = affect["longing"]
    warmth = affect["warmth"]
    security = affect["security"]
    energy = affect["energy"]

    if irritation >= 0.55 and relation["trust"] < 0.55:
        return "烦躁"
    if longing >= 0.60 and security < 0.50:
        return "想念"
    if energy <= 0.30 and arousal <= 0.40:
        return "疲惫"
    if valence >= 0.30 and warmth >= 0.50:
        return "愉快"
    if security >= 0.60 and warmth >= 0.50:
        return "安心"
    if arousal >= 0.60 and valence < 0.0:
        return "警惕"
    if valence <= -0.22:
        return "低落"
    return "平静"


def intensity_of(state: dict[str, Any]) -> float:
    """主导强度：取几个最容易被看见的维度，避免被无关数值稀释。"""
    affect = state["affect"]
    return round(
        clamp(
            max(
                abs(affect["valence"]),
                affect["arousal"],
                affect["irritation"],
                affect["longing"],
                affect["warmth"] if affect["valence"] > 0 else 0.0,
            ),
            0.0,
            1.0,
        ),
        3,
    )


def strategy_of(state: dict[str, Any], *, kind: str, local_hour: float | None) -> str:
    """调节策略：决定「先压住」「愿意分享」还是「问清楚」。"""
    affect = state["affect"]
    relation = state["relation"]
    if kind in ("criticism", "rejection") and (
        affect["irritation"] >= 0.50 or relation["tension"] >= 0.45
    ):
        return "delay"
    if local_hour is not None and (local_hour < 6.0 or local_hour >= 23.0) and affect["energy"] <= 0.35:
        return "hold"
    if affect["warmth"] >= 0.60 and affect["security"] >= 0.45 and affect["irritation"] < 0.35:
        return "share"
    if affect["security"] <= 0.35:
        return "seek_info"
    if affect["irritation"] >= 0.35:
        return "soften"
    return "hold"


def update(
    state: dict[str, Any],
    *,
    kind: str,
    text: str,
    elapsed_hours: float,
    local_hour: float | None,
    interest_attention: float,
    now: float | None = None,
    sender: str = "",
) -> dict[str, Any]:
    """衰减 + 评估 + 落地，返回本轮 appraisal（便于写 episode）。"""
    decay(state, elapsed_hours, local_hour)
    appraisal = appraise(
        kind=kind,
        text=text,
        state=state,
        interest_attention=interest_attention,
    )
    apply(state, appraisal, now=now, sender=sender)
    return appraisal
