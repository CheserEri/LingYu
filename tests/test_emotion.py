"""混合人格内核（``lingyu.bot.emotion``）的单元测试。

覆盖五层：身份（只读契约）、心境（L1）、情节记忆（L2）、人格札记（L3）与
表达（L4），以及宿主适配层。所有用例都不联网：模型客户端用假实现，状态与
账本目录一律用临时目录。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from lingyu.bot.emotion import (
    AsyncLLMAdapter,
    EmotionBridge,
    PersonaLedger,
    SessionStore,
    classify,
    default_state,
    dominant_of,
    normalize_state,
    pacing_plan,
    persistable,
    render_memo,
    run_turn,
)
from lingyu.bot.emotion import affect, episode, expression, memo, writer
from lingyu.bot.emotion.core import interests
from lingyu.bot.emotion.core.identity import list_identities, load_identity
from lingyu.bot.emotion.core.llm import NullClient
from lingyu.bot.emotion.pacing import MAX_LEAD
from lingyu.bot.emotion.session import MAX_STATE_BYTES, SCHEMA

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------- 事件分类


class ClassifyTests(unittest.TestCase):
    """微信消息 → 事件类型。分类错了，整条心境曲线就跟着错。"""

    def test_plain_chat_is_message(self):
        self.assertEqual(classify("今天下午开会到六点"), "message")

    def test_empty_is_message(self):
        self.assertEqual(classify(""), "message")
        self.assertEqual(classify(None), "message")

    def test_praise(self):
        self.assertEqual(classify("谢谢你，今天真的辛苦了"), "praise")

    def test_criticism(self):
        self.assertEqual(classify("你怎么老是这样"), "criticism")

    def test_rejection(self):
        self.assertEqual(classify("算了，不想聊了"), "rejection")

    def test_reassurance(self):
        self.assertEqual(classify("没事，你别担心"), "reassurance")

    def test_rejection_wins_over_praise(self):
        """「谢谢你，不用了」是拒绝，不是称赞——顺序即优先级。"""
        self.assertEqual(classify("谢谢你，不用了"), "rejection")

    def test_outcome_needs_negative_previous(self):
        text = "昨天在忙，刚看到"
        self.assertEqual(classify(text, prev_kind="rejection"), "outcome")
        self.assertEqual(classify(text, prev_kind="praise"), "message")

    def test_match_window_ignores_long_tail(self):
        body = "嗯" * 130 + "谢谢你"
        self.assertEqual(classify(body), "message")


# --------------------------------------------------------------------- 状态分片


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="lingyu-emotion-store-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_roundtrip_keeps_key(self):
        store = SessionStore(self.dir)
        store.save("wxid_a", turn=3, state=default_state(), chat_name="张三")
        payload = SessionStore(self.dir).load("wxid_a")
        self.assertEqual(payload["schema"], SCHEMA)
        self.assertEqual(payload["key"], "wxid_a")
        self.assertEqual(payload["chat"], "张三")
        self.assertEqual(payload["turn"], 3)
        self.assertEqual(payload["state"]["schema"], 2)

    def test_missing_state_is_empty_dict(self):
        self.assertEqual(SessionStore(self.dir).load("nobody"), {})

    def test_broken_file_treated_as_empty(self):
        store = SessionStore(self.dir)
        path = store.path_for("wxid_b")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(store.load("wxid_b"), {})

    def test_old_schema_is_ignored(self):
        """旧内核的状态不再迁移：直接当作「没有状态」重来。"""
        store = SessionStore(self.dir)
        path = store.path_for("old")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"key": "old", "turn": 9, "state": {"self.name": "宋暖"}}),
            encoding="utf-8",
        )
        self.assertEqual(store.load("old"), {})
        self.assertEqual(store.list_chats(), [])

    def test_list_chats_uses_real_key_not_hash(self):
        store = SessionStore(self.dir)
        store.save("wxid_a", turn=1, state=default_state(), chat_name="张三")
        self.assertEqual(len(store.list_chats()), 1)
        self.assertEqual(len(SessionStore(self.dir).list_chats()), 1)
        self.assertEqual(store.list_chats()[0]["key"], "wxid_a")

    def test_max_chats_evicts_oldest(self):
        store = SessionStore(self.dir, max_chats=2)
        store.save("a", turn=1, state=default_state(), last_at=1.0)
        store.save("b", turn=1, state=default_state(), last_at=2.0)
        store.save("c", turn=1, state=default_state(), last_at=3.0)
        keys = [item["key"] for item in SessionStore(self.dir, max_chats=2).list_chats()]
        self.assertEqual(sorted(keys), ["b", "c"])

    def test_shrink_trims_only_episodes(self):
        store = SessionStore(self.dir)
        state = default_state()
        state["episodes"] = [
            {"id": f"ep-{i:04d}", "excerpt": "x" * 2000} for i in range(400)
        ]
        store.save("big", turn=1, state=state)
        saved = store.load("big")["state"]
        self.assertLess(len(saved["episodes"]), len(state["episodes"]))
        self.assertIn("affect", saved)
        size = len(json.dumps(store.load("big"), ensure_ascii=False).encode("utf-8"))
        self.assertLess(size, MAX_STATE_BYTES)

    def test_clear_and_clear_all(self):
        store = SessionStore(self.dir)
        store.save("a", turn=1, state=default_state())
        store.save("b", turn=1, state=default_state())
        self.assertTrue(store.clear("a"))
        self.assertFalse(store.clear("a"))
        self.assertEqual(store.clear_all(), 1)


# --------------------------------------------------------------------- 模型适配


class _FakeClient:
    """宿主的 ``LLMClient`` 替身：``chat`` 是协程，记录调用次数。"""

    def __init__(self, reply: str = '{"text": "嗯"}') -> None:
        self.reply = reply
        self.calls = 0

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        return self.reply


class _LegacyClient:
    """不接受 ``response_format`` 的旧客户端：适配层应退回不带它的调用。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, *, max_tokens=300, temperature=0.9):
        self.calls += 1
        return '{"text": "在"}'


class AsyncLLMAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_expression_mode_skips_other_tags(self):
        fake = _FakeClient()
        adapter = AsyncLLMAdapter(fake, asyncio.get_running_loop(), mode="expression")
        self.assertIsNone(
            await asyncio.to_thread(adapter.complete_json, "s", "u", "persona")
        )
        self.assertEqual(fake.calls, 0)
        data = await asyncio.to_thread(adapter.complete_json, "s", "u", "expression")
        self.assertEqual(data, {"text": "嗯"})
        self.assertEqual(fake.calls, 1)

    async def test_all_mode_calls_for_every_tag(self):
        fake = _FakeClient('{"clues": []}')
        adapter = AsyncLLMAdapter(fake, asyncio.get_running_loop(), mode="all")
        await asyncio.to_thread(adapter.complete_json, "s", "u", "persona")
        await asyncio.to_thread(adapter.complete_json, "s", "u", "expression")
        self.assertEqual(fake.calls, 2)

    async def test_plain_text_accepted_for_expression(self):
        adapter = AsyncLLMAdapter(
            _FakeClient("听起来今天挺累的。"), asyncio.get_running_loop(),
            mode="expression",
        )
        data = await asyncio.to_thread(adapter.complete_json, "s", "u", "expression")
        self.assertEqual(data, {"text": "听起来今天挺累的。"})

    async def test_bad_json_reports_error(self):
        adapter = AsyncLLMAdapter(
            _FakeClient("我不好好返回 JSON"), asyncio.get_running_loop(), mode="all"
        )
        data = await asyncio.to_thread(adapter.complete_json, "s", "u", "persona")
        self.assertIsNone(data)
        self.assertEqual(adapter.last_error, "bad_content_json")

    async def test_client_without_response_format_still_works(self):
        legacy = _LegacyClient()
        adapter = AsyncLLMAdapter(legacy, asyncio.get_running_loop(), mode="all")
        data = await asyncio.to_thread(adapter.complete_json, "s", "u", "expression")
        self.assertEqual(data, {"text": "在"})
        self.assertEqual(legacy.calls, 1)


# --------------------------------------------------------------------- L1 心境


class AffectTests(unittest.TestCase):
    def test_default_state_is_schema_two(self):
        state = default_state()
        self.assertEqual(state["schema"], 2)
        self.assertEqual(set(state), {
            "schema", "affect", "relation", "episodes", "episode_count",
            "interest", "regulation", "meta",
        })
        self.assertIn("valence", state["affect"])
        self.assertIn("trust", state["relation"])

    def test_normalize_fills_defaults_and_clamps(self):
        state = normalize_state({
            "affect": {"valence": -9, "energy": 9},
            "episodes": [{"id": f"ep-{i}"} for i in range(80)],
        })
        self.assertEqual(state["affect"]["valence"], -1.0)
        self.assertEqual(state["affect"]["energy"], 1.0)
        self.assertEqual(len(state["episodes"]), 60)
        self.assertEqual(state["meta"]["recent_topics"], [])

    def test_normalize_rejects_garbage(self):
        self.assertEqual(normalize_state("nope"), default_state())
        self.assertEqual(normalize_state(None), default_state())

    def test_decay_lowers_arousal_and_raises_longing(self):
        state = default_state()
        state["affect"]["arousal"] = 0.8
        state["affect"]["longing"] = 0.2
        affect.decay(state, 6.0, 14.0)
        self.assertLess(state["affect"]["arousal"], 0.5)
        self.assertGreater(state["affect"]["longing"], 0.2)

    def test_decay_night_lowers_energy(self):
        state = default_state()
        affect.decay(state, 0.0, 3.0)
        self.assertLess(state["affect"]["energy"], 0.6)

    def test_appraise_criticism_raises_irritation(self):
        state = default_state()
        result = affect.appraise(kind="criticism", text="你怎么老是这样", state=state)
        delta = result["delta"]
        self.assertLess(delta["valence"], 0.0)
        self.assertGreater(delta["irritation"], 0.0)
        self.assertGreater(result["importance"], 0.2)

    def test_appraise_specific_cues(self):
        state = default_state()
        result = affect.appraise(kind="message", text="今天好累，有点烦", state=state)
        self.assertGreater(result["delta"]["irritation"], 0.0)
        self.assertLess(result["delta"]["energy"], 0.0)

    def test_apply_updates_relation_for_praise(self):
        state = default_state()
        before = state["relation"]["trust"]
        appraisal = affect.appraise(kind="praise", text="谢谢你", state=state)
        affect.apply(state, appraisal, sender="张三")
        self.assertGreater(state["relation"]["trust"], before)
        self.assertEqual(state["meta"]["last_kind"], "praise")

    def test_dominant_labels(self):
        state = default_state()
        state["affect"]["irritation"] = 0.7
        state["relation"]["trust"] = 0.2
        self.assertEqual(dominant_of(state), "烦躁")

        state = default_state()
        state["affect"]["energy"] = 0.2
        state["affect"]["arousal"] = 0.2
        self.assertEqual(dominant_of(state), "疲惫")

        state = default_state()
        state["affect"]["valence"] = -0.3
        self.assertEqual(dominant_of(state), "低落")

    def test_strategy(self):
        state = default_state()
        state["affect"]["irritation"] = 0.6
        self.assertEqual(affect.strategy_of(state, kind="criticism", local_hour=14.0), "delay")

        state = default_state()
        state["affect"].update(warmth=0.7, security=0.6, irritation=0.0)
        self.assertEqual(affect.strategy_of(state, kind="message", local_hour=14.0), "share")

        state = default_state()
        state["affect"]["security"] = 0.2
        self.assertEqual(
            affect.strategy_of(state, kind="message", local_hour=14.0), "seek_info"
        )

    def test_update_mutates_state(self):
        state = default_state()
        appraisal = affect.update(
            state, kind="praise", text="谢谢你", elapsed_hours=1.0,
            local_hour=14.0, interest_attention=0.4,
        )
        self.assertIn("delta", appraisal)
        self.assertEqual(state["meta"]["last_kind"], "praise")


# --------------------------------------------------------------------- 体感备忘


class MemoTests(unittest.TestCase):
    def test_default_memo_has_no_numbers(self):
        text = render_memo(default_state())
        self.assertTrue(text.endswith("。"))
        self.assertNotIn("0.", text)
        self.assertLessEqual(len(text), memo.MAX_CHARS)

    def test_warm_state_says_willing_to_listen(self):
        state = default_state()
        state["affect"].update(warmth=0.8, valence=0.4)
        self.assertIn("松的", render_memo(state))

    def test_low_energy_says_not_many_words(self):
        state = default_state()
        state["affect"]["energy"] = 0.2
        self.assertIn("话不太多", render_memo(state))

    def test_at_most_two_clauses(self):
        state = default_state()
        state["affect"].update(irritation=0.8, longing=0.8, security=0.2, energy=0.2)
        text = render_memo(state)
        clauses = [
            "语气会比平时短一点", "想先开口", "不太确定", "心里有点沉",
            "坐不太住", "心里是松的", "愿意多讲一点", "话不太多",
            "说话会先绕一下", "还算平静",
        ]
        self.assertLessEqual(sum(1 for item in clauses if item in text), memo.MAX_CLAUSES)


# --------------------------------------------------------------------- L2 情节记忆


def _episode(seq: int, *, kind: str = "praise", dominant: str = "愉快",
             importance: float = 0.6, chat: str | None = None) -> dict:
    item = {
        "id": f"ep-{seq:04d}",
        "at": float(seq),
        "kind": kind,
        "importance": importance,
        "dominant": dominant,
        "excerpt": "x",
        "then_belief": {"dominant": dominant},
        "now_review": None,
    }
    if chat is not None:
        item["chat"] = chat
    return item


class EpisodeTests(unittest.TestCase):
    def test_low_importance_is_dropped(self):
        state = default_state()
        self.assertIsNone(
            episode.append(
                state, kind="message", text="嗯",
                appraisal={"importance": 0.1}, dominant="平静",
            )
        )
        self.assertEqual(state["episodes"], [])

    def test_append_creates_bounded_ids_and_excerpt(self):
        state = default_state()
        item = episode.append(
            state, kind="criticism", text="你" * 120,
            appraisal={"importance": 0.8}, dominant="烦躁",
        )
        self.assertEqual(item["id"], "ep-0001")
        self.assertLessEqual(len(item["excerpt"]), episode.MAX_EXCERPT)
        self.assertEqual(len(state["episodes"]), 1)

    def test_episodes_are_capped(self):
        state = default_state()
        for i in range(70):
            state["episodes"].append(_episode(i))
        state["episodes"] = state["episodes"][-episode.MAX_EPISODES:]
        self.assertEqual(len(state["episodes"]), episode.MAX_EPISODES)

    def test_review_refutes_negative_then_outcome(self):
        state = default_state()
        state["episodes"] = [_episode(1, kind="criticism", dominant="低落")]
        self.assertEqual(
            episode.review_outcome(state, kind="outcome"), "refuted"
        )

    def test_review_confirms_positive_then_praise(self):
        state = default_state()
        state["episodes"] = [_episode(1, kind="praise", dominant="愉快")]
        self.assertEqual(
            episode.review_outcome(state, kind="reassurance"), "confirmed"
        )

    def test_review_uncertain_for_plain_message(self):
        state = default_state()
        state["episodes"] = [_episode(1, kind="praise", dominant="平静")]
        self.assertEqual(
            episode.review_outcome(state, kind="outcome"), "uncertain"
        )

    def test_review_ignores_light_kinds(self):
        state = default_state()
        state["episodes"] = [_episode(1)]
        self.assertIsNone(episode.review_outcome(state, kind="message"))

    def test_pending_excludes_refuted(self):
        state = default_state()
        state["episodes"] = [
            _episode(1, importance=0.8),
            _episode(2, importance=0.8),
            _episode(3, importance=0.2),
        ]
        state["episodes"][1]["now_review"] = {"verdict": "refuted"}
        ids = [item["id"] for item in episode.pending(state)]
        self.assertEqual(ids, ["ep-0001"])

    def test_with_chat_adds_source_hash(self):
        out = episode.with_chat([_episode(1)], "hash")
        self.assertEqual(out[0]["chat"], "hash")
        self.assertNotIn("chat", _episode(1))


# --------------------------------------------------------------------- L3 人格札记


class PersonaLedgerTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="lingyu-persona-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.ledger = PersonaLedger(self.dir, "violet")

    def _append(self, text: str = "被夸的时候，我会先不好意思。"):
        return self.ledger.append(
            text=text,
            signature="praise|愉快",
            sources=[{"chat": "abc", "episode": "ep-0001"}],
            counterexample="也有被敷衍的时候。",
            uncertainty=0.3,
            confidence=0.6,
            day="2026-11-12",
        )

    def test_append_is_active_and_persisted(self):
        entry = self._append()
        self.assertEqual(entry["id"], "p-0001")
        self.assertEqual(entry["status"], "active")
        again = PersonaLedger(self.dir, "violet").entries()
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["text"], entry["text"])

    def test_has_written_today(self):
        self._append()
        self.assertTrue(self.ledger.has_written_today("2026-11-12"))
        self.assertFalse(self.ledger.has_written_today("2026-11-13"))

    def test_revise_supersedes_old_and_links_new(self):
        old = self._append()
        new = self.ledger.revise(old["id"], "被夸的时候，我可以先接住。")
        self.assertIsNotNone(new)
        self.assertEqual(new["revision_of"], old["id"])
        statuses = {item["id"]: item["status"] for item in self.ledger.entries()}
        self.assertEqual(statuses[old["id"]], "superseded")
        self.assertEqual(statuses[new["id"]], "active")

    def test_revise_retracted_returns_none(self):
        old = self._append()
        self.ledger.retract(old["id"], reason="不成立")
        self.assertIsNone(self.ledger.revise(old["id"], "换一句说法。"))

    def test_retract_keeps_history(self):
        entry = self._append()
        out = self.ledger.retract(entry["id"], reason="证据不够")
        self.assertEqual(out["status"], "retracted")
        self.assertEqual(out["history"][-1]["action"], "retracted")
        self.assertEqual(out["history"][-1]["reason"], "证据不够")
        self.assertFalse(
            any(item["status"] == "active" for item in self.ledger.entries())
        )

    def test_active_excludes_retracted_and_superseded(self):
        first = self._append()
        second = self.ledger.revise(first["id"], "换个说法。")
        self.ledger.retract(second["id"], reason="证据不够")
        self.assertEqual(self.ledger.active(), [])

    def test_reset_returns_blank_ledger(self):
        self._append()
        self.ledger.reset()
        self.assertEqual(self.ledger.entries(), [])
        self.assertFalse(self.ledger.has_written_today("2026-11-12"))

    def test_trim_keeps_latest_entries(self):
        ledger = PersonaLedger(self.dir, "violet", max_entries=3)
        for i in range(5):
            ledger.append(
                text=f"第{i}条想法。", signature=f"s{i}", sources=[], day="2026-11-12"
            )
        texts = [item["text"] for item in ledger.entries()]
        self.assertEqual(len(texts), 3)
        self.assertIn("第4条想法。", texts)
        self.assertNotIn("第0条想法。", texts)

    def test_active_limit_archives_oldest(self):
        ledger = PersonaLedger(self.dir, "violet", max_active=1)
        first = ledger.append(
            text="第一条。", signature="a", sources=[], now=1.0
        )
        ledger.append(text="第二条。", signature="b", sources=[], now=2.0)
        statuses = {item["id"]: item["status"] for item in ledger.entries()}
        self.assertEqual(statuses[first["id"]], "archived")

    def test_corrupt_file_is_empty_and_not_overwritten(self):
        path = self.dir / "persona_violet.json"
        path.write_text("{ broken", encoding="utf-8")
        self.assertEqual(PersonaLedger(self.dir, "violet").entries(), [])
        self.assertEqual(path.read_text(encoding="utf-8"), "{ broken")

    def test_stats(self):
        self._append()
        stats = self.ledger.stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["counts"]["active"], 1)
        self.assertEqual(stats["identity"], "violet")
        self.assertTrue(stats["path"].endswith("persona_violet.json"))


# --------------------------------------------------------------------- 札记写入


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="lingyu-writer-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.ledger = PersonaLedger(self.dir, "violet")
        self.identity = load_identity("violet")

    def test_collect_candidates_groups_by_signature(self):
        episodes = [_episode(i, importance=0.8) for i in range(3)]
        candidates = writer.collect_candidates(episodes, min_evidence=3)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].signature, "praise|愉快")
        self.assertEqual(candidates[0].total, 3)

    def test_collect_candidates_requires_evidence(self):
        self.assertEqual(
            writer.collect_candidates([_episode(1)], min_evidence=3), []
        )

    def test_rule_text_is_short_and_numberless(self):
        text = writer.rule_text("praise", "愉快")
        self.assertGreaterEqual(len(text), 4)
        self.assertLessEqual(len(text), writer.MAX_TEXT)
        self.assertFalse(any(ch.isdigit() for ch in text))

    def test_consider_skips_without_evidence(self):
        result = writer.consider(
            identity=self.identity,
            ledger=self.ledger,
            episodes=[],
            day="2026-11-12",
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "证据不足")
        self.assertTrue(self.ledger.has_written_today("2026-11-12") is False)
        self.assertIn("2026-11-12", self.ledger.load()["days"])

    def test_consider_writes_rule_entry(self):
        episodes = [_episode(i, importance=0.8) for i in range(3)]
        result = writer.consider(
            identity=self.identity,
            ledger=self.ledger,
            episodes=episodes,
            day="2026-11-12",
        )
        self.assertEqual(result["status"], "written")
        entry = result["entry"]
        self.assertEqual(entry["status"], "active")
        self.assertEqual(entry["created_day"], "2026-11-12")
        self.assertEqual(len(entry["sources"]), 3)
        self.assertTrue(self.ledger.has_written_today("2026-11-12"))

    def test_consider_only_once_per_day(self):
        episodes = [_episode(i, importance=0.8) for i in range(3)]
        writer.consider(
            identity=self.identity, ledger=self.ledger, episodes=episodes,
            day="2026-11-12",
        )
        again = writer.consider(
            identity=self.identity, ledger=self.ledger, episodes=episodes,
            day="2026-11-12",
        )
        self.assertEqual(again["status"], "already_written")

    def test_consider_dedupes_same_signature(self):
        episodes = [_episode(i, importance=0.8) for i in range(3)]
        stamp = 1_800_000_000.0
        self.ledger.append(
            text="被夸的时候，我会先愣一下。",
            signature="praise|愉快",
            sources=[],
            day="2026-11-12",
            now=stamp,
        )
        next_day = writer.consider(
            identity=self.identity, ledger=self.ledger, episodes=episodes,
            day="2026-11-13", now=stamp,
        )
        self.assertEqual(next_day["status"], "skipped")
        self.assertEqual(next_day["reason"], "同一读法已存在")

    def test_disabled_identity_skips(self):
        identity = SimpleNamespace(persona_enabled=False)
        result = writer.consider(
            identity=identity, ledger=self.ledger,
            episodes=[_episode(i) for i in range(3)],
        )
        self.assertEqual(result["status"], "disabled")

    def test_llm_text_used_when_valid(self):
        identity = SimpleNamespace(
            persona_enabled=True,
            persona_min_evidence=1,
            persona_min_importance=0.3,
            persona_read_limit=3,
            persona_allow_llm=True,
            name="薇尔莉特",
            self_narrative="",
            principles=[],
        )
        proposal = {
            "action": "write",
            "text": "被夸的时候我可以先接住，不必急着否认。",
            "counterexample": "也有被敷衍的时候。",
            "uncertainty": 0.3,
            "confidence": 0.6,
            "sources": ["ep-0001"],
        }
        result = writer.consider(
            identity=identity,
            ledger=self.ledger,
            episodes=[_episode(1, importance=0.8)],
            llm=_FakeJSON(proposal),
            mode="llm",
        )
        self.assertEqual(result["status"], "written")
        self.assertEqual(result["entry"]["text"], proposal["text"])
        self.assertEqual(result["entry"]["history"][0]["by"], "llm")

    def test_llm_forbidden_text_falls_back_to_rule(self):
        identity = SimpleNamespace(
            persona_enabled=True,
            persona_min_evidence=1,
            persona_min_importance=0.3,
            persona_read_limit=3,
            persona_allow_llm=True,
            name="薇尔莉特",
            self_narrative="",
            principles=[],
        )
        proposal = {
            "action": "write",
            "text": "模型告诉我应该这样回应。",
            "counterexample": "",
            "uncertainty": 0.0,
        }
        result = writer.consider(
            identity=identity,
            ledger=self.ledger,
            episodes=[_episode(1, importance=0.8)],
            llm=_FakeJSON(proposal),
            mode="llm",
        )
        self.assertEqual(result["status"], "written")
        self.assertEqual(result["entry"]["history"][0]["by"], "rule")


class _FakeJSON:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def complete_json(self, system, user, tag, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        return dict(self.payload)


# --------------------------------------------------------------------- L4 表达


class ExpressionTests(unittest.TestCase):
    def test_rule_fallback_without_llm(self):
        state = default_state()
        result = expression.render(identity=load_identity("violet"), state=state, memo="平静。")
        self.assertFalse(result["silent"])
        self.assertTrue(result["text"])
        self.assertEqual(result["source"], "rule")

    def test_high_irritation_low_trust_is_silent(self):
        state = default_state()
        state["affect"]["irritation"] = 0.9
        state["relation"]["trust"] = 0.2
        result = expression.render(
            identity=load_identity("violet"),
            state=state,
            memo="烦。",
            event={"kind": "criticism"},
        )
        self.assertTrue(result["silent"])
        self.assertIn("缓一下", result["reason"])

    def test_llm_text_is_used(self):
        state = default_state()
        result = expression.render(
            identity=load_identity("violet"),
            state=state,
            memo="平静。",
            llm=_FakeJSON({"text": "我在，你慢慢说。"}),
        )
        self.assertEqual(result["source"], "llm")
        self.assertEqual(result["text"], "我在，你慢慢说。")

    def test_long_text_is_clipped_to_identity_limit(self):
        state = default_state()
        long_text = "我在。" * 200
        result = expression.render(
            identity=load_identity("violet"),
            state=state,
            memo="平静。",
            llm=_FakeJSON({"text": long_text}),
        )
        self.assertLessEqual(len(result["text"]), load_identity("violet").max_chars)

    def test_llm_silent_is_respected(self):
        state = default_state()
        result = expression.render(
            identity=load_identity("violet"),
            state=state,
            memo="平静。",
            llm=_FakeJSON({"silent": True, "reason": "先不接"}),
        )
        self.assertTrue(result["silent"])
        self.assertEqual(result["reason"], "先不接")

    def test_pacing_is_planned(self):
        state = default_state()
        result = expression.render(identity=load_identity("violet"), state=state, memo="平静。")
        self.assertGreaterEqual(result["pacing"].count, 1)
        self.assertIn("pacing", result["plan"])


# --------------------------------------------------------------------- 表达节奏


class PacingTests(unittest.TestCase):
    TEXT = "今天真的有点累，不想说话。我先躺一会儿。晚点再找你。"

    def test_empty_text_has_no_parts(self):
        self.assertEqual(pacing_plan("").parts, ())

    def test_low_activation_is_one_message(self):
        result = pacing_plan(self.TEXT, activation=0.15, intensity=0.8)
        self.assertEqual(result.count, 1)
        self.assertIn("唤醒度低", result.reason)

    def test_high_activation_splits_and_preserves_text(self):
        result = pacing_plan(self.TEXT, activation=0.9, intensity=0.6)
        self.assertGreater(result.count, 1)
        self.assertLessEqual(result.count, 3)
        self.assertEqual("".join(result.parts), self.TEXT)
        self.assertEqual(len(result.gaps), result.count - 1)

    def test_short_text_never_splits(self):
        self.assertEqual(pacing_plan("好。", activation=0.9, intensity=0.9).count, 1)

    def test_delay_strategy_waits_longest(self):
        delayed = pacing_plan("我在。", strategy="delay", intensity=0.6)
        shared = pacing_plan("我在。", strategy="share", intensity=0.6)
        self.assertGreater(delayed.lead, shared.lead)
        self.assertGreaterEqual(delayed.lead, 7.0)
        self.assertLess(shared.lead, 1.0)

    def test_lead_is_capped(self):
        self.assertLessEqual(pacing_plan("嗯。", strategy="delay", intensity=1.0).lead, MAX_LEAD)

    def test_gaps_shrink_as_activation_rises(self):
        hot = pacing_plan(self.TEXT, activation=0.95, intensity=0.8)
        warm = pacing_plan(self.TEXT, activation=0.65, intensity=0.8)
        self.assertLess(hot.gaps[0], warm.gaps[0])


# --------------------------------------------------------------------- 兴趣


class InterestTests(unittest.TestCase):
    def test_detects_software(self):
        cats = [cat for cat, _ in interests.detect("这段代码报错，日志里是空指针")]
        self.assertIn("software", cats)

    def test_small_talk_has_no_topic(self):
        for text in ("嗯", "在吗", "好的", ""):
            self.assertEqual(interests.detect(text), [], text)

    def test_violet_roots(self):
        roots = interests.roots_of(load_identity("violet"))
        for cat in ("software", "art", "engineering", "history"):
            self.assertIn(cat, roots)
        self.assertEqual(max(roots, key=roots.get), "emotion")

    def test_unknown_identity_gets_flat_roots(self):
        roots = interests.roots_of(None)
        self.assertEqual(set(roots), set(interests.CATEGORY_IDS))
        self.assertEqual(len(set(roots.values())), 1)

    def test_turn_records_topic_and_attention(self):
        result = run_turn(text="这段代码报错，日志里是空指针", llm=NullClient())
        interest = result.snapshot["state"]["interest"]
        self.assertEqual(interest["topic"], "software")
        self.assertGreater(interest["attention"], 0.5)
        self.assertIn("软件", interest["note"])


# --------------------------------------------------------------------- 一轮周期


class RunTurnTests(unittest.TestCase):
    def test_offline_turn_produces_text_or_silence(self):
        result = run_turn(text="今天有点累，不太想说话", llm=NullClient())
        self.assertEqual(result.rounds, 1)
        self.assertTrue(result.text or result.silent)
        self.assertIn("affect", result.snapshot["state"])

    def test_display_carries_observation_fields(self):
        result = run_turn(text="在吗", llm=NullClient())
        display = result.snapshot["display"]
        for key in ("dominant", "intensity", "memo", "affect", "relation", "interest", "persona"):
            self.assertIn(key, display)

    def test_persistable_returns_state_only(self):
        result = run_turn(text="在吗", llm=NullClient())
        kept = persistable(result.snapshot)
        self.assertEqual(kept["schema"], 2)
        self.assertIn("affect", kept)
        self.assertNotIn("expression", kept)

    def test_state_survives_to_next_turn(self):
        first = run_turn(
            text="谢谢你，今天真的辛苦了", kind="praise", llm=NullClient()
        )
        kept = persistable(first.snapshot)
        second = run_turn(
            text="谢谢你，今天真的很开心", state=kept, kind="praise",
            turn=2, llm=NullClient(),
        )
        self.assertEqual(second.snapshot["state"]["meta"]["turn"], 2)
        self.assertGreaterEqual(len(second.snapshot["state"]["episodes"]), 2)

    def test_persona_written_from_supplied_evidence(self):
        directory = Path(tempfile.mkdtemp(prefix="lingyu-runtime-persona-"))
        self.addCleanup(shutil.rmtree, directory, True)
        ledger = PersonaLedger(directory, "violet")
        evidence = [_episode(i, importance=0.8) for i in range(3)]
        result = run_turn(
            text="谢谢你，我今天真的很开心",
            llm=NullClient(),
            ledger=ledger,
            persona_episodes=evidence,
            today="2026-11-12",
        )
        self.assertEqual(result.snapshot["persona"]["status"], "written")
        self.assertTrue(ledger.has_written_today("2026-11-12"))
        self.assertTrue(result.snapshot["display"]["persona"]["entries"])

    def test_retracted_persona_not_in_prompt(self):
        directory = Path(tempfile.mkdtemp(prefix="lingyu-runtime-persona-"))
        self.addCleanup(shutil.rmtree, directory, True)
        ledger = PersonaLedger(directory, "violet")
        entry = ledger.append(
            text="被夸的时候，我会先不好意思。",
            signature="praise|愉快",
            sources=[],
            day="2026-11-12",
        )
        ledger.retract(entry["id"], reason="证据不够")

        result = run_turn(text="在吗", llm=NullClient(), ledger=ledger)
        self.assertEqual(result.snapshot["display"]["persona"]["entries"], [])

    def test_no_ledger_means_no_persona(self):
        result = run_turn(text="在吗", llm=NullClient())
        self.assertEqual(result.snapshot["display"]["persona"]["status"], "disabled")


# --------------------------------------------------------------------- 门面


def _config(directory: Path, **overrides):
    base = dict(
        emotion_identity="violet",
        emotion_llm_mode="none",
        emotion_persona_dir=str(directory),
        emotion_persona_enabled=True,
        temperature=0.9,
        max_tokens=300,
        timeout_seconds=30.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class EmotionBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="lingyu-emotion-bridge-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _bridge(self, **kwargs) -> EmotionBridge:
        config = _config(self.dir, **kwargs.pop("config", {}))
        return EmotionBridge(config, store=SessionStore(self.dir), **kwargs)

    async def test_respond_records_state(self):
        bridge = self._bridge()
        decision = await bridge.respond(
            chat_key="wxid_a", chat_name="张三", text="今天有点累", sender="张三"
        )
        self.assertEqual(decision.kind, "message")
        self.assertTrue(decision.text or decision.silent)
        for key in ("dominant", "memo", "affect", "relation", "interest", "pacing"):
            self.assertIn(key, decision.display)
        self.assertGreaterEqual(decision.pacing.count, 1)

        state = bridge.state("wxid_a")
        self.assertEqual(state["chat"], "张三")
        self.assertEqual(state["turn"], 1)
        self.assertEqual(bridge.stats()["chats"], 1)

    async def test_classify_feeds_into_decision(self):
        bridge = self._bridge()
        decision = await bridge.respond(
            chat_key="wxid_a", chat_name="张三", text="算了，不想聊了", sender="张三"
        )
        self.assertEqual(decision.kind, "rejection")

    async def test_reopen_is_debounced(self):
        bridge = self._bridge()
        await bridge.reopen(chat_key="a", chat_name="张三", elapsed_hours=10.0)
        again = await bridge.reopen(chat_key="a", chat_name="张三", elapsed_hours=10.0)
        self.assertTrue(again.silent)
        self.assertIn("刚刚评估过", again.reason)

    async def test_reset_clears_one_chat(self):
        bridge = self._bridge()
        await bridge.respond(chat_key="a", chat_name="张三", text="在吗", sender="张三")
        await bridge.respond(chat_key="b", chat_name="李四", text="在吗", sender="李四")
        self.assertEqual(bridge.reset("a"), 1)
        self.assertEqual(bridge.stats()["chats"], 1)
        self.assertEqual(bridge.reset(None), 1)
        self.assertEqual(bridge.stats()["chats"], 0)

    async def test_same_chat_runs_serially(self):
        bridge = self._bridge()
        await asyncio.gather(
            bridge.respond(chat_key="a", chat_name="张三", text="在吗", sender="张三"),
            bridge.respond(chat_key="a", chat_name="张三", text="在的", sender="张三"),
        )
        self.assertEqual(bridge.state("a")["turn"], 2)

    async def test_persona_disabled_means_empty_state(self):
        bridge = self._bridge(config={"emotion_persona_enabled": False})
        state = bridge.persona_state()
        self.assertFalse(state["enabled"])
        self.assertEqual(state["entries"], [])
        self.assertFalse(bridge.reset_persona())

    async def test_persona_revise_retract_reset(self):
        bridge = self._bridge()
        ledger = bridge._ledger("violet")
        entry = ledger.append(
            text="被夸的时候，我会先不好意思。",
            signature="praise|愉快",
            sources=[{"chat": "abc", "episode": "ep-0001"}],
            day="2026-11-12",
        )
        state = bridge.persona_state()
        self.assertTrue(state["enabled"])
        self.assertEqual(len(state["entries"]), 1)

        revised = bridge.revise_persona(entry["id"], "被夸的时候，我可以先接住。")
        self.assertEqual(revised["revision_of"], entry["id"])

        retracted = bridge.retract_persona(revised["id"], "证据不够")
        self.assertEqual(retracted["status"], "retracted")

        self.assertTrue(bridge.reset_persona())
        self.assertEqual(bridge.persona_state()["entries"], [])


# --------------------------------------------------------------------- 身份


class IdentityTests(unittest.TestCase):
    def test_builtin_identity_is_loadable(self):
        ident = load_identity("violet")
        self.assertEqual(ident.id, "violet")
        self.assertEqual(ident.name, "薇尔莉特")
        self.assertTrue(ident.self_narrative)
        self.assertTrue(ident.style_samples)

    def test_old_identity_id_maps_to_violet(self):
        self.assertEqual(load_identity("song_nuan").id, "violet")

    def test_list_identities_contains_violet_only_once(self):
        ids = [item["id"] for item in list_identities()]
        self.assertIn("violet", ids)
        self.assertNotIn("song_nuan", ids)

    def test_persona_policy_is_read(self):
        ident = load_identity("violet")
        self.assertTrue(ident.persona_enabled)
        self.assertGreaterEqual(ident.persona_min_evidence, 1)
        self.assertEqual(ident.persona_read_limit, 3)

    def test_unknown_identity_falls_back_to_neutral(self):
        ident = load_identity("no_such_identity")
        self.assertTrue(ident.id)

    def test_packaging_declares_identity_dir(self):
        spec = (ROOT / "packaging" / "lingyu.spec").read_text(encoding="utf-8")
        self.assertIn("lingyu/bot/emotion/identity", spec)


if __name__ == "__main__":
    unittest.main()
