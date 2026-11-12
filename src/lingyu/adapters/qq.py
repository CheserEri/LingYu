"""QQ 机器人适配层（官方 API v2）。

与 ``WeChatAdapter`` 保持同名接口（鸭子类型），因此 ``BotEngine`` / ``WeChatService``
不需要为平台写分支。接入方式完全走官方开放平台：

- HTTP：``https://api.bot.qq.com``，``Authorization: QQBot {access_token}``；
- 网关：``GET /gateway`` 取 ``wss://`` 地址，WebSocket 上完成 Hello / Identify /
  Heartbeat / Resume；
- 消息：``C2C_MESSAGE_CREATE``、``GROUP_AT_MESSAGE_CREATE``、``AT_MESSAGE_CREATE``、
  ``DIRECT_MESSAGE_CREATE``，发送走 ``/v2/users/...``、``/v2/groups/...``、
  ``/channels/...``、``/dms/...``。

已知限制：

- QQ 只允许「被动回复」：必须携带收到的那条消息的 ``msg_id``，群聊 5 分钟 / 单聊
  60 分钟内有效，同一 ``msg_id`` 最多回复 5 次。超出后返回结构化错误，不伪装成功。
- 事件里没有群名/昵称（只有 openid），因此会话名用「群 <openid>」这类标签展示，
  ``chat_id`` 始终是可直接发送的 openid。
- 本地文件发送走官方富媒体：先上传到 ``/v2/users|groups/{id}/files``，再用
  ``msg_type=7`` + ``media.file_info`` 发送。该能力只在单聊 / 群聊开放；
  文字子频道与频道私信没有对应的富媒体上传接口，会明确返回不支持。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import logging
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..config import AdapterConfig
from ..errors import (
    BackendUnavailableError,
    ConfirmationRequiredError,
    LingyuError,
    NotConnectedError,
)
from ..monitoring import MonitoredChats
from ..schemas import (
    ChatHistoryResult,
    ChatInfoResult,
    ChatListResult,
    ChatSummary,
    ConnectionState,
    ContactCandidate,
    ContactSearchResult,
    FileSendResult,
    MessageRecord,
    MonitoredChatsResult,
    RecentMessagesResult,
    SendResult,
    SendStatus,
    StatusResult,
)
from ..security import validate_message, validate_recipient, validate_send_path

_logger = logging.getLogger(__name__)

# ---- Intents（官方文档「事件订阅 Intents」）----
INTENT_GUILDS = 1 << 0
INTENT_GROUP_AND_C2C = 1 << 25
INTENT_PUBLIC_GUILD_MESSAGES = 1 << 30
DEFAULT_INTENTS = INTENT_GUILDS | INTENT_GROUP_AND_C2C | INTENT_PUBLIC_GUILD_MESSAGES

# Access Token 在过期前 60 秒内重新获取会拿到新 token。
_TOKEN_REFRESH_MARGIN = 60.0

# 被动回复窗口（秒）。群聊/频道/频道私信 5 分钟，单聊 60 分钟。
_REPLY_WINDOWS = {"group": 300.0, "channel": 300.0, "dm": 300.0, "c2c": 3600.0}
# 同一条消息允许的被动回复次数：单聊 4 次，其余 5 次。
_MAX_REPLY_PER_MESSAGE = 5
_MAX_REPLY_BY_KIND = {"c2c": 4}

_MENTION_RE = re.compile(r"<@!?\d+>")
_CHAT_KINDS = {"c2c", "group", "channel", "dm"}

# 富媒体 file_type：1 图片（png/jpg）、2 视频（mp4）、3 语音（silk/wav/mp3/flac）、4 文件。
# 未识别的后缀按「文件」上传，由服务端决定是否接受。
_RICH_FILE_TYPES = {
    ".png": 1,
    ".jpg": 1,
    ".jpeg": 1,
    ".mp4": 2,
    ".silk": 3,
    ".wav": 3,
    ".mp3": 3,
    ".flac": 3,
}


def _rich_file_type(path: Path) -> int:
    """按扩展名推断 QQ 富媒体类型。"""
    return _RICH_FILE_TYPES.get(path.suffix.lower(), 4)


class QQApiError(LingyuError):
    """QQ 开放平台返回的业务错误。"""

    code = "qq_api_error"

    def __init__(
        self,
        message: str,
        *,
        code: str = "qq_api_error",
        detail: dict[str, Any] | None = None,
    ):
        self.code = code
        super().__init__(message, detail=detail)


class _Reconnect(Exception):
    """网关要求重新连接（op 7 / op 9）。"""


class _InvalidSession(_Reconnect):
    """登录态失效，需要重新 Identify。"""


async def _default_ws_connector(url: str, **kwargs: Any) -> Any:
    """默认 WebSocket 连接器；缺少 websockets 时给出明确安装提示。"""
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底
        raise BackendUnavailableError(
            'QQ 机器人需要 websockets 库，请先安装：pip install "websockets>=12"'
        ) from exc
    return await websockets.connect(url, **kwargs)


def _default_http_factory() -> Any:
    import httpx

    return httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))


def _parse_timestamp(raw: Any) -> float | None:
    """把事件里的 RFC3339 时间转成 Unix 秒；解析失败返回 None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _attachment_marker(attachments: Any) -> tuple[str, str]:
    """从附件列表推断占位文本与消息类型。"""
    items = attachments if isinstance(attachments, list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        ctype = str(item.get("content_type") or "").lower()
        if "image" in ctype:
            return "[图片]", "image"
        if "video" in ctype:
            return "[视频]", "video"
        if "voice" in ctype or "audio" in ctype or "silk" in ctype:
            return "[语音]", "voice"
        if "file" in ctype:
            return "[文件]", "file"
    return "", "text"


class QQBotAdapter:
    """独立的 QQ 机器人适配层，可脱离业务逻辑直接调用。"""

    platform = "qq"

    def __init__(
        self,
        config: AdapterConfig | None = None,
        *,
        http_factory: Callable[[], Any] | None = None,
        ws_connector: Callable[..., Any] | None = None,
    ):
        self._config = config or AdapterConfig.from_env()
        self._http_factory = http_factory or _default_http_factory
        self._ws_connector = ws_connector or _default_ws_connector
        self._http: Any | None = None

        self._buffer: deque[MessageRecord] = deque(
            maxlen=self._config.history_buffer_size
        )
        self._buffer_lock = threading.Lock()
        self._seq = 0
        self._message_listeners: list[Callable[[MessageRecord], Any]] = []
        self._monitor = MonitoredChats.from_config(self._config)

        # 会话 → 被动回复上下文（类型 / openid / 最新 msg_id / 已用序号 / 到达时间）
        self._targets: dict[str, dict[str, Any]] = {}
        # 最近一次成功发送的消息 ID，供撤回使用。
        self._sent: dict[str, tuple[str, str, str]] = {}

        self._task: asyncio.Task[Any] | None = None
        self._ws: Any | None = None
        self._ws_send_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._ready: asyncio.Event | None = None
        self._handshake: asyncio.Event | None = None
        self._closing = False

        self._token = ""
        self._token_deadline = 0.0
        self._token_lock = asyncio.Lock()

        self._session_id = ""
        self._ws_seq = 0
        self._acked = True
        self._bot_name = ""
        self._bot_id = ""
        self._last_error: dict[str, Any] | None = None

    # ------------------------------------------------------------------ 生命周期

    @property
    def config(self) -> AdapterConfig:
        return self._config

    @property
    def is_connected(self) -> bool:
        return bool(
            self._ws is not None
            and self._ready is not None
            and self._ready.is_set()
        )

    def connections(self) -> list[str]:
        return list(self._targets)

    async def connect(self) -> StatusResult:
        """获取网关并完成 Identify，直到 READY 或超时。"""
        if self.is_connected:
            return self.get_status()

        missing = [
            name
            for name, value in (
                ("AppID", self._config.qq_app_id),
                ("AppSecret", self._config.qq_app_secret),
            )
            if not str(value or "").strip()
        ]
        if missing:
            self._last_error = {
                "code": "qq_config_missing",
                "message": "尚未配置 QQ 机器人 " + " / ".join(missing),
            }
            return self.get_status()

        if self._task is None or self._task.done():
            self._closing = False
            self._ready = asyncio.Event()
            self._handshake = asyncio.Event()
            self._last_error = None
            self._task = asyncio.create_task(
                self._supervise(), name="lingyu-qq-gateway"
            )

        handshake = self._handshake
        if handshake is not None:
            try:
                await asyncio.wait_for(
                    handshake.wait(),
                    timeout=max(3.0, float(self._config.qq_connect_timeout)),
                )
            except asyncio.TimeoutError:
                _logger.info("QQ 网关连接超时，后台继续重试")
        return self.get_status()

    async def disconnect(self) -> StatusResult:
        self._closing = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
        await self._close_ws()
        http, self._http = self._http, None
        if http is not None:
            with contextlib.suppress(Exception):
                await http.aclose()
        self._ready = None
        self._handshake = None
        self._session_id = ""
        self._ws_seq = 0
        self._targets.clear()
        self._last_error = None
        return self.get_status()

    # ------------------------------------------------------------------ 状态

    def get_status(self) -> StatusResult:
        task = self._task
        if task is None or self._closing:
            state = (
                ConnectionState.ERROR
                if (task is None and self._last_error)
                else ConnectionState.DISCONNECTED
            )
        elif (
            self._ws is not None
            and self._ready is not None
            and self._ready.is_set()
        ):
            state = ConnectionState.CONNECTED
        elif task.done() and self._last_error:
            state = ConnectionState.ERROR
        else:
            state = ConnectionState.CONNECTING

        return StatusResult(
            ok=state == ConnectionState.CONNECTED,
            state=state,
            backend="qqbot",
            listening=state == ConnectionState.CONNECTED,
            # QQ 走官方 API，没有桌面窗口/UIA 的概念。
            window_visible=False,
            uia_available=False,
            detail={
                "platform": "qq",
                "app_id": self._config.qq_app_id,
                "bot_name": self._bot_name,
                "bot_id": self._bot_id,
                "session": bool(self._session_id),
                "last_seq": self._ws_seq,
                "intents": self._intents(),
                "known_chats": len(self._targets),
            },
            error=self._last_error,
        )

    def _intents(self) -> int:
        return int(self._config.qq_intents or DEFAULT_INTENTS)

    async def probe(self) -> dict[str, Any]:
        """校验 AppID / AppSecret 是否可用：取 Access Token 并请求一次网关地址。

        只做只读探测，不建立 WebSocket，也不会影响正在运行的适配器。
        """
        missing = [
            name
            for name, value in (
                ("AppID", self._config.qq_app_id),
                ("AppSecret", self._config.qq_app_secret),
            )
            if not str(value or "").strip()
        ]
        if missing:
            return {
                "ok": False,
                "error": {
                    "ok": False,
                    "code": "qq_config_missing",
                    "message": "尚未填写 QQ 机器人 " + " / ".join(missing),
                },
            }
        try:
            await self._ensure_token(force=True)
            gateway = await self._gateway_url()
        except LingyuError as exc:
            return {"ok": False, "error": exc.to_dict()}
        except Exception as exc:  # 网络层兜底
            return {
                "ok": False,
                "error": {
                    "ok": False,
                    "code": "qq_probe_failed",
                    "message": str(exc) or exc.__class__.__name__,
                },
            }
        return {
            "ok": True,
            "app_id": self._config.qq_app_id,
            "gateway": gateway,
            "note": "凭据有效，可以保存并连接。",
        }

    # ------------------------------------------------------------------ 网关

    async def _supervise(self) -> None:
        """保持网关长连接；断开后指数退避重连（优先 Resume）。"""
        backoff = 1.0
        while not self._closing:
            error: Exception | None = None
            try:
                await self._session_once()
                backoff = 1.0
                if not self._closing:
                    self._last_error = {
                        "code": "qq_gateway_closed",
                        "message": "QQ 网关连接已关闭，正在重连",
                    }
                    if self._ready is not None:
                        self._ready.clear()
                    if self._handshake is not None:
                        self._handshake.set()
            except asyncio.CancelledError:
                raise
            except _InvalidSession:
                # 登录态失效：丢弃 session，下一次重新 Identify。
                self._session_id = ""
                self._ws_seq = 0
            except _Reconnect:
                _logger.info("QQ 网关要求重连，准备 Resume")
            except LingyuError as exc:
                error = exc
                self._last_error = exc.to_dict()
            except Exception as exc:  # 网络异常、网关主动关闭等
                error = exc
                self._last_error = {
                    "code": "qq_gateway_error",
                    "message": str(exc) or exc.__class__.__name__,
                }
            if error is not None:
                _logger.warning("QQ 网关连接中断：%s", self._last_error)
                if self._ready is not None:
                    self._ready.clear()
                if self._handshake is not None:
                    self._handshake.set()
            if self._closing:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _session_once(self) -> None:
        url = await self._gateway_url()
        ws = await self._ws_connector(url, max_size=None)
        self._ws = ws
        heartbeat: asyncio.Task[Any] | None = None
        try:
            hello_raw = await asyncio.wait_for(
                ws.recv(),
                timeout=max(3.0, float(self._config.qq_connect_timeout)),
            )
            hello = self._loads(hello_raw)
            interval = float((hello.get("d") or {}).get("heartbeat_interval") or 45000)
            token = await self._ensure_token()
            self._acked = True
            if self._session_id:
                await self._ws_send(
                    {
                        "op": 6,
                        "d": {
                            "token": f"QQBot {token}",
                            "session_id": self._session_id,
                            "seq": self._ws_seq,
                        },
                    },
                    ws=ws,
                )
            else:
                await self._ws_send(
                    {
                        "op": 2,
                        "d": {
                            "token": f"QQBot {token}",
                            "intents": self._intents(),
                            "shard": [0, 1],
                            "properties": {
                                "$os": "windows",
                                "$browser": "lingyu",
                                "$device": "lingyu",
                            },
                        },
                    },
                    ws=ws,
                )

            heartbeat = asyncio.create_task(
                self._heartbeat_loop(ws, max(5.0, interval / 1000.0))
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + max(3.0, float(self._config.qq_connect_timeout))
            while True:
                if self._ready is not None and self._ready.is_set():
                    timeout = None
                else:
                    timeout = max(0.2, deadline - loop.time())
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError as exc:
                    raise QQApiError(
                        "QQ 网关握手超时：没有收到 READY 事件",
                        code="qq_connect_timeout",
                    ) from exc
                await self._handle_payload(raw)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await heartbeat
            await self._close_ws(ws)
            self._ws = None

    async def _heartbeat_loop(self, ws: Any, interval: float) -> None:
        """周期发送心跳；上一拍没收到 ACK 就主动断开，让上层 Resume。"""
        try:
            while True:
                await asyncio.sleep(interval)
                await self._ws_send({"op": 1, "d": self._ws_seq or None}, ws=ws)
                if not self._acked:
                    _logger.warning("QQ 网关心跳未确认，主动断开重连")
                    with contextlib.suppress(Exception):
                        await ws.close()
                    return
                self._acked = False
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _handle_payload(self, raw: Any) -> None:
        payload = self._loads(raw)
        op = payload.get("op")
        if op == 11:
            self._acked = True
            return
        if op == 7:
            raise _Reconnect()
        if op == 9:
            raise _InvalidSession()
        if op != 0:
            return

        try:
            self._ws_seq = int(payload.get("s") or self._ws_seq)
        except (TypeError, ValueError):
            pass
        await self._dispatch(str(payload.get("t") or ""), payload.get("d") or {})

    async def _dispatch(self, event: str, data: dict[str, Any]) -> None:
        if event == "READY":
            self._session_id = str(data.get("session_id") or "")
            user = data.get("user") or {}
            if isinstance(user, dict):
                self._bot_name = str(user.get("username") or "")
                self._bot_id = str(user.get("id") or "")
            self._last_error = None
            if self._ready is not None:
                self._ready.set()
            if self._handshake is not None:
                self._handshake.set()
            _logger.info(
                "QQ 机器人已连接：%s", self._bot_name or self._bot_id or "(未知)"
            )
            return
        if event == "RESUMED":
            if self._ready is not None:
                self._ready.set()
            if self._handshake is not None:
                self._handshake.set()
            return

        builder = {
            "C2C_MESSAGE_CREATE": self._build_c2c,
            "GROUP_AT_MESSAGE_CREATE": self._build_group,
            "AT_MESSAGE_CREATE": self._build_channel,
            "DIRECT_MESSAGE_CREATE": self._build_dm,
        }.get(event)
        if builder is None:
            return
        try:
            built = builder(data)
        except Exception:  # 单条脏数据不该打断整条网关连接
            _logger.exception("解析 QQ 事件失败：%s", event)
            return
        if built is None:
            return
        record, target = built
        self._targets[record.chat] = target
        with self._buffer_lock:
            self._seq += 1
            record.seq = self._seq
            self._buffer.append(record)
        for listener in tuple(self._message_listeners):
            try:
                result = listener(record)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                _logger.exception("消息订阅回调执行失败")

    # ---- 事件 → MessageRecord ----

    def _build_c2c(self, data: dict[str, Any]) -> tuple[MessageRecord, dict[str, Any]] | None:
        author = data.get("author") or {}
        openid = str(author.get("user_openid") or "").strip()
        if not openid:
            return None
        content, message_type = self._content(data)
        if not content:
            return None
        message_id = str(data.get("id") or "")
        record = MessageRecord(
            chat=f"私聊 {openid}",
            chat_id=openid,
            sender=str(author.get("username") or author.get("nickname") or openid),
            content=content,
            message_type=message_type,
            is_group=False,
            timestamp=_parse_timestamp(data.get("timestamp")),
            message_id=message_id,
            is_at_me=True,
        )
        return record, self._target("c2c", openid, message_id)

    def _build_group(
        self, data: dict[str, Any]
    ) -> tuple[MessageRecord, dict[str, Any]] | None:
        group_openid = str(data.get("group_openid") or "").strip()
        if not group_openid:
            return None
        author = data.get("author") or {}
        content, message_type = self._content(data)
        if not content:
            return None
        message_id = str(data.get("id") or "")
        record = MessageRecord(
            chat=f"群 {group_openid}",
            chat_id=group_openid,
            sender=str(
                author.get("member_openid")
                or author.get("username")
                or author.get("id")
                or ""
            ),
            content=content,
            message_type=message_type,
            is_group=True,
            timestamp=_parse_timestamp(data.get("timestamp")),
            message_id=message_id,
            is_at_me=True,
        )
        return record, self._target("group", group_openid, message_id)

    def _build_channel(
        self, data: dict[str, Any]
    ) -> tuple[MessageRecord, dict[str, Any]] | None:
        channel_id = str(data.get("channel_id") or "").strip()
        if not channel_id:
            return None
        author = data.get("author") or {}
        content = _MENTION_RE.sub("", str(data.get("content") or "")).strip()
        content, message_type = self._content(data, content=content)
        if not content:
            return None
        message_id = str(data.get("id") or "")
        record = MessageRecord(
            chat=f"频道 {channel_id}",
            chat_id=channel_id,
            sender=str(author.get("username") or author.get("nickname") or author.get("id") or ""),
            content=content,
            message_type=message_type,
            is_group=True,
            timestamp=_parse_timestamp(data.get("timestamp")),
            message_id=message_id,
            is_at_me=True,
        )
        return record, self._target("channel", channel_id, message_id)

    def _build_dm(
        self, data: dict[str, Any]
    ) -> tuple[MessageRecord, dict[str, Any]] | None:
        guild_id = str(data.get("guild_id") or "").strip()
        if not guild_id:
            guild_id = str(data.get("channel_id") or "").strip()
        if not guild_id:
            return None
        author = data.get("author") or {}
        content, message_type = self._content(data)
        if not content:
            return None
        message_id = str(data.get("id") or "")
        record = MessageRecord(
            chat=f"私信 {guild_id}",
            chat_id=guild_id,
            sender=str(author.get("username") or author.get("nickname") or author.get("id") or ""),
            content=content,
            message_type=message_type,
            is_group=False,
            timestamp=_parse_timestamp(data.get("timestamp")),
            message_id=message_id,
            is_at_me=True,
        )
        return record, self._target("dm", guild_id, message_id)

    @staticmethod
    def _target(kind: str, target_id: str, message_id: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "id": target_id,
            "msg_id": message_id,
            "seq": 0,
            "ts": time.time(),
        }

    @staticmethod
    def _content(
        data: dict[str, Any], *, content: str | None = None
    ) -> tuple[str, str]:
        text = (
            str(data.get("content") or "").strip()
            if content is None
            else str(content or "").strip()
        )
        if text:
            return text, "text"
        marker, message_type = _attachment_marker(data.get("attachments"))
        return marker, message_type

    # ------------------------------------------------------------------ 发送

    async def send_message(
        self,
        recipient: str,
        message: str,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> SendResult:
        """向 QQ 单聊 / 群聊 / 子频道 / 频道私信发送文本（被动回复）。"""
        try:
            recipient = validate_recipient(recipient)
            message = validate_message(message)
        except LingyuError as exc:
            return SendResult(
                ok=False,
                status=SendStatus.FAILED,
                recipient=str(recipient or ""),
                error=exc.to_dict(),
            )

        if not self.is_connected:
            return SendResult(
                ok=False,
                status=SendStatus.FAILED,
                recipient=recipient,
                error=NotConnectedError("尚未连接 QQ 机器人，无法发送消息").to_dict(),
            )

        target = self._resolve_target(recipient)
        if target is None:
            return SendResult(
                ok=False,
                status=SendStatus.FAILED,
                recipient=recipient,
                error={
                    "ok": False,
                    "code": "qq_target_unknown",
                    "message": (
                        f"未知的 QQ 会话「{recipient}」。QQ 只能回复已经收到过消息的会话；"
                        "也可以显式写成 user:<openid> / group:<openid> / "
                        "channel:<id> / dm:<guild_id>"
                    ),
                    "detail": {"recipient": recipient},
                },
            )

        preview = message[:50]
        if dry_run:
            return SendResult(
                ok=True,
                status=SendStatus.DRY_RUN,
                recipient=recipient,
                resolved_recipient=str(target["id"]),
                message_preview=preview,
                dry_run=True,
            )
        if self._config.require_confirmation and not confirm:
            return SendResult(
                ok=False,
                status=SendStatus.CONFIRMATION_REQUIRED,
                recipient=recipient,
                resolved_recipient=str(target["id"]),
                message_preview=preview,
                error=ConfirmationRequiredError(
                    "当前配置要求发送前确认，请以 confirm=true 复核后再发送",
                    detail={"recipient": recipient, "message_preview": preview},
                ).to_dict(),
            )

        body: dict[str, Any] = {"content": message, "msg_type": 0}
        kind = str(target.get("kind") or "")
        reply_fields, reply_error = self._passive_reply(target)
        if reply_error is not None:
            return SendResult(
                ok=False,
                status=SendStatus.FAILED,
                recipient=recipient,
                resolved_recipient=str(target["id"]),
                message_preview=preview,
                error=reply_error,
            )
        body.update(reply_fields)

        try:
            async with self._send_lock:
                payload = await self._request_json(
                    "POST", self._send_path(kind, str(target["id"])), json_body=body
                )
        except LingyuError as exc:
            return SendResult(
                ok=False,
                status=SendStatus.FAILED,
                recipient=recipient,
                resolved_recipient=str(target["id"]),
                message_preview=preview,
                error=exc.to_dict(),
            )
        except Exception as exc:  # 兜底，避免异常穿透
            return SendResult(
                ok=False,
                status=SendStatus.FAILED,
                recipient=recipient,
                resolved_recipient=str(target["id"]),
                message_preview=preview,
                error={"ok": False, "code": "send_failed", "message": str(exc)},
            )

        message_id = str(payload.get("id") or "")
        if message_id:
            self._sent[recipient] = (kind, str(target["id"]), message_id)
        return SendResult(
            ok=True,
            status=SendStatus.SENT,
            recipient=recipient,
            resolved_recipient=str(target["id"]),
            message_preview=preview,
        )

    def _resolve_target(self, recipient: str) -> dict[str, Any] | None:
        value = str(recipient or "").strip()
        target = self._targets.get(value)
        if target is not None:
            return target
        for item in self._targets.values():
            if str(item.get("id") or "") == value:
                return item
        if ":" in value:
            prefix, _, rest = value.partition(":")
            kind = prefix.strip().lower()
            # 与提示文案一致：user: 是单聊（c2c）的别名。
            kind = "c2c" if kind == "user" else kind
            rest = rest.strip()
            if kind in _CHAT_KINDS and rest:
                return {"kind": kind, "id": rest, "msg_id": "", "seq": 0, "ts": 0.0}
        return None

    @staticmethod
    def _send_path(kind: str, target_id: str) -> str:
        if kind == "c2c":
            return f"/v2/users/{target_id}/messages"
        if kind == "group":
            return f"/v2/groups/{target_id}/messages"
        if kind == "dm":
            return f"/dms/{target_id}/messages"
        return f"/channels/{target_id}/messages"

    @staticmethod
    def _passive_reply(
        target: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """构造被动回复字段；超出次数或窗口时返回错误（不发送）。"""
        msg_id = str(target.get("msg_id") or "")
        if not msg_id:
            return {}, None
        kind = str(target.get("kind") or "")
        seq = int(target.get("seq") or 0) + 1
        reply_limit = _MAX_REPLY_BY_KIND.get(kind, _MAX_REPLY_PER_MESSAGE)
        if seq > reply_limit:
            return {}, {
                "ok": False,
                "code": "qq_reply_limit",
                "message": (
                    f"同一条 QQ 消息最多被动回复 {reply_limit} 次，"
                    "请等对方发新消息后再回复"
                ),
            }
        window = _REPLY_WINDOWS.get(kind, 300.0)
        arrived = float(target.get("ts") or 0.0)
        if arrived and time.time() - arrived > window:
            return {}, {
                "ok": False,
                "code": "qq_reply_expired",
                "message": (
                    "QQ 被动回复窗口已过期"
                    f"（{'单聊 60 分钟' if kind == 'c2c' else '5 分钟'}），"
                    "请等对方再次发消息"
                ),
            }
        target["seq"] = seq
        return {"msg_id": msg_id, "msg_seq": seq}, None

    @staticmethod
    def _rich_upload_path(kind: str, target_id: str) -> str:
        """富媒体上传路径只存在于单聊与群聊。"""
        return f"/v2/users/{target_id}/files" if kind == "c2c" else f"/v2/groups/{target_id}/files"

    async def send_file(
        self,
        recipient: str,
        file_path: str,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> FileSendResult:
        """发送本地文件：先上传富媒体，再以 ``msg_type=7`` 被动回复。"""
        try:
            recipient = validate_recipient(recipient)
            resolved_path = validate_send_path(
                file_path, self._config.allow_send_dirs
            )
        except LingyuError as exc:
            return FileSendResult(
                ok=False,
                status=SendStatus.FAILED,
                recipient=str(recipient or ""),
                file_path=str(file_path or ""),
                error=exc.to_dict(),
            )

        file_name = resolved_path.name
        try:
            file_size = resolved_path.stat().st_size
        except OSError:
            file_size = 0
        base: dict[str, Any] = {
            "recipient": recipient,
            "file_path": str(resolved_path),
            "file_name": file_name,
            "file_size": file_size,
            "dry_run": dry_run,
        }

        if not self.is_connected:
            return FileSendResult(
                ok=False,
                status=SendStatus.FAILED,
                **base,
                error=NotConnectedError("尚未连接 QQ 机器人，无法发送文件").to_dict(),
            )

        target = self._resolve_target(recipient)
        if target is None:
            return FileSendResult(
                ok=False,
                status=SendStatus.FAILED,
                **base,
                error={
                    "ok": False,
                    "code": "qq_target_unknown",
                    "message": (
                        f"未知的 QQ 会话「{recipient}」。QQ 只能回复已经收到过消息的会话；"
                        "也可以显式写成 user:<openid> / group:<openid>"
                    ),
                    "detail": {"recipient": recipient},
                },
            )

        kind = str(target.get("kind") or "")
        target_id = str(target.get("id") or "")
        if kind not in ("c2c", "group"):
            return FileSendResult(
                ok=False,
                status=SendStatus.FAILED,
                resolved_recipient=target_id,
                **base,
                error={
                    "ok": False,
                    "code": "qq_media_scene_unsupported",
                    "message": (
                        "QQ 富媒体消息只在单聊 / 群聊开放；"
                        "文字子频道与频道私信没有上传接口，请改发文本或公网链接"
                    ),
                },
            )

        if dry_run:
            return FileSendResult(
                ok=True,
                status=SendStatus.DRY_RUN,
                resolved_recipient=target_id,
                **base,
            )
        if self._config.require_confirmation and not confirm:
            return FileSendResult(
                ok=False,
                status=SendStatus.CONFIRMATION_REQUIRED,
                resolved_recipient=target_id,
                **base,
                error=ConfirmationRequiredError(
                    "当前配置要求发送前确认，请以 confirm=true 复核后再发送",
                    detail={"recipient": recipient, "file_name": file_name},
                ).to_dict(),
            )

        reply_fields, reply_error = self._passive_reply(target)
        if reply_error is not None:
            return FileSendResult(
                ok=False,
                status=SendStatus.FAILED,
                resolved_recipient=target_id,
                **base,
                error=reply_error,
            )

        try:
            encoded = base64.b64encode(resolved_path.read_bytes()).decode("ascii")
        except OSError as exc:
            return FileSendResult(
                ok=False,
                status=SendStatus.FAILED,
                resolved_recipient=target_id,
                **base,
                error={
                    "ok": False,
                    "code": "qq_file_read_failed",
                    "message": f"读取待发送文件失败：{exc}",
                },
            )

        try:
            async with self._send_lock:
                uploaded = await self._request_json(
                    "POST",
                    self._rich_upload_path(kind, target_id),
                    json_body={
                        "file_type": _rich_file_type(resolved_path),
                        "file_data": encoded,
                        "srv_send_msg": False,
                    },
                )
                file_info = str(uploaded.get("file_info") or "")
                if not file_info:
                    raise QQApiError(
                        "富媒体上传成功但返回里没有 file_info",
                        code="qq_media_upload_invalid",
                        detail={"response": uploaded},
                    )
                payload = await self._request_json(
                    "POST",
                    self._send_path(kind, target_id),
                    json_body={
                        "content": "",
                        "msg_type": 7,
                        "media": {"file_info": file_info},
                        **reply_fields,
                    },
                )
        except LingyuError as exc:
            return FileSendResult(
                ok=False,
                status=SendStatus.FAILED,
                resolved_recipient=target_id,
                **base,
                error=exc.to_dict(),
            )
        except Exception as exc:  # 兜底，避免异常穿透
            return FileSendResult(
                ok=False,
                status=SendStatus.FAILED,
                resolved_recipient=target_id,
                **base,
                error={"ok": False, "code": "send_failed", "message": str(exc)},
            )

        message_id = str(payload.get("id") or "")
        if message_id:
            self._sent[recipient] = (kind, target_id, message_id)
        return FileSendResult(
            ok=True,
            status=SendStatus.SENT,
            resolved_recipient=target_id,
            **base,
        )

    async def recall_last_message(self, chat_name: str) -> SendResult:
        """撤回该会话最近一条由机器人发出的消息。"""
        recipient = str(chat_name or "").strip()
        if not self.is_connected:
            return SendResult(
                ok=False,
                status=SendStatus.FAILED,
                recipient=recipient,
                error=NotConnectedError("尚未连接 QQ 机器人，无法撤回").to_dict(),
            )
        entry = self._sent.pop(recipient, None)
        if entry is None:
            target = self._resolve_target(recipient)
            if target is not None:
                entry = self._sent.pop(str(target.get("id") or ""), None)
        if entry is None:
            return SendResult(
                ok=False,
                status=SendStatus.FAILED,
                recipient=recipient,
                error={
                    "ok": False,
                    "code": "qq_nothing_to_recall",
                    "message": "本进程还没有在该会话成功发出过消息，无法撤回",
                },
            )
        kind, target_id, message_id = entry
        if kind == "channel":
            path = f"/channels/{target_id}/messages/{message_id}"
        elif kind == "dm":
            path = f"/dms/{target_id}/messages/{message_id}"
        elif kind == "group":
            path = f"/v2/groups/{target_id}/messages/{message_id}"
        else:
            path = f"/v2/users/{target_id}/messages/{message_id}"
        try:
            await self._request_json("DELETE", path)
        except LingyuError as exc:
            self._sent[recipient] = entry
            return SendResult(
                ok=False,
                status=SendStatus.FAILED,
                recipient=recipient,
                resolved_recipient=target_id,
                error=exc.to_dict(),
            )
        return SendResult(
            ok=True,
            status=SendStatus.SENT,
            recipient=recipient,
            resolved_recipient=target_id,
            message_preview="[已撤回]",
        )

    # ------------------------------------------------------------------ 读取

    def get_chat_list(
        self, limit: int | None = None, keyword: str = ""
    ) -> ChatListResult:
        records = [
            record for record in self._snapshot() if self._record_allowed(record)
        ]
        keyword = str(keyword or "").strip()

        summaries: dict[str, ChatSummary] = {}
        for record in records:
            if keyword and keyword not in record.chat:
                continue
            item = summaries.get(record.chat)
            if item is None:
                summaries[record.chat] = ChatSummary(
                    name=record.chat,
                    is_group=record.is_group,
                    message_count=1,
                    last_message_at=record.timestamp,
                )
                continue
            item.message_count += 1
            if record.timestamp and (
                item.last_message_at is None or record.timestamp > item.last_message_at
            ):
                item.last_message_at = record.timestamp

        chats = sorted(
            summaries.values(),
            key=lambda chat: chat.last_message_at or 0.0,
            reverse=True,
        )
        total = len(chats)
        if limit is not None and int(limit) > 0:
            chats = chats[: int(limit)]
        return ChatListResult(
            ok=True,
            chats=chats,
            total=total,
            complete=not self._buffer_full(),
            note=self._read_note(),
        )

    def get_chat_history(self, chat_name: str, limit: int = 20) -> ChatHistoryResult:
        try:
            chat_name = validate_recipient(chat_name)
        except LingyuError as exc:
            return ChatHistoryResult(
                ok=False, chat=str(chat_name or ""), complete=False, note=exc.message
            )
        if not self._chat_name_allowed(chat_name):
            return ChatHistoryResult(
                ok=True,
                chat=chat_name,
                messages=[],
                complete=not self._buffer_full(),
                note="该会话当前不在关注列表中，读取结果已被过滤。",
            )
        records = [
            record
            for record in self._snapshot()
            if record.chat == chat_name or record.chat_id == chat_name
        ]
        if limit is not None and int(limit) > 0:
            records = records[-int(limit) :]
        return ChatHistoryResult(
            ok=True,
            chat=chat_name,
            messages=records,
            complete=not self._buffer_full(),
            note=self._read_note(),
        )

    def get_recent_messages(
        self,
        after_seq: int = 0,
        limit: int = 20,
        chat_name: str = "",
    ) -> RecentMessagesResult:
        cursor = max(0, int(after_seq or 0))
        chat_name = str(chat_name or "").strip()
        if chat_name:
            try:
                chat_name = validate_recipient(chat_name)
            except LingyuError as exc:
                return RecentMessagesResult(
                    ok=False, next_seq=cursor, error=exc.to_dict()
                )
        records = [
            record
            for record in self._snapshot()
            if record.seq > cursor
            and self._record_allowed(record)
            and (
                not chat_name
                or record.chat == chat_name
                or record.chat_id == chat_name
            )
        ]
        records.sort(key=lambda record: record.seq)
        truncated = False
        if limit is not None and int(limit) > 0 and len(records) > int(limit):
            records = records[: int(limit)]
            truncated = True
        if records:
            next_seq = records[-1].seq
        else:
            with self._buffer_lock:
                next_seq = max(cursor, self._seq)
        return RecentMessagesResult(
            ok=True,
            messages=records,
            next_seq=next_seq,
            complete=not truncated and not self._buffer_full(),
            note=self._read_note(),
        )

    def get_chat_info(self, chat_name: str) -> ChatInfoResult:
        try:
            chat_name = validate_recipient(chat_name)
        except LingyuError as exc:
            return ChatInfoResult(
                ok=False, name=str(chat_name or ""), error=exc.to_dict()
            )
        records = [
            record
            for record in self._snapshot()
            if record.chat == chat_name or record.chat_id == chat_name
        ]
        is_group = records[0].is_group if records else chat_name.startswith("群 ")
        last_message_at: float | None = None
        for record in records:
            if record.timestamp and (
                last_message_at is None or record.timestamp > last_message_at
            ):
                last_message_at = record.timestamp
        return ChatInfoResult(
            ok=True,
            name=chat_name,
            is_group=is_group,
            message_count=len(records),
            last_message_at=last_message_at,
            monitored=self._chat_name_allowed(chat_name),
            note=self._read_note(),
        )

    def search_contact(
        self, keyword: str, limit: int = 20
    ) -> ContactSearchResult:
        keyword = str(keyword or "").strip()
        if not keyword:
            return ContactSearchResult(
                ok=False,
                error={"code": "target_invalid", "message": "搜索关键词不能为空"},
            )
        candidates: list[ContactCandidate] = []
        seen: set[str] = set()
        for record in self._snapshot():
            for name in (record.chat, record.chat_id):
                if not name or name in seen or keyword not in name:
                    continue
                seen.add(name)
                candidates.append(
                    ContactCandidate(name=name, username=record.chat_id, is_group=record.is_group)
                )
        total = len(candidates)
        if limit is not None and int(limit) > 0:
            candidates = candidates[: int(limit)]
        return ContactSearchResult(
            ok=True,
            keyword=keyword,
            candidates=candidates,
            total=total,
            source="buffer",
            note="QQ 开放平台不提供联系人检索，结果基于适配层运行期间收到的会话（openid）。",
        )

    def set_monitored_chats(
        self,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        mode: str | None = None,
    ) -> MonitoredChatsResult:
        try:
            self._monitor.apply(add=add, remove=remove, mode=mode)
        except LingyuError as exc:
            snapshot = self._monitor.snapshot()
            return MonitoredChatsResult(
                ok=False,
                mode=snapshot["mode"],
                allow=snapshot["allow"],
                block=snapshot["block"],
                error=exc.to_dict(),
            )
        snapshot = self._monitor.snapshot()
        return MonitoredChatsResult(
            ok=True,
            mode=snapshot["mode"],
            allow=snapshot["allow"],
            block=snapshot["block"],
            note="该过滤仅作用于 MCP 读取结果，不改变网关事件订阅。",
        )

    # ------------------------------------------------------------------ 订阅

    def add_message_listener(self, listener: Callable[[MessageRecord], Any]) -> None:
        if listener not in self._message_listeners:
            self._message_listeners.append(listener)

    def remove_message_listener(self, listener: Callable[[MessageRecord], Any]) -> None:
        if listener in self._message_listeners:
            self._message_listeners.remove(listener)

    def self_names(self) -> set[str]:
        names = {self._bot_name, self._bot_id}
        return {name for name in names if name}

    def resolve_chat_identifier(self, identifier: str) -> str:
        """QQ 的会话标识本身就是 openid，可直接用于发送，无需反查。"""
        return str(identifier or "").strip()

    # ------------------------------------------------------------------ HTTP

    def _http_client(self) -> Any:
        if self._http is None:
            self._http = self._http_factory()
        return self._http

    async def _ensure_token(self, *, force: bool = False) -> str:
        async with self._token_lock:
            now = time.monotonic()
            if not force and self._token and now < self._token_deadline:
                return self._token
            payload = await self._request_json(
                "POST",
                "/app/getAppAccessToken",
                json_body={
                    "appId": self._config.qq_app_id,
                    "clientSecret": self._config.qq_app_secret,
                },
                auth=False,
            )
            token = str(payload.get("access_token") or "").strip()
            if not token:
                raise QQApiError(
                    "获取 QQ Access Token 失败：响应中没有 access_token",
                    code="qq_token_missing",
                    detail={"response": payload},
                )
            try:
                expires = float(payload.get("expires_in") or 7200)
            except (TypeError, ValueError):
                expires = 7200.0
            self._token = token
            self._token_deadline = now + max(60.0, expires - _TOKEN_REFRESH_MARGIN)
            return token

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        auth: bool = True,
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        url = self._config.qq_api_base.rstrip("/") + path
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"QQBot {await self._ensure_token()}"
        try:
            response = await self._http_client().request(
                method, url, json=json_body, params=params, headers=headers
            )
        except Exception as exc:
            raise QQApiError(
                f"请求 QQ 接口失败：{exc}",
                code="qq_network_error",
                detail={"path": path},
            ) from exc

        try:
            payload = response.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        status = int(getattr(response, "status_code", 200) or 200)
        if status == 401 and auth and retry_auth:
            # Access Token 失效：清掉缓存再取一次。
            self._token = ""
            return await self._request_json(
                method,
                path,
                json_body=json_body,
                params=params,
                auth=auth,
                retry_auth=False,
            )

        code = payload.get("code", payload.get("err_code"))
        if status >= 400 or code not in (None, 0, "0", ""):
            raise QQApiError(
                str(payload.get("message") or payload.get("msg") or f"HTTP {status}"),
                code=f"qq_{code}" if code not in (None, "", 0, "0") else "qq_http_error",
                detail={"status": status, "response": payload, "path": path},
            )
        return payload

    async def _gateway_url(self) -> str:
        payload = await self._request_json("GET", "/gateway")
        url = str(payload.get("url") or "").strip()
        if not url:
            raise QQApiError(
                "QQ 网关地址获取失败：响应中没有 url",
                code="qq_gateway_missing",
                detail={"response": payload},
            )
        return url

    # ------------------------------------------------------------------ 内部

    async def _ws_send(self, payload: dict[str, Any], *, ws: Any | None = None) -> None:
        socket = ws or self._ws
        if socket is None:
            raise _Reconnect()
        async with self._ws_send_lock:
            await socket.send(json.dumps(payload, ensure_ascii=False))

    async def _close_ws(self, ws: Any | None = None) -> None:
        socket = ws or self._ws
        if socket is None:
            return
        with contextlib.suppress(Exception):
            await socket.close()

    @staticmethod
    def _loads(raw: Any) -> dict[str, Any]:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _snapshot(self) -> list[MessageRecord]:
        with self._buffer_lock:
            return list(self._buffer)

    def _record_allowed(self, record: MessageRecord) -> bool:
        """关注列表既认展示名，也认 openid（QQ 事件里两者都可能被使用）。"""
        if self._monitor.is_allowed(record.chat):
            return True
        return bool(record.chat_id and self._monitor.is_allowed(record.chat_id))

    def _chat_name_allowed(self, chat_name: str) -> bool:
        if self._monitor.is_allowed(chat_name):
            return True
        return any(
            (record.chat == chat_name or record.chat_id == chat_name)
            and self._record_allowed(record)
            for record in self._snapshot()
        )

    def _buffer_full(self) -> bool:
        maxlen = self._buffer.maxlen
        return bool(maxlen is not None and len(self._buffer) >= maxlen)

    def _read_note(self) -> str:
        return (
            "结果基于 QQ 网关运行期间收到的事件，不是完整历史记录；"
            "会话名中的 openid 可直接用于发送。"
        )
