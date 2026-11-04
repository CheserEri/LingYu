"""Persistent Codex App Server client for WeChat conversations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import psutil

from .config import BotConfig, default_config_path
from .llm import LLMError


class _AppServerConnection:
    def __init__(self, command: str, workspace: Path, sandbox: str, debug):
        self.command = command
        self.workspace = workspace
        self.sandbox = sandbox
        self.debug = debug
        self.process_registry = Path(os.environ.get("APPDATA", Path.home())) / "lingyu" / "appserver-processes.json"
        self.process_key = hashlib.sha256(
            f"{command}|{workspace.resolve()}|{sandbox}".encode("utf-8")
        ).hexdigest()[:24]
        self.process: asyncio.subprocess.Process | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._reader: asyncio.Task | None = None
        self._turns: dict[str, dict[str, Any]] = {}
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._thread_status: dict[str, str] = {}
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._start_lock:
            if self.process and self.process.returncode is None:
                return
            self._cleanup_stale_process()
            self.process = await asyncio.create_subprocess_exec(
                self.command, "app-server", "--stdio",
                cwd=str(self.workspace), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            self._save_process_identity(self.process)
            self._reader = asyncio.create_task(self._read_messages())
            asyncio.create_task(self._read_stderr())
            try:
                await self.request("initialize", {
                    "clientInfo": {"name": "lingyu", "title": "LingYu WeChat", "version": "1.0.0"},
                    "capabilities": {"experimentalApi": True},
                })
                await self.notify("initialized", {})
            except Exception:
                await self.close()
                raise

    def _read_process_registry(self) -> dict[str, Any]:
        try:
            data = json.loads(self.process_registry.read_text(encoding="utf-8"))
            if data.get("version") == _PROCESS_REGISTRY_VERSION:
                return data
        except (OSError, ValueError, TypeError):
            pass
        return {"version": _PROCESS_REGISTRY_VERSION, "processes": {}}

    def _write_process_registry(self, data: dict[str, Any]) -> None:
        try:
            self.process_registry.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.process_registry.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
            temporary.replace(self.process_registry)
        except OSError as exc:
            self.debug(f"cannot write app-server process registry: {exc}")

    def _save_process_identity(self, process: asyncio.subprocess.Process) -> None:
        if process.pid is None:
            return
        try:
            child = psutil.Process(process.pid)
            registry = self._read_process_registry()
            registry.setdefault("processes", {})[self.process_key] = {
                "pid": process.pid, "create_time": child.create_time(),
            }
            self._write_process_registry(registry)
        except (psutil.Error, OSError) as exc:
            self.debug(f"cannot record app-server process identity: {exc}")

    def _remove_process_identity(self, *, expected_pid: int | None = None) -> None:
        registry = self._read_process_registry()
        identity = registry.get("processes", {}).get(self.process_key)
        if identity and (expected_pid is None or identity.get("pid") == expected_pid):
            registry["processes"].pop(self.process_key, None)
            self._write_process_registry(registry)

    def _owned_process(self, identity: dict[str, Any]) -> psutil.Process | None:
        try:
            process = psutil.Process(int(identity["pid"]))
            if abs(process.create_time() - float(identity["create_time"])) > 0.01:
                return None
            command_line = [part.lower().replace("/", "\\") for part in process.cmdline()]
            executable = process.exe().lower().replace("/", "\\")
            if Path(executable).name not in {"codex.exe", "codex"}:
                return None
            if "app-server" not in command_line:
                return None
            if not any(part in {"--stdio", "stdio://"} for part in command_line):
                return None
            return process
        except (psutil.Error, KeyError, TypeError, ValueError):
            return None

    def _cleanup_stale_process(self) -> None:
        identity = self._read_process_registry().get("processes", {}).get(self.process_key)
        if not identity:
            return
        process = self._owned_process(identity)
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=5)
                self.debug(f"terminated stale owned app-server pid={process.pid}")
            except psutil.Error as exc:
                self.debug(f"failed to terminate stale app-server pid={identity.get('pid')}: {exc}")
        else:
            self.debug(f"stale pid={identity.get('pid')} did not match ownership; not terminating")
        self._remove_process_identity(expected_pid=identity.get("pid"))

    async def close(self) -> None:
        process = self.process
        if process is None:
            return
        self.process = None
        if process.stdin and not process.stdin.is_closing():
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        self._remove_process_identity(expected_pid=process.pid)
        if self._reader and not self._reader.done():
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        self._reader = None
        for state in self._turns.values():
            if state.get("status") == "inProgress":
                state["status"] = "interrupted"
                state["error"] = "LingYu stopped"
                state["done"].set()

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.process or self.process.returncode is not None:
            await self.start()
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout=30.0)
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, message: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise RuntimeError("app-server stdin is unavailable")
        data = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        async with self._write_lock:
            self.process.stdin.write(data)
            await self.process.stdin.drain()

    async def _read_messages(self) -> None:
        assert self.process and self.process.stdout
        process = self.process
        try:
            while line := await process.stdout.readline():
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                request_id = message.get("id")
                if request_id is not None and request_id in self._pending:
                    future = self._pending[request_id]
                    if message.get("error"):
                        error = message["error"]
                        if not future.done():
                            future.set_exception(RuntimeError(str(error.get("message", error))))
                    elif not future.done():
                        future.set_result(message.get("result") or {})
                    continue
                await self._notification(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.debug(f"app-server reader failed: {exc}")
        finally:
            if self.process is process:
                self.process = None
            self._remove_process_identity(expected_pid=process.pid)
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("app-server connection closed"))
            for state in self._turns.values():
                if state.get("status") == "inProgress":
                    state["status"] = "failed"
                    state["error"] = "app-server connection closed"
                    state["done"].set()

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        process = self.process
        while line := await process.stderr.readline():
            self.debug("app-server: " + line.decode("utf-8", errors="replace").rstrip()[-500:])

    async def _notification(self, message: dict[str, Any]) -> None:
        params = message.get("params") or {}
        method = message.get("method")
        if method == "thread/status/changed":
            self._thread_status[params.get("threadId", "")] = (params.get("status") or {}).get("type", "")
            return
        if method != "turn/completed":
            return
        state = self._turns.get(params.get("threadId", ""))
        turn = params.get("turn") or {}
        if state and turn.get("id") == state.get("turn_id"):
            state["status"] = turn.get("status", "failed")
            state["error"] = (turn.get("error") or {}).get("message", "")
            state["text"] = self._turn_text(turn)
            state["done"].set()

    @staticmethod
    def _turn_text(turn: dict[str, Any]) -> str:
        parts = []
        for item in turn.get("items") or []:
            if item.get("type") == "agentMessage":
                text = item.get("text", "")
                if text:
                    parts.append(text)
        return "\n".join(parts)

    def _thread_lock(self, thread_id: str) -> asyncio.Lock:
        return self._thread_locks.setdefault(thread_id, asyncio.Lock())

    async def run(
        self, thread_id: str, prompt: str, image_path: str = "", *, fresh: bool = False
    ) -> str:
        await self.start()
        async with self._thread_lock(thread_id):
            state = self._turns.get(thread_id)
            if state and state["status"] == "inProgress":
                await self.request("turn/steer", {
                    "threadId": thread_id, "expectedTurnId": state["turn_id"],
                    "input": self._input(prompt, image_path),
                })
            elif fresh:
                state = await self._start_turn(thread_id, prompt, image_path)
            else:
                response = await self.request("thread/resume", {
                    "threadId": thread_id, "cwd": str(self.workspace),
                    "initialTurnsPage": {"limit": 20, "sortDirection": "desc"},
                })
                turns = ((response.get("initialTurnsPage") or {}).get("data") or [])
                active = next((turn for turn in turns if turn.get("status") == "inProgress"), None)
                if active:
                    state = {"turn_id": active["id"], "status": "inProgress", "done": asyncio.Event(), "text": "", "error": ""}
                    self._turns[thread_id] = state
                    await self.request("turn/steer", {
                        "threadId": thread_id, "expectedTurnId": active["id"],
                        "input": self._input(prompt, image_path),
                    })
                else:
                    state = await self._start_turn(thread_id, prompt, image_path)
        await state["done"].wait()
        if state["status"] != "completed":
            raise RuntimeError(state.get("error") or f"turn {state['status']}")
        if not state["text"].strip():
            raise RuntimeError("Codex completed the turn without an agent message")
        return state["text"].strip()

    async def new_thread(self, prompt: str, image_path: str = "") -> tuple[str, str]:
        for attempt in range(2):
            response = await self.request("thread/start", {
                "cwd": str(self.workspace), "sandbox": self.sandbox,
            })
            thread_id = str((response.get("thread") or {}).get("id", ""))
            if not thread_id:
                raise RuntimeError("thread/start returned no thread id")
            try:
                return thread_id, await self.run(thread_id, prompt, image_path, fresh=True)
            except Exception:
                state = self._turns.get(thread_id)
                if attempt or not state or state.get("status") not in {"failed", "interrupted", "cancelled"}:
                    raise
                self.debug(f"new thread turn failed; retrying once in a fresh thread: {state.get('error', '')}")
        raise RuntimeError("could not start a healthy thread")

    async def _start_turn(self, thread_id: str, prompt: str, image_path: str) -> dict[str, Any]:
        response = await self.request("turn/start", {
            "threadId": thread_id, "cwd": str(self.workspace),
            "input": self._input(prompt, image_path),
        })
        turn = response.get("turn") or {}
        state = {"turn_id": turn.get("id", ""), "status": turn.get("status", "inProgress"), "done": asyncio.Event(), "text": self._turn_text(turn), "error": ""}
        self._turns[thread_id] = state
        if state["status"] == "failed":
            state["error"] = (turn.get("error") or {}).get("message", "turn failed")
        if state["status"] != "inProgress":
            state["done"].set()
        return state

    @staticmethod
    def _input(prompt: str, image_path: str) -> list[dict[str, Any]]:
        result = [{"type": "text", "text": prompt}]
        if image_path and Path(image_path).is_file():
            result.append({"type": "localImage", "path": str(Path(image_path).resolve())})
        return result


_CONNECTIONS: dict[tuple[str, str, str], _AppServerConnection] = {}
_PROCESS_REGISTRY_VERSION = 1


async def close_agent_connections() -> None:
    """Stop App Server children started by this process and clear cached clients."""
    connections = list(_CONNECTIONS.values())
    _CONNECTIONS.clear()
    await asyncio.gather(*(conn.close() for conn in connections), return_exceptions=True)


class CodexAgentClient:
    def __init__(self, config: BotConfig, *, session_key: str = ""):
        self._config = config
        self._session_key = session_key or "default"

    def has_active_turn(self) -> bool:
        thread_id = self._load_session_id()
        if not thread_id:
            return False
        state = self._connection(Path(self._config.agent_workspace).expanduser())._turns.get(thread_id)
        return bool(state and state.get("status") == "inProgress")

    def _session_path(self) -> Path:
        configured = self._config.agent_session_file
        return Path(configured).expanduser() if configured else default_config_path().with_name("agent-sessions.json")

    def _load_session_id(self) -> str:
        try:
            data = json.loads(self._session_path().read_text(encoding="utf-8"))
            value = data.get(self._session_key, "")
            if isinstance(value, dict):
                return str(value.get("thread_id", ""))
            return str(value)
        except (OSError, ValueError, TypeError):
            return ""

    def _save_session_id(self, session_id: str) -> None:
        if not session_id:
            return
        path = self._session_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except (OSError, ValueError):
                data = {}
            data[self._session_key] = session_id
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            self._debug(f"cannot save thread id: {exc}")

    def _command(self) -> str:
        configured = self._config.agent_command.strip() or "codex"
        if os.name == "nt" and Path(configured).suffix.lower() not in {".exe", ".cmd", ".bat", ".ps1"}:
            return shutil.which(configured + ".cmd") or shutil.which(configured + ".exe") or configured
        return configured

    def _debug(self, message: str) -> None:
        try:
            path = self._session_path().with_name("agent-debug.log")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        except OSError:
            pass

    @staticmethod
    def _prompt(config: BotConfig, kwargs: dict[str, Any], messages: list[dict[str, Any]]) -> tuple[str, str]:
        pending = next((item for item in reversed(messages) if item.get("role") == "user"), None)
        if pending is None:
            raise LLMError("agent_empty_context", "当前会话没有待处理的微信消息。")
        content = pending.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text")
        chat_name = str(kwargs.get("chat_name") or "")
        sender = str(kwargs.get("sender") or "")
        kind = "群" if kwargs.get("is_group") else "私"
        prompt = f"[wx:{kind}] {chat_name}|{sender}\n{content}"
        if kwargs.get("voice_path"):
            prompt += f"\n[v:{kwargs['voice_path']}]"
        if config.agent_reply_transport == "http":
            prompt += "\n[r:Http]"
        else:
            prompt += "\n[r:Mcp]"
        return prompt, str(kwargs.get("image_path") or "")

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        workspace = Path(self._config.agent_workspace).expanduser()
        if not self._config.agent_enabled:
            raise LLMError("agent_disabled", "本地 Agent 未启用。")
        if not workspace.is_dir():
            raise LLMError("agent_workspace_missing", f"Agent 工作区不存在：{workspace}")
        prompt, image_path = self._prompt(self._config, kwargs, messages)
        thread_id = self._load_session_id()
        conn = self._connection(workspace)
        try:
            if not thread_id:
                await conn.start()
                thread_id, reply = await conn.new_thread(prompt, image_path)
                self._save_session_id(thread_id)
                return reply
            try:
                return await conn.run(thread_id, prompt, image_path)
            except Exception as resume_error:
                message = str(resume_error).lower()
                if not any(token in message for token in ("not found", "does not exist", "invalid thread", "no such thread", "unknown thread", "no rollout found")):
                    state = conn._turns.get(thread_id)
                    if state and state.get("status") in {"failed", "interrupted", "cancelled"}:
                        # A failed turn can leave the chat with no response. Retire the
                        # thread and retry once in a new thread so the conversation can
                        # recover; future messages reuse that newly saved thread.
                        self._clear_session_id()
                        self._debug(f"turn failed; opening replacement thread: {resume_error}")
                        new_id, reply = await conn.new_thread(prompt, image_path)
                        self._save_session_id(new_id)
                        return reply
                    raise
                self._debug(f"saved thread is unavailable; opening new thread: {resume_error}")
                self._clear_session_id()
                thread_id, reply = await conn.new_thread(prompt, image_path)
                self._save_session_id(thread_id)
                return reply
        except Exception as exc:
            self._debug(f"thread={thread_id or '-'} app-server error: {exc}")
            raise LLMError("agent_failed", f"Codex 会话失败：{exc}") from exc

    def _clear_session_id(self) -> None:
        path = self._session_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop(self._session_key, None)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, ValueError, TypeError):
            pass

    async def steer_record(self, record: Any) -> bool:
        thread_id = self._load_session_id()
        if not thread_id:
            return False
        workspace = Path(self._config.agent_workspace).expanduser()
        conn = self._connection(workspace)
        state = conn._turns.get(thread_id)
        if not state or state.get("status") != "inProgress":
            return False
        kind = "群" if record.is_group else "私"
        prompt = f"[wx:{kind}] {record.chat}|{record.sender}\n{record.content}"
        if record.voice_path:
            prompt += f"\n[v:{record.voice_path}]"
        if self._config.agent_reply_transport == "http":
            prompt += "\n[r:Http]"
        else:
            prompt += "\n[r:Mcp]"
        image_path = record.image_path or ""
        try:
            await conn.request("turn/steer", {
                "threadId": thread_id,
                "expectedTurnId": state["turn_id"],
                "input": conn._input(prompt, image_path),
            })
            self._debug(f"steered active turn thread={thread_id} chat={record.chat!r}")
            return True
        except Exception as exc:
            self._debug(f"steer failed thread={thread_id}: {exc}")
            return False

    def _connection(self, workspace: Path) -> _AppServerConnection:
        command = self._command()
        key = (command, str(workspace.resolve()), self._config.agent_sandbox_mode)
        conn = _CONNECTIONS.get(key)
        if conn is None:
            conn = _AppServerConnection(command, workspace, self._config.agent_sandbox_mode, self._debug)
            _CONNECTIONS[key] = conn
        return conn
