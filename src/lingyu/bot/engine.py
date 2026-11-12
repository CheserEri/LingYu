"""实时自动回复引擎。

与「MCP 被动工具 + Agent 轮询」不同，引擎直接订阅适配层的实时消息：
每条入站消息到达瞬间入会话历史；当消息满足触发条件（群里 @我 / 引用回复我 /
刚跟它说过话的人继续发言 / 私聊）就交给**混合人格内核**处理——身份、心境、
情节记忆与人格札记算出这一刻「我是什么心情、该不该说话、说到什么程度」。

宿主**不配置任何行为**：
- **多久回**：由心境决定。调节策略 ``delay``（高烦躁 + 负效价）时先停几秒再回，
  ``share`` 时几乎立刻回；停顿上限 8 秒——再久就该是「沉默」而不是「延迟」。
- **怎么表达**：由心境决定。高唤醒且强度够 → 拆成几条连发，低唤醒 → 一整段说完
  （见 ``emotion/pacing.py``）。没有「最多拆几条」「间隔多少秒」这类开关。
- **人设**：身份文件（原则 / 边界 / 说话方式 / 第一人称自述）是只读内核；
  长期变化只通过可修订、可撤回的人格札记发生，没有「自定义人设」输入框。
- **几点不说话**：内核按本地钟点判断深夜、按 ``energy`` 决定收着说，
  没有「静默时段」起止时间要填。

「不回」仍是一等结果：自发周期里已表达过、调节要求收着时，这一轮就不发声。
保留的只有工程底线：消息去重、连发合并、发送重试、撤回、链接解析。
"""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from ..schemas import MessageRecord
from .config import BotConfig
from . import recall as recall_mod
from . import storage, tweet, xiaohongshu
from .card import note_to_card
from .emotion import EmotionBridge, EmotionDecision, SessionStore
from .links import (
    DownloadOutcome,
    LinkResolver,
    extract_urls,
    format_link_block,
    supported_urls,
    usable_cookies_file,
)
from .llm import LLMClient, LLMError

# 每个会话在引擎内保留的历史条数上限（上下文窗口之外的环形缓冲）。
PER_CHAT_HISTORY = 200

#: 送给情绪引擎的「最近对话」条数。比 context_messages 小得多：
#: 内核要的是指代与语气，不是把整段聊天记录塞进提示词。
RECENT_CONTEXT_LINES = 8

#: 连发合并窗口（秒）：**同一个人**在这个窗口内连发，算同一句话的补充，
#: 不再起一轮新周期。这是工程防抖（有人一口气发五条「在吗」），
#: 不是「回复间隔」——后者由情绪决定。不同人说话互不影响。
BURST_WINDOW_SECONDS = 1.5

# 对话闭合语：同一人以简短确认/致谢结束当前轮时，不再把它当作自动续聊触发。
# 若消息还包含问题或明确的新话题，则仍允许继续。
CONVERSATION_CLOSURES = ("好的", "好吧", "好哒", "收到", "知道了", "明白了",
                        "谢谢", "谢啦", "感谢", "嗯嗯", "行", "可以", "晚安")

#: 重新开口的评估节拍（秒）。只决定「多久看一次有没有该开口的会话」，
#: 不决定「说不说」；说不说在内核里（沉默时长 + 是否已重开过 + 是否深夜）。
REOPEN_TICK_SECONDS = 300.0

#: 同一个会话两次「重新开口」评估之间的最小间隔（秒）。防抖用：同一段沉默
#: 不必每 5 分钟问一遍内核「要不要开口」，答案不会变。
REOPEN_EVAL_GAP_SECONDS = 1800.0

# 下载失败重试之间的退避（秒）。首次立刻重试意义不大——yt-dlp 偶发的
# 「Unable to extract initial state」往往要等对面接口缓过来，隔一两秒再撞。
_RETRY_DELAYS = (1.5, 3.0, 6.0, 10.0)

async def _sleep(seconds: float) -> None:
    """发送节奏用的等待。

    单独抽出来是为了**可替换**：情绪决定的停顿最长 8 秒，一套用例若真等，
    跑一次要好几分钟。测试把这里换成「不等」，停顿的**数值**由
    ``emotion/pacing.py`` 的单元测试与一条流程用例（断言调用参数）覆盖。
    """
    await asyncio.sleep(seconds)


# 可交给视觉模型的图片类型与单张体积上限（超过则跳过，避免请求体过大）。
_IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
_MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _image_data_uri(path: str) -> str | None:
    """把本地图片读成 data URI；类型不支持、文件缺失或过大时返回 None。"""
    text = str(path or "").strip()
    if not text:
        return None
    file = Path(text)
    mime = _IMAGE_MIME.get(file.suffix.lower())
    if mime is None or not file.is_file():
        return None
    try:
        if file.stat().st_size > _MAX_IMAGE_BYTES:
            return None
        encoded = base64.b64encode(file.read_bytes()).decode("ascii")
    except OSError:
        return None
    return f"data:{mime};base64,{encoded}"


def _send_error(result: Any) -> str:
    """从发送结果里取一句可读的错误说明。"""
    error = getattr(result, "error", None)
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "未知原因")
    return str(error or "未知原因")


@dataclass
class HistoryItem:
    timestamp: float
    role: str  # "user" / "assistant"
    sender: str
    content: str
    is_group: bool = False
    # 该条消息对应的本地图片路径（非图片为空串），供视觉模型识别。
    image_path: str = ""


@dataclass
class LogLine:
    seq: int
    level: str
    message: str
    timestamp: float


class BotEngine:
    """订阅实时消息并按需自动回复。"""

    def __init__(
        self,
        adapter: Any,
        config: BotConfig | None = None,
        llm_factory: Callable[[BotConfig], Any] | None = None,
        emotion_store: SessionStore | None = None,
        *,
        adapters: list[Any] | None = None,
    ):
        # 多端并行：adapters 里的每个适配器都会订阅消息，回复回到消息来源那一端。
        self._adapters: list[Any] = list(adapters) if adapters else [adapter]
        self._adapter = self._adapters[0]
        # 适配器 → 监听回调。必须保留同一个回调对象，stop 时才能摘干净。
        self._listeners: dict[int, Callable[[MessageRecord], None]] = {}
        # 会话显示名 / 会话 ID → 来源适配器，用于把回复路由回正确的一端。
        self._chat_sources: dict[str, Any] = {}
        self._config = config or BotConfig.load()
        self._llm_factory = llm_factory or (lambda cfg: LLMClient(cfg))

        self._queue: asyncio.Queue[tuple[MessageRecord, Any]] = asyncio.Queue()
        self._history: dict[str, deque[HistoryItem]] = {}
        self._log_lines: deque[LogLine] = deque(maxlen=500)
        self._log_seq = 0
        # 日志可能来自任意线程（适配层回调、下载线程、桌面壳的控制台输出），
        # 而 get_logs 会整体遍历该 deque——并发追加会触发
        # 「deque mutated during iteration」，故读写都加同一把锁。
        self._log_lock = threading.Lock()
        self._processed_ids: deque[str] = deque(maxlen=1000)
        # 「会话::发送者」→ 上一次跑完情绪周期的时刻，用于连发合并（工程防抖）。
        self._last_cycle_at: dict[str, float] = {}
        # 已做过「链接服务」的消息 ID。与 _processed_ids 分开：链接服务独立于
        # 是否触发大模型回复，同一条消息可能既发固定提示、又触发一次模型回复。
        self._link_handled_ids: deque[str] = deque(maxlen=1000)
        # 会话键 → (最近被回复的发送者, 回复时间)，用于会话延续触发。
        self._last_reply_to: dict[str, tuple[str, float]] = {}
        # 会话键 → 最近一条入站消息，供主动参与挑选目标。
        self._last_inbound: dict[str, MessageRecord] = {}
        # 会话键 → 下一次允许「重新开口」评估的时刻（防抖，不参与判断）。
        self._proactive_cooldown_until: dict[str, float] = {}
        # 最近成功发言与入站事件落在情绪状态分片的元数据中，供重启恢复。
        self._reopen_meta: dict[str, dict[str, Any]] = {}

        self._consume_task: asyncio.Task | None = None
        self._proactive_task: asyncio.Task | None = None
        self._running = False

        self._date_key = time.strftime("%Y-%m-%d")
        self.reply_count_today = 0
        self.last_trigger_at: float | None = None

        # 链接解析（内置 yt-dlp）：结果带缓存，跨消息复用。
        self._links = LinkResolver(
            cache_size=256,
            timeout=self._config.link_parse_timeout,
            logger=self._log,
            cookies_file=self._config.link_cookies_file,
        )
        # 「下载并回发」在后台执行：大视频要下几分钟，而 _consume 串行消费队列，
        # 若在 _handle 里 await 会把后续消息全卡住。这里跟踪在跑的任务与 URL，
        # 同一个链接不会并发下载多次。
        self._download_tasks: set[asyncio.Task] = set()
        self._downloading_urls: set[str] = set()
        # 下载任务 → 会话显示名：撤回时要把该会话还没发出去的下载一起掐掉，
        # 免得刚撤完又冒出来一条同样的内容。
        self._download_task_chat: dict[asyncio.Task, str] = {}

        # 撤回：记录机器人最近发出的消息（文本 + 附件），供「@机器人 撤回」定位。
        self._recall_book = recall_mod.RecallBook()
        # 会话 → 上次执行撤回的时间，避免连点/重复投递把后续消息也撤了。
        self._last_recall_at: dict[str, float] = {}

        # 情绪引擎：回复由九个 Agent 共同决定，状态按会话分片落盘。
        # 必须在 _log 可用之后构造——它在出错时要写运行日志。
        self._emotion = EmotionBridge(
            self._config,
            store=emotion_store or SessionStore(self._config.emotion_dir()),
            logger=self._log,
            llm_factory=self._llm_factory,
        )

        # 桌面壳可注入：新日志产生时回调（用于推送到前端）。
        self.on_log: Callable[[LogLine], Any] | None = None

        # 附件发送失败后的重试次数由桥接层的类常量控制，随配置推给适配层
        # （适配层会在建桥/重连时套用，所以这里推一次就够了）。必须放在最后：
        # 推失败时会写运行日志，而写日志依赖上面刚建好的 _log_lines/_log_lock。
        self._push_send_attempts()

    # ------------------------------------------------------------------ 配置

    @property
    def config(self) -> BotConfig:
        return self._config

    @property
    def running(self) -> bool:
        return self._running

    def update_config(self, config: BotConfig) -> None:
        """界面保存配置后热替换（下次处理即生效）。"""
        self._config = config
        # 情绪引擎同步拿到新配置：身份、模型参与程度、轮次上限都要立刻生效。
        self._emotion.update_config(config)
        # 解析超时随配置热更新；缓存保留，避免重复解析。
        self._links.timeout = config.link_parse_timeout
        # 发送重试次数同样热更新，不必重启。
        self._push_send_attempts()
        # cookies.txt 换了路径要立刻生效：清掉失败缓存，让之前因缺 Cookie
        # 失败的链接可以马上重试，不用等 TTL 过期或重启程序。
        cookies_file = usable_cookies_file(config.link_cookies_file)
        if cookies_file != self._links.cookies_file:
            self._links.cookies_file = cookies_file
            self._links.clear_failed_cache()
            if cookies_file:
                self._log("info", f"已启用链接解析 Cookie：{cookies_file}")
            else:
                self._log("info", "已停用链接解析 Cookie（cookies.txt 未设置或不可读）。")

    def _push_send_attempts(self) -> None:
        """把「附件没发成功时最多再试几次」推给每个适配层。

        适配层是可注入的接口，测试里的假适配器不一定实现这个方法，取不到就
        安静跳过——重试次数推不过去不该拖垮启动或配置保存。
        """
        for adapter in self._adapters:
            apply = getattr(adapter, "apply_send_attempts", None)
            if apply is None:
                continue
            try:
                apply(self._config.link_send_attempts)
            except Exception as exc:  # noqa: BLE001 - 尽力而为
                self._log("warning", f"应用「发送失败后重试次数」失败：{exc}")

    # ------------------------------------------------------------------ 启停

    def _adapter_for(self, chat: str) -> Any:
        """按会话找来源适配器；找不到（如主动发送）回退到主适配器。"""
        return self._chat_sources.get(chat) or self._adapter

    def _listener_for(self, adapter: Any) -> Callable[[MessageRecord], None]:
        def listener(record: MessageRecord) -> None:
            self._chat_sources[record.chat] = adapter
            if record.chat_id:
                self._chat_sources[record.chat_id] = adapter
            self._enqueue(record, adapter)

        return listener

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for adapter in self._adapters:
            listener = self._listener_for(adapter)
            self._listeners[id(adapter)] = listener
            adapter.add_message_listener(listener)
        self._consume_task = asyncio.create_task(self._consume())
        self._proactive_task = asyncio.create_task(self._proactive_loop())
        self._log("info", "自动回复已开启。")

    async def set_adapters(self, adapters: list[Any]) -> None:
        """替换全部适配器（多端并行 / 平台或凭据变更），保留会话与人格状态。

        旧适配器应先由调用方 ``disconnect``，避免多条监听同时存活。
        """
        was_running = self._running
        if was_running:
            await self.stop()
        self._adapters = list(adapters)
        self._adapter = self._adapters[0]
        self._listeners.clear()
        self._chat_sources.clear()
        if was_running:
            await self.start()

    async def swap_adapter(self, adapter: Any) -> None:
        """单适配器热切换，等价于 ``set_adapters([adapter])``。"""
        await self.set_adapters([adapter])

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for adapter in self._adapters:
            listener = self._listeners.pop(id(adapter), None)
            if listener is not None:
                adapter.remove_message_listener(listener)
        for attr in ("_consume_task", "_proactive_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)
        for task in list(self._download_tasks):
            task.cancel()
        self._download_tasks.clear()
        self._downloading_urls.clear()
        self._log("info", "自动回复已停止。")

    def _enqueue(self, record: MessageRecord, adapter: Any = None) -> None:
        # 适配层回调在事件循环线程中同步调用，put_nowait 安全。
        self._queue.put_nowait((record, adapter))

    async def _consume(self) -> None:
        while self._running:
            record, source = await self._queue.get()
            # 两端可能碰巧用同一个显示名：处理这一条前按入队时记录重新钉一次来源，
            # 保证本条回复不会因为另一端的同名会话先入队而串到对面。
            if source is not None:
                self._chat_sources[record.chat] = source
                if record.chat_id:
                    self._chat_sources[record.chat_id] = source
            self._record_inbound(record)
            try:
                await self._handle(record)
            except Exception as exc:  # 兜底：单条异常不影响消费循环
                self._log("error", f"处理消息异常：{exc}")
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ 历史

    def _bucket(self, chat: str) -> deque[HistoryItem]:
        return self._history.setdefault(
            chat, deque(maxlen=PER_CHAT_HISTORY)
        )

    @staticmethod
    def _chat_key(record: MessageRecord) -> str:
        """会话稳定标识：优先用会话 ID，显示名可能是解析失败的退化值。"""
        return record.chat_id or record.chat

    @classmethod
    def _emotion_key(cls, record: MessageRecord) -> str:
        """情绪关系按人隔离；群聊历史仍按群共享。"""
        chat_key = cls._chat_key(record)
        if record.is_group:
            return f"{chat_key}::sender::{record.sender.strip()}"
        return chat_key

    def _record_inbound(self, record: MessageRecord) -> None:
        key = self._chat_key(record)
        self._bucket(key).append(
            HistoryItem(
                timestamp=record.timestamp or time.time(),
                role="user",
                sender=record.sender,
                content=record.content,
                is_group=record.is_group,
                image_path=record.image_path,
            )
        )
        # 记住最近一条入站消息，供主动参与挑选目标会话。
        self._last_inbound[key] = record
        emotion_key = self._emotion_key(record)
        meta = self._reopen_meta.setdefault(emotion_key, {})
        meta.update({"chat": record.chat, "sender": record.sender,
                     "is_group": record.is_group, "chat_id": key,
                     "last_inbound_at": record.timestamp or time.time(),
                     "pending": bool(meta.get("last_outbound_at"))})
        self._emotion.store.touch(emotion_key, reopen_meta=meta)

    def _record_assistant(self, chat: str, content: str, *, record: MessageRecord | None = None) -> None:
        self._bucket(chat).append(
            HistoryItem(
                timestamp=time.time(),
                role="assistant",
                sender="",
                content=content,
            )
        )
        if record is not None:
            emotion_key = self._emotion_key(record)
            meta = self._reopen_meta.setdefault(emotion_key, {})
            meta.update({"chat": record.chat, "sender": record.sender,
                         "is_group": record.is_group, "chat_id": self._chat_key(record),
                         "last_outbound_at": time.time(), "pending": True})
            self._emotion.store.touch(emotion_key, reopen_meta=meta)

    # ------------------------------------------------------------------ 触发

    def _in_group_scope(self, record: MessageRecord) -> bool:
        """会话是否落在配置的群聊作用范围内（群名或会话 ID 都认）。"""
        config = self._config
        if config.all_groups:
            return True
        return bool(
            record.chat in config.groups
            or (record.chat_id and record.chat_id in config.groups)
        )

    def _should_reply(self, record: MessageRecord) -> bool:
        """这条消息算不算「在跟我说话」——只判断**是不是对我说的**，不判断多久回。

        「隔了三个小时才回上一句」这种事交给内核：它拿到 ``elapsed_hours``
        与关系、需求受阻程度，自己决定该不该接、该用什么强度接。宿主设时间窗
        只会把它变成「过点就不理人」的机器。
        """
        if not record.content:
            return False
        if self._is_conversation_closure(record.content):
            # 礼貌收尾同时清掉续聊资格；之后同一个人发新话题仍可重新触发。
            self._last_reply_to.pop(self._chat_key(record), None)
            self._log("debug", f"识别到对话收尾，结束本轮续聊 [{record.chat}] {record.sender}。")
            return False
        if not record.is_group:
            return self._config.reply_private
        if not self._in_group_scope(record):
            return False
        if self._config.trigger_at and record.is_at_me:
            return True
        if self._config.trigger_reply and self._is_reply_to_me(record):
            return True
        return self._is_continuation(record)

    def _is_continuation(self, record: MessageRecord) -> bool:
        """会话延续：上一次对话里被回的就是这个人，他接着说，就接着听。

        不设时间窗：只要「最后被回的是他」且中间没有别人插进来，他说的每句话
        都进内核。是否真的接话由情绪决定（可能一句话都不回），而不是被
        「超过 120 秒就不理」这种规则挡在门外。
        """
        last = self._last_reply_to.get(self._chat_key(record))
        if not last:
            return False
        sender, _ts = last
        return sender == record.sender

    @staticmethod
    def _is_conversation_closure(text: str) -> bool:
        """识别无新问题的简短收尾语，避免礼貌确认被续成无限问答。"""
        body = str(text or "").strip()
        if not body or any(mark in body for mark in ("?", "？", "吗", "怎么", "什么", "为什么")):
            return False
        normalized = body.strip("。.!！~～… ")
        return normalized in CONVERSATION_CLOSURES

    def _is_reply_to_me(self, record: MessageRecord) -> bool:
        target = record.reply_to_name.strip()
        if not target:
            return False
        names = self._adapter_for(record.chat).self_names()
        if not names:
            # 上游尚未记录到自身昵称时，非空引用目标按「回复本人」处理。
            return True
        return target in names

    def _explain_no_reply(self, record: MessageRecord) -> None:
        """群消息没被触发时给一行可读原因。

        「群里 @ 了机器人却不回」最常见的原因就是**@ 的名字和机器人微信昵称不一致**
        （手打「@某某」不会产生真正的 @ 提醒，而本项目的 @ 识别就是拿消息文本里的
        ``@昵称`` 去比对登录账号的昵称）。此前这条路径**完全不打日志**，用户只看到
        入站消息、看不到任何解释，容易误以为程序坏了。这里把机器人**当前识别到的
        昵称**一并打出来，照着 @ 即可。
        """
        if not record.is_group:
            return
        if not self._in_group_scope(record):
            self._log("debug", f"群不在作用范围，已忽略 [{record.chat}]。")
            return
        if not self._config.trigger_at:
            self._log(
                "debug",
                f"群消息未命中触发条件（「@我」触发已关闭），已忽略 [{record.chat}]。",
            )
            return
        names = "、".join(sorted(self._adapter_for(record.chat).self_names())) or "（未识别到昵称）"
        self._log(
            "debug",
            f"群消息未 @ 机器人，已忽略 [{record.chat}] {record.sender}："
            f"{record.content[:30]}｜机器人昵称：{names}",
        )

    # ------------------------------------------------------------------ 处理

    async def _handle(self, record: MessageRecord) -> None:
        self._roll_day()

        if record.message_id and record.message_id in self._processed_ids:
            return

        # 撤回指令最先处理：违规内容越早撤掉越好，不能被链接服务、冷却或
        # 「拟人化概率」这些挡在前面。
        if await self._handle_recall(record):
            return

        # 链接服务：与「这条消息要不要回」无关。群里没 @ 机器人也该解析，
        # 因为这是确定性动作，不是「接话」。放在 _should_reply 之前。
        acked = await self._handle_links(record)

        if not self._should_reply(record):
            self._explain_no_reply(record)
            return

        if acked and not self._config.link_llm_followup:
            # 已经用固定文案应过声了，按配置不再让模型就这条消息接话。
            self._log(
                "info",
                f"已回固定链接提示，按配置不再让模型接话 [{record.chat}]。",
            )
            return

        now = time.time()
        chat_key = self._chat_key(record)
        # 连发合并：同一个人刚跑完一轮，紧接着又发一条，算同一句话的补充。
        # 只按「同一个人」算：群里张三李四先后 @ 它，是两件事，都要处理。
        burst_key = f"{chat_key}::{record.sender}"
        if now - self._last_cycle_at.get(burst_key, 0.0) < BURST_WINDOW_SECONDS:
            self._log("debug", f"连发合并，跳过 [{record.sender}]。")
            return

        if record.message_id:
            self._processed_ids.append(record.message_id)
        self.last_trigger_at = now

        self._log(
            "info",
            f"触发 [{record.chat}] {record.sender}：{record.content[:40]}",
        )

        # 先把上下文里的链接解析好，模型才能在下面看到「[链接] …」信息。
        if self._config.link_parse_enabled:
            await self._prefetch_links(chat_key)

        # 「这次回不回」不再掷骰子：交给情绪引擎判断（沉默是一等结果）。
        recent = self._recent_lines(chat_key)
        # 触发消息里若带媒体链接，已解析的标题/作者要一起进去，
        # 否则内核只看到一串 URL，评估不出这条消息对「我」意味着什么。
        block = self._trigger_link_block(chat_key)
        if block:
            recent.append(block)
        try:
            decision = await self._emotion.respond(
                chat_key=self._emotion_key(record),
                chat_name=record.chat,
                text=record.content,
                sender=record.sender,
                is_group=record.is_group,
                recent=recent,
                images=self._image_uris(chat_key),
            )
        finally:
            # 无论成败都记时刻：失败的周期也算「刚跑过」，避免异常时被连发刷屏。
            self._last_cycle_at[burst_key] = time.time()
        self._log(
            "info", decision.log_line(f"情绪 [{record.chat}] {record.sender}")
        )
        if decision.silent:
            return

        if not await self._send_reply(record.chat, decision):
            return

        self._record_assistant(chat_key, decision.text, record=record)
        self._last_reply_to[chat_key] = (record.sender, time.time())
        self.reply_count_today += 1
        if decision.pacing.count > 1:
            self._log(
                "info",
                f"已回复 [{record.chat}]（{decision.pacing.count} 条，"
                f"先停 {decision.pacing.lead:.1f}s）：{decision.text[:40]}",
            )
        else:
            self._log("info", f"已回复 [{record.chat}]：{decision.text[:40]}")

        # 链接内容的「下载并回发」已由 _handle_links 统一调度（无论这条消息
        # 是否触发模型回复），这里不再重复调度，避免同一链接被下载两次。

    # ------------------------------------------------------------------ 撤回

    def _is_recall_admin(self, sender: str) -> bool:
        name = str(sender or "").strip()
        if not name:
            return False
        return name in {item.strip() for item in self._config.recall_admins}

    def _cancel_downloads(self, chat: str) -> int:
        """取消该会话还没跑完的「下载并回发」任务，返回取消数量。

        正在跑的下载线程本身停不下来（线程池任务不可取消），但协程被取消后
        就**不会**再走到发送那一步——这正是撤回想要的效果。
        """
        cancelled = 0
        for task in list(self._download_tasks):
            if self._download_task_chat.get(task) != chat:
                continue
            if task.done():
                continue
            task.cancel()
            cancelled += 1
        return cancelled

    async def _handle_recall(self, record: MessageRecord) -> bool:
        """处理「@机器人 撤回」：撤掉机器人最新发出的（媒体）消息。

        返回 True 表示这条消息已被当成指令消费掉，后面不再走链接服务/大模型。
        """
        config = self._config
        if not config.recall_enabled:
            return False
        if not recall_mod.is_recall_command(
            record.content, config.recall_keyword_list()
        ):
            return False
        # 群聊作用范围：不在关注群里的「撤回」不当指令，避免误操作别的群。
        if record.is_group:
            if not self._in_group_scope(record):
                return False
            if config.recall_require_at and not record.is_at_me:
                # 群里必须 @ 机器人：日常聊天里出现「撤回」两个字太常见了。
                self._log(
                    "info",
                    f"群内「撤回」未 @ 机器人，不当指令 [{record.chat}] "
                    f"{record.sender}。",
                )
                return False
            if config.recall_admin_only and not self._is_recall_admin(
                record.sender
            ):
                self._log(
                    "info",
                    f"「撤回」指令来自非授权成员，忽略 [{record.chat}] "
                    f"{record.sender}。",
                )
                return False

        chat = record.chat
        now = time.time()
        # 指令已被消费：记进已处理集合，重复投递不会再撤一次。
        if record.message_id and record.message_id not in self._processed_ids:
            self._processed_ids.append(record.message_id)
        # 连点/重复投递：3 秒内的第二条指令直接吞掉，否则会把更早的正常消息也撤了。
        if now - self._last_recall_at.get(chat, 0.0) < 3.0:
            self._log("info", f"撤回指令过于频繁，忽略 [{chat}]。")
            return True
        self._last_recall_at[chat] = now

        self._log(
            "info", f"收到撤回指令 [{chat}] {record.sender}：{record.content[:30]}"
        )
        if config.recall_stop_download:
            stopped = self._cancel_downloads(chat)
            if stopped:
                self._log("info", f"已取消 {stopped} 个未完成的下载回发 [{chat}]。")

        plan = self._recall_book.plan(
            chat,
            now=now,
            window=config.recall_window_seconds,
            max_items=config.recall_max_messages,
        )
        if not plan:
            # 账本里没有媒体记录（刚重启、或撤的不是链接内容）：兜底撤最新一条，
            # 微信只提供「撤最新一条」，能撤掉就是有用的。
            plan = self._recall_book.plan(
                chat,
                now=now,
                window=config.recall_window_seconds,
                max_items=1,
                require_media=False,
            )
        fallback_single = not plan
        if fallback_single:
            plan = [recall_mod.SentItem(kind="unknown")]

        recalled: list[recall_mod.SentItem] = []
        for item in plan:
            if not await self._recall_one(chat):
                break
            recalled.append(item)

        if recalled:
            self._recall_book.forget(chat, len(recalled))
            described = "、".join(
                item.describe() for item in recalled if item.kind != "unknown"
            )
            self._log(
                "info",
                f"已撤回 {len(recalled)} 条消息 [{chat}]"
                + (f"：{described}" if described else "。"),
            )
        else:
            self._log("warning", f"撤回失败 [{chat}]（可能已超过 2 分钟或窗口不可识别）。")

        if config.recall_notice_enabled:
            text = config.recall_ok_text if recalled else config.recall_fail_text
            if text:
                await self._send_plain(chat, text)
        return True

    async def _recall_one(self, chat: str) -> bool:
        """让适配层撤回该会话最新一条机器人消息；成功返回 True。"""
        action = getattr(self._adapter_for(chat), "recall_last_message", None)
        if action is None:
            self._log("warning", "适配层不支持撤回，已跳过。")
            return False
        try:
            result = await action(chat)
        except Exception as exc:  # 撤回失败不该影响后续消息处理
            self._log("warning", f"撤回异常：{exc}")
            return False
        if getattr(result, "ok", False):
            return True
        self._log("warning", f"撤回未成功：{_send_error(result)}")
        return False

    # ------------------------------------------------------------------ 发送

    async def _send_reply(
        self, chat_display: str, decision: "EmotionDecision"
    ) -> bool:
        """按情绪定下的节奏发送：先停 ``lead``，再逐条发，条间按 ``gaps`` 停。

        节奏来自 ``emotion/pacing.py``（唤醒度 + 强度 + 调节策略），不是配置项。
        任一条失败即停止并返回 False——半句话比不发更糟。
        """
        pacing = decision.pacing
        parts = list(pacing.parts) or [decision.text]
        if pacing.lead > 0:
            # 「缓缓再回」：delay 策略下这是「先降低反应强度」在时间上的表现。
            # 上限 8 秒（pacing.MAX_LEAD），再长会堵住消息队列，那该用沉默表达。
            await _sleep(pacing.lead)
        for index, part in enumerate(parts):
            # 发送目标用显示名；适配层会把「退化成的会话 ID」反查成可搜索名。
            result = await self._adapter_for(chat_display).send_message(chat_display, part)
            if not result.ok:
                message = (result.error or {}).get("message", "结果未确认")
                self._log("error", f"回复发送失败：{message}")
                return False
            # 记账：撤回指令要能定位到「机器人最新发了什么」。
            self._recall_book.record_text(chat_display, part)
            if index < len(pacing.gaps):
                await _sleep(pacing.gaps[index])
        return True

    async def _send_plain(self, chat_display: str, text: str) -> bool:
        """发一条**不带情绪节奏**的固定文案（链接提示、撤回回执）。

        这些是机械通知，不是「我想说点什么」：不排队、不拆条、不等停顿。
        """
        result = await self._adapter_for(chat_display).send_message(chat_display, text)
        if not result.ok:
            message = (result.error or {}).get("message", "结果未确认")
            self._log("error", f"发送失败：{message}")
            return False
        self._recall_book.record_text(chat_display, text)
        return True

    def _recent_lines(self, chat: str) -> list[str]:
        """送给情绪引擎的「最近几轮对话」。

        不含当前这条触发消息（它本身就是事件文本）；群聊带发言人前缀，
        这样内核才能分清「谁在说话」——它默认面对的是一对一。
        """
        items = self._context_items(chat)
        if items and items[-1].role == "user":
            # 触发消息已经被 _record_inbound 追加进历史了，别重复给。
            items = items[:-1]
        lines: list[str] = []
        for item in items[-RECENT_CONTEXT_LINES:]:
            if item.role == "assistant":
                lines.append(f"我: {item.content}")
            elif item.is_group and item.sender:
                lines.append(f"{item.sender}: {item.content}")
            else:
                lines.append(item.content)
        return lines

    def _trigger_link_block(self, chat: str) -> str:
        """触发消息里已解析链接的上下文块；没有则空串。"""
        items = self._history.get(chat)
        if not items:
            return ""
        last = items[-1]
        if last.role != "user":
            return ""
        return self._link_block(last)

    def _image_uris(self, chat: str) -> list[str]:
        """按视觉范围选取图片（data URI），仅在开启视觉时给出。

        内核把图片交给「感知」模块：看到的是什么，比文字描述更可靠。
        """
        config = self._config
        if not config.vision_enabled or config.vision_max_images <= 0:
            return []
        uris: list[str] = []
        items = self._context_items(chat)
        if config.vision_scope == "current_message":
            items = items[-1:]
        for item in reversed(items):
            if item.role != "user" or not item.image_path:
                continue
            uri = _image_data_uri(item.image_path)
            if uri:
                uris.append(uri)
                if len(uris) >= config.vision_max_images:
                    break
        return uris

    # ------------------------------------------------------------------ 链接解析

    def _supported_urls(self, text: str) -> list[str]:
        """消息里**值得处理**的链接。

        默认只认媒体站点白名单（抖音/B 站/YouTube/X/小红书…）：购物、团购、
        新闻、公众号文章这类链接既没有音视频可发，也不该让机器人去解析并回一句
        「解析失败」。关掉 ``link_supported_only`` 才回到「所有链接都试一遍」。
        """
        if not self._config.link_supported_only:
            return extract_urls(text)
        return supported_urls(text, self._config.link_extra_site_list())

    def _context_items(self, chat: str) -> list[HistoryItem]:
        """当前会话送给模型的最近上下文（受 context_messages 限制）。"""
        items = self._history.get(chat)
        if not items:
            return []
        return list(items)[-self._config.context_messages :]

    async def _prefetch_links(self, chat: str) -> None:
        """解析上下文里出现过的**媒体站点**链接（带缓存，已解析过的不重复请求）。

        普通网页链接（购物、新闻、公众号…）不在这里解析：它们进不了上下文块，
        解析它们只是白白多发几次请求。
        """
        urls: list[str] = []
        for item in self._context_items(chat):
            if item.role == "user" and item.content:
                urls.extend(self._supported_urls(item.content))
        if not urls:
            return
        try:
            await self._links.resolve_many(urls, limit=self._config.link_parse_max)
        except Exception as exc:  # 解析失败不应影响正常回复
            self._log("warning", f"链接解析异常：{exc}")

    def _link_block(self, item: HistoryItem) -> str:
        """该条消息里「已解析」链接的上下文块；无则返回空串。"""
        if not self._config.link_parse_enabled or item.role != "user":
            return ""
        if not item.content:
            return ""
        infos = [
            info
            for info in (
                self._links.cached(url) for url in self._supported_urls(item.content)
            )
            if info is not None
        ]
        return format_link_block(infos)

    async def _handle_links(self, record: MessageRecord) -> bool:
        """链接服务：识别到**媒体链接**就回一句固定提示，并把内容处理掉。

        刻意**独立于** ``_should_reply``：群聊里没 @ 机器人时也要解析链接，
        因为这是确定性的服务动作，不是「接话」。提示语由配置写死，
        **不经过大模型**（``link_ack_text``），避免为一句「正在解析链接」烧 token。

        只有「媒体站点白名单」里的链接才会走到这里：购物、团购、新闻这类链接
        既不解析、也不提示——**失败提示只在真的下载不下来、发不出去时出现**。

        返回是否真的发出了固定提示（供 ``link_llm_followup`` 判断要不要再让模型接话）。
        """
        config = self._config
        if not config.link_parse_enabled:
            return False
        if not self._supported_urls(record.content):
            return False
        # 群聊作用范围：只在配置允许的群里动手，避免在无关群里刷屏。
        if record.is_group and not self._in_group_scope(record):
            return False
        if record.message_id:
            if record.message_id in self._link_handled_ids:
                return False
            self._link_handled_ids.append(record.message_id)

        ack = config.link_ack_text.strip()
        if not (config.link_ack_enabled and ack):
            # 提示关掉时仍要把链接内容回发，否则这个功能就完全没动作了。
            if config.link_download_enabled:
                self._schedule_download(record)
            return False

        if await self._send_plain(record.chat, ack):
            self._log("info", f"已发送链接提示 [{record.chat}]：{ack}")
            sent = True
        else:
            self._log("warning", f"链接提示发送失败 [{record.chat}]。")
            sent = False

        # 下载并回发（含推文卡片）在后台跑，不阻塞消息消费循环。
        if config.link_download_enabled:
            self._schedule_download(record)
        return sent

    def _schedule_download(self, record: MessageRecord) -> None:
        """把「下载并回发」放到后台执行，避免拖住消息消费循环。

        同一个链接在下载完成前不会重复起任务（用户可能连着发几次同一条链接）。
        """
        urls = self._supported_urls(record.content)[: self._config.link_download_max]
        fresh = [url for url in urls if url not in self._downloading_urls]
        if not fresh:
            return
        self._downloading_urls.update(fresh)
        task = asyncio.create_task(self._download_and_send(record))
        self._download_tasks.add(task)
        self._download_task_chat[task] = record.chat

        def _done(_task: "asyncio.Task[None]") -> None:
            self._download_tasks.discard(task)
            self._download_task_chat.pop(task, None)
            self._downloading_urls.difference_update(fresh)

        task.add_done_callback(_done)

    async def _download_and_send(self, record: MessageRecord) -> None:
        """把触发消息里链接的内容发回当前聊天。

        X（Twitter）推文与小红书笔记走特殊流程：先本地渲染一张卡片图发出去
        （两者共用同一套版式），推文/笔记**含视频**时再下载视频一并回发；
        其它链接沿用「下载后回发」。
        """
        urls = self._supported_urls(record.content)[: self._config.link_download_max]
        if not urls:
            return
        outdir = self._config.download_dir()
        self._ensure_download_dir_allowed(outdir)
        try:
            for url in urls:
                if tweet.is_tweet_url(url):
                    await self._send_tweet(record, url, outdir)
                elif xiaohongshu.is_xhs_url(url):
                    await self._send_xhs(record, url, outdir)
                else:
                    await self._download_and_send_one(record, url, outdir)
        finally:
            # 无论成功与否都收尾：下载目录长期累积会吃满磁盘。
            await self._enforce_download_quota(outdir)

    async def _enforce_download_quota(self, outdir: Path) -> None:
        """下载目录总占用超过配置上限时，删除最旧的文件直到降到上限以内。

        配置为 0 表示不限制，直接跳过。清理走线程池（要遍历目录），失败只记日志，
        不影响已经完成的下载回发。
        """
        quota = self._config.download_quota_bytes()
        if quota <= 0:
            return
        loop = asyncio.get_running_loop()
        try:
            removed = await loop.run_in_executor(
                None,
                lambda: storage.enforce_quota(outdir, quota, logger=self._log),
            )
        except Exception as exc:  # 清理是收尾动作，异常不该影响主流程
            self._log("warning", f"下载目录清理异常：{exc}")
            return
        if removed:
            freed = storage.total_size(removed) / (1024 * 1024)
            limit_mb = self._config.link_download_quota_mb
            self._log(
                "info",
                f"下载目录超过 {limit_mb} MB，已清理 {len(removed)} 个旧文件"
                f"（释放 {freed:.1f} MB）。",
            )

    async def _send_tweet(self, record: MessageRecord, url: str, outdir: Path) -> None:
        """X 推文：发卡片图（可选）；含视频则再下载视频回发。

        **多图合成一张长图**：几张配图按原比例竖排摞在卡片里，整条卡片就是
        一条长图——不裁切、也不拆成多条消息发。只有视频另发。
        """
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(
            None,
            lambda: tweet.fetch_tweet_cached(
                url, timeout=self._config.link_parse_timeout, logger=self._log
            ),
        )
        if info is None:
            # 抓不到推文（已删除/网络不通）时退回通用下载，尽量给出点东西。
            self._log("warning", f"推文抓取失败，改为直接下载：{url}")
            await self._download_and_send_one(record, url, outdir)
            return

        if self._config.link_tweet_card_enabled:
            # 先翻译（外文推文），再渲染：卡片正文用译文，原文缩成灰色小字。
            await self._translate_tweet(info, url)
            card = await loop.run_in_executor(
                None,
                lambda: tweet.render_card(
                    info, outdir, timeout=self._config.link_parse_timeout, logger=self._log
                ),
            )
            if card:
                await self._send_file(record.chat, card, "推文卡片")
            else:
                self._log("warning", f"推文卡片渲染失败：{url}")

        if info.has_video:
            await self._send_tweet_video(record, info, url, outdir)
        else:
            self._log("info", f"推文无视频，仅发卡片：{url}")

    async def _send_xhs(self, record: MessageRecord, url: str, outdir: Path) -> None:
        """小红书笔记：发卡片图（与推文卡片同一套版式）；视频笔记再下载视频回发。

        图文笔记**只发卡片**：原图常有 5～9 张，逐张发等于往聊天里刷 N 条消息，
        和「一条链接回发一份内容」的语义不符；卡片把标题、正文、配图竖排长图
        一次给全，信息不丢。
        """
        loop = asyncio.get_running_loop()
        # 分享出来的是短链（xhslink.cn/o/xxx），本身没有笔记 ID，必须先展开成
        # 带 xsec_token 的真实地址——下载那一路会自己展开，卡片这条路得先做。
        target = await loop.run_in_executor(
            None, self._links.expand_short_link, url
        )
        note = await loop.run_in_executor(
            None,
            lambda: xiaohongshu.note_for(
                target,
                cookies_file=usable_cookies_file(self._config.link_cookies_file),
                timeout=self._config.link_parse_timeout,
                logger=self._log,
            ),
        )
        if not note.ok:
            # 解析不出来（缺 xsec_token / 笔记已删 / 要求登录）时退回通用下载：
            # 那一路会把失败原因当提示发进聊天，用户能知道到底怎么了。
            self._log("warning", f"小红书解析失败，改为直接下载：{url}｜{note.error}")
            await self._download_and_send_one(record, url, outdir)
            return

        sent_card = False
        if self._config.link_xhs_card_enabled:
            card_path = await loop.run_in_executor(
                None,
                lambda: tweet.render_card(
                    note_to_card(note),
                    outdir,
                    timeout=self._config.link_parse_timeout,
                    logger=self._log,
                ),
            )
            if card_path:
                sent_card = await self._send_file(record.chat, card_path, "小红书卡片")
            else:
                self._log("warning", f"小红书卡片渲染失败：{url}")

        if note.is_video:
            # 视频在卡片之后再发一条；下载/重试/失败提示都复用通用那一路。
            await self._download_and_send_one(record, url, outdir)
        elif not sent_card:
            # 卡片关了或没渲染出来：图文退回通用下载，至少给出「暂不支持下载」的
            # 提示，而不是链接发出去石沉大海。
            await self._download_and_send_one(record, url, outdir)
        else:
            self._log("info", f"小红书图文笔记，仅发卡片：{url}")

    async def _translate_tweet(self, info: "tweet.Tweet", url: str) -> None:
        """把外文推文正文翻成中文，写进 ``info.translation``。

        失败一律**不抛异常**：翻译只是卡片上的增益，翻不出来就照原样渲染，
        不能因为模型不可用而把整张卡片也丢了。译好之后 ``Tweet`` 对象留在
        ``tweet._CACHE`` 里，同一条推文再次出现时不会重复翻译。
        """
        config = self._config
        if not config.link_tweet_translate:
            return
        if info.translation or not tweet.needs_translation(info.text):
            return
        target = (config.link_tweet_translate_target or "简体中文").strip()
        messages = [
            {
                "role": "system",
                "content": (
                    f"你是专业翻译。把用户给的推文正文翻译成{target}。"
                    "要求：只输出译文本身，不要加引号或任何解释，不要保留原文；"
                    "@用户名、#话题、链接、emoji 原样保留；语气自然、口语化。"
                ),
            },
            {"role": "user", "content": info.text.strip()},
        ]
        try:
            client = self._llm_factory(config)
            translated = await client.chat(
                messages, max_tokens=max(600, int(config.max_tokens)), temperature=0.3
            )
        except LLMError as exc:
            self._log("warning", f"推文翻译失败（沿用原文）：{exc.message}")
            return
        except Exception as exc:  # 翻译不该成为卡片的单点故障
            self._log("warning", f"推文翻译异常（沿用原文）：{exc}")
            return
        cleaned = str(translated or "").strip().strip("\"'“”").strip()
        if not cleaned:
            self._log("warning", f"推文翻译结果为空（沿用原文）：{url}")
            return
        info.translation = cleaned
        self._log("info", f"已翻译推文（{len(info.text)}→{len(cleaned)} 字）：{url}")

    async def _send_tweet_video(
        self, record: MessageRecord, info: "tweet.Tweet", url: str, outdir: Path
    ) -> None:
        """推文含视频时下载并回发。

        优先用 syndication 给的 mp4 直链（``Tweet.video_url``）——比走 yt-dlp 的
        X 提取器可靠（后者默认 GraphQL，未登录易 403）；拿不到直链或直链下载失败
        时再退回 yt-dlp 通用下载。
        """
        if info.video_url:
            path = await self._retry_tweet_video(info, url, outdir)
            if path and await self._send_file(record.chat, path, "推文视频"):
                return
            self._log("warning", f"推文视频直链下载失败，改用 yt-dlp 兜底：{url}")
        # 兜底这一路负责发失败提示：直链挂了还有 yt-dlp，等它也没辙再提示一次，
        # 免得同一段视频往聊天里连发两条坏消息。
        await self._download_and_send_one(record, url, outdir)

    async def _retry_tweet_video(
        self, info: "tweet.Tweet", url: str, outdir: Path
    ) -> str | None:
        """直链下载推文视频，失败按 ``link_download_attempts`` 重试。

        这里**不发**失败提示，由兜底的 yt-dlp 那一路统一发（见上）。
        """
        loop = asyncio.get_running_loop()
        attempts = max(1, int(self._config.link_download_attempts))
        for index in range(1, attempts + 1):
            path = await loop.run_in_executor(
                None,
                lambda: tweet.download_video(
                    info,
                    outdir,
                    timeout=max(self._config.link_parse_timeout, 60.0),
                    logger=self._log,
                    max_mb=self._config.link_download_max_mb,
                ),
            )
            if path:
                if index > 1:
                    self._log("info", f"推文视频第 {index} 次尝试下载成功：{url}")
                return path
            self._log(
                "warning", f"推文视频直链下载失败（第 {index}/{attempts} 次）：{url}"
            )
            if index < attempts:
                await asyncio.sleep(
                    _RETRY_DELAYS[min(index - 1, len(_RETRY_DELAYS) - 1)]
                )
        return None

    async def _download_with_retry(
        self,
        record: MessageRecord,
        url: str,
        outdir: Path,
        label: str = "链接内容",
        notify: bool = True,
    ) -> str | None:
        """下载链接内容，失败自动重试；最终仍失败时往聊天里发一句提示。

        次数取 ``link_download_attempts``（默认 3，含首次）。两次之间按
        ``_RETRY_DELAYS`` 退避——yt-dlp 偶发的「Unable to extract initial
        state」往往要等对面接口缓过来，立刻重试基本必挂。

        **确定性失败**（体积超限、图文作品、站点不支持）不重试：再试也一样，
        只会让用户多等几轮退避。
        """
        config = self._config
        attempts = max(1, int(config.link_download_attempts))
        outcome = DownloadOutcome(error="未尝试")
        used = 0
        for index in range(1, attempts + 1):
            used = index
            outcome = await self._links.download_detailed(
                url, str(outdir), max_mb=config.link_download_max_mb
            )
            if outcome.path:
                if index > 1:
                    self._log("info", f"{label}第 {index} 次尝试下载成功：{url}")
                return outcome.path
            detail = f"｜{outcome.error}" if outcome.error else ""
            self._log(
                "warning", f"{label}第 {index}/{attempts} 次下载失败：{url}{detail}"
            )
            if not outcome.retryable:
                self._log("info", f"{label}属于确定性失败，不再重试：{url}")
                break
            if index < attempts:
                await asyncio.sleep(
                    _RETRY_DELAYS[min(index - 1, len(_RETRY_DELAYS) - 1)]
                )
        if notify:
            await self._notify_download_failure(record, label, outcome, used)
        return None

    async def _notify_download_failure(
        self,
        record: MessageRecord,
        label: str,
        outcome: DownloadOutcome,
        used: int,
    ) -> None:
        """下载最终失败时告诉用户一声，免得链接发出去像石沉大海。

        走 ``_send_plain``：这是机械通知，不该被情绪节奏拆条或延迟。
        """
        if not self._config.link_download_fail_notice:
            return
        reason = (outcome.error or "").strip()[:80]
        head = f"{label}下载失败"
        if used > 1:
            head += f"（重试 {used - 1} 次仍不行）"
        text = f"{head}：{reason}，先不发了。" if reason else f"{head}，先不发了。"
        if await self._send_plain(record.chat, text):
            self._log("info", f"已发送{label}下载失败提示 [{record.chat}]：{text}")
        else:
            self._log("warning", f"{label}下载失败提示发送失败 [{record.chat}]。")

    async def _download_and_send_one(
        self,
        record: MessageRecord,
        url: str,
        outdir: Path,
        notify: bool = True,
    ) -> None:
        self._log("info", f"开始下载链接内容：{url}")
        path = await self._download_with_retry(
            record, url, outdir, notify=notify
        )
        if not path:
            return
        await self._send_file(record.chat, path, "链接内容")

    async def _send_file(self, chat: str, path: str | Path, label: str) -> bool:
        """把文件发回聊天；成功返回 True。"""
        try:
            result = await self._adapter_for(chat).send_file(chat, str(path))
        except Exception as exc:
            self._log("warning", f"{label} 发送异常：{exc}")
            return False
        if getattr(result, "ok", False):
            self._log("info", f"已发回{label}：{Path(path).name}")
            # 记账：媒体是「违规内容」的主要载体，撤回时优先撤它。
            self._recall_book.record_media(chat, path, label)
            return True
        self._log("warning", f"{label} 发送未成功：{_send_error(result)}")
        return False

    def _ensure_download_dir_allowed(self, outdir: Path) -> None:
        """把下载目录纳入每个适配器的发送白名单。

        该目录由本程序创建、内容可控，故可安全放行；不这样做的话，适配层的
        「未配置允许目录即拒发」会让自动回发永远失败。
        """
        try:
            resolved = Path(outdir).expanduser().resolve()
        except OSError:
            return
        for adapter in self._adapters:
            config = getattr(adapter, "config", None)
            if config is None:
                continue
            existing = tuple(getattr(config, "allow_send_dirs", ()) or ())
            for item in existing:
                try:
                    if Path(str(item)).expanduser().resolve() == resolved:
                        break
                except OSError:
                    continue
            else:
                try:
                    config.allow_send_dirs = existing + (resolved,)
                except Exception:  # 配置不可写时静默：发送时会给出明确错误
                    pass

    # ------------------------------------------------------------------ 重新开口

    async def _proactive_loop(self) -> None:
        """周期性检查「我说过话、对方一直没回」的会话，由情绪决定要不要开口。

        宿主在这里只提供**一个时钟**：多久看一次由 ``REOPEN_TICK_SECONDS`` 决定，
        而说不说完全在内核里（沉默够不够久、是否已经重开过、是否深夜）。
        这里刻意没有「允不允许主动开口」的开关——设计文档里「是否保持沉默」
        是表达 Agent 的职责（§3.7），把它做成宿主配置就等于让用户替她决定。
        唯一的闸门是总开关 ``enabled``：机器人停用时不该有任何外发。
        """
        while self._running:
            await asyncio.sleep(REOPEN_TICK_SECONDS)
            if not self._running:
                break
            if not self._config.enabled:
                continue
            try:
                await self._reopen_tick()
            except Exception as exc:  # 兜底：单次异常不影响循环
                self._log("error", f"重新开口检查异常：{exc}")

    def _reopen_candidates(self, now: float) -> list[tuple[str, MessageRecord]]:
        """筛选出「可以评估一次要不要重新开口」的会话。

        条件只有两个：最后一条是**我说的话**、且不在评估冷却里。沉默够不够久
        由内核判断（``silence_reopen_hours`` 在身份文件里），深夜不开口也是
        内核的规则——宿主不再用「安静几小时」「这个群回过几条」去挡。
        """
        candidates: list[tuple[str, MessageRecord]] = []
        entries = dict(self._reopen_meta)
        for item in self._emotion.store.list_chats():
            emotion_key = item["key"]
            payload = self._emotion.store.load(emotion_key)
            meta = payload.get("reopen_meta")
            if isinstance(meta, dict):
                entries.setdefault(emotion_key, dict(meta))
        for emotion_key, meta in entries.items():
            if not meta.get("pending"):
                continue
            chat = str(meta.get("chat") or "")
            record = next((r for r in self._last_inbound.values()
                           if self._emotion_key(r) == emotion_key), None)
            if record is None:
                record = MessageRecord(chat=chat, sender=str(meta.get("sender") or ""),
                                       content="", is_group=bool(meta.get("is_group")),
                                       chat_id=str(meta.get("chat_id") or ""))
            if record.is_group and not self._in_group_scope(record):
                continue
            inbound_at = float(meta.get("last_inbound_at") or 0.0)
            outbound_at = float(meta.get("last_outbound_at") or 0.0)
            if inbound_at > outbound_at:
                continue
            if now < self._proactive_cooldown_until.get(emotion_key, 0.0):
                continue
            candidates.append((emotion_key, record))
        candidates.sort(key=lambda item: float(entries[item[0]].get("last_outbound_at") or 0.0))
        return candidates

    async def _reopen_tick(self) -> None:
        """一次检查：至多让一个会话重新开口。"""
        now = time.time()
        candidates = self._reopen_candidates(now)
        for emotion_key, record in candidates:
            await self._reopen_one(emotion_key, record, now)

    def _hours_since_last(self, key: str, now: float | None = None) -> float:
        """该会话「最后一条消息距今多少小时」；没有历史时返回 0。"""
        meta = self._reopen_meta.get(key)
        if meta is None:
            payload = self._emotion.store.load(key)
            meta = payload.get("reopen_meta") or {}
        last = float(meta.get("last_outbound_at") or 0.0)
        if last <= 0:
            return 0.0
        moment = now if now is not None else time.time()
        return max(0.0, min(168.0, (moment - last) / 3600.0))

    async def _reopen_one(
        self, key: str, record: MessageRecord, now: float
    ) -> None:
        """让情绪引擎跑一轮自发周期：说，或者不说。

        说不说由沉默时长、需求受阻程度与上一次的表达共同决定；说过一次之后
        内核会记住 ``reopen_done``，不会反复追问。深夜内核自己会 hold。
        """
        decision = await self._emotion.reopen(
            chat_key=self._emotion_key(record),
            chat_name=record.chat,
            # 宿主比状态文件更清楚「安静了多久」：按最后一条消息的时间算，
            # 而不是按上一次跑周期的时刻算（两者可能差好几个小时）。
            elapsed_hours=self._hours_since_last(key, now),
        )
        self._log("info", decision.log_line(f"重新开口 [{record.chat}]"))
        # 无论说没说都推后下一次评估：既避免刚说完又冒一句，也避免每 5 分钟
        # 拿同一段沉默反复问内核（答案不会变）。
        self._proactive_cooldown_until[key] = now + REOPEN_EVAL_GAP_SECONDS
        if decision.silent:
            return

        if not await self._send_reply(record.chat, decision):
            return
        self._record_assistant(self._chat_key(record), decision.text, record=record)
        meta = self._reopen_meta.get(key)
        if meta is not None:
            meta["pending"] = False
            self._emotion.store.touch(key, reopen_meta=meta)
        self.reply_count_today += 1
        self._log("info", f"主动开口 [{record.chat}]：{decision.text[:40]}")

    # ------------------------------------------------------------------ 观测

    def status_snapshot(self) -> dict[str, Any]:
        snapshots = [adapter.get_status().to_dict() for adapter in self._adapters]
        platforms = {
            str(getattr(adapter, "platform", "wechat") or "wechat")
            for adapter in self._adapters
        }
        platform = platforms.pop() if len(platforms) == 1 else "both"
        return {
            "running": self._running,
            "reply_count_today": self.reply_count_today,
            "last_trigger_at": self.last_trigger_at,
            "platform": platform,
            "adapter": snapshots[0] if snapshots else {},
            "adapters": snapshots,
            "emotion": self._emotion.stats(),
        }

    # ------------------------------------------------------------------ 情绪观测

    def emotion_state(self, chat: str | None = None) -> dict[str, Any]:
        """某个会话（或全部会话）的情绪状态摘要，供界面与诊断使用。"""
        if chat:
            key = self._resolve_chat_key(chat)
            return self._emotion.state(key)
        return {
            "stats": self._emotion.stats(),
            "chats": self._emotion.recent_states(limit=30),
        }

    def identities(self) -> list[dict[str, str]]:
        """可选身份清单（内置 + 用户目录覆盖），供界面下拉框使用。"""
        return self._emotion.identities()

    def persona_state(self, identity_id: str | None = None) -> dict[str, Any]:
        """人格札记账本，供界面查看与人工修订。"""
        return self._emotion.persona_state(identity_id)

    def revise_persona(
        self, entry_id: str, text: str, identity_id: str | None = None
    ) -> dict[str, Any] | None:
        """修订一条札记：生成新条目，旧条目转为 superseded。"""
        return self._emotion.revise_persona(entry_id, text, identity_id)

    def retract_persona(
        self, entry_id: str, reason: str = "", identity_id: str | None = None
    ) -> dict[str, Any] | None:
        """撤回一条札记：保留历史，但不再进入表达 prompt。"""
        return self._emotion.retract_persona(entry_id, reason, identity_id)

    def reset_persona(self, identity_id: str | None = None) -> bool:
        """清空当前身份的人格札记；不影响会话心境。"""
        return self._emotion.reset_persona(identity_id)

    def reset_emotion(self, chat: str | None = None) -> int:
        """清空情绪状态：指定会话则只清那一个，否则全部清空。"""
        key = self._resolve_chat_key(chat) if chat else None
        removed = self._emotion.reset(key)
        self._log(
            "info",
            f"已清空情绪状态：{'全部会话' if key is None else chat}"
            f"（{removed} 个）",
        )
        return removed

    def _resolve_chat_key(self, chat: str) -> str:
        """界面传的是显示名，状态是按会话 ID 存的，这里做一次反查。"""
        target = str(chat or "").strip()
        for key, record in self._last_inbound.items():
            if record.chat == target or key == target:
                return key
        return target

    def get_logs(self, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._log_lock:
            lines = [
                asdict(line)
                for line in self._log_lines
                if line.seq > int(after_seq or 0)
            ]
        return lines

    def log(self, level: str, message: str) -> None:
        """外部（桌面壳）把控制台输出并入运行日志。"""
        self._log(level, message)

    def _log(self, level: str, message: str) -> None:
        with self._log_lock:
            self._log_seq += 1
            line = LogLine(
                seq=self._log_seq,
                level=level,
                message=message,
                timestamp=time.time(),
            )
            self._log_lines.append(line)
        # 回调在锁外调用：它可能再次写日志，避免同一把非重入锁死锁。
        if self.on_log is not None:
            try:
                self.on_log(line)
            except Exception:
                pass

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._date_key:
            self._date_key = today
            self.reply_count_today = 0
