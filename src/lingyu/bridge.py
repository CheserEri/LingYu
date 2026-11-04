"""Local loopback bridge for sharing the GUI's WeChat adapter with MCP."""

from __future__ import annotations

import json
import secrets
import threading
import socket
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.request import Request, urlopen


def bridge_port_closed(url: str) -> bool:
    """Only treat a refused loopback connection as a stale bridge."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
            return False
        with socket.create_connection((parsed.hostname, parsed.port), timeout=1):
            return False
    except ConnectionRefusedError:
        return True
    except (OSError, ValueError):
        return False


class GuiBridge:
    def __init__(self, call: Callable[[str, dict[str, Any]], Any]):
        self._call = call
        self.token = secrets.token_urlsafe(32)
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> str:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if self.headers.get("X-Lingyu-Bridge-Token") != owner.token:
                    self.send_error(403)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(size) or b"{}")
                    result = owner._call(self.path, body)
                    payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except Exception as exc:  # noqa: BLE001
                    self.send_error(500, str(exc))

            def log_message(self, *_: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


class BridgeClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    async def call(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        import asyncio

        def request() -> dict[str, Any]:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = Request(
                self.base_url + path,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Lingyu-Bridge-Token": self.token,
                },
                method="POST",
            )
            with urlopen(req, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))

        return await asyncio.to_thread(request)


class BridgeService:
    def __init__(self, base_url: str, token: str):
        self.client = BridgeClient(base_url, token)

    async def get_status(self): return await self.client.call("/status", {})
    async def get_chat_list(self, limit=None, keyword=""): return await self.client.call("/chat_list", {"limit": limit, "keyword": keyword})
    async def get_chat_history(self, chat_name, limit=20): return await self.client.call("/chat_history", {"chat_name": chat_name, "limit": limit})
    async def send_message(self, recipient, message, dry_run=False, confirm=False): return await self.client.call("/send_message", {"recipient": recipient, "message": message, "dry_run": dry_run, "confirm": confirm})
    async def send_file(self, recipient, file_path, dry_run=False, confirm=False): return await self.client.call("/send_file", {"recipient": recipient, "file_path": file_path, "dry_run": dry_run, "confirm": confirm})
    async def get_recent_messages(self, after_seq=0, limit=20, chat_name=""): return await self.client.call("/recent_messages", {"after_seq": after_seq, "limit": limit, "chat_name": chat_name})
    async def get_chat_info(self, chat_name): return await self.client.call("/chat_info", {"chat_name": chat_name})
    async def search_contact(self, keyword, limit=20): return await self.client.call("/search_contact", {"keyword": keyword, "limit": limit})
    async def set_monitored_chats(self, **kwargs): return {"ok": False, "error": {"code": "bridge_unsupported", "message": "请在 GUI 中配置关注列表"}}
