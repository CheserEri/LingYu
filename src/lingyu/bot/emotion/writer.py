"""人格札记的写入策略：规则筛选优先，LLM 只在严格边界内写一句。

它不负责「让模型自由反思」。它只做四件事：

1. 从情节记忆里挑出够重要、没被推翻的证据；
2. 同一类读法攒够证据才允许写；
3. 每天最多写一条；没有合格证据就明确跳过；
4. LLM 的文本必须通过来源、长度、措辞与反例校验，否则回规则模板。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .persona import MAX_TEXT, PersonaLedger
from .core.util import clamp

DEDUPE_DAYS = 30
FORBIDDEN = ("系统", "规则", "提示词", "模型", "AI", "人工智能", "用户")


@dataclass
class Candidate:
    signature: str
    kind: str
    dominant: str
    episodes: list[dict[str, Any]] = field(default_factory=list)
    confirmed: int = 0
    refuted: int = 0
    total: int = 0

    @property
    def eligible(self) -> int:
        return max(0, self.total - self.refuted)

    @property
    def score(self) -> float:
        return self.eligible + 0.5 * self.confirmed - 0.25 * self.refuted


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    body = " ".join(str(value or "").split())
    return body[:limit]


def collect_candidates(
    episodes: list[dict[str, Any]],
    *,
    min_importance: float = 0.45,
    min_evidence: int = 3,
) -> list[Candidate]:
    """按 (kind, dominant) 分组，返回够格的候选，按证据强度排序。"""
    groups: dict[str, Candidate] = {}
    for episode in episodes:
        if not isinstance(episode, dict):
            continue
        if _num(episode.get("importance"), 0.0) < min_importance:
            continue
        kind = str(episode.get("kind") or "message")
        dominant = str(episode.get("dominant") or "平静")
        signature = f"{kind}|{dominant}"
        candidate = groups.get(signature)
        if candidate is None:
            candidate = Candidate(signature=signature, kind=kind, dominant=dominant)
            groups[signature] = candidate
        candidate.total += 1
        verdict = (episode.get("now_review") or {}).get("verdict")
        if verdict == "confirmed":
            candidate.confirmed += 1
        elif verdict == "refuted":
            candidate.refuted += 1
        else:
            candidate.episodes.append(episode)

    out = [item for item in groups.values() if item.eligible >= max(1, int(min_evidence))]
    out.sort(key=lambda item: (-item.score, item.signature))
    return out


def rule_text(kind: str, dominant: str) -> str:
    """确定性模板：宁可朴素，也不替模型编造具体经历。"""
    table = {
        ("criticism", "烦躁"): "被顶回来的时候，我会先上火；先停一下，比立刻回嘴更有用。",
        ("criticism", "低落"): "被指出问题时，我会先往自己身上收；先分清是事还是人。",
        ("criticism", "平静"): "被指出问题时，我会先紧一下；但对方说的不一定是在否定我。",
        ("rejection", "低落"): "被推开的时候，我会先怀疑自己；但拒绝不一定是否定我。",
        ("rejection", "烦躁"): "被拒绝时，我会先有点冲；先缓一下，别把难受变成回击。",
        ("praise", "愉快"): "被认真夸的时候，我会不好意思；我可以先接住，而不是急着否认。",
        ("praise", "安心"): "被夸的时候，我会先愣一下；原来我做好的地方也有人看见。",
        ("reassurance", "安心"): "被安抚之后，我会慢慢松下来；原来不用马上证明自己。",
        ("reassurance", "低落"): "被安抚的时候，我会先想躲；但有人愿意接住，也没那么可怕。",
        ("message_silence", "想念"): "安静下来的时候，我会先担心；但沉默不等于答案，我可以先问一句。",
        ("message_silence", "警惕"): "对方不说话时，我会先往坏处想；这只是我的读法，不是事实。",
        ("self_expression", "愉快"): "我愿意讲自己的时候，心里是亮的；分享不会让我更脆弱。",
        ("self_expression", "安心"): "讲自己的时候，我会先看对方反应；被接住之后，才敢多讲一点。",
        ("outcome", "安心"): "事情说开之后，我会发现很多担心是自己补出来的；先问，比先猜好。",
    }
    return table.get(
        (kind, dominant),
        f"在{kind}反复出现时，我发现自己会先{dominant}；先看清，再决定怎么回应。",
    )


def rule_counterexample(candidate: Candidate) -> str:
    if candidate.refuted > 0:
        return "也有几次，事情并不是这样。"
    return "这只是我目前的读法。"


def _valid_text(text: str) -> bool:
    if not 4 <= len(text) <= MAX_TEXT:
        return False
    if "\n" in text or "\r" in text:
        return False
    if re.search(r"\d", text):
        return False
    if any(word in text for word in FORBIDDEN):
        return False
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _active_sources(candidate: Candidate) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for episode in candidate.episodes[:6]:
        sources.append(
            {
                "chat": str(episode.get("chat") or ""),
                "episode": str(episode.get("id") or ""),
                "kind": str(episode.get("kind") or ""),
                "at": _num(episode.get("at"), 0.0),
            }
        )
    return sources


def _normalized(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?；;：:\"'“”‘’]", "", text or "")


def _llm_proposal(
    llm: Any,
    identity: Any,
    candidate: Candidate,
    active: list[dict[str, Any]],
    current_text: str,
) -> dict[str, Any] | None:
    """让模型写一句，但只允许它返回一个严格 JSON。"""
    system = (
        "你在为一个人格账本写一句自我认识。只输出 JSON，不要解释。\n"
        "规则：\n"
        "1. 用第一人称，写一句不超过六十个汉字的话；\n"
        "2. 不出现阿拉伯数字、系统、规则、提示词、模型等词；\n"
        "3. 必须带一个反例，或把 uncertainty 设为不低于 0.2；\n"
        "4. 不能编造具体人物、地点、事件；只能依据给出的证据类型；\n"
        "5. 如果证据不足，返回 {\"action\":\"skip\",\"reason\":\"...\"}。"
    )
    payload = {
        "identity": {
            "name": getattr(identity, "name", ""),
            "self_narrative": getattr(identity, "self_narrative", ""),
            "principles": list(getattr(identity, "principles", []) or [])[:5],
        },
        "existing": [entry.get("text", "") for entry in active],
        "candidate": {
            "signature": candidate.signature,
            "kind": candidate.kind,
            "dominant": candidate.dominant,
            "evidence_count": candidate.total,
            "confirmed": candidate.confirmed,
            "refuted": candidate.refuted,
        },
        "current_event": _text(current_text, 120),
        "output": {
            "action": "write|skip",
            "text": "一句自我认识",
            "counterexample": "一个反例或不确定之处",
            "uncertainty": 0.2,
            "confidence": 0.6,
            "sources": ["ep-0001"],
        },
    }
    try:
        raw = llm.complete_json(system, json.dumps(payload, ensure_ascii=False), "persona")
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action") or "").strip().lower()
    if action == "skip":
        return {"action": "skip", "reason": _text(raw.get("reason"), 80) or "证据不足"}
    if action != "write":
        return None

    text = _text(raw.get("text"))
    if not _valid_text(text):
        return None
    counterexample = _text(raw.get("counterexample"), 80)
    uncertainty = clamp(_num(raw.get("uncertainty"), 0.0), 0.0, 1.0)
    if not counterexample and uncertainty < 0.2:
        return None
    confidence = clamp(_num(raw.get("confidence"), 0.5), 0.0, 1.0)

    allowed = {str(item.get("id")) for item in candidate.episodes}
    sources = raw.get("sources")
    if not isinstance(sources, list):
        return None
    picked: list[dict[str, Any]] = []
    for item in sources:
        episode_id = item if isinstance(item, str) else (item or {}).get("episode") if isinstance(item, dict) else ""
        if str(episode_id) not in allowed:
            continue
        source = next(
            (ep for ep in candidate.episodes if str(ep.get("id")) == str(episode_id)),
            None,
        )
        if source:
            picked.append(
                {
                    "chat": str(source.get("chat") or ""),
                    "episode": str(source.get("id") or ""),
                    "kind": str(source.get("kind") or ""),
                    "at": _num(source.get("at"), 0.0),
                }
            )
    if not picked:
        return None
    return {
        "action": "write",
        "text": text,
        "counterexample": counterexample,
        "uncertainty": uncertainty,
        "confidence": confidence,
        "sources": picked,
    }


def consider(
    *,
    identity: Any,
    ledger: PersonaLedger,
    episodes: list[dict[str, Any]],
    day: str | None = None,
    llm: Any = None,
    mode: str = "rule",
    current_text: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """每天最多一次：写一条札记，或明确跳过。"""
    today = day or date.today().isoformat()
    if not bool(getattr(identity, "persona_enabled", True)):
        return {"status": "disabled"}
    if ledger.has_written_today(today):
        return {"status": "already_written"}

    min_importance = float(getattr(identity, "persona_min_importance", 0.45) or 0.45)
    min_evidence = int(getattr(identity, "persona_min_evidence", 3) or 3)
    candidates = collect_candidates(
        episodes,
        min_importance=min_importance,
        min_evidence=min_evidence,
    )
    if not candidates:
        ledger.record_day(today, "skipped", reason="证据不足")
        return {"status": "skipped", "reason": "证据不足"}

    candidate = candidates[0]
    active = ledger.active(limit=int(getattr(identity, "persona_read_limit", 3) or 3))
    proposal = None
    use_llm = (
        mode == "llm"
        and llm is not None
        and bool(getattr(identity, "persona_allow_llm", False))
    )
    if use_llm:
        proposal = _llm_proposal(llm, identity, candidate, active, current_text)
        if proposal and proposal.get("action") == "skip":
            ledger.record_day(today, "skipped", reason=proposal.get("reason") or "证据不足")
            return {"status": "skipped", "reason": proposal.get("reason") or "证据不足"}

    if proposal and proposal.get("action") == "write":
        text = proposal["text"]
        counterexample = proposal.get("counterexample", "")
        uncertainty = proposal.get("uncertainty", 0.0)
        confidence = proposal.get("confidence", 0.5)
        sources = proposal.get("sources") or _active_sources(candidate)
        by = "llm"
    else:
        text = rule_text(candidate.kind, candidate.dominant)
        counterexample = rule_counterexample(candidate)
        uncertainty = round(clamp(1.0 - candidate.confirmed / max(1, candidate.total), 0.0, 1.0), 3)
        confidence = round(clamp(candidate.confirmed / max(1, candidate.total), 0.0, 1.0), 3)
        sources = _active_sources(candidate)
        by = "rule"

    normalized = _normalized(text)
    if not normalized:
        ledger.record_day(today, "skipped", reason="空文本")
        return {"status": "skipped", "reason": "空文本"}
    for entry in active:
        if _normalized(entry.get("text", "")) == normalized:
            ledger.record_day(today, "skipped", reason="重复")
            return {"status": "skipped", "reason": "重复"}
        if entry.get("signature") == candidate.signature:
            stamp = _num(entry.get("created_at"), 0.0)
            current = time.time() if now is None else float(now)
            if current - stamp <= DEDUPE_DAYS * 86400:
                ledger.record_day(today, "skipped", reason="同一读法已存在")
                return {"status": "skipped", "reason": "同一读法已存在"}

    entry = ledger.append(
        text=text,
        signature=candidate.signature,
        sources=sources,
        counterexample=counterexample,
        uncertainty=uncertainty,
        confidence=confidence,
        day=today,
        by=by,
        now=now,
    )
    ledger.record_day(today, "written", entry=entry["id"])
    return {
        "status": "written",
        "entry": entry,
        "candidate": {
            "signature": candidate.signature,
            "total": candidate.total,
            "confirmed": candidate.confirmed,
            "refuted": candidate.refuted,
        },
    }
