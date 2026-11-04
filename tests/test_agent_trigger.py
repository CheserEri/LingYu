import unittest
from unittest.mock import AsyncMock, patch

from lingyu.bot.config import BotConfig
from lingyu.bot.engine import BotEngine
from tests.test_bot import FakeAdapter, make_record


class TriggerOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_event_triggers_once_without_outer_send(self):
        adapter = FakeAdapter()
        config = BotConfig(enabled=True, agent_enabled=True, agent_self_reply=True,
                           all_groups=False, groups=[], humanize=True,
                           reply_probability=0.0, quiet_hours_enabled=True,
                           quiet_hours_start="00:00", quiet_hours_end="23:59")
        engine = BotEngine(adapter, config)
        record = make_record(is_group=True, is_at_me=False, message_id="test-1")
        agent = AsyncMock()
        agent.chat.return_value = "not delivered by outer engine"
        with patch("lingyu.bot.engine.CodexAgentClient", return_value=agent):
            engine._record_inbound(record)
            await engine._handle(record)
            await engine._handle(record)
        self.assertEqual(agent.chat.await_count, 1)
        self.assertEqual(adapter.sent, [])
