"""小红书卡片样张：真实链接端到端出图（展开短链 → 解析 → CardData → 渲染）。

用法（可在任意目录执行）::

    python scripts/dev/xhs_card_demo.py [分享链接或笔记地址]

产物落在 ``build/xhs-<noteId>.png``（``build/`` 已被 .gitignore 忽略）。
"""

from __future__ import annotations

import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image  # noqa: E402

from lingyu.bot import tweet, xiaohongshu  # noqa: E402
from lingyu.bot.card import note_to_card  # noqa: E402

DEFAULT_URL = "https://xhslink.cn/o/C9IHM7t3Ir"
OUT = ROOT / "build"


def log(level: str, message: str) -> None:
    print(f"[{level}] {message}")


def expand(url: str, timeout: float = 25.0) -> str:
    """短链展开（线上由 ``links.LinkResolver`` 做，这里手动走同样两步）。"""
    request = urllib.request.Request(
        url, headers={"User-Agent": xiaohongshu.user_agent()}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return xiaohongshu.unwrap_redirect(response.geturl())


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    target = url if xiaohongshu.extract_note_id(url) else expand(url)
    if target != url:
        print(f"展开后：{target}")

    started = time.time()
    note = xiaohongshu.note_for(target, timeout=25, logger=log)
    print(f"解析耗时 {time.time() - started:.1f}s ok={note.ok} err={note.error!r}")
    if not note.ok:
        return 1
    print(f"标题：{note.title}")
    print(f"作者：{note.uploader}  头像：{note.avatar[:60]}")
    print(f"类型：{note.kind_label}  赞：{note.likes}  评论：{note.comments}")

    card = note_to_card(note)
    print(
        f"卡片正文 {len(card.text)} 字，配图 {len(card.photos)} 张，"
        f"photo_total={card.photo_total}"
    )
    started = time.time()
    path = tweet.render_card(card, OUT, timeout=25, logger=log)
    print(f"渲染耗时 {time.time() - started:.1f}s -> {path}")
    if path:
        with Image.open(path) as img:
            print(f"尺寸：{img.size[0]}×{img.size[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
