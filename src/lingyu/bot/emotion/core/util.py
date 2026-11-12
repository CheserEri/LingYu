"""通用工具函数。

只放无副作用的纯函数，保证 Agent 可重放（设计文档 §9 阶段二）。
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def sha(text: str, n: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def char_bigrams(text: str) -> set[str]:
    """中文没有空格，用字符二元组做粗糙的相似度。"""
    cleaned = "".join(ch for ch in text if ch.isalnum())
    if len(cleaned) < 2:
        return {cleaned}
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def overlap_score(a: str, b: str) -> float:
    """两个字符串的字符二元组 Jaccard 相似度，范围 [0, 1]。"""
    ga, gb = char_bigrams(a), char_bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def merge_unique(*groups) -> tuple:
    seen: list = []
    for group in groups:
        for item in group or ():
            if item not in seen:
                seen.append(item)
    return tuple(seen)
