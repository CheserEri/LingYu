"""常驻自动回复 bot：实时监听 → 情绪引擎（九个 Agent）→ 有理由的回复/沉默。"""

from .config import BotConfig, default_config_path
from .engine import BotEngine
from .llm import LLMClient, LLMError

__all__ = [
    "BotConfig",
    "BotEngine",
    "LLMClient",
    "LLMError",
    "default_config_path",
]
