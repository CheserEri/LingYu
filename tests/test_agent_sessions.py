import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from lingyu.bot.agent import CodexAgentClient, _AppServerConnection
from lingyu.bot.config import BotConfig


class AgentSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_uses_compact_channel_and_metadata_markers(self):
        config = BotConfig(agent_reply_transport="http")
        prompt, image = CodexAgentClient._prompt(
            config,
            {"chat_name": "群聊", "sender": "Alice", "is_group": True},
            [{"role": "user", "content": "hello"}],
        )
        self.assertEqual(prompt, "[wx:群] 群聊|Alice\nhello\n[r:Http]")
        self.assertEqual(image, "")

    async def test_failed_turn_reopens_in_new_thread_and_saves_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            mapping = Path(tmp) / "sessions.json"
            mapping.write_text(json.dumps({"chat": "old-id"}), encoding="utf-8")
            config = BotConfig(agent_enabled=True, agent_workspace=tmp,
                               agent_session_file=str(mapping))
            client = CodexAgentClient(config, session_key="chat")
            state = {"status": "failed", "error": "provider error"}
            conn = AsyncMock()
            conn.run.side_effect = RuntimeError("provider error")
            conn._turns = {"old-id": state}
            conn.new_thread.return_value = ("new-id", "recovered")
            with patch.object(client, "_connection", return_value=conn):
                result = await client.chat([
                    {"role": "user", "content": "hello"}
                ])
            self.assertEqual(result, "recovered")
            self.assertEqual(json.loads(mapping.read_text(encoding="utf-8")),
                             {"chat": "new-id"})
            conn.new_thread.assert_awaited_once()

    async def test_missing_rollout_reopens_in_new_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            mapping = Path(tmp) / "sessions.json"
            mapping.write_text(json.dumps({"chat": "missing-id"}), encoding="utf-8")
            config = BotConfig(agent_enabled=True, agent_workspace=tmp,
                               agent_session_file=str(mapping))
            client = CodexAgentClient(config, session_key="chat")
            conn = AsyncMock()
            conn.run.side_effect = RuntimeError(
                "no rollout found for thread id missing-id"
            )
            conn.new_thread.return_value = ("replacement-id", "recovered")
            with patch.object(client, "_connection", return_value=conn):
                result = await client.chat([
                    {"role": "user", "content": "hello"}
                ])
            self.assertEqual(result, "recovered")
            self.assertEqual(json.loads(mapping.read_text(encoding="utf-8")),
                             {"chat": "replacement-id"})

    async def test_turn_completion_notification_reports_failure(self):
        connection = _AppServerConnection("codex", Path.cwd(), "read-only", lambda _: None)
        done = asyncio.Event()
        connection._turns["thread"] = {
            "turn_id": "turn", "status": "inProgress", "done": done,
            "text": "", "error": "",
        }
        await connection._notification({
            "method": "turn/completed",
            "params": {"threadId": "thread", "turn": {
                "id": "turn", "status": "failed",
                "error": {"message": "network error"},
            }},
        })
        self.assertTrue(done.is_set())
        self.assertEqual(connection._turns["thread"]["status"], "failed")
        self.assertEqual(connection._turns["thread"]["error"], "network error")

    async def test_new_thread_starts_turn_without_resuming_empty_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            connection = _AppServerConnection("codex", Path(tmp), "read-only", lambda _: None)
            connection.process_registry = Path(tmp) / "processes.json"
            connection.start = AsyncMock()
            requests = []

            async def request(method, params):
                requests.append((method, params))
                if method == "thread/start":
                    return {"thread": {"id": "new-thread"}}
                return {"turn": {
                    "id": "new-turn", "status": "completed",
                    "items": [{"type": "agentMessage", "text": "reply"}],
                }}

            connection.request = request
            thread_id, reply = await connection.new_thread("hello")

        self.assertEqual((thread_id, reply), ("new-thread", "reply"))
        self.assertEqual([method for method, _ in requests], ["thread/start", "turn/start"])

    async def test_stale_pid_cleanup_only_terminates_owned_app_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            connection = _AppServerConnection("codex", Path(tmp), "read-only", lambda _: None)
            connection.process_registry = Path(tmp) / "processes.json"

            def registry(pid):
                connection.process_registry.write_text(json.dumps({
                    "version": 1,
                    "processes": {connection.process_key: {"pid": pid, "create_time": 1.0}},
                }), encoding="utf-8")

            owned = Mock()
            owned.create_time.return_value = 1.0
            owned.cmdline.return_value = ["codex", "app-server", "--stdio"]
            owned.exe.return_value = "C:\\tools\\codex.exe"
            with patch("lingyu.bot.agent.psutil.Process", return_value=owned):
                registry(123)
                connection._cleanup_stale_process()
            owned.terminate.assert_called_once_with()

            foreign = Mock()
            foreign.create_time.return_value = 1.0
            foreign.cmdline.return_value = ["codex", "app-server"]
            foreign.exe.return_value = "C:\\tools\\codex.exe"
            with patch("lingyu.bot.agent.psutil.Process", return_value=foreign):
                registry(456)
                connection._cleanup_stale_process()
            foreign.terminate.assert_not_called()
