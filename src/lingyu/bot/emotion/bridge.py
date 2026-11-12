"""宿主适配层：把微信的一条消息变成一轮情绪周期，再把结果翻译回「发什么」。

这一层负责四件事，情感内核本身不需要知道其中任何一件：

1. **线程边界**：内核是同步的（同步计算 + 同步 HTTP），宿主是 asyncio。
   这里把整轮计算丢进线程池，同一会话串行、不同会话并行。
2. **模型接线**：内核只认 ``complete_json(system, user, tag)``；宿主的大模型
   客户端是 ``async def chat(...)``。中间的转换在这里做（``run_coroutine_threadsafe``）。
3. **状态**：逐会话读写（:mod:`lingyu.bot.emotion.session`），并给出「距上次多久」。
4. **降级**：没配 Key、端点挂了、返回的不是 JSON —— 内核自己会退回规则模板，
   所以微信这一侧永远有话说，而不是「模型失败就不回复」。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from . import episode as episode_store
from .affect import normalize_state
from .core.identity import list_identities, load_identity
from .core.llm import NullClient, _extract_json
from .core.util import sha
from .kind import classify
from .pacing import Pacing, plan as pacing_plan
from .persona import PersonaLedger
from .runtime import TurnResult, persistable, run_turn
from .session import SessionStore

#: 同一会话里两次「重新开口」之间的最小间隔（秒）。真正的门槛由内核的
#: ``regulation.silence_hours`` 判定，这里只防抖。
MIN_REOPEN_GAP_SECONDS = 600.0


@dataclass
class EmotionDecision:
    """一轮周期的结论：发什么，或者为什么不发。"""

    text: str = ""
    silent: bool = False
    kind: str = "message"
    reason: str = ""
    turn: int = 0
    elapsed_hours: float = 0.0
    llm_calls: int = 0
    llm_error: str | None = None
    dominant: str = ""
    intensity: float = 0.0
    display: dict[str, Any] = field(default_factory=dict)
    #: 发送节奏（几条、间隔、先停多久），由情绪状态推导，不读配置。
    pacing: Pacing = field(default_factory=lambda: Pacing(parts=(), gaps=(), lead=0.0))

    def log_line(self, label: str) -> str:
        """一行可读的周期摘要，写进运行日志。"""
        head = f"{label} [{self.kind}] 第 {self.turn} 轮"
        mood = f"{self.dominant or '平静'} {self.intensity:.2f}"
        if self.silent:
            text = f"{head}：不发声（{mood}；{self.reason}）"
        else:
            text = f"{head}：{mood}｜{self.reason}"
        if not self.silent and self.pacing.parts and (
            self.pacing.count > 1 or self.pacing.lead >= 1.5
        ):
            # 节奏也进日志：为什么这条回得慢、为什么拆成三条，要能复核。
            text += f"｜{self.pacing.reason}"
        if self.llm_error and self.llm_error != "disabled":
            # 模型失败不静默吞掉：用户要能看出「这句是规则版回的」。
            # 「disabled」是「本来就没接模型」（没配 Key / 模式为 none），
            # 那是配置结果不是失败，不必每条都喊一次。
            text += f"｜模型失败（{self.llm_error}），已降级为规则版"
        return text


class AsyncLLMAdapter:
    """把宿主的 ``async def chat(messages)`` 适配成内核要的同步 ``complete_json``。

    只在工作线程里被调用；``run_coroutine_threadsafe`` 把协程交回宿主事件循环，
    所以两个世界各自保持自己的形态，不需要把任何一边改成对方的样子。
    """

    available = True

    def __init__(
        self,
        client: Any,
        loop: asyncio.AbstractEventLoop,
        *,
        mode: str = "all",
        temperature: float = 0.9,
        max_tokens: int = 300,
        timeout: float = 90.0,
    ):
        self._client = client
        self._loop = loop
        self._mode = mode
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout = timeout
        self.calls = 0
        self.last_error: str | None = None

    def complete_json(
        self,
        system: str,
        user: str,
        tag: str,
        *,
        images: list[str] | None = None,
    ) -> dict | None:
        if self._mode == "none":
            self.last_error = "llm_disabled"
            return None
        # ``expression`` 模式只让表达走模型：感知与评估用规则版，
        # 一轮只花一次调用，延迟与 token 都降到三分之一。
        if self._mode == "expression" and tag != "expression":
            return None

        content: Any = user
        if images:
            content = [{"type": "text", "text": user}]
            content += [
                {"type": "image_url", "image_url": {"url": uri}} for uri in images
            ]
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]
        # 感知与评估要的是稳定的结构化输出；只有表达需要一点随机性。
        temperature = self._temperature if tag == "expression" else 0.0
        try:
            coro = self._client.chat(
                messages,
                max_tokens=self._max_tokens,
                temperature=temperature,
                response_format={"type": "json_object"},
            )
        except TypeError:
            # 宿主客户端还没支持 response_format（旧实现 / 测试替身）。
            coro = self._client.chat(
                messages, max_tokens=self._max_tokens, temperature=temperature
            )

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            raw = future.result(timeout=self._timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            self.last_error = "timeout"
            return None
        except Exception as exc:  # noqa: BLE001 - 任何失败都只降级，不中断周期
            detail = getattr(exc, "message", None) or str(exc) or type(exc).__name__
            self.last_error = f"{type(exc).__name__}: {detail}"[:120]
            return None

        self.calls += 1
        text = str(raw or "").strip()
        data = _extract_json(text)
        if data is not None:
            return data
        self.last_error = "bad_content_json"
        # 表达模块允许模型直接给一句人话：比退回通用模板更贴语境。
        if tag == "expression" and 0 < len(text) <= 200 and "{" not in text:
            return {"text": text}
        return None


class EmotionBridge:
    """情绪引擎的门面：宿主只跟它打交道。"""

    def __init__(
        self,
        config: Any = None,
        *,
        store: SessionStore | None = None,
        logger: Callable[[str, str], None] | None = None,
        llm_factory: Callable[[Any], Any] | None = None,
    ):
        self._config = config
        self._store = store or SessionStore()
        self._log = logger or (lambda _level, _msg: None)
        self._llm_factory = llm_factory
        self._locks: dict[str, asyncio.Lock] = {}
        self._reopen_at: dict[str, float] = {}
        self._last_decision: dict[str, EmotionDecision] = {}
        self._personas: dict[str, PersonaLedger] = {}
        self._bind_user_identity_dir()

    @staticmethod
    def _bind_user_identity_dir() -> None:
        """把用户身份目录接到内核（``%APPDATA%\\lingyu\\identity``）。

        内置身份在包内 ``emotion/identity/``，用户想换人只要往这个目录放一个
        同名 ``<id>.json``；``load_identity`` 会先查用户目录。目录不存在时
        什么都不做——``list_identities`` 对缺失目录是安全的。
        """
        try:
            from ..config import default_config_path
            from .core.identity import set_user_identity_dir

            set_user_identity_dir(default_config_path().parent / "identity")
        except Exception:  # noqa: BLE001 - 身份目录不可用不该拖垮引擎
            pass

    def identities(self) -> list[dict[str, str]]:
        """可选身份清单。"""
        return list_identities()

    # ------------------------------------------------------------------ 配置

    def update_config(self, config: Any) -> None:
        self._config = config

    @property
    def store(self) -> SessionStore:
        return self._store

    # ------------------------------------------------------------------ 对外

    async def respond(
        self,
        *,
        chat_key: str,
        chat_name: str,
        text: str,
        sender: str,
        is_group: bool = False,
        recent: list[str] | None = None,
        images: list[str] | None = None,
    ) -> EmotionDecision:
        """处理一条入站消息：更新情绪，给出要发的话。"""
        # 状态读取与轮次计算必须在锁内：同一会话并行进来时，两边都读旧
        # payload 会把 turn 算成同一个值，后者覆盖前者。
        async with self._lock(chat_key):
            payload = self._store.load(chat_key)
            turn = int(payload.get("turn") or 0) + 1
            elapsed = _hours_since(payload.get("last_at"))
            kind = classify(
                text,
                prev_kind=str(payload.get("last_kind") or "") or None,
                unanswered_hours=elapsed,
            )
            event_text = f"{sender}: {text}" if is_group and sender else text
            # 对方重新开口了：上一段沉默的「已重开」标记作废，下次安静够久
            # 仍可主动开一次口。
            state = normalize_state(payload.get("state"))
            state["regulation"]["reopen_done"] = False
            return await self._cycle(
                chat_key=chat_key,
                chat_name=chat_name,
                text=event_text,
                sender=sender or "对方",
                kind=kind,
                turn=turn,
                elapsed=elapsed,
                state=state,
                recent=recent,
                images=images,
            )

    async def reopen(
        self,
        *,
        chat_key: str,
        chat_name: str,
        sender: str = "",
        elapsed_hours: float | None = None,
    ) -> EmotionDecision:
        """自发周期：久未回应时，由情绪状态决定「要不要轻轻开一次口」。

        这是对「按概率主动水群」的替换：说不说、说什么，由沉默时长、
        需求受阻程度与上一次的表达决定，而不是掷骰子。

        ``elapsed_hours`` 由宿主按「最后一条消息的时间」算好传进来——宿主的
        会话历史比状态文件更清楚「到底安静了多久」，内核只负责判断够不够久。
        """
        now = time.time()
        if now - self._reopen_at.get(chat_key, 0.0) < MIN_REOPEN_GAP_SECONDS:
            return EmotionDecision(silent=True, reason="刚刚评估过，先不重复打扰")
        self._reopen_at[chat_key] = now

        async with self._lock(chat_key):
            payload = self._store.load(chat_key)
            turn = int(payload.get("turn") or 0) + 1
            elapsed = (
                _hours_since(payload.get("last_at"))
                if elapsed_hours is None
                else max(0.0, min(168.0, float(elapsed_hours)))
            )
            state = normalize_state(payload.get("state"))
            identity = load_identity(str(getattr(self._config, "emotion_identity", "") or ""))
            # 沉默够不够久由身份文件的 silence_reopen_hours 决定，而不是
            # 宿主的定时器；同一段沉默只主动开一次口。
            if elapsed < identity.silence_reopen_hours:
                return EmotionDecision(
                    silent=True,
                    kind="self_expression",
                    reason=f"才安静 {elapsed:.1f} 小时，还没到开口的时候",
                    turn=turn,
                    elapsed_hours=elapsed,
                )
            if state["regulation"].get("reopen_done"):
                return EmotionDecision(
                    silent=True,
                    kind="self_expression",
                    reason="这段沉默已经开过一次口",
                    turn=turn,
                    elapsed_hours=elapsed,
                )
            decision = await self._cycle(
                chat_key=chat_key,
                chat_name=chat_name,
                text="",
                sender=sender,
                kind="self_expression",
                turn=turn,
                elapsed=elapsed,
                state=state,
                recent=None,
                images=None,
            )
            if not decision.silent:
                stored = normalize_state(self._store.load(chat_key).get("state"))
                stored["regulation"]["reopen_done"] = True
                self._store.touch(chat_key, state=stored)
            return decision

    def state(self, chat_key: str) -> dict[str, Any]:
        """某个会话的情绪状态摘要（界面 / 日志用）。"""
        payload = self._store.load(chat_key)
        display = dict(payload.get("display") or {})
        display.setdefault("chat", payload.get("chat") or chat_key)
        display["turn"] = int(payload.get("turn") or 0)
        display["last_kind"] = payload.get("last_kind") or ""
        display["last_at"] = payload.get("last_at")
        return display

    def recent_states(self, limit: int = 20) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in self._store.list_chats()[:limit]:
            summary = self.state(item["key"])
            summary.setdefault("chat", item["chat"])
            out.append(summary)
        return out

    def reset(self, chat_key: str | None = None) -> int:
        if chat_key:
            self._last_decision.pop(chat_key, None)
            self._reopen_at.pop(chat_key, None)
            return 1 if self._store.clear(chat_key) else 0
        self._last_decision.clear()
        self._reopen_at.clear()
        return self._store.clear_all()

    def stats(self) -> dict[str, Any]:
        identity_id = str(getattr(self._config, "emotion_identity", "violet") or "violet")
        ledger = self._ledger(identity_id)
        return {
            "chats": len(self._store.list_chats()),
            "dir": str(self._store.root),
            "persona": ledger.stats() if ledger else {"total": 0},
        }

    # ------------------------------------------------------------------ 内部

    def _lock(self, chat_key: str) -> asyncio.Lock:
        lock = self._locks.get(chat_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[chat_key] = lock
            if len(self._locks) > 512:
                # 只清理没被持有的锁：持有中的锁一旦换掉，同一会话就会并行。
                for key in [k for k, v in self._locks.items() if not v.locked()][:256]:
                    self._locks.pop(key, None)
        return lock

    async def _cycle(
        self,
        *,
        chat_key: str,
        chat_name: str,
        text: str,
        sender: str,
        kind: str,
        turn: int,
        elapsed: float,
        state: dict[str, Any] | None,
        recent: list[str] | None,
        images: list[str] | None,
    ) -> EmotionDecision:
        config = self._config
        identity_id = str(getattr(config, "emotion_identity", "violet") or "violet")
        identity = load_identity(identity_id)
        client = self._client()
        ledger = self._ledger(identity_id)
        local = time.localtime()
        local_hour = round(local.tm_hour + local.tm_min / 60.0, 3)
        today = date.today().isoformat()
        mode = "llm" if str(getattr(config, "emotion_llm_mode", "expression") or "").lower() == "all" else "rule"
        persona_episodes: list[dict[str, Any]] = []
        if ledger is not None and not ledger.has_written_today(today):
            persona_episodes = self._persona_evidence()

        # 调用方已持有同一会话的锁（respond / reopen）：不同会话之间没有
        # 共享的会话状态，全局人格账本自己带锁。
        loop = asyncio.get_running_loop()
        job = functools.partial(
            run_turn,
            text=text,
            state=state,
            kind=kind,
            elapsed=elapsed,
            turn=turn,
            sender=sender,
            identity_id=identity_id,
            llm=client,
            recent=recent,
            images=images,
            local_hour=local_hour,
            chat_key_hash=sha(chat_key, 16),
            ledger=ledger,
            persona_episodes=persona_episodes,
            today=today,
            mode=mode,
        )
        try:
            result: TurnResult = await loop.run_in_executor(None, job)
        except Exception as exc:  # noqa: BLE001 - 内核异常不能拖垮回复链路
            self._log("error", f"情绪周期异常 [{chat_name}]：{exc}")
            return EmotionDecision(
                silent=True, kind=kind, reason=f"周期异常：{type(exc).__name__}",
                turn=turn, elapsed_hours=elapsed,
                llm_error=f"emotion_cycle_error: {type(exc).__name__}: {exc}"[:120],
            )

        decision = _decision(
            result,
            kind=kind,
            turn=turn,
            elapsed=elapsed,
            client=client,
            max_chars=getattr(identity, "max_chars", 120),
        )
        self._store.save(
            chat_key,
            turn=turn,
            state=persistable(result.snapshot),
            chat_name=chat_name,
            last_kind=kind,
            last_at=time.time(),
            display=decision.display,
        )
        self._last_decision[chat_key] = decision
        return decision

    def _persona_dir(self) -> str:
        config = self._config
        raw = str(getattr(config, "emotion_persona_dir", "") or "").strip()
        if raw:
            return raw
        try:
            from ..config import default_config_path

            return str(default_config_path().parent / "persona")
        except Exception:  # noqa: BLE001
            return ""

    def _ledger(self, identity_id: str) -> PersonaLedger | None:
        if not bool(getattr(self._config, "emotion_persona_enabled", True)):
            return None
        directory = self._persona_dir()
        if not directory:
            return None
        ledger = self._personas.get(identity_id)
        if ledger is None:
            ledger = PersonaLedger(directory, identity_id)
            self._personas[identity_id] = ledger
        return ledger

    def _persona_evidence(self) -> list[dict[str, Any]]:
        """跨会话收集可写入札记的证据；只带来源哈希，不重复存原文。"""
        out: list[dict[str, Any]] = []
        for item in self._store.list_chats()[:50]:
            payload = self._store.load(item["key"])
            state = payload.get("state")
            if not isinstance(state, dict):
                continue
            episodes = state.get("episodes")
            if not isinstance(episodes, list):
                continue
            out.extend(episode_store.with_chat(episodes, sha(item["key"], 16)))
            if len(out) >= 200:
                break
        return out[:200]

    def persona_state(self, identity_id: str | None = None) -> dict[str, Any]:
        """给 WebUI：当前身份的完整札记账本。"""
        target = identity_id or str(getattr(self._config, "emotion_identity", "violet") or "violet")
        ledger = self._ledger(target)
        if ledger is None:
            return {"enabled": False, "entries": [], "stats": {}}
        return {
            "enabled": True,
            "entries": ledger.entries(),
            "stats": ledger.stats(),
        }

    def revise_persona(
        self, entry_id: str, text: str, identity_id: str | None = None
    ) -> dict[str, Any] | None:
        target = identity_id or str(getattr(self._config, "emotion_identity", "violet") or "violet")
        ledger = self._ledger(target)
        return ledger.revise(entry_id, text) if ledger else None

    def retract_persona(
        self, entry_id: str, reason: str = "", identity_id: str | None = None
    ) -> dict[str, Any] | None:
        target = identity_id or str(getattr(self._config, "emotion_identity", "violet") or "violet")
        ledger = self._ledger(target)
        return ledger.retract(entry_id, reason=reason) if ledger else None

    def reset_persona(self, identity_id: str | None = None) -> bool:
        target = identity_id or str(getattr(self._config, "emotion_identity", "violet") or "violet")
        ledger = self._ledger(target)
        if ledger is None:
            return False
        ledger.reset()
        return True

    def _client(self) -> Any:
        """按配置构造内核用的同步客户端；不可用时返回 NullClient（纯规则）。

        刻意**不在这里判 api_key**：没有 Key 时宿主客户端自己会立刻抛
        ``api_key_missing``（不产生网络请求），内核照常降级到规则版。
        少一个特判分支，测试替身也就不必关心 Key 有没有填。
        """
        config = self._config
        if self._llm_factory is None:
            return NullClient()
        mode = str(getattr(config, "emotion_llm_mode", "expression") or "expression").strip().lower()
        if mode not in ("all", "expression"):
            return NullClient()
        try:
            client = self._llm_factory(config)
        except Exception as exc:  # noqa: BLE001
            self._log("warning", f"情绪引擎取模型客户端失败，改用规则版：{exc}")
            return NullClient()
        if getattr(client, "chat", None) is None:
            return NullClient()
        return AsyncLLMAdapter(
            client,
            asyncio.get_running_loop(),
            mode=mode,
            temperature=float(getattr(config, "temperature", 0.9) or 0.0),
            max_tokens=max(200, int(getattr(config, "max_tokens", 300) or 300)),
            timeout=float(getattr(config, "timeout_seconds", 30.0) or 30.0) * 3.0,
        )


# ---------------------------------------------------------------------- 工具


def _hours_since(timestamp: Any) -> float:
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    return max(0.0, min(168.0, (time.time() - value) / 3600.0))


def _decision(
    result: TurnResult,
    *,
    kind: str,
    turn: int,
    elapsed: float,
    client: Any,
    max_chars: int = 120,
) -> EmotionDecision:
    snapshot = result.snapshot
    expression = snapshot.get("expression") if isinstance(snapshot.get("expression"), dict) else {}
    text = result.text.strip()
    silent = bool(result.silent) or not text
    reason = str(expression.get("reason") or "")
    if silent and not reason:
        reason = "选择沉默"
    elif not silent and not reason:
        reason = "表达完成"

    dominant = str(expression.get("dominant") or "")
    intensity = float(expression.get("intensity") or 0.0)
    raw_pacing = expression.get("pacing")
    if isinstance(raw_pacing, Pacing):
        pacing = raw_pacing
    elif isinstance(raw_pacing, dict):
        pacing = Pacing.from_dict(raw_pacing)
    else:
        pacing = pacing_plan(text)

    display = dict(snapshot.get("display") or {})
    display.update(
        {
            "kind": kind,
            "reason": reason,
            "silent": silent,
            "text": text,
            "rounds": result.rounds,
            "elapsed_hours": round(elapsed, 3),
            "at": time.time(),
            "max_chars": int(max_chars),
            "pacing": {
                "count": pacing.count,
                "lead": pacing.lead,
                "gaps": list(pacing.gaps),
                "reason": pacing.reason,
            },
        }
    )

    return EmotionDecision(
        text=text,
        silent=silent,
        kind=kind,
        reason=reason,
        turn=turn,
        elapsed_hours=elapsed,
        llm_calls=int(getattr(client, "calls", 0) or 0),
        llm_error=getattr(client, "last_error", None) or expression.get("llm_error") or None,
        dominant=dominant,
        intensity=round(intensity, 3),
        display=display,
        pacing=pacing,
    )
