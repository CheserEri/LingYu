"""self 层：长期身份与原则（不是情绪状态）。

三条结构性约束：
1. 身份只由作者写，模型和情绪都不能改写它。
2. 原则只约束"如何判断与表达"，不预设任何情绪结论或答案。
3. 人格成长不在这里发生——它只发生在独立的全局人格札记里。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .paths import resource_dir

#: 内置身份目录（随包分发）。
IDENTITY_DIR = resource_dir() / "identity"
DEFAULT_ID = "violet"

#: 用户自定义身份目录：由宿主在启动时设置（``%APPDATA%\\lingyu\\identity``）。
#: 放在这里而不是写死，是因为内核不该知道宿主把用户数据放在哪。
_USER_DIR: Path | None = None


def set_user_identity_dir(path: str | Path | None) -> None:
    """设置用户身份目录；传 None 表示只用内置身份。"""
    global _USER_DIR
    _USER_DIR = Path(path) if path else None
    load_identity.cache_clear()


def identity_dirs() -> tuple[Path, ...]:
    """查找顺序：用户目录优先（同名可覆盖内置身份），再内置。"""
    return ((_USER_DIR,) if _USER_DIR else ()) + (IDENTITY_DIR,)


def list_identities() -> list[dict[str, str]]:
    """可供选择的身份清单（用户目录覆盖同名的内置身份）。"""
    found: dict[str, dict[str, str]] = {}
    for base in reversed(identity_dirs()):
        try:
            files = sorted(base.glob("*.json"))
        except OSError:
            continue
        for file in files:
            ident = _parse_or_none(file)
            if ident is None:
                continue
            if ident.id == "song_nuan":
                # 旧 id 只用于读取旧配置，不在新身份选择器中重复展示。
                continue
            found[ident.id] = {
                "id": ident.id,
                "name": ident.name,
                "status": ident.status,
                "source": str(file),
            }
    return sorted(found.values(), key=lambda item: item["id"])


def _parse_or_none(path: Path) -> "Identity | None":
    try:
        return _parse(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None



@dataclass(frozen=True)
class Identity:
    id: str = "neutral"
    name: str = "未命名"
    version: int = 0
    status: str = ""
    voice: dict = field(default_factory=dict)
    principles: tuple[dict, ...] = ()
    epistemic: dict = field(default_factory=dict)
    boundaries: dict = field(default_factory=dict)
    memory_policy: dict = field(default_factory=dict)
    interests: dict = field(default_factory=dict)
    self_narrative: str = ""
    style_samples: tuple[str, ...] = ()
    persona_policy: dict = field(default_factory=dict)

    # ---------- 表达 ----------

    @property
    def tone(self) -> tuple[str, ...]:
        return tuple(self.voice.get("tone") or ("平稳",))

    @property
    def avoid_words(self) -> tuple[str, ...]:
        """高风险措辞：不是绝对禁用，而是由 `core/wording.py` 按证据与心情判定。"""
        return tuple(self.voice.get("avoid") or ())

    @property
    def hedges(self) -> tuple[str, ...]:
        """限定词：低置信时用来把"猜测"标成"我的读法"。"""
        return tuple(self.voice.get("hedges")
                     or ("我觉得", "可能", "是不是", "听起来", "说不定"))

    @property
    def max_chars(self) -> int:
        return int(self.voice.get("max_chars") or 120)

    # ---------- 认知 ----------

    @property
    def min_alternatives(self) -> int:
        return int(self.epistemic.get("min_alternatives_when_uncertain") or 2)

    @property
    def uncertainty_threshold(self) -> float:
        return float(self.epistemic.get("uncertainty_threshold") or 0.5)

    # ---------- 边界 ----------

    @property
    def no_mind_reading(self) -> bool:
        return bool(self.boundaries.get("no_mind_reading", True))

    @property
    def respects_space(self) -> bool:
        return bool(self.boundaries.get("respect_space", True))

    @property
    def silence_cap(self) -> float:
        return float(self.boundaries.get("silence_intensity_cap") or 0.4)

    @property
    def silence_reopen_hours(self) -> float:
        """沉默多久之后允许重新开口；0 表示不主动重开。"""
        return float(self.boundaries.get("silence_reopen_hours") or 0.0)

    # ---------- 人格札记 ----------

    @property
    def persona_enabled(self) -> bool:
        return bool(self.persona_policy.get("enabled", True))

    @property
    def persona_min_evidence(self) -> int:
        return int(self.persona_policy.get("min_evidence") or 3)

    @property
    def persona_min_importance(self) -> float:
        return float(self.persona_policy.get("min_importance") or 0.45)

    @property
    def persona_read_limit(self) -> int:
        return int(self.persona_policy.get("read_limit") or 3)

    @property
    def persona_allow_llm(self) -> bool:
        return bool(self.persona_policy.get("allow_llm", False))

    # ---------- 记忆 ----------

    @property
    def memory_min_importance(self) -> float:
        return float(self.memory_policy.get("min_importance_to_keep") or 0.45)

    @property
    def record_then_belief(self) -> bool:
        return bool(self.memory_policy.get("record_then_belief", True))

    @property
    def allow_revision(self) -> bool:
        return bool(self.memory_policy.get("allow_revision", True))

    @property
    def review_prompt(self) -> str:
        return str(self.memory_policy.get("review_prompt")
                   or "当时为什么这样理解，现在是否仍然认同")

    # ---------- 兴趣（根源记忆） ----------

    @property
    def interest_roots(self) -> dict[str, float]:
        """根源兴趣：她生来偏向什么。键是兴趣类目 id，值 0~1。

        这是「根源记忆」的可计算部分——不是一段散文，而是一组能参与推导的数。
        它只出现在身份文件里，任何 Agent 都写不了（``self.*` 受保护），
        所以「她慢慢变得更爱聊机械」只能改 ``interest.values``，改不了这里。
        """
        raw = self.interests.get("roots") if isinstance(self.interests, dict) else None
        if not isinstance(raw, dict):
            return {}
        out: dict[str, float] = {}
        for key, value in raw.items():
            if isinstance(value, dict):
                value = value.get("value")
            try:
                out[str(key)] = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
        return out

    @property
    def interest_plasticity(self) -> float:
        """可塑性：经历最多能把兴趣推离根源多远（0~0.8）。

        0 表示「她不会变」，0.8 表示「几乎完全由经历决定」。默认 0.35——
        底色还在，但日子能改变她。
        """
        if not isinstance(self.interests, dict):
            return 0.35
        try:
            return float(self.interests.get("plasticity") or 0.35)
        except (TypeError, ValueError):
            return 0.35

    def interest_reason(self, category: str) -> str:
        """根源兴趣的来由（身份文件里写的一句话），用于界面与日志。"""
        raw = self.interests.get("roots") if isinstance(self.interests, dict) else None
        item = (raw or {}).get(category)
        if isinstance(item, dict):
            return str(item.get("why") or "")
        return ""

    # ---------- 输出 ----------


NEUTRAL = Identity()


def _parse(data: dict) -> Identity:
    persona = data.get("persona") if isinstance(data.get("persona"), dict) else {}
    samples = data.get("style_samples") or persona.get("style_samples") or ()
    return Identity(
        id=str(data.get("id") or "neutral"),
        name=str(data.get("name") or "未命名"),
        version=int(data.get("version") or 0),
        status=str(data.get("status") or ""),
        voice=dict(data.get("voice") or {}),
        principles=tuple(data.get("principles") or ()),
        epistemic=dict(data.get("epistemic") or {}),
        boundaries=dict(data.get("boundaries") or {}),
        memory_policy=dict(data.get("memory_policy") or {}),
        interests=dict(data.get("interests") or {}),
        self_narrative=str(data.get("self_narrative") or persona.get("self_narrative") or ""),
        style_samples=tuple(str(item) for item in samples),
        persona_policy=dict(persona.get("policy") or data.get("persona_policy") or {}),
    )


@lru_cache(maxsize=8)
def load_identity(identity_id: str = DEFAULT_ID) -> Identity:
    """加载身份。文件缺失或损坏时返回中性身份，绝不抛出异常打断运行。"""
    # song_nuan 是旧版配置中的身份 id；保留兼容，统一加载改名后的薇尔莉特。
    if identity_id == "song_nuan":
        identity_id = "violet"
    for base in identity_dirs():
        path = base / f"{identity_id}.json"
        if not path.is_file():
            continue
        ident = _parse_or_none(path)
        if ident is not None:
            return ident
    return NEUTRAL
