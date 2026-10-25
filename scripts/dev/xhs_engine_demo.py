"""端到端：把真实分享短链丢进引擎，走「展开 → 解析 → 渲染卡片 → 发图」全链路。

用法（可在任意目录执行，需要联网）::

    python scripts/dev/xhs_engine_demo.py [分享链接]

用的是一个假的适配层：不发微信，只把「本该发出去的文件」打印出来。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image  # noqa: E402

from lingyu.bot import BotConfig, BotEngine  # noqa: E402
from lingyu.bot.llm import LLMClient  # noqa: E402
from lingyu.schemas import (  # noqa: E402
    ConnectionState,
    MessageRecord,
    SendResult,
    SendStatus,
    StatusResult,
)

DEFAULT_URL = "https://xhslink.cn/o/C9IHM7t3Ir"


class _FakeAdapter:
    """只实现引擎用到的适配层接口，把发送结果记下来。"""

    config = None

    def __init__(self) -> None:
        self.files: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []

    def add_message_listener(self, listener) -> None:  # noqa: ANN001
        pass

    def remove_message_listener(self, listener) -> None:  # noqa: ANN001
        pass

    def self_names(self) -> set[str]:
        return set()

    def get_status(self) -> StatusResult:
        return StatusResult(
            ok=True, state=ConnectionState.CONNECTED, backend="fake", listening=True
        )

    def apply_send_attempts(self, attempts: int) -> None:
        pass

    async def send_message(self, chat: str, text: str) -> SendResult:
        self.sent.append((chat, text))
        return SendResult(ok=True, status=SendStatus.SENT, recipient=chat)

    async def send_file(
        self, chat: str, path: str, dry_run: bool = False, confirm: bool = False
    ) -> SimpleNamespace:
        self.files.append((chat, path))
        return SimpleNamespace(ok=True, status=SendStatus.SENT, error=None)


async def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    adapter = _FakeAdapter()
    with tempfile.TemporaryDirectory() as tmp:
        config = BotConfig(
            enabled=True,
            link_download_enabled=True,
            link_download_dir=tmp,
            cooldown_seconds=0.0,
        )
        engine = BotEngine(adapter, config, llm_factory=LLMClient)
        record = MessageRecord(
            chat="测试群",
            sender="张三",
            content=f"看这个 {url}",
            is_group=True,
            is_at_me=True,
            message_id="m1",
            chat_id="",
            image_path="",
            timestamp=1000.0,
        )
        await engine._download_and_send(record)

    if not adapter.files:
        print("没有发出任何文件")
        return 1
    for _, path in adapter.files:
        target = Path(path)
        with Image.open(target) as img:
            size = f"{img.size[0]}×{img.size[1]}"
        print(f"发出：{target.name}  {size}  {target.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
