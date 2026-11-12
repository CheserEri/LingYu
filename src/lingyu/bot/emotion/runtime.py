"""一轮混合人格周期：心境 → 评估 → 情节记忆 → 人格札记 → 表达。

与旧版的区别是这里不再有十个 Agent、黑板和五阶段轮次。所有跨轮状态都在
``state`` 里，所有长期人格都在独立的 ``PersonaLedger`` 里。宿主只需要：

- 传入当前消息与会话 state；
- 传入一个可选的 ``ledger``；
- 读取返回的 ``text`` / ``silent`` / ``pacing``。

这一层保持同步：宿主把它丢进线程池即可。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from . import affect, episode, expression, memo
from .core import interests
from .core.identity import load_identity
from .persona import PersonaLedger
from .writer import consider

#: 新会话的种子：心境与关系都从「刚认识不久」开始。
DEFAULT_SEED: dict[str, Any] = affect.default_state()


@dataclass
class TurnResult:
    """一轮周期的结果：对外只有 text / silent / pacing 是必须读的。"""

    snapshot: dict[str, Any]
    rounds: int = 1
    converged: bool = True
    trace: list[dict] = field(default_factory=list)
    turn: int = 1

    @property
    def text(self) -> str:
        payload = self.snapshot.get("expression") or {}
        return str(payload.get("text") or "")

    @property
    def silent(self) -> bool:
        payload = self.snapshot.get("expression") or {}
        return bool(payload.get("silent"))


def persistable(snapshot: dict[str, Any]) -> dict[str, Any]:
    """只把跨轮有意义的状态交还给宿主；表达结果与 display 不落盘。"""
    state = snapshot.get("state")
    return state if isinstance(state, dict) else {}


def _local_hour() -> float:
    now = datetime.now()
    return now.hour + now.minute / 60.0


def _evolve_interest(
    state: dict[str, Any],
    identity: Any,
    text: str,
) -> tuple[str, float, list[dict[str, Any]]]:
    """更新本轮注意度与长期兴趣值；返回 (话题, 注意度, 排行)。"""
    roots = interests.roots_of(identity)
    values = state["interest"].get("values")
    if not isinstance(values, dict) or not values:
        values = dict(roots)
    else:
        for cat_id, root in roots.items():
            values[cat_id] = interests.pull_toward_root(root, float(values.get(cat_id, root)))

    matches = interests.detect(text)
    recent_topics = list(state["meta"].get("recent_topics") or [])
    if not matches:
        state["interest"] = {
            "values": values,
            "topic": "",
            "attention": 0.4,
            "note": "",
        }
        return "", 0.4, interests.top(values, 3)

    topic, match = matches[0]
    satiety = interests.satiety(recent_topics, topic)
    value = float(values.get(topic, roots.get(topic, 0.4)))
    attention = interests.attention_of(value, match, satiety_factor=satiety)
    plasticity = interests.plasticity_of(identity)
    values[topic] = interests.evolve(
        roots.get(topic, value),
        value,
        float(state["affect"]["valence"]),
        plasticity=plasticity,
    )
    recent_topics.append(topic)
    state["meta"]["recent_topics"] = recent_topics[-6:]
    state["interest"] = {
        "values": values,
        "topic": topic,
        "attention": attention,
        "note": interests.CATEGORY_NAMES.get(topic, topic),
    }
    return topic, attention, interests.top(values, 3)


def _display(
    state: dict[str, Any],
    expr: dict[str, Any],
    memo_text: str,
    persona_result: dict[str, Any],
    persona_entries: list[dict[str, Any]],
    interest_top: list[dict[str, Any]],
) -> dict[str, Any]:
    affect_state = state["affect"]
    relation = state["relation"]
    return {
        "dominant": expr.get("dominant", "平静"),
        "intensity": expr.get("intensity", 0.0),
        "strategy": expr.get("strategy", "hold"),
        "memo": memo_text,
        "source": expr.get("source", "rule"),
        "affect": {
            key: round(float(affect_state.get(key, 0.0)), 3)
            for key in ("valence", "arousal", "longing", "irritation",
                        "warmth", "sharing", "security", "energy")
        },
        "relation": {
            key: round(float(relation.get(key, 0.0)), 3)
            for key in ("familiarity", "trust", "closeness", "tension")
        },
        "interest": {
            "topic": state["interest"].get("note") or state["interest"].get("topic", ""),
            "attention": round(float(state["interest"].get("attention", 0.4)), 3),
            "top": interest_top,
        },
        "persona": {
            "status": persona_result.get("status", ""),
            "entries": [
                {
                    "id": entry.get("id"),
                    "text": entry.get("text"),
                    "created_day": entry.get("created_day"),
                    "confidence": entry.get("confidence"),
                }
                for entry in persona_entries
            ],
        },
        "wording": (expr.get("plan") or {}).get("wording", {}),
    }


def run_turn(
    *,
    text: str,
    state: dict[str, Any] | None = None,
    kind: str = "message",
    elapsed: float = 0.0,
    turn: int = 1,
    sender: str = "对方",
    identity_id: str = "violet",
    llm: Any = None,
    recent: list[Any] | None = None,
    images: list[str] | None = None,
    local_hour: float | None = None,
    chat_key_hash: str = "",
    ledger: PersonaLedger | None = None,
    persona_episodes: list[dict[str, Any]] | None = None,
    today: str | None = None,
    mode: str = "rule",
) -> TurnResult:
    """跑一轮混合人格周期，返回结果与可持久化的新状态。"""
    identity = load_identity(identity_id)
    normalized = affect.normalize_state(state)
    normalized["meta"]["turn"] = max(1, int(turn or 1))
    hour = _local_hour() if local_hour is None else float(local_hour)
    now = time.time()

    appraisal = affect.update(
        normalized,
        kind=kind,
        text=text,
        elapsed_hours=elapsed,
        local_hour=hour,
        interest_attention=float(normalized["interest"].get("attention", 0.4) or 0.4),
        now=now,
        sender=sender,
    )
    dominant = affect.dominant_of(normalized)

    episode.review_outcome(normalized, kind=kind, now=now)
    episode.append(
        normalized,
        kind=kind,
        text=text,
        appraisal=appraisal,
        dominant=dominant,
        now=now,
    )
    _topic, _attention, interest_top = _evolve_interest(normalized, identity, text)

    persona_result: dict[str, Any] = {"status": "disabled"}
    persona_entries: list[dict[str, Any]] = []
    evidence = episode.with_chat(normalized.get("episodes") or [], chat_key_hash)
    for item in persona_episodes or []:
        if isinstance(item, dict):
            evidence.append(item)
    if ledger is not None:
        persona_result = consider(
            identity=identity,
            ledger=ledger,
            episodes=evidence,
            day=today or date.today().isoformat(),
            llm=llm,
            mode=mode,
            current_text=text,
            now=now,
        )
        persona_entries = ledger.active(limit=identity.persona_read_limit)
    else:
        persona_entries = []

    memo_text = memo.render_memo(normalized)
    expr = expression.render(
        identity=identity,
        state=normalized,
        memo=memo_text,
        persona_entries=persona_entries,
        event={
            "kind": kind,
            "text": text,
            "sender": "" if kind == "self_expression" else sender,
            "elapsed_hours": float(elapsed or 0.0),
            "local_hour": hour,
        },
        recent=list(recent or []),
        images=images,
        llm=llm,
        local_hour=hour,
    )

    display = _display(normalized, expr, memo_text, persona_result, persona_entries, interest_top)
    return TurnResult(
        snapshot={
            "state": normalized,
            "expression": expr,
            "display": display,
            "persona": persona_result,
            "interest": interest_top,
        },
        rounds=1,
        converged=True,
        trace=[],
        turn=int(turn or 1),
    )


def recent_topics(state: dict[str, Any]) -> list[str]:
    """供外部读取最近话题；避免调用方直接摸 meta 结构。"""
    items = state.get("meta", {}).get("recent_topics")
    return [str(item) for item in items] if isinstance(items, list) else []
