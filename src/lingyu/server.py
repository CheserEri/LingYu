"""灵语（LingYu）启动与工具注册。

通过 stdio 传输向本地 Agent 客户端暴露微信工具（P0 + P1）。
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from .logging_config import configure_logging
from .service import WeChatService
from .bridge import BridgeService, bridge_port_closed
import os
import json
from pathlib import Path

SERVER_NAME = "lingyu"
SERVER_VERSION = "0.8.19"


def build_server(service: WeChatService | None = None) -> MCPServer:
    """构建并注册全部工具的 MCP Server。"""
    server = MCPServer(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        instructions=(
            "操作本机已登录的微信桌面客户端。发送前请确认目标名称唯一；"
            "无法确认成功时不要重复发送；发送文件前需确认文件位于允许目录内。"
        ),
    )
    bridge_url = os.environ.get("LINGYU_GUI_BRIDGE_URL", "")
    bridge_token = os.environ.get("LINGYU_GUI_BRIDGE_TOKEN", "")
    if not (bridge_url and bridge_token):
        path = Path(os.environ.get("APPDATA", str(Path.home()))) / "lingyu" / "gui-bridge.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            bridge_url, bridge_token = data.get("url", ""), data.get("token", "")
        except (OSError, ValueError, TypeError):
            pass
    if bridge_url and bridge_port_closed(bridge_url):
        bridge_url = bridge_token = ""
    svc = service or (BridgeService(bridge_url, bridge_token) if bridge_url and bridge_token else WeChatService())

    @server.tool(
        name="get_wechat_status",
        description="检查微信客户端连接状态、窗口可见性与自动化后端是否可用。",
    )
    async def get_wechat_status() -> dict[str, Any]:
        return await svc.get_status()

    @server.tool(
        name="get_chat_list",
        description=(
            "返回当前可读取的会话列表。基于监听期间被动接收到的消息，"
            "不是微信完整会话列表。"
        ),
    )
    async def get_chat_list(
        limit: int | None = None, keyword: str = ""
    ) -> dict[str, Any]:
        return await svc.get_chat_list(limit=limit, keyword=keyword)

    @server.tool(
        name="get_chat_history",
        description=(
            "读取指定会话在缓冲区内最近的消息。需明确指定会话名称；"
            "返回数量受缓冲区容量限制。"
        ),
    )
    async def get_chat_history(chat_name: str, limit: int = 20) -> dict[str, Any]:
        return await svc.get_chat_history(chat_name, limit=limit)

    @server.tool(
        name="send_message",
        description=(
            "向明确指定的联系人或群聊发送文本消息。目标不唯一时拒绝发送并返回候选列表；"
            "无法确认发送成功时返回失败状态，不自动重试。"
        ),
    )
    async def send_message(
        recipient: str,
        message: str,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        return await svc.send_message(
            recipient, message, dry_run=dry_run, confirm=confirm
        )

    @server.tool(
        name="search_contact",
        description=(
            "按名称搜索联系人/群聊，返回候选列表。同名时原样返回多个候选，"
            "不自行猜测，需由调用方进一步指定。"
        ),
    )
    async def search_contact(keyword: str, limit: int = 20) -> dict[str, Any]:
        return await svc.search_contact(keyword, limit=limit)

    @server.tool(
        name="get_chat_info",
        description=(
            "返回指定会话已确认可获取的信息：名称、类型、观察到的消息数与最后时间，"
            "以及该会话当前是否在关注列表中。"
        ),
    )
    async def get_chat_info(chat_name: str) -> dict[str, Any]:
        return await svc.get_chat_info(chat_name)

    @server.tool(
        name="send_file",
        description=(
            "向明确指定的联系人或群聊发送本地文件。文件必须存在且位于允许目录内；"
            "未配置可发送目录时拒绝发送。结果无法确认时返回失败，不自动重试。"
        ),
    )
    async def send_file(
        recipient: str,
        file_path: str,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        return await svc.send_file(
            recipient, file_path, dry_run=dry_run, confirm=confirm
        )

    @server.tool(
        name="send_voice",
        description=(
            "发送语音/音频文件。注意：wechatauto 通过剪贴板粘贴发送，音频文件会以"
            "附件形式发送（非语音消息气泡）。如需真正语音气泡需微信 UI 流程。"
        ),
    )
    async def send_voice(
        recipient: str,
        file_path: str,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        return await svc.send_voice(
            recipient, file_path, dry_run=dry_run, confirm=confirm
        )

    @server.tool(
        name="get_recent_messages",
        description=(
            "增量读取监听缓冲区中新增的消息。传入上次返回的 next_seq 作为 after_seq，"
            "即可只获取新消息。可选用 chat_name 限定单个会话。"
        ),
    )
    async def get_recent_messages(
        after_seq: int = 0,
        limit: int = 20,
        chat_name: str = "",
    ) -> dict[str, Any]:
        return await svc.get_recent_messages(
            after_seq=after_seq, limit=limit, chat_name=chat_name
        )

    @server.tool(
        name="set_monitored_chats",
        description=(
            "设置会话关注列表：add/remove 增删会话名，mode 选择 allow（白名单，空=全部）"
            "或 block（黑名单，优先级更高）。仅过滤本服务返回的读取结果，"
            "不改变底层监听行为。"
        ),
    )
    async def set_monitored_chats(
        add: list[str] | None = None,
        remove: list[str] | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        return await svc.set_monitored_chats(add=add, remove=remove, mode=mode)

    return server


def main() -> None:
    """命令行入口：以 stdio 传输启动 MCP Server。"""
    configure_logging()
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()


# ======================================================================
# HTTP Webhook 集成
# ======================================================================
# Webhook 作为 MCP 的替代/补充接口：
# - MCP: 进程内协议，需要 tool calling 支持（适合 Codex 内置模型）
# - HTTP: 通用接口，任何模型都能调（适合 Qwen 等自定义模型）
#
# 启动方式：
#   from lingyu.webhook import start_webhook
#   from lingyu.server import build_server
#   from lingyu.service import WeChatService
#   from lingyu.config import AdapterConfig
#
#   svc = WeChatService()
#   server = build_server(svc)
#   server.run(transport='stdio')  # MCP 主进程
#   webhook = start_webhook(svc, port=18765)  # HTTP 补充
#
# 模型调用示例（任何支持 HTTP 的模型）：
#   curl -X POST http://127.0.0.1:18765/webhook \\
#     -H 'Authorization: Bearer <token>' \\
#     -H 'Content-Type: application/json' \\
#     -d '{"action":"send_message","recipient":"张三","content":"Hello!"}'
