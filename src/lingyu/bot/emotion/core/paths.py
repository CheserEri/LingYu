"""资源定位：源码运行与 PyInstaller 冻结包共用一套查找规则。

冻结包（onedir）里 ``__file__`` 落在 ``_internal/lingyu/bot/emotion/core/paths.py``，
而数据文件按 spec 的 ``datas`` 放在 ``_MEIPASS/lingyu/bot/emotion/`` 下。两者不一致，
直接用 ``Path(__file__).parent.parent`` 会在冻结包里找不到身份文件，而
``load_identity`` 是**静默回落**中性身份的——表现为「人设莫名其妙没了」且不报错。
所以统一走这里。
"""
from __future__ import annotations

import sys
from pathlib import Path

#: 包根目录（``.../bot/emotion``）：源码运行时就是它自己。
PACKAGE_DIR = Path(__file__).resolve().parent.parent


def frozen_root() -> Path | None:
    """PyInstaller 解包目录；源码运行时为 None。"""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def resource_dir() -> Path:
    """数据目录（``identity/`` 等）所在位置。"""
    root = frozen_root()
    if root is not None:
        candidate = root / "lingyu" / "bot" / "emotion"
        if candidate.is_dir():
            return candidate
        return root
    return PACKAGE_DIR


def resource_path(*parts: str) -> Path:
    return resource_dir().joinpath(*parts)
