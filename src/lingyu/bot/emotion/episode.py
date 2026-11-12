"""L2 情节记忆：有界的、可回填结果的事件记录。

它不是聊天记录的替代品，只保留「对人格有意义」的片段：

- 每条都有 ``importance``，低影响事件不进来；
- 当时的读法写在 ``then_belief``，后来是否成立写在 ``now_review``；
- 被事后推翻的读法不会进入人格札记；
- 每个会话最多 60 条，避免长期运行后状态无限膨胀。
"""
from __future__ import annotations

import time
from typing import Any

from .affect import NEGATIVE_DOMINANTS, POSITIVE_DOMINANTS
from .core.util import clamp

MAX_EPISODES = 60
MAX_EXCERPT = 60
MIN_IMPORTANCE = 0.35

SETTLE_KINDS = frozenset({"outcome", "reassurance", "praise", "rejection", "criticism"})


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _excerpt(text: str) -> str:
    body = " ".join(str(text or "").split())
    return body[:MAX_EXCERPT]


def append(
    state: dict[str, Any],
    *,
    kind: str,
    text: str,
    appraisal: dict[str, Any],
    dominant: str,
    now: float | None = None,
) -> dict[str, Any] | None:
    """把一条高影响事件写入 episodes；低影响事件直接丢弃。"""
    importance = round(clamp(_num(appraisal.get("importance"), 0.0), 0.0, 1.0), 3)
    if importance < MIN_IMPORTANCE:
        return None

    stamp = time.time() if now is None else float(now)
    seq = max(int(_num(state.get("episode_count"), 0.0)), len(state.get("episodes") or [])) + 1
    affect = state.get("affect") or {}
    episode = {
        "id": f"ep-{seq:04d}",
        "at": stamp,
        "kind": str(kind or "message"),
        "importance": importance,
        "dominant": str(dominant or "平静"),
        "excerpt": _excerpt(text),
        "then_belief": {
            "dominant": str(dominant or "平静"),
            "valence": round(_num(affect.get("valence"), 0.0), 3),
            "irritation": round(_num(affect.get("irritation"), 0.0), 3),
        },
        "now_review": None,
    }
    episodes = list(state.get("episodes") or [])
    episodes.append(episode)
    state["episodes"] = episodes[-MAX_EPISODES:]
    state["episode_count"] = seq
    return episode


def review_outcome(
    state: dict[str, Any],
    *,
    kind: str,
    now: float | None = None,
) -> str | None:
    """用后续事件回填上一条未定读法：证实、推翻或仍不确定。"""
    if kind not in SETTLE_KINDS:
        return None
    episodes = list(state.get("episodes") or [])
    if not episodes:
        return None

    stamp = time.time() if now is None else float(now)
    for episode in reversed(episodes[-6:]):
        if episode.get("now_review"):
            continue
        prior = str((episode.get("then_belief") or {}).get("dominant") or "")
        if kind in ("outcome", "reassurance") and prior in NEGATIVE_DOMINANTS:
            verdict = "refuted"
        elif kind in ("praise", "reassurance") and prior in POSITIVE_DOMINANTS:
            verdict = "confirmed"
        elif kind in ("rejection", "criticism") and prior in POSITIVE_DOMINANTS:
            verdict = "refuted"
        else:
            verdict = "uncertain"
        episode["now_review"] = {"verdict": verdict, "at": stamp, "by": kind}
        state["episodes"] = episodes
        return verdict
    return None


def pending(
    state: dict[str, Any],
    *,
    min_importance: float = 0.45,
) -> list[dict[str, Any]]:
    """可以作为人格札记证据的情节：够重要，且没有被推翻。"""
    out: list[dict[str, Any]] = []
    for episode in state.get("episodes") or []:
        if not isinstance(episode, dict):
            continue
        if _num(episode.get("importance"), 0.0) < min_importance:
            continue
        verdict = (episode.get("now_review") or {}).get("verdict")
        if verdict == "refuted":
            continue
        out.append(episode)
    return out


def with_chat(episodes: list[dict[str, Any]], chat_hash: str) -> list[dict[str, Any]]:
    """给来自不同会话的证据加上来源哈希，账本只存引用不存原文。"""
    out: list[dict[str, Any]] = []
    for episode in episodes:
        if not isinstance(episode, dict):
            continue
        item = dict(episode)
        item["chat"] = str(chat_hash or "")
        out.append(item)
    return out
