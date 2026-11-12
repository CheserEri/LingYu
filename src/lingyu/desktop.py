"""桌面应用：pywebview 原生窗口承载 DSH 风格 Web UI。

用法：

    .venv\\Scripts\\python.exe -m lingyu.desktop

pywebview 6.x 不支持把 ``async def`` 暴露给 JS（其桥接无 asyncio 集成），
因此这里采用经典模式：一个后台线程跑专用 asyncio loop，承载适配层与 bot
引擎；暴露给 JS 的方法都是同步方法，内部通过
``run_coroutine_threadsafe(...).result()`` 等待结果。每次 JS 调用在 pywebview
的工作线程中执行，阻塞等待不影响界面。
"""

from __future__ import annotations

import asyncio
import contextvars
import io
import sys
import webbrowser
import threading
from collections import deque
from pathlib import Path
from typing import Any

import webview

from .adapters import QQBotAdapter, WeChatAdapter, create_adapter
from .bot import BotConfig, BotEngine, default_config_path
from .bot.llm import LLMClient, LLMError
from .config import AdapterConfig
from .unblock import unblock_bundled_assemblies


def webui_dir() -> Path:
    """定位前端静态目录：冻结包用 _MEIPASS/webui，源码用包内 webui。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base) / "webui"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent / "webui"


def window_icon() -> Path | None:
    """定位窗口图标：冻结包用 _MEIPASS/lingyu.ico，源码用 packaging/lingyu.ico。

    必须显式传给 ``webview.start(icon=...)``：不传时 pywebview 会从
    ``sys.executable`` 提取图标，而源码方式运行时那里是 ``python.exe``，
    窗口标题栏与任务栏就会显示 Python 图标。
    """
    base = getattr(sys, "_MEIPASS", None)
    candidates = []
    if base:
        candidates.append(Path(base) / "lingyu.ico")
    candidates.append(
        Path(__file__).resolve().parents[2] / "packaging" / "lingyu.ico"
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


#: 标记「当前这条日志正从界面 sink 回流进引擎」。
#:
#: 为什么需要：loguru 的界面 sink 会把输出送进 ``UI_LOG.emit`` → ``engine.log``
#: → ``on_log`` → ``forward_engine_log`` → 又交回 loguru。界面 sink 自己能用
#: record 上的 ``ENGINE_LOG_MARK`` 过滤掉回流，但**文件 sink 不认这个标记**，
#: 于是同一条桥接层日志会落盘两次（一次原样、一次带 ``[引擎]`` 前缀）。
#:
#: 这里在调用 ``engine.log`` 期间置位，``forward_engine_log`` 见到置位就直接返回。
#: 界面 sink 是同步调用（没有 ``enqueue=True``），所以 contextvar 能可靠地跨越
#: 这条调用链——即便日志来自别的线程，置位与检查也在同一调用栈里完成。
_ENGINE_FORWARDING: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "lingyu_engine_forwarding", default=False
)


class _UiLogSink:
    """把控制台输出并入界面「运行日志」。

    exe 是控制台程序（MCP stdio 模式要靠 stdout 通信），打包后的桌面端启动时会
    附带一个黑色命令行窗口。启动时把它隐藏掉，原本打在里面的内容（loguru 日志、
    ``print``、异常回溯）改由这里转投到引擎的日志缓冲，界面照常轮询显示。

    引擎在后台线程里延迟创建，此前的输出先暂存，引擎就绪后补发。
    """

    def __init__(self, maxlen: int = 200) -> None:
        self._lock = threading.Lock()
        self._engine: BotEngine | None = None
        self._pending: deque[tuple[str, str]] = deque(maxlen=maxlen)

    def attach(self, engine: BotEngine) -> None:
        with self._lock:
            self._engine = engine
            pending, self._pending = self._pending, deque(maxlen=200)
        for level, line in pending:
            self._feed(engine, level, line)

    def emit(self, level: str, message: str) -> None:
        line = str(message).strip()
        if not line:
            return
        with self._lock:
            engine = self._engine
            if engine is None:
                self._pending.append((level, line))
                return
        self._feed(engine, level, line)

    @staticmethod
    def _feed(engine: BotEngine, level: str, line: str) -> None:
        """把一条日志交给引擎，并在此期间标记「正在回流」。

        置位是为了让 ``forward_engine_log`` 认出这条日志本来就来自 loguru，
        不要再转发一次（否则桥接层日志会在文件里出现两遍）。
        """
        token = _ENGINE_FORWARDING.set(True)
        try:
            engine.log(level, line)
        finally:
            _ENGINE_FORWARDING.reset(token)


# 全局唯一：loguru sink、stdout/stderr 重定向与桌面 API 共用同一个收集器。
UI_LOG = _UiLogSink()

# loguru 级别名 → 界面日志级别（对应 style.css 的 .log-* 配色）。
_LOGURU_LEVELS = {
    "TRACE": "debug",
    "DEBUG": "debug",
    "INFO": "info",
    "SUCCESS": "info",
    "WARNING": "warning",
    "ERROR": "error",
    "CRITICAL": "error",
}

# 引擎日志级别 → loguru 级别名（方向与上面相反）。
_ENGINE_LEVELS = {
    "debug": "DEBUG",
    "info": "INFO",
    "success": "INFO",
    "warning": "WARNING",
    "error": "ERROR",
}

#: 转发到 loguru 的引擎日志会带上这个标记，界面 sink 据此跳过（避免重复 + 递归）。
ENGINE_LOG_MARK = "_engine_forwarded"


def forward_engine_log(line: Any) -> None:
    """把引擎自己的运行日志转发给 loguru，让它们也能**落盘**。

    为什么需要：``UI_LOG.attach(engine)`` 只把引擎日志塞进**内存**里的界面日志
    （界面靠轮询 ``get_logs()`` 显示），而文件 sink 挂在 loguru 上。于是日志文件
    里只有桥接层/适配层的话——引擎自己的判断（「已发送链接提示」「开始下载链接
    内容」「下载失败或超出体积上限，已跳过」…）**一条都没有**。现象就是
    「链接发了、提示回了、然后什么都没有」时，翻日志文件**查不出任何原因**。

    必须打标记：``UI_LOG.emit`` 最终会调 ``engine._log``，若不过滤就会
    「引擎日志 → loguru → 界面 sink → engine._log → …」无限递归。

    另外要拦住**回流**：桥接层用 loguru 打的日志会经界面 sink 回到引擎，此时
    ``_ENGINE_FORWARDING`` 已置位——那条日志本来就在 loguru 手里（文件 sink 已经
    写过一次），再转发就会在文件里出现两遍（一遍原样、一遍带 ``[引擎]`` 前缀）。
    """
    if _ENGINE_FORWARDING.get():
        return
    try:
        from loguru import logger
    except Exception:  # noqa: BLE001 - 没有 loguru 时静默跳过
        return
    level = _ENGINE_LEVELS.get(str(getattr(line, "level", "info")).lower(), "INFO")
    try:
        logger.bind(**{ENGINE_LOG_MARK: True}).log(
            level, f"[引擎] {getattr(line, 'message', line)}"
        )
    except Exception:  # noqa: BLE001 - 写日志失败不该影响运行
        pass


def _guess_level(text: str) -> str:
    """从裸文本（print / 回溯）里猜一个日志级别。"""
    upper = text.upper()
    if "| ERROR" in upper or "ERROR:" in upper or "TRACEBACK" in upper:
        return "error"
    if "| WARNING" in upper or "WARN" in upper:
        return "warning"
    if "| DEBUG" in upper:
        return "debug"
    return "info"


class _TeeStream(io.TextIOBase):
    """写入时同时转给原控制台与界面运行日志的流。"""

    def __init__(self, original: Any) -> None:
        self._original = original
        self._buffer = ""

    def write(self, text: str) -> int:
        if self._original is not None:
            try:
                written = self._original.write(text)
            except Exception:  # noqa: BLE001 - 原控制台不可写时以本次写入为准
                written = len(text)
        else:
            written = len(text)
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            text_line = line.rstrip("\r")
            if text_line.strip():
                UI_LOG.emit(_guess_level(text_line), text_line)
        return written

    def flush(self) -> None:
        if self._original is not None:
            try:
                self._original.flush()
            except Exception:  # noqa: BLE001 - 冲刷失败无需上报
                pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        if self._original is None:
            raise OSError("没有可用的文件描述符")
        return self._original.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self._original, "encoding", None) or "utf-8"

    def __getattr__(self, name: str) -> Any:
        # 未覆盖的属性（如 .buffer）回落到原控制台流；下划线名字直接拒绝，
        # 避免 _original 尚未赋值时无限递归。
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._original, name)


def log_dir() -> Path:
    """运行日志目录：``%APPDATA%\\lingyu\\logs``。"""
    return default_config_path().parent / "logs"


def install_file_logging(level: str = "INFO", retention_days: int = 7) -> Path | None:
    """给 loguru 再挂一个**文件** sink，让运行日志能落盘回溯。

    界面上的「运行日志」只在内存里，进程一退出就没了——出问题（比如「链接发了、
    提示也回了、然后什么都没有」）时完全没有线索，只能靠猜。这里把同样的日志
    按天写进 ``logs/app-YYYY-MM-DD.log``，保留最近若干天。

    返回日志文件路径；挂载失败返回 ``None``（不能因为日志影响程序启动）。
    """
    try:
        from loguru import logger
    except Exception:  # noqa: BLE001 - 没有 loguru 时静默跳过
        return None
    try:
        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(directory / "app-{time:YYYY-MM-DD}.log"),
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {message}",
            level=level,
            encoding="utf-8",
            enqueue=True,  # 跨线程写日志，串行化到队列，避免多线程交叉
            rotation="00:00",
            retention=f"{max(1, int(retention_days))} days",
            backtrace=False,
            diagnose=False,
        )
    except Exception:  # noqa: BLE001 - 落盘失败不影响界面运行
        return None
    return directory


def install_console_capture() -> None:
    """接管控制台输出，让桌面端不再需要那个黑色命令行窗口。

    loguru 的默认 sink 在它自己被导入时就绑定了当时的 ``sys.stderr``，之后再替换
    ``sys.stderr`` 也收不到它的输出，因此这里另外挂一个 sink。
    """
    try:
        from loguru import logger
    except Exception:  # noqa: BLE001 - 没有 loguru 时只接管 print/回溯
        logger = None
    if logger is not None:
        try:
            logger.add(
                lambda message: UI_LOG.emit(
                    _LOGURU_LEVELS.get(message.record["level"].name, "info"),
                    message,
                ),
                format="{message}",
                colorize=False,
                level="INFO",
                # 引擎日志已经由 UI_LOG.attach 进过界面了，转发到 loguru 只是为了让
                # 它们落盘；这里跳过，既避免界面重复显示，也避免
                # 「引擎日志 → loguru → 界面 sink → engine._log」无限递归。
                filter=lambda record: not record["extra"].get(ENGINE_LOG_MARK),
            )
        except Exception:  # noqa: BLE001 - 挂载失败不影响界面运行
            pass
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and not isinstance(stream, _TeeStream):
            setattr(sys, name, _TeeStream(stream))


class _AsyncHub:
    """后台线程 + 专用事件循环。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro: Any, timeout: float | None = None) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)


class DesktopApi:
    """暴露给前端的同步 API；内部转发到后台事件循环。"""

    # 各操作默认最长等待秒数（模型测试可能较慢）。
    DEFAULT_TIMEOUT = 120.0

    def __init__(self, hub: _AsyncHub) -> None:
        self._hub = hub
        self.adapter: Any | None = None
        self.adapters: list[Any] = []
        self.engine: BotEngine | None = None
        self.config: BotConfig | None = None
        self._ready = threading.Event()

    def _call(self, coro: Any, timeout: float = DEFAULT_TIMEOUT) -> Any:
        self._ready.wait(timeout=timeout)
        return self._hub.call(coro, timeout=timeout)

    # ------------------------------------------------------------------ 初始化

    async def _bootstrap_async(self) -> None:
        self.config = BotConfig.load()
        self.adapters = self._build_adapters(self.config)
        self.adapter = self.adapters[0]
        self.engine = BotEngine(self.adapter, self.config, adapters=self.adapters)
        # 引擎就绪后接管控制台输出，连接微信期间的日志即可直接显示到界面。
        UI_LOG.attach(self.engine)
        # 界面那份只在内存里（进程一退就没了），再转发一份给 loguru 才能落盘。
        self.engine.on_log = forward_engine_log
        # 多端并行连接：一端失败不影响另一端，界面可单独重连。
        results = await asyncio.gather(
            *(adapter.connect() for adapter in self.adapters),
            return_exceptions=True,
        )
        for adapter, result in zip(self.adapters, results):
            if isinstance(result, BaseException):
                self.engine.log(
                    "warning", f"{self._adapter_label(adapter)} 连接失败：{result}"
                )
        if self.config.enabled:
            await self.engine.start()

    @staticmethod
    def _adapter_label(adapter: Any) -> str:
        return "QQ 机器人" if getattr(adapter, "platform", "") == "qq" else "微信"

    def _build_adapters(self, config: BotConfig) -> list[Any]:
        """按界面开关构建适配器；两个开关都打开即微信 + QQ 并行。"""
        adapter_config = self._adapter_config(config)
        adapters: list[Any] = []
        if config.wechat_enabled:
            adapters.append(WeChatAdapter(adapter_config))
        if config.qq_enabled:
            adapters.append(QQBotAdapter(adapter_config))
        if not adapters:
            # 配置异常（两个都关）时回退到单端，避免引擎没有可用适配器。
            adapters.append(create_adapter(adapter_config))
        return adapters

    @staticmethod
    def _adapter_config(config: BotConfig) -> AdapterConfig:
        """环境变量提供默认值，界面保存的平台与 QQ 凭据优先。"""
        adapter_config = AdapterConfig.from_env()
        adapter_config.platform = config.platform
        if config.qq_app_id:
            adapter_config.qq_app_id = config.qq_app_id
        if config.qq_app_secret:
            adapter_config.qq_app_secret = config.qq_app_secret
        return adapter_config

    def bootstrap(self) -> None:
        try:
            self._hub.call(self._bootstrap_async(), timeout=90.0)
        finally:
            self._ready.set()

    # ------------------------------------------------------------------ 状态

    def _config_view(self) -> dict:
        """返回给界面的配置：AppSecret 只回「是否已设置」，不回明文。"""
        data = self.config.to_dict()
        secret = str(data.get("qq_app_secret") or "")
        data["qq_app_secret"] = ""
        data["qq_app_secret_set"] = bool(secret)
        return data

    def _binding_view(self) -> dict:
        """接入概览：每个已启用终端的连接状态与机器人身份。"""
        entries: list[dict] = []
        for adapter in self.adapters:
            status = adapter.get_status()
            detail = dict(getattr(status, "detail", None) or {})
            state = getattr(status, "state", "")
            entries.append(
                {
                    "platform": str(getattr(adapter, "platform", "wechat") or "wechat"),
                    "label": self._adapter_label(adapter),
                    "ok": bool(getattr(status, "ok", False)),
                    "state": str(getattr(state, "value", "") or ""),
                    "bot_name": detail.get("bot_name") or "",
                    "bot_id": detail.get("bot_id") or "",
                    "error": getattr(status, "error", None),
                    "backend": detail.get("backend") or "",
                }
            )
        primary = entries[0] if entries else {}
        return {
            "platform": "both" if len(entries) > 1 else self.config.platform,
            "label": "微信 + QQ" if len(entries) > 1 else primary.get("label", "微信"),
            "app_id": self.config.qq_app_id,
            "secret_set": bool(self.config.qq_app_secret),
            "wechat_enabled": bool(self.config.wechat_enabled),
            "qq_enabled": bool(self.config.qq_enabled),
            "ok": bool(entries) and all(item["ok"] for item in entries),
            "state": primary.get("state", ""),
            "bot_name": primary.get("bot_name", ""),
            "bot_id": primary.get("bot_id", ""),
            "error": next(
                (item["error"] for item in entries if item.get("error")), None
            ),
            "entries": entries,
        }

    async def _get_state_async(self) -> dict:
        return {
            "config": self._config_view(),
            "engine": self.engine.status_snapshot(),
            "binding": self._binding_view(),
        }

    def get_state(self) -> dict:
        return self._call(self._get_state_async())

    # ------------------------------------------------------------------ 保存

    async def _save_config_async(self, new: BotConfig) -> dict:
        # 界面不回填密钥：提交空串表示沿用已保存的 AppSecret。
        if not new.qq_app_secret:
            new.qq_app_secret = self.config.qq_app_secret
        binding_changed = (
            self.config.platform != new.platform
            or self.config.wechat_enabled != new.wechat_enabled
            or self.config.qq_enabled != new.qq_enabled
            or self.config.qq_app_id != new.qq_app_id
            or self.config.qq_app_secret != new.qq_app_secret
        )
        connect_result = None
        if binding_changed:
            # 终端开关 / 凭据变了：立刻重建全部适配器并连接，不需要重启程序。
            connect_result = await self._switch_adapters_async(new)
        else:
            if new.enabled and not self.engine.running:
                await self.engine.start()
            elif not new.enabled and self.engine.running:
                await self.engine.stop()
            # 就地更新共享对象：引擎与本 API 持有同一引用，立即生效。
            self.config.__dict__.update(new.to_dict())
            self.config.save()
        state = await self._get_state_async()
        state["applied"] = binding_changed
        state["restart_required"] = False
        state["connect"] = connect_result
        return state

    async def _switch_adapters_async(self, new: BotConfig) -> dict:
        """按终端开关 / 凭据重建全部适配器，保留引擎与人格状态。"""
        for adapter in self.adapters:
            try:
                await adapter.disconnect()
            except Exception as exc:  # noqa: BLE001 - 旧连接断不开不该挡住新配置
                self.engine.log(
                    "warning", f"断开{self._adapter_label(adapter)}失败：{exc}"
                )
        # 就地更新共享对象：引擎与本 API 持有同一引用，立即生效。
        self.config.__dict__.update(new.to_dict())
        self.config.save()
        self.adapters = self._build_adapters(self.config)
        self.adapter = self.adapters[0]
        # set_adapters 会按引擎原本的运行状态停/启，监听自动挂到新适配器上。
        await self.engine.set_adapters(self.adapters)
        if self.config.enabled and not self.engine.running:
            await self.engine.start()
        elif not self.config.enabled and self.engine.running:
            await self.engine.stop()

        entries: list[dict] = []
        for adapter in self.adapters:
            label = self._adapter_label(adapter)
            try:
                result = await adapter.connect()
                entry = result.to_dict()
            except Exception as exc:  # noqa: BLE001 - 连接失败要回给界面，而不是抛进 JS
                entry = {
                    "ok": False,
                    "error": {"message": str(exc) or exc.__class__.__name__},
                }
            entry["platform"] = str(getattr(adapter, "platform", "wechat") or "wechat")
            entry["label"] = label
            entries.append(entry)
        failed = [item for item in entries if not item.get("ok")]
        return {
            "ok": not failed,
            "adapters": entries,
            "error": failed[0].get("error") if failed else None,
        }

    def save_config(self, data: dict) -> dict:
        return self._call(self._save_config_async(BotConfig.from_dict(data)))

    # ------------------------------------------------------------------ 连接

    async def _connect_async(self) -> dict:
        entries: list[dict] = []
        for adapter in self.adapters:
            label = self._adapter_label(adapter)
            try:
                entry = (await adapter.connect()).to_dict()
            except Exception as exc:  # noqa: BLE001 - 单端失败不影响另一端
                entry = {
                    "ok": False,
                    "error": {"message": str(exc) or exc.__class__.__name__},
                }
            entry["platform"] = str(getattr(adapter, "platform", "wechat") or "wechat")
            entry["label"] = label
            entries.append(entry)
        failed = [item for item in entries if not item.get("ok")]
        return {
            "ok": not failed,
            "adapters": entries,
            "error": failed[0].get("error") if failed else None,
        }

    def connect_wechat(self) -> dict:
        return self._call(self._connect_async(), timeout=90.0)

    def connect_platform(self) -> dict:
        """重新连接所有已启用终端（界面使用）。"""
        return self.connect_wechat()

    # ------------------------------------------------------------------ QQ 绑定

    async def _test_qq_binding_async(self, data: dict) -> dict:
        """用界面上正在编辑的凭据取一次 Token / 网关，不改动运行中的连接。"""
        merged = {**self.config.to_dict(), **dict(data or {})}
        candidate = BotConfig.from_dict(merged)
        # 界面不回填密钥：留空就沿用已保存的那一份。
        if not candidate.qq_app_secret:
            candidate.qq_app_secret = self.config.qq_app_secret
        adapter_config = self._adapter_config(candidate)
        adapter_config.platform = "qq"
        adapter = QQBotAdapter(adapter_config)
        try:
            return await adapter.probe()
        finally:
            await adapter.disconnect()

    def test_qq_binding(self, data: dict) -> dict:
        """「测试连接」：只校验 AppID / AppSecret 是否可用。"""
        return self._call(self._test_qq_binding_async(data), timeout=60.0)

    # ------------------------------------------------------------------ 日志

    def get_logs(self, after_seq: int = 0) -> list[dict]:
        return self._call(self._get_logs_async(int(after_seq or 0)))

    async def _get_logs_async(self, after_seq: int) -> list[dict]:
        return self.engine.get_logs(after_seq)

    # ------------------------------------------------------------------ 模型测试

    async def _test_connection_async(self, data: dict) -> dict:
        cfg = BotConfig.from_dict(data)
        try:
            reply = await LLMClient(cfg).chat(
                [{"role": "user", "content": "用一句简短的话打个招呼"}]
            )
            return {"ok": True, "reply": reply}
        except LLMError as exc:
            return {"ok": False, "error": exc.to_dict()}

    def test_connection(self, data: dict) -> dict:
        return self._call(self._test_connection_async(data), timeout=90.0)

    # ------------------------------------------------------------------ 情绪

    async def _emotion_state_async(self, chat: str | None) -> dict:
        return self.engine.emotion_state(chat or None)

    def get_emotion_state(self, chat: str | None = None) -> dict:
        """某个会话（传显示名）或全部会话的情绪摘要。"""
        return self._call(self._emotion_state_async(chat))

    async def _reset_emotion_async(self, chat: str | None) -> dict:
        removed = self.engine.reset_emotion(chat or None)
        # 连清空后的最新状态一起返回，前端不必再跑一次往返。
        return {"removed": removed, "state": self.engine.emotion_state(None)}

    def reset_emotion(self, chat: str | None = None) -> dict:
        return self._call(self._reset_emotion_async(chat))

    async def _list_identities_async(self) -> list[dict]:
        return self.engine.identities()

    def list_identities(self) -> list[dict]:
        """可选身份清单（内置 + 用户目录覆盖），供「情绪」页下拉框使用。"""
        return self._call(self._list_identities_async())

    async def _persona_state_async(self, identity_id: str | None) -> dict:
        return self.engine.persona_state(identity_id or None)

    def get_persona(self, identity_id: str | None = None) -> dict:
        """人格札记：条目、来源、反例、状态与统计。"""
        return self._call(self._persona_state_async(identity_id))

    async def _revise_persona_async(
        self, entry_id: str, text: str, identity_id: str | None
    ) -> dict:
        entry = self.engine.revise_persona(entry_id, text, identity_id or None)
        return {"entry": entry, "state": self.engine.persona_state(identity_id or None)}

    def revise_persona(
        self, entry_id: str, text: str, identity_id: str | None = None
    ) -> dict:
        return self._call(self._revise_persona_async(entry_id, text, identity_id))

    async def _retract_persona_async(
        self, entry_id: str, reason: str, identity_id: str | None
    ) -> dict:
        entry = self.engine.retract_persona(entry_id, reason, identity_id or None)
        return {"entry": entry, "state": self.engine.persona_state(identity_id or None)}

    def retract_persona(
        self, entry_id: str, reason: str = "", identity_id: str | None = None
    ) -> dict:
        return self._call(self._retract_persona_async(entry_id, reason, identity_id))

    async def _reset_persona_async(self, identity_id: str | None) -> dict:
        ok = self.engine.reset_persona(identity_id or None)
        return {"ok": ok, "state": self.engine.persona_state(identity_id or None)}

    def reset_persona(self, identity_id: str | None = None) -> dict:
        return self._call(self._reset_persona_async(identity_id))


#: 启动死锁预防：必须串行预热的重模块（详见 :func:`_prewarm_serial_imports`）。
_PREWARM_MODULES = ("win32ui", "wechatauto", "clr")


def _prewarm_serial_imports() -> None:
    """在主线程串行预热重模块导入，消除启动期的跨线程导入死锁。

    实测死锁（2026-11 抓僵死进程调用栈确认）：``webview.start()``（主线程）走到
    ``import clr`` 时由 cffi 调 ``LoadLibrary`` 加载 .NET 运行时——**持有 GIL**
    等 Windows loader lock；与此同时 bootstrap 线程在 ``adapter.connect()`` 里
    ``from wechatauto import WeChat`` 触发 ``import win32ui``——``LoadLibrary
    ("win32ui.pyd")`` **持有 loader lock**（MFC 扩展 DLL 的初始化在 loader lock
    内调 Python C API，win32ui.pyd 静态链接 python313.dll）**等 GIL**。两把锁
    互相等待，进程僵死：窗口永不出现、日志停在「自动选择微信自动化后端」。

    解法：让这两条导入在**没有任何并发导入**的阶段先行完成（命中 ``sys.modules``
    缓存后重复导入是空操作），死锁窗口即消失。必须在
    :func:`unblock_bundled_assemblies` 之后（``import clr`` 要加载 .NET 程序集，
    带「Internet 区域」标记会拒绝加载）且在 bootstrap 线程与
    ``webview.start()`` 之前调用。

    预热失败不阻断启动——真用到时会再报一次，这里只留痕便于回溯。
    """
    for name in _PREWARM_MODULES:
        try:
            __import__(name)
        except Exception as exc:  # noqa: BLE001 - 预热失败不能挡启动
            try:
                from loguru import logger

                logger.warning(f"预热导入 {name} 未成功（不阻断启动）: {exc}")
            except Exception:  # noqa: BLE001 - 连日志都不可用就静默跳过
                pass


def _show_fatal(message: str) -> None:
    """界面起不来时弹一个错误框。

    双击启动没有控制台，一旦 ``webview.start()`` 抛异常，用户只会看到
    黑屏/闪退，毫无线索。这里把原因显示出来。
    """
    print(message, file=sys.stderr)
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None, message, "灵语 启动失败", 0x10
        )
    except Exception:  # noqa: BLE001 - 弹框失败不能再掩盖原始异常
        pass


#: WebView2 运行时的注册表标识（EdgeUpdate 的固定 GUID）。
_WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
#: 微软官方的 Evergreen 引导程序下载页（装一次即可，Win11 通常自带）。
_WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


def webview2_available() -> bool:
    """本机是否装了 Microsoft Edge WebView2 运行时。

    pywebview 在 Windows 上只支持 EdgeChromium 后端，缺运行时会在
    ``webview.start()`` 里抛异常；而 :func:`_show_fatal` 的文案是照着
    「Python.Runtime.dll 加载失败」写的，用户照着排查也解决不了。所以启动前先查一次。
    """
    if sys.platform != "win32":
        return True
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows 必有 winreg
        return True
    paths = (
        rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT}",
        rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT}",
    )
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for path in paths:
            try:
                with winreg.OpenKey(root, path) as key:
                    version, _ = winreg.QueryValueEx(key, "pv")
            except OSError:
                continue
            if str(version or "").strip() not in ("", "0.0.0.0"):
                return True
    return False


def _prompt_webview2() -> None:
    """提示缺少 WebView2，并打开官方下载页。"""
    _show_fatal(
        "缺少 Microsoft Edge WebView2 运行时，界面无法显示。\n\n"
        "点「确定」会打开微软官方下载页；装完（只需一次）再启动本程序即可。\n"
        f"离线安装包地址：{_WEBVIEW2_URL}"
    )
    try:
        webbrowser.open(_WEBVIEW2_URL)
    except Exception:  # noqa: BLE001 - 打不开浏览器不该再抛一次
        pass


def run() -> None:
    # 先把控制台输出接管到界面运行日志，再启动窗口；这样启动期的日志也不会丢。
    install_console_capture()
    # 同一份日志再落盘一份：界面日志进程一退就没了，出问题时无从回溯。
    install_file_logging()

    index = webui_dir() / "index.html"
    if not index.is_file():
        raise SystemExit(f"缺少前端入口: {index}")

    # 界面靠 WebView2 渲染，缺了会抛一句与真实原因无关的异常，先查再启动。
    if not webview2_available():
        _prompt_webview2()
        raise SystemExit(1)

    # 从 zip 解压的冻结包会被打上「Internet 区域」标记，.NET 拒绝加载
    # 带标记的程序集 → pythonnet 起不来 → 黑屏。这里先自行清除。
    unblocked = unblock_bundled_assemblies()

    # 串行预热 win32ui / wechatauto / clr：启动期主线程（import clr）与
    # bootstrap 线程（import win32ui）并发导入会死锁，见函数内注释。
    # 必须在 unblock 之后、bootstrap 线程与 webview.start() 之前。
    _prewarm_serial_imports()

    hub = _AsyncHub()
    hub.start()
    api = DesktopApi(hub)

    webview.create_window(
        "灵语",
        url=index.as_uri(),
        js_api=api,
        width=1180,
        height=780,
        min_size=(960, 640),
        background_color="#ffffff",
    )

    # 后台初始化（连接微信可能耗时），完成后放行其它 API。
    threading.Thread(target=api.bootstrap, daemon=True).start()

    if unblocked:
        print(f"已清除 {unblocked} 个内置程序集的 Internet 区域标记。")

    icon = window_icon()
    try:
        webview.start(icon=str(icon) if icon else None)
    except Exception as exc:  # noqa: BLE001 - 兜底成可读提示，避免黑屏
        _show_fatal(
            "界面启动失败："
            f"{exc}\n\n"
            "若提示无法加载 Python.Runtime.dll，通常是解压时文件被标记为"
            "「来自 Internet」。请右键压缩包 → 属性 → 勾选「解除锁定」，"
            "然后重新解压。"
        )
        raise


if __name__ == "__main__":
    run()
