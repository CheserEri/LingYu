"""消息平台适配层：桌面微信与 QQ 机器人（官方 API v2）。"""

from __future__ import annotations

from typing import Any

from ..config import AdapterConfig
from .qq import QQBotAdapter
from .wechat import WeChatAdapter, load_wechat_bridge

__all__ = [
    "WeChatAdapter",
    "QQBotAdapter",
    "load_wechat_bridge",
    "create_adapter",
]


def create_adapter(config: AdapterConfig | None = None) -> Any:
    """按 ``config.platform`` 创建适配器（wechat / qq）。"""
    cfg = config or AdapterConfig.from_env()
    if cfg.is_qq():
        return QQBotAdapter(cfg)
    return WeChatAdapter(cfg)
