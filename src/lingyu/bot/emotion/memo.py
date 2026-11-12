"""把心境数值翻译成「不带数字的体感」。

模型不该看见 ``valence=0.72``，它只需要知道「心里是松的」。这里只做定性映射：

- 最多两句、总长不超过 80 字；
- 不出现阿拉伯数字、字段名、括号标签；
- 不替模型下结论，只描述此刻的倾向。
"""
from __future__ import annotations

from typing import Any

MAX_CHARS = 80
MAX_CLAUSES = 2
DEFAULT_MEMO = "还算平静，愿意听你说。"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def render_memo(state: dict[str, Any]) -> str:
    """从 affect/relation 生成一到两句体感备忘。"""
    affect = state.get("affect") if isinstance(state.get("affect"), dict) else {}
    relation = state.get("relation") if isinstance(state.get("relation"), dict) else {}

    valence = _f(affect.get("valence"))
    arousal = _f(affect.get("arousal"), 0.35)
    longing = _f(affect.get("longing"))
    irritation = _f(affect.get("irritation"))
    warmth = _f(affect.get("warmth"), 0.45)
    sharing = _f(affect.get("sharing"), 0.4)
    security = _f(affect.get("security"), 0.5)
    energy = _f(affect.get("energy"), 0.6)
    tension = _f(relation.get("tension"))

    clauses: list[str] = []
    if irritation >= 0.55:
        clauses.append("语气会比平时短一点")
    if longing >= 0.60:
        clauses.append("有点想先开口" if security >= 0.45 else "有点想先开口，又怕打扰")
    if security <= 0.30:
        clauses.append("不太确定对方现在想不想听")
    if valence <= -0.35:
        clauses.append("心里有点沉")
    if arousal >= 0.70 and valence < 0.0:
        clauses.append("坐不太住")
    if warmth >= 0.65 and irritation < 0.35:
        clauses.append("心里是松的，愿意多听一会儿")
    if sharing >= 0.70 and irritation < 0.45:
        clauses.append("愿意多讲一点")
    if energy <= 0.30:
        clauses.append("话不太多")
    if tension >= 0.50:
        clauses.append("说话会先绕一下")
    if not clauses:
        clauses.append("还算平静")

    picked = clauses[:MAX_CLAUSES]
    memo = "，".join(picked) + "。"
    if len(memo) > MAX_CHARS:
        memo = memo[: MAX_CHARS - 1].rstrip("，、") + "。"
    return memo or DEFAULT_MEMO
