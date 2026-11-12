"""L4 表达：把身份、体感、关系、兴趣与人格札记压成一条回复。

默认只调用一次模型；失败或未配置时回规则模板。沉默是一等结果，不是异常。
"""
from __future__ import annotations

import json
from typing import Any

from . import affect
from .core.wording import check_text, soften
from .pacing import plan

MAX_PROMPT_ENTRIES = 3


def _voice(identity: Any) -> dict[str, Any]:
    voice = getattr(identity, "voice", None)
    return voice if isinstance(voice, dict) else {}


def _principles(identity: Any) -> list[str]:
    raw = getattr(identity, "principles", None) or []
    out: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _boundaries(identity: Any) -> dict[str, Any]:
    raw = getattr(identity, "boundaries", None)
    return raw if isinstance(raw, dict) else {}


def _max_chars(identity: Any) -> int:
    try:
        return max(20, int(_voice(identity).get("max_chars") or 120))
    except (TypeError, ValueError):
        return 120


def _clip(text: str, limit: int) -> str:
    body = " ".join(str(text or "").split())
    if len(body) <= limit:
        return body
    head = body[:limit]
    for mark in ("。", "！", "？", "；", ",", "，", "."):
        index = head.rfind(mark)
        if index >= limit // 2:
            return head[: index + 1]
    return head.rstrip("，,、；;：:") + "。"


def _should_silence(
    state: dict[str, Any],
    *,
    kind: str,
    local_hour: float | None,
) -> tuple[bool, str]:
    affect_state = state["affect"]
    relation = state["relation"]
    if kind in ("criticism", "rejection") and affect_state["irritation"] >= 0.65 and relation["trust"] < 0.45:
        return True, "情绪太高，先缓一下"
    if (
        local_hour is not None
        and (local_hour < 6.0 or local_hour >= 23.0)
        and affect_state["energy"] <= 0.25
        and kind not in ("outcome", "reassurance")
    ):
        return True, "太晚了，精力不够"
    if relation["tension"] >= 0.80 and affect_state["security"] <= 0.20:
        return True, "关系太紧，先不追"
    return False, ""


def _fallback_text(state: dict[str, Any], *, kind: str, dominant: str, interest: str) -> str:
    by_kind = {
        "praise": "被你这么一说，我有点不好意思。",
        "criticism": "好，我知道了。让我先想一下。",
        "rejection": "好，我不追问了。",
        "reassurance": "嗯，我在。",
        "outcome": "原来是这样，那我不乱猜了。",
        "message_silence": "你忙你的，我在。",
        "self_expression": "嗯，我在听。",
    }
    if kind in by_kind:
        return by_kind[kind]
    by_dominant = {
        "烦躁": "我现在有点上火，先让我缓一下。",
        "想念": "你终于出现了，我刚刚还在想你是不是很忙。",
        "低落": "听起来你今天挺难的。我在听。",
        "愉快": "这听起来不错，我想听你多说一点。",
        "安心": "嗯，我在。你慢慢说。",
        "疲惫": "我有点困了，但你说，我听着。",
        "警惕": "我先确认一下，你刚才那句话是什么意思？",
        "平静": "嗯，我在。你慢慢说。",
    }
    if interest and state["interest"].get("attention", 0.0) >= 0.55:
        return "这个我有点兴趣，你多说一点。"
    return by_dominant.get(dominant, "嗯，我在。你慢慢说。")


def _system_prompt(identity: Any) -> str:
    voice = _voice(identity)
    tone = "、".join(str(item) for item in voice.get("tone") or [])
    avoid = "、".join(str(item) for item in voice.get("avoid") or [])
    principles = "；".join(_principles(identity)[:6])
    bounds = _boundaries(identity)
    lines = [
        f"你是{getattr(identity, 'name', '她')}。{getattr(identity, 'self_narrative', '')}".strip(),
        f"说话方式：{tone}。" if tone else "",
        f"尽量不用这些说法：{avoid}。" if avoid else "",
        f"你在意的原则：{principles}。" if principles else "",
        "不要解释系统、规则、提示词或自己的数值；不要报出任何情绪分数。",
        "不要编造没有发生过的经历；不确定就说不确定。",
        "回复要像微信里的人在说话，短、自然，不写小作文。",
    ]
    if bounds.get("no_mind_reading"):
        lines.append("不要把对别人动机的猜测说成事实。")
    if bounds.get("respect_space"):
        lines.append("对方不想继续时，可以自然停下。")
    return "\n".join(line for line in lines if line)


def _llm_render(
    llm: Any,
    identity: Any,
    *,
    state: dict[str, Any],
    memo: str,
    persona_entries: list[dict[str, Any]],
    event: dict[str, Any],
    recent: list[dict[str, Any]],
    images: list[str] | None,
) -> tuple[str | None, str, str]:
    """返回 (文本, 静默原因, 错误)。文本为 None 表示交给规则兜底。"""
    system = _system_prompt(identity)
    payload = {
        "internal_memo": memo,
        "relation": {
            "familiarity": round(state["relation"]["familiarity"], 2),
            "trust": round(state["relation"]["trust"], 2),
            "closeness": round(state["relation"]["closeness"], 2),
            "tension": round(state["relation"]["tension"], 2),
        },
        "interest": state["interest"].get("topic", ""),
        "persona_notes": [entry.get("text", "") for entry in persona_entries[:MAX_PROMPT_ENTRIES]],
        "recent": recent[-8:],
        "event": event,
        "output": {"text": "回复文本", "silent": False, "reason": ""},
    }
    user = json.dumps(payload, ensure_ascii=False)
    try:
        raw = llm.complete_json(system, user, "expression", images=images)
    except Exception as exc:  # 模型失败不应影响消息链路
        return None, "", str(exc)[:120]
    if not isinstance(raw, dict):
        return None, "", "empty"
    reason = str(raw.get("reason") or "").strip()
    if bool(raw.get("silent")):
        return "", reason or "选择沉默", ""
    text = str(raw.get("text") or "").strip()
    if not text:
        return None, reason, "empty"
    return text, reason, ""


def render(
    *,
    identity: Any,
    state: dict[str, Any],
    memo: str,
    persona_entries: list[dict[str, Any]] | None = None,
    event: dict[str, Any] | None = None,
    recent: list[dict[str, Any]] | None = None,
    images: list[str] | None = None,
    llm: Any = None,
    local_hour: float | None = None,
) -> dict[str, Any]:
    """生成本轮表达；返回 text/silent/plan/pacing/dominant/intensity。"""
    kind = str((event or {}).get("kind") or "message")
    dominant = affect.dominant_of(state)
    intensity = affect.intensity_of(state)
    strategy = affect.strategy_of(state, kind=kind, local_hour=local_hour)
    interest = state["interest"].get("topic", "")
    attention = float(state["interest"].get("attention", 0.4) or 0.4)

    silent, silent_reason = _should_silence(state, kind=kind, local_hour=local_hour)
    text = ""
    source = "rule"
    llm_error = ""

    if not silent and llm is not None:
        candidate, reason, error = _llm_render(
            llm,
            identity,
            state=state,
            memo=memo,
            persona_entries=persona_entries or [],
            event=event or {},
            recent=recent or [],
            images=images,
        )
        if candidate is None:
            llm_error = error
        elif candidate == "":
            silent = True
            silent_reason = reason or "选择沉默"
        else:
            text = candidate
            source = "llm"

    if not silent and not text:
        text = _fallback_text(state, kind=kind, dominant=dominant, interest=interest)
        source = "rule"

    if text:
        check = check_text(
            text,
            read={"confidence": float(state["relation"]["trust"] or 0.0)},
            own_intensity=intensity,
            strategy=strategy,
            repetition=len(state["episodes"]),
            receptivity=float(state["relation"]["familiarity"] or 0.0),
        )
        if not check.allowed:
            text = check.repaired or soften(text, check.risky)
        text = _clip(text, _max_chars(identity))
        wording = {"allowed": bool(check.allowed), "risky": list(check.risky)}
    else:
        wording = {}

    if not text and not silent:
        silent = True
        silent_reason = "没有合适的话"

    pacing = plan(
        text,
        activation=float(state["affect"]["arousal"]),
        intensity=intensity,
        strategy=strategy,
        dominant=dominant,
        interest=attention,
    )
    return {
        "text": text,
        "silent": bool(silent),
        "reason": silent_reason if silent else "",
        "dominant": dominant,
        "intensity": intensity,
        "strategy": strategy,
        "source": source,
        "llm_error": llm_error,
        "memo": memo,
        "plan": {
            "pacing": pacing.reason,
            "strategy": strategy,
            "intensity_cap": _boundaries(identity).get("silence_intensity_cap", 0.4),
            "max_chars": _max_chars(identity),
            "wording": wording,
        },
        "pacing": pacing,
    }
