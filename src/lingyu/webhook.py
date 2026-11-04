"""通用 HTTP Webhook 接口：替代 MCP 让任意模型主动发送消息。

对比 MCP 的优势：
- MCP 是进程内协议，模型需要原生支持 tool calling 才能调用
- HTTP webhook 是通用接口，任何模型都能通过 prompt 指导调用 POST /webhook
- 不需要 MCP 工具定义、不需要 Codex 专属协议

接口规范（对标 MCP tool call）：
    POST /webhook
    Authorization: Bearer <token>
    Content-Type: application/json

    {
        action: 'send_message',     // send_message | send_file | send_voice
        recipient: '张三',           // 联系人/群聊名称
        content: 'Hello!',           // 文本内容
        file_path: '',               // 文件路径（send_file/send_voice 用）
        dry_run: false               // 预览模式，不实际发送
    }

返回：
    {
        ok: true,
        status: 'sent',
        message: '消息已发送'
    }

使用场景：
    1. 大模型在回复时直接调用 webhook 发微信
    2. 异步回复（webhook 立即返回，发送在后台进行）
    3. 任何支持 HTTP 调用的模型都能用
"""

from __future__ import annotations

import asyncio
import json
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import urlsplit

from .service import WeChatService

logger = logging.getLogger(__name__)


class WebhookRequest:
    """Webhook 请求的标准化结构。"""

    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.action: str = data.get('action', 'send_message')
        self.recipient: str = data.get('recipient', '')
        self.content: str = data.get('content', '')
        self.file_path: str = data.get('file_path', '')
        self.dry_run: bool = data.get('dry_run', False)
        # 可选：语音参数
        self.voice_duration: int = data.get('voice_duration', 0)
        self.delay_seconds: float = data.get('delay_seconds', 0.0)

    def validate(self) -> dict[str, Any] | None:
        """验证请求是否合法。"""
        if self.action not in (
            'get_wechat_status', 'get_chat_list', 'get_chat_history',
            'search_contact', 'get_chat_info', 'get_recent_messages',
            'set_monitored_chats', 'send_message', 'send_file', 'send_voice',
        ):
            return {'ok': False, 'error': {'code': 'invalid_action', 'message': f'不支持的动作: {self.action}'}}
        needs_recipient = self.action in ('send_message', 'send_file', 'send_voice', 'get_chat_history', 'get_chat_info')
        if needs_recipient and not self.recipient and not self.data.get('chat_name'):
            return {'ok': False, 'error': {'code': 'missing_recipient', 'message': 'recipient 不能为空'}}
        if self.action == 'send_message' and not self.content:
            return {'ok': False, 'error': {'code': 'missing_content', 'message': 'send_message 需要 content 字段'}}
        if self.action in ('send_file', 'send_voice') and not self.file_path:
            return {'ok': False, 'error': {'code': 'missing_file', 'message': f'{self.action} 需要 file_path 字段'}}
        fp = Path(self.file_path) if self.file_path else None
        if fp and not fp.is_file():
            return {'ok': False, 'error': {'code': 'file_not_found', 'message': f'文件不存在: {self.file_path}'}}
        return None


class WebhookAuth:
    """Bearer Token 认证。"""

    def __init__(self, token: str | None = None):
        # 本地回环服务默认不认证；只有显式配置 token 时才启用认证。
        self.token = (token or "").strip()

    def verify(self, auth_header: str) -> bool:
        if not self.token:
            return True
        if not auth_header.startswith('Bearer '):
            return False
        return auth_header[7:] == self.token


class _WebhookHandler(BaseHTTPRequestHandler):
    """HTTP Webhook 请求处理器。"""

    _service: WeChatService | None = None
    _auth: WebhookAuth | None = None
    _loop: asyncio.AbstractEventLoop | None = None

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        if self._auth:
            auth = self.headers.get('Authorization', '')
            if not self._auth.verify(auth):
                self._send_json(401, {'ok': False, 'error': {'code': 'unauthorized', 'message': '认证失败'}})
                return

        try:
            size = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(size) if size > 0 else b'{}'
            data = json.loads(body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError) as exc:
            self._send_json(400, {'ok': False, 'error': {'code': 'parse_error', 'message': str(exc)}})
            return

        request = WebhookRequest(data)
        error = request.validate()
        if error:
            self._send_json(400, error)
            return

        if self._service:
            if self._loop is None or not self._loop.is_running():
                self._send_json(503, {'ok': False, 'error': {'code': 'event_loop_unavailable', 'message': '微信事件循环未运行'}})
                return
            future = asyncio.run_coroutine_threadsafe(self._dispatch(request), self._loop)
            if request.action.startswith('send_'):
                self._send_json(202, {'ok': True, 'status': 'queued', 'message': '消息已交给后台发送'})
            else:
                try:
                    result = future.result(timeout=30.0)
                    self._send_json(200 if result.get('ok', True) else 400, result)
                except Exception as exc:
                    future.cancel()
                    self._send_json(500, {'ok': False, 'error': {'code': 'request_failed', 'message': str(exc)}})
        else:
            self._send_json(503, {'ok': False, 'error': {'code': 'service_unavailable', 'message': '微信服务未就绪'}})

    async def _dispatch(self, request: WebhookRequest) -> dict[str, Any]:
        if request.action == 'get_wechat_status':
            return await self._service.get_status()
        if request.action == 'get_chat_list':
            return await self._service.get_chat_list(
                limit=request.data.get('limit'), keyword=request.data.get('keyword', '')
            )
        if request.action == 'get_chat_history':
            return await self._service.get_chat_history(
                request.data.get('chat_name', request.recipient),
                limit=int(request.data.get('limit', 20) or 20),
            )
        if request.action == 'search_contact':
            return await self._service.search_contact(
                request.data.get('keyword', request.recipient),
                limit=int(request.data.get('limit', 20) or 20),
            )
        if request.action == 'get_chat_info':
            return await self._service.get_chat_info(request.data.get('chat_name', request.recipient))
        if request.action == 'get_recent_messages':
            return await self._service.get_recent_messages(
                after_seq=int(request.data.get('after_seq', 0) or 0),
                limit=int(request.data.get('limit', 20) or 20),
                chat_name=request.data.get('chat_name', request.recipient),
            )
        if request.action == 'set_monitored_chats':
            return await self._service.set_monitored_chats(
                add=request.data.get('add'), remove=request.data.get('remove'), mode=request.data.get('mode')
            )
        for attempt in range(1, 2):
            try:
                if request.action == 'send_message':
                    result = await self._service.send_message(
                        request.recipient, request.content,
                        dry_run=request.dry_run, confirm=True,
                    )
                elif request.action == 'send_file':
                    result = await self._service.send_file(
                        request.recipient, request.file_path,
                        dry_run=request.dry_run, confirm=True,
                    )
                elif request.action == 'send_voice':
                    result = await self._service.send_voice(
                        request.recipient, request.file_path,
                        dry_run=request.dry_run, confirm=True,
                    )
                else:
                    result = {'ok': False, 'error': {'code': 'unknown_action', 'message': request.action}}

                if result.get('ok'):
                    logger.info(f'Webhook 发送成功 [{request.recipient}] {request.action} attempt={attempt}')
                    return result
                error = result.get('error', {})
                logger.warning(
                    f'Webhook 发送失败 [{request.recipient}] attempt={attempt}/1 '
                    f"{error.get('code', 'unknown')}: {error.get('message', '')}"
                )
            except Exception:
                logger.exception(f'Webhook 后台发送异常 attempt={attempt}/1')
                result = {'ok': False, 'error': {'code': 'send_exception', 'message': '微信发送异常'}}
        return result

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        response = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def start_webhook(
    service: WeChatService,
    port: int = 18765,
    token: str | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> tuple[HTTPServer, Thread]:
    """启动 HTTP Webhook 服务。

    Returns:
        (HTTPServer, Thread)
    """
    auth = WebhookAuth(token)
    _WebhookHandler._service = service
    _WebhookHandler._auth = auth
    _WebhookHandler._loop = loop

    server = HTTPServer(('127.0.0.1', port), _WebhookHandler)
    # 供桌面端把运行时随机 token 传给 Agent 提示词；不写入日志正文。
    server.webhook_token = auth.token
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    webhook_url = f'http://127.0.0.1:{port}/webhook'
    logger.info(f'Webhook 服务已启动: {webhook_url} (token: {auth.token[:16]}...)')
    return server, thread
