"""QQ 机器人适配层离线测试。

不联网：HTTP 用假客户端，WebSocket 用内存队列模拟网关。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lingyu.adapters.qq import (
    DEFAULT_INTENTS,
    INTENT_GUILDS,
    INTENT_GROUP_AND_C2C,
    INTENT_PUBLIC_GUILD_MESSAGES,
    QQBotAdapter,
)
from lingyu.config import AdapterConfig
from lingyu.schemas import ConnectionState


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


class FakeHttp:
    """按 (method, path) 返回预置响应，并记录请求。"""

    def __init__(self, routes: dict[tuple[str, str], dict]):
        self.routes = dict(routes)
        self.calls: list[dict] = []

    async def request(self, method, url, json=None, params=None, headers=None):
        path = url.split("api.bot.qq.com", 1)[-1]
        self.calls.append(
            {
                "method": method,
                "path": path,
                "json": json,
                "params": params,
                "headers": headers or {},
            }
        )
        payload = self.routes.get((method, path), {})
        if isinstance(payload, list):
            payload = payload.pop(0) if payload else {}
        return FakeResponse(payload)

    async def aclose(self):
        return None

    def find(self, method: str, path: str) -> list[dict]:
        return [
            call
            for call in self.calls
            if call["method"] == method and call["path"] == path
        ]


class FakeWebSocket:
    def __init__(self, incoming: list[dict]):
        self._queue: asyncio.Queue = asyncio.Queue()
        for item in incoming:
            self._queue.put_nowait(json.dumps(item))
        self.sent: list[dict] = []
        self.closed = False

    async def recv(self) -> str:
        return await self._queue.get()

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def close(self) -> None:
        self.closed = True

    def push(self, payload: dict) -> None:
        self._queue.put_nowait(json.dumps(payload))


def _hello() -> dict:
    return {"op": 10, "d": {"heartbeat_interval": 45000}}


def _ready(session_id: str = "sess-1") -> dict:
    return {
        "op": 0,
        "s": 1,
        "t": "READY",
        "d": {"session_id": session_id, "user": {"id": "bot-1", "username": "灵语"}},
    }


def _group_message(
    message_id: str = "m1", group_openid: str = "g1", content: str = "你好"
) -> dict:
    return {
        "op": 0,
        "s": 2,
        "t": "GROUP_AT_MESSAGE_CREATE",
        "d": {
            "id": message_id,
            "group_openid": group_openid,
            "content": content,
            "timestamp": "2026-11-12T10:00:00+08:00",
            "author": {"id": "u1", "member_openid": "member-1"},
        },
    }


def _c2c_message(message_id: str = "c1", content: str = "私聊") -> dict:
    return {
        "op": 0,
        "s": 3,
        "t": "C2C_MESSAGE_CREATE",
        "d": {
            "id": message_id,
            "content": content,
            "timestamp": "2026-11-12T10:00:00+08:00",
            "author": {"id": "u2", "user_openid": "user-openid-1"},
        },
    }


def _channel_message() -> dict:
    return {
        "op": 0,
        "s": 4,
        "t": "AT_MESSAGE_CREATE",
        "d": {
            "id": "ch-msg-1",
            "channel_id": "chan-1",
            "guild_id": "guild-1",
            "content": "<@!123> 在吗",
            "timestamp": "2026-11-12T10:00:00+08:00",
            "author": {"id": "u3", "username": "频道用户"},
        },
    }


def _config(**overrides) -> AdapterConfig:
    values = dict(
        platform="qq",
        qq_app_id="appid",
        qq_app_secret="secret",
        require_confirmation=False,
    )
    values.update(overrides)
    return AdapterConfig(**values)


def _adapter(
    ws: FakeWebSocket,
    http: FakeHttp,
    config: AdapterConfig | None = None,
) -> QQBotAdapter:
    async def connector(url: str, **kwargs):
        return ws

    return QQBotAdapter(
        config or _config(),
        http_factory=lambda: http,
        ws_connector=connector,
    )


def _http(gateway_url: str = "wss://api.bot.qq.com/websocket/") -> FakeHttp:
    return FakeHttp(
        {
            ("POST", "/app/getAppAccessToken"): {
                "access_token": "TOKEN",
                "expires_in": "7200",
            },
            ("GET", "/gateway"): {"url": gateway_url},
        }
    )


class QQConfigTests(unittest.TestCase):
    def test_from_env_reads_platform_and_credentials(self):
        env = {
            "LINGYU_PLATFORM": "qq",
            "QQ_BOT_APP_ID": "123",
            "QQ_BOT_APP_SECRET": "abc",
            "QQ_BOT_INTENTS": "1025",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            config = AdapterConfig.from_env()
        self.assertEqual(config.platform, "qq")
        self.assertTrue(config.is_qq())
        self.assertEqual(config.qq_app_id, "123")
        self.assertEqual(config.qq_app_secret, "abc")
        self.assertEqual(config.qq_intents, 1025)

    def test_invalid_platform_falls_back_to_wechat(self):
        with mock.patch.dict(os.environ, {"LINGYU_PLATFORM": "telegram"}, clear=False):
            config = AdapterConfig.from_env()
        self.assertEqual(config.platform, "wechat")

    def test_default_intents_covers_group_and_guild(self):
        self.assertEqual(
            DEFAULT_INTENTS,
            INTENT_GUILDS | INTENT_GROUP_AND_C2C | INTENT_PUBLIC_GUILD_MESSAGES,
        )


class QQGatewayTests(unittest.TestCase):
    def test_connect_identifies_with_access_token(self):
        ws = FakeWebSocket([_hello(), _ready()])
        http = _http()
        adapter = _adapter(ws, http)

        async def run():
            status = await adapter.connect()
            identify = [item for item in ws.sent if item.get("op") == 2]
            await adapter.disconnect()
            return status, identify

        status, identify = asyncio.run(run())
        self.assertTrue(status.ok)
        self.assertEqual(status.state, ConnectionState.CONNECTED)
        self.assertEqual(status.backend, "qqbot")
        self.assertEqual(len(identify), 1)
        self.assertEqual(identify[0]["d"]["token"], "QQBot TOKEN")
        self.assertEqual(identify[0]["d"]["intents"], DEFAULT_INTENTS)
        self.assertEqual(
            http.find("POST", "/app/getAppAccessToken")[0]["json"],
            {"appId": "appid", "clientSecret": "secret"},
        )

    def test_send_after_inbound_reuses_msg_id_and_increments_seq(self):
        ws = FakeWebSocket([_hello(), _ready()])
        http = _http()
        adapter = _adapter(ws, http)
        received: list = []
        done = asyncio.Event()

        async def listener(record):
            received.append(record)
            done.set()

        adapter.add_message_listener(listener)

        async def run():
            await adapter.connect()
            ws.push(_group_message())
            await asyncio.wait_for(done.wait(), timeout=2)
            first = await adapter.send_message(received[0].chat, "第一条")
            second = await adapter.send_message(received[0].chat, "第二条")
            await adapter.disconnect()
            return first, second

        first, second = asyncio.run(run())
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        sends = http.find("POST", "/v2/groups/g1/messages")
        self.assertEqual(len(sends), 2)
        self.assertEqual(sends[0]["json"]["msg_id"], "m1")
        self.assertEqual(sends[0]["json"]["msg_seq"], 1)
        self.assertEqual(sends[1]["json"]["msg_seq"], 2)
        self.assertEqual(sends[0]["json"]["msg_type"], 0)
        self.assertEqual(sends[0]["headers"]["Authorization"], "QQBot TOKEN")

    def test_reply_limit_is_reported(self):
        ws = FakeWebSocket([_hello(), _ready()])
        adapter = _adapter(ws, _http())
        final = None
        done = asyncio.Event()

        async def listener(record):
            done.set()

        adapter.add_message_listener(listener)

        async def run():
            nonlocal final
            await adapter.connect()
            ws.push(_group_message())
            await asyncio.wait_for(done.wait(), timeout=2)
            for _ in range(6):
                final = await adapter.send_message("群 g1", "刷屏")
            await adapter.disconnect()

        asyncio.run(run())
        self.assertIsNotNone(final)
        self.assertFalse(final.ok)
        self.assertEqual(final.error["code"], "qq_reply_limit")

    def test_expired_group_reply_window(self):
        ws = FakeWebSocket([_hello(), _ready()])
        adapter = _adapter(ws, _http())
        done = asyncio.Event()

        async def listener(record):
            done.set()

        adapter.add_message_listener(listener)

        async def run():
            await adapter.connect()
            ws.push(_group_message())
            await asyncio.wait_for(done.wait(), timeout=2)
            # 把到达时间拨到 10 分钟前，模拟超过 5 分钟被动回复窗口。
            adapter._targets["群 g1"]["ts"] -= 600
            result = await adapter.send_message("群 g1", "迟到")
            await adapter.disconnect()
            return result

        result = asyncio.run(run())
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "qq_reply_expired")

    def test_unknown_target_is_rejected_without_network(self):
        ws = FakeWebSocket([_hello(), _ready()])
        http = _http()
        adapter = _adapter(ws, http)

        async def run():
            await adapter.connect()
            result = await adapter.send_message("不存在的群", "hi")
            await adapter.disconnect()
            return result

        result = asyncio.run(run())
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "qq_target_unknown")

    def test_explicit_prefix_target_is_supported(self):
        ws = FakeWebSocket([_hello(), _ready()])
        http = _http()
        adapter = _adapter(ws, http)

        async def run():
            await adapter.connect()
            result = await adapter.send_message("group:g-explicit", "主动消息")
            await adapter.disconnect()
            return result

        result = asyncio.run(run())
        self.assertTrue(result.ok)
        sends = http.find("POST", "/v2/groups/g-explicit/messages")
        self.assertEqual(len(sends), 1)
        self.assertNotIn("msg_id", sends[0]["json"])

    def test_c2c_event_and_send_path(self):
        ws = FakeWebSocket([_hello(), _ready()])
        http = _http()
        adapter = _adapter(ws, http)
        done = asyncio.Event()
        received: list = []

        async def listener(record):
            received.append(record)
            done.set()

        adapter.add_message_listener(listener)

        async def run():
            await adapter.connect()
            ws.push(_c2c_message())
            await asyncio.wait_for(done.wait(), timeout=2)
            # 展示名与 openid 都能作为收件人。
            first = await adapter.send_message(received[0].chat, "收到")
            second = await adapter.send_message("user-openid-1", "再收到")
            await adapter.disconnect()
            return first, second

        first, second = asyncio.run(run())
        self.assertFalse(received[0].is_group)
        self.assertEqual(received[0].chat_id, "user-openid-1")
        self.assertTrue(received[0].is_at_me)
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        sends = http.find("POST", "/v2/users/user-openid-1/messages")
        self.assertEqual(len(sends), 2)
        self.assertEqual(sends[1]["json"]["msg_seq"], 2)

    def test_guild_event_strips_mentions(self):
        ws = FakeWebSocket([_hello(), _ready()])
        adapter = _adapter(ws, _http())
        done = asyncio.Event()
        received: list = []

        async def listener(record):
            received.append(record)
            done.set()

        adapter.add_message_listener(listener)

        async def run():
            await adapter.connect()
            ws.push(_channel_message())
            await asyncio.wait_for(done.wait(), timeout=2)
            await adapter.disconnect()

        asyncio.run(run())
        self.assertEqual(received[0].content, "在吗")
        self.assertEqual(received[0].sender, "频道用户")
        self.assertEqual(received[0].chat_id, "chan-1")
        self.assertTrue(received[0].is_group)

    def test_recent_messages_cursor_and_history(self):
        ws = FakeWebSocket([_hello(), _ready()])
        adapter = _adapter(ws, _http())
        done = asyncio.Event()

        async def listener(record):
            done.set()

        adapter.add_message_listener(listener)

        async def run():
            await adapter.connect()
            ws.push(_group_message("m1", content="一"))
            await asyncio.wait_for(done.wait(), timeout=2)
            done.clear()
            ws.push(_group_message("m2", content="二"))
            await asyncio.wait_for(done.wait(), timeout=2)
            first = adapter.get_recent_messages(after_seq=0, limit=1)
            second = adapter.get_recent_messages(after_seq=first.next_seq, limit=10)
            history = adapter.get_chat_history("群 g1", limit=10)
            chats = adapter.get_chat_list()
            info = adapter.get_chat_info("群 g1")
            await adapter.disconnect()
            return first, second, history, chats, info

        first, second, history, chats, info = asyncio.run(run())
        self.assertEqual(len(first.messages), 1)
        self.assertEqual(first.messages[0].content, "一")
        self.assertEqual(len(second.messages), 1)
        self.assertEqual(second.messages[0].content, "二")
        self.assertEqual(len(history.messages), 2)
        self.assertEqual(chats.total, 1)
        self.assertTrue(chats.chats[0].is_group)
        self.assertEqual(info.message_count, 2)
        self.assertTrue(info.is_group)

    def test_group_file_uploads_rich_media_then_sends_media_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(b"video-bytes")
            http = _http()
            http.routes[("POST", "/v2/groups/g1/files")] = {
                "file_info": "FILE-INFO",
                "file_uuid": "U1",
                "ttl": 0,
            }
            http.routes[("POST", "/v2/groups/g1/messages")] = {"id": "media-1"}
            ws = FakeWebSocket([_hello(), _ready()])
            adapter = _adapter(
                ws,
                http,
                _config(allow_send_dirs=(Path(tmp),)),
            )
            done = asyncio.Event()

            async def listener(record):
                done.set()

            adapter.add_message_listener(listener)

            async def run():
                await adapter.connect()
                ws.push(_group_message())
                await asyncio.wait_for(done.wait(), timeout=2)
                result = await adapter.send_file("群 g1", str(path))
                await adapter.disconnect()
                return result

            result = asyncio.run(run())

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.resolved_recipient, "g1")
        uploads = http.find("POST", "/v2/groups/g1/files")
        self.assertEqual(len(uploads), 1)
        upload = uploads[0]["json"]
        self.assertEqual(upload["file_type"], 2)
        self.assertFalse(upload["srv_send_msg"])
        self.assertEqual(base64.b64decode(upload["file_data"]), b"video-bytes")
        sends = http.find("POST", "/v2/groups/g1/messages")
        self.assertEqual(len(sends), 1)
        body = sends[0]["json"]
        self.assertEqual(body["msg_type"], 7)
        self.assertEqual(body["media"], {"file_info": "FILE-INFO"})
        self.assertEqual(body["msg_id"], "m1")
        self.assertEqual(body["msg_seq"], 1)

    def test_c2c_image_uses_user_files_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "photo.png"
            path.write_bytes(b"png-bytes")
            http = _http()
            http.routes[("POST", "/v2/users/user-openid-1/files")] = {
                "file_info": "IMG-INFO",
                "file_uuid": "U2",
                "ttl": 0,
            }
            http.routes[("POST", "/v2/users/user-openid-1/messages")] = {
                "id": "media-2"
            }
            ws = FakeWebSocket([_hello(), _ready()])
            adapter = _adapter(
                ws,
                http,
                _config(allow_send_dirs=(Path(tmp),)),
            )
            done = asyncio.Event()

            async def listener(record):
                done.set()

            adapter.add_message_listener(listener)

            async def run():
                await adapter.connect()
                ws.push(_c2c_message())
                await asyncio.wait_for(done.wait(), timeout=2)
                result = await adapter.send_file("user:user-openid-1", str(path))
                await adapter.disconnect()
                return result

            result = asyncio.run(run())

        self.assertTrue(result.ok, result.error)
        upload = http.find("POST", "/v2/users/user-openid-1/files")[0]["json"]
        self.assertEqual(upload["file_type"], 1)
        body = http.find("POST", "/v2/users/user-openid-1/messages")[0]["json"]
        self.assertEqual(body["msg_type"], 7)
        self.assertEqual(body["media"], {"file_info": "IMG-INFO"})

    def test_channel_file_send_is_rejected_without_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.txt"
            path.write_bytes(b"hello")
            http = _http()
            ws = FakeWebSocket([_hello(), _ready()])
            adapter = _adapter(
                ws,
                http,
                _config(allow_send_dirs=(Path(tmp),)),
            )
            done = asyncio.Event()

            async def listener(record):
                done.set()

            adapter.add_message_listener(listener)

            async def run():
                await adapter.connect()
                ws.push(_channel_message())
                await asyncio.wait_for(done.wait(), timeout=2)
                result = await adapter.send_file("频道 chan-1", str(path))
                await adapter.disconnect()
                return result

            result = asyncio.run(run())

        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "qq_media_scene_unsupported")
        self.assertEqual(http.find("POST", "/channels/chan-1/files"), [])

    def test_rich_media_upload_error_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(b"video-bytes")
            http = _http()
            http.routes[("POST", "/v2/groups/g1/files")] = {
                "code": 304082,
                "message": "upload media info fail",
            }
            ws = FakeWebSocket([_hello(), _ready()])
            adapter = _adapter(
                ws,
                http,
                _config(allow_send_dirs=(Path(tmp),)),
            )
            done = asyncio.Event()

            async def listener(record):
                done.set()

            adapter.add_message_listener(listener)

            async def run():
                await adapter.connect()
                ws.push(_group_message())
                await asyncio.wait_for(done.wait(), timeout=2)
                result = await adapter.send_file("群 g1", str(path))
                await adapter.disconnect()
                return result

            result = asyncio.run(run())

        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "qq_304082")
        self.assertEqual(http.find("POST", "/v2/groups/g1/messages"), [])

    def test_missing_credentials_reports_error(self):
        ws = FakeWebSocket([_hello(), _ready()])
        adapter = _adapter(ws, _http(), _config(qq_app_id="", qq_app_secret=""))

        async def run():
            return await adapter.connect()

        status = asyncio.run(run())
        self.assertFalse(status.ok)
        self.assertEqual(status.state, ConnectionState.ERROR)
        self.assertEqual(status.error["code"], "qq_config_missing")
        self.assertEqual(ws.sent, [])

    def test_dry_run_does_not_call_api(self):
        ws = FakeWebSocket([_hello(), _ready()])
        http = _http()
        adapter = _adapter(ws, http)

        async def run():
            await adapter.connect()
            result = await adapter.send_message("group:g1", "预览", dry_run=True)
            await adapter.disconnect()
            return result

        result = asyncio.run(run())
        self.assertTrue(result.ok)
        self.assertEqual(result.status.value, "dry_run")
        self.assertEqual(http.find("POST", "/v2/groups/g1/messages"), [])

    def test_disconnect_sets_disconnected_state(self):
        ws = FakeWebSocket([_hello(), _ready()])
        adapter = _adapter(ws, _http())

        async def run():
            await adapter.connect()
            status = await adapter.disconnect()
            return status

        status = asyncio.run(run())
        self.assertEqual(status.state, ConnectionState.DISCONNECTED)
        self.assertFalse(status.ok)


class QQProbeTests(unittest.TestCase):
    """界面「测试连接」只校验凭据：取 Token + 网关，不建立 WebSocket。"""

    def test_valid_credentials_return_gateway(self):
        http = _http(gateway_url="wss://api.bot.qq.com/websocket/")
        adapter = QQBotAdapter(_config(), http_factory=lambda: http)

        result = asyncio.run(adapter.probe())

        self.assertTrue(result["ok"])
        self.assertEqual(result["app_id"], "appid")
        self.assertEqual(result["gateway"], "wss://api.bot.qq.com/websocket/")
        self.assertTrue(http.find("POST", "/app/getAppAccessToken"))
        self.assertTrue(http.find("GET", "/gateway"))

    def test_missing_credentials_do_not_call_api(self):
        http = _http()
        adapter = QQBotAdapter(
            _config(qq_app_id="", qq_app_secret=""), http_factory=lambda: http
        )

        result = asyncio.run(adapter.probe())

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "qq_config_missing")
        self.assertEqual(http.calls, [])

    def test_rejected_credentials_report_api_error(self):
        http = FakeHttp(
            {
                ("POST", "/app/getAppAccessToken"): {
                    "code": 100016,
                    "message": "invalid appid or secret",
                }
            }
        )
        adapter = QQBotAdapter(_config(), http_factory=lambda: http)

        result = asyncio.run(adapter.probe())

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "qq_100016")
        self.assertIn("invalid appid", result["error"]["message"])

    def test_network_failure_is_reported(self):
        class BrokenHttp:
            async def request(self, *args, **kwargs):
                raise OSError("connection reset")

            async def aclose(self):
                return None

        adapter = QQBotAdapter(_config(), http_factory=lambda: BrokenHttp())

        result = asyncio.run(adapter.probe())

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "qq_network_error")


if __name__ == "__main__":
    unittest.main()
