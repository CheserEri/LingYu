"""混合人格内核：身份、心境、情节记忆、人格札记与表达。

宿主只需要记住一个入口：

    from lingyu.bot.emotion import EmotionBridge

    bridge = EmotionBridge(config, logger=engine.log)
    decision = await bridge.respond(
        chat_key="...", chat_name="张三", text="今天有点累", sender="张三",
        recent=["我: 在吗", "张三: 嗯"],
    )
    if not decision.silent:
        await send(decision.text)

``decision`` 里除了要发的话，还有心境、体感备忘、关系与人格札记摘要，方便
界面和日志复核「她为什么这么说」。
"""
from __future__ import annotations

from .affect import default_state, dominant_of, normalize_state
from .bridge import AsyncLLMAdapter, EmotionBridge, EmotionDecision
from .kind import classify, is_negative
from .memo import render_memo
from .pacing import Pacing, plan as pacing_plan
from .persona import PersonaLedger
from .runtime import DEFAULT_SEED, TurnResult, persistable, run_turn
from .session import SessionStore, default_state_dir

__all__ = [
    "AsyncLLMAdapter",
    "DEFAULT_SEED",
    "EmotionBridge",
    "EmotionDecision",
    "Pacing",
    "PersonaLedger",
    "SessionStore",
    "TurnResult",
    "classify",
    "default_state",
    "default_state_dir",
    "dominant_of",
    "is_negative",
    "normalize_state",
    "pacing_plan",
    "persistable",
    "render_memo",
    "run_turn",
]
