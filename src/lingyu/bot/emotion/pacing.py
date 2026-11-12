"""宿主侧发送节奏 API，转发至 Emotion 内核的表达节奏实现。

保留该模块路径，供 LingYu 的发送端与公开 emotion 包接口使用。
"""
from .core.pacing import MAX_LEAD, Pacing, plan

__all__ = ["MAX_LEAD", "Pacing", "plan"]
