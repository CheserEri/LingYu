"""桌面端：运行日志落盘。"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lingyu import desktop


class FileLoggingTests(unittest.TestCase):
    """`install_file_logging`：界面日志进程一退就没了，必须另存一份到磁盘。"""

    def setUp(self):
        from loguru import logger

        self.tmp = Path(tempfile.mkdtemp(prefix="lingyu-logtest-"))
        self._before = set(logger._core.handlers)  # noqa: SLF001

    def tearDown(self):
        from loguru import logger

        for sink_id in set(logger._core.handlers) - self._before:  # noqa: SLF001
            try:
                logger.remove(sink_id)
            except ValueError:
                pass
        # sink 撤掉之后文件句柄才释放，这时才能删临时目录。
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _install(self) -> Path | None:
        with mock.patch.object(desktop, "log_dir", lambda: self.tmp):
            return desktop.install_file_logging()

    def test_writes_log_file_with_content(self):
        from loguru import logger

        self.assertIsNotNone(self._install(), "应返回日志目录")

        logger.info("单元测试：链接解析开始")
        deadline = time.time() + 5.0
        files: list[Path] = []
        while time.time() < deadline:
            files = list(self.tmp.glob("*.log"))
            if files and "链接解析开始" in files[0].read_text(encoding="utf-8"):
                break
            time.sleep(0.05)

        self.assertTrue(files, "应生成日志文件")
        self.assertEqual(files[0].name, f"app-{time.strftime('%Y-%m-%d')}.log")
        self.assertIn("链接解析开始", files[0].read_text(encoding="utf-8"))

    def test_missing_loguru_returns_none(self):
        with mock.patch.dict("sys.modules", {"loguru": None}):
            self.assertIsNone(desktop.install_file_logging())

    def test_log_dir_is_under_appdata(self):
        path = desktop.log_dir()
        self.assertEqual(path.name, "logs")
        self.assertEqual(path.parent, desktop.default_config_path().parent)


class EngineLogForwardingTests(unittest.TestCase):
    """引擎日志必须也落盘。

    回归点（0.8.10 实测）：``on_log`` 在全仓**没有任何地方赋值**，于是引擎自己的
    日志只进内存里的界面缓冲（``UI_LOG.attach``），文件 sink 挂在 loguru 上、收不到
    ——日志文件里只有桥接层/适配层的话。现象是「链接发了、提示回了、然后什么都没有」
    时，翻日志文件**查不出任何原因**（引擎的「开始下载链接内容」「下载失败或超出
    体积上限，已跳过」全都不在文件里）。
    """

    def setUp(self):
        from loguru import logger

        self.tmp = Path(tempfile.mkdtemp(prefix="lingyu-englog-"))
        self._before = set(logger._core.handlers)  # noqa: SLF001

    def tearDown(self):
        from loguru import logger

        for sink_id in set(logger._core.handlers) - self._before:  # noqa: SLF001
            try:
                logger.remove(sink_id)
            except ValueError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read(self, needle: str, timeout: float = 5.0) -> str:
        deadline = time.time() + timeout
        text = ""
        while time.time() < deadline:
            files = list(self.tmp.glob("*.log"))
            if files:
                text = files[0].read_text(encoding="utf-8")
                if needle in text:
                    return text
            time.sleep(0.05)
        return text

    def _install(self) -> None:
        with mock.patch.object(desktop, "log_dir", lambda: self.tmp):
            desktop.install_file_logging()

    def test_engine_line_reaches_the_file(self):
        self._install()
        desktop.forward_engine_log(
            SimpleNamespace(level="info", message="开始下载链接内容：https://x")
        )
        self.assertIn("开始下载链接内容", self._read("开始下载链接内容"))
        self.assertIn("[引擎]", self._read("[引擎]"))

    def test_engine_levels_are_mapped(self):
        self._install()
        desktop.forward_engine_log(
            SimpleNamespace(level="warning", message="下载失败或超出体积上限，已跳过")
        )
        text = self._read("下载失败或超出体积上限")
        self.assertIn("WARNING", text, "warning 级别不该被降级成 INFO")

    def test_debug_lines_stay_out_of_the_file(self):
        """文件 sink 是 INFO+，引擎的 debug 行（未 @ 机器人等）不该刷进来。"""
        self._install()
        desktop.forward_engine_log(
            SimpleNamespace(level="debug", message="单元测试：这条不该落盘")
        )
        time.sleep(0.4)
        files = list(self.tmp.glob("*.log"))
        text = files[0].read_text(encoding="utf-8") if files else ""
        self.assertNotIn("这条不该落盘", text)

    def test_ui_sink_skips_forwarded_records(self):
        """转发到 loguru 的引擎日志不能再回到 UI_LOG，否则会无限递归。

        ``UI_LOG.emit`` 最终调 ``engine._log``；不拦的话链路是
        「引擎日志 → loguru → 界面 sink → engine._log → …」。
        """
        from loguru import logger

        seen: list[tuple[str, str]] = []
        with mock.patch.object(
            desktop.UI_LOG, "emit", lambda level, message: seen.append((level, message))
        ):
            desktop.install_console_capture()
            try:
                desktop.forward_engine_log(
                    SimpleNamespace(level="info", message="引擎转发的这条")
                )
                logger.info("桥接层直接打的这条")
            finally:
                for sink_id in set(logger._core.handlers) - self._before:  # noqa: SLF001
                    try:
                        logger.remove(sink_id)
                    except ValueError:
                        pass
        messages = [str(m).strip() for _level, m in seen]
        self.assertIn("桥接层直接打的这条", messages, "普通 loguru 日志仍要进界面")
        self.assertNotIn(
            "引擎转发的这条", messages, "引擎转发的不该再回界面（会递归）"
        )

    def test_bridge_log_line_lands_in_the_file_once(self):
        """桥接层经 loguru 打的日志不能被「回流」写成两遍。

        回归点（0.8.10 实机）：``UI_LOG.emit`` 会把 loguru 的输出交给引擎，引擎再经
        ``on_log`` 转发回 loguru——界面 sink 的 filter 挡住了回流界面（防递归），但
        **文件 sink 不认那个标记**，于是同一条日志落盘两次（一遍原样、一遍带
        ``[引擎]`` 前缀）。修法是回流期间置位 ``_ENGINE_FORWARDING``。
        """
        from loguru import logger

        class _Engine:
            @staticmethod
            def log(level, message):
                # 模拟 BotEngine._log → on_log 这一段
                desktop.forward_engine_log(
                    SimpleNamespace(level=level, message=message)
                )

        self._install()
        with mock.patch.object(desktop.UI_LOG, "_engine", _Engine()):
            desktop.install_console_capture()
            try:
                logger.info("桥接层直接打的这条")
            finally:
                for sink_id in set(logger._core.handlers) - self._before:  # noqa: SLF001
                    try:
                        logger.remove(sink_id)
                    except ValueError:
                        pass
        # 文件 sink 是 enqueue=True（异步），多等一会儿让它彻底冲刷。
        time.sleep(0.6)
        text = self._read("桥接层直接打的这条")
        self.assertEqual(text.count("桥接层直接打的这条"), 1, text)
        self.assertNotIn("[引擎] 桥接层直接打的这条", text)

    def test_bootstrap_wires_on_log(self):
        """桌面壳启动时必须真的把 ``on_log`` 接上（这就是当初漏掉的那一步）。"""
        import asyncio

        from lingyu.bot import BotConfig

        fake_engine = SimpleNamespace(start=mock.AsyncMock())
        fake_adapter = SimpleNamespace(connect=mock.AsyncMock())
        api = desktop.DesktopApi.__new__(desktop.DesktopApi)

        with mock.patch.object(
            desktop, "create_adapter", lambda *a, **k: fake_adapter
        ), mock.patch.object(
            desktop, "BotEngine", lambda *a, **k: fake_engine
        ), mock.patch.object(desktop, "BotConfig") as cfg, mock.patch.object(
            desktop, "UI_LOG"
        ) as ui_log:
            cfg.load.return_value = BotConfig()
            asyncio.run(desktop.DesktopApi._bootstrap_async(api))

        self.assertIs(fake_engine.on_log, desktop.forward_engine_log)
        ui_log.attach.assert_called_once_with(fake_engine)


class PersonaApiTests(unittest.TestCase):
    """人格札记 API：参数透传与返回结构。

    界面只读这一层，所以这里不重复测账本逻辑（在 ``test_emotion`` 里），
    只钉住「JS 调到的名字 → 引擎方法 → JSON 结构」这条线没接错。
    """

    def _api(self):
        import asyncio

        calls: list[tuple] = []
        engine = SimpleNamespace(
            persona_state=lambda identity=None: (
                calls.append(("state", identity)) or {"entries": []}
            ),
            revise_persona=lambda entry_id, text, identity=None: (
                calls.append(("revise", entry_id, text, identity))
                or {"id": entry_id}
            ),
            retract_persona=lambda entry_id, reason="", identity=None: (
                calls.append(("retract", entry_id, reason, identity))
                or {"id": entry_id}
            ),
            reset_persona=lambda identity=None: (
                calls.append(("reset", identity)) or True
            ),
        )
        api = desktop.DesktopApi.__new__(desktop.DesktopApi)
        api.engine = engine
        api._call = lambda coro: asyncio.run(coro)
        return api, calls

    def test_get_persona_passes_identity(self):
        api, calls = self._api()
        self.assertEqual(api.get_persona("violet"), {"entries": []})
        self.assertEqual(calls, [("state", "violet")])

    def test_revise_returns_entry_and_state(self):
        api, calls = self._api()
        out = api.revise_persona("p-0001", "新的说法。", "violet")
        self.assertEqual(out["entry"], {"id": "p-0001"})
        self.assertEqual(out["state"], {"entries": []})
        self.assertIn(("revise", "p-0001", "新的说法。", "violet"), calls)

    def test_retract_passes_reason(self):
        api, calls = self._api()
        out = api.retract_persona("p-0001", "证据不够", "violet")
        self.assertEqual(out["entry"], {"id": "p-0001"})
        self.assertIn(("retract", "p-0001", "证据不够", "violet"), calls)

    def test_reset_reports_ok_and_state(self):
        api, calls = self._api()
        out = api.reset_persona()
        self.assertTrue(out["ok"])
        self.assertEqual(out["state"], {"entries": []})
        self.assertIn(("reset", None), calls)


class WebView2CheckTests(unittest.TestCase):
    """缺 WebView2 时要在启动前给出可操作的提示。

    pywebview 只认 EdgeChromium 后端；缺运行时 `webview.start()` 抛的异常会被
    `_show_fatal` 说成「Python.Runtime.dll 加载失败」，用户照着做也解决不了。
    """

    @staticmethod
    def _fake_winreg(version=None):
        class _Key:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def _open(root, path):
            if version is None:
                raise OSError(2, "系统找不到指定的文件")
            return _Key()

        return SimpleNamespace(
            HKEY_LOCAL_MACHINE=1,
            HKEY_CURRENT_USER=2,
            OpenKey=_open,
            QueryValueEx=lambda key, name: (version, 1),
        )

    def test_missing_runtime_is_detected(self):
        fake = self._fake_winreg()
        with mock.patch.dict(sys.modules, {"winreg": fake}):
            self.assertFalse(desktop.webview2_available())

    def test_installed_runtime_is_detected(self):
        fake = self._fake_winreg(version="130.0.2849.68")
        with mock.patch.dict(sys.modules, {"winreg": fake}):
            self.assertTrue(desktop.webview2_available())

    def test_zero_version_counts_as_missing(self):
        """Edge 更新器会写 `pv=0.0.0.0` 表示未安装。"""
        fake = self._fake_winreg(version="0.0.0.0")
        with mock.patch.dict(sys.modules, {"winreg": fake}):
            self.assertFalse(desktop.webview2_available())


class PrewarmSerialImportTests(unittest.TestCase):
    """启动死锁预防：`win32ui` / `wechatauto` / `clr` 必须在主线程串行预热。

    回归点（2026-11-08 实测）：`webview.start()`（主线程）`import clr` 经 cffi
    调 `LoadLibrary` 持有 GIL 等 loader lock，与 bootstrap 线程
    `import win32ui`（MFC DllInit 持有 loader lock 等 GIL）并发时互相等待，
    进程僵死、窗口永不出现。两者任一先在单线程阶段导完（命中 sys.modules
    缓存）死锁窗口即消失，所以预热顺序与覆盖面都不能退。
    """

    def test_prewarms_all_modules(self):
        seen: list[str] = []
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            seen.append(name)
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            desktop._prewarm_serial_imports()

        # 每个目标模块至少被请求过一次（嵌套导入会重复出现，只看是否覆盖）。
        for name in desktop._PREWARM_MODULES:
            self.assertIn(name, seen, f"预热应请求导入 {name}")

    def test_top_level_calls_follow_preload_order(self):
        """顶层预热调用的顺序就是 _PREWARM_MODULES 的顺序（嵌套导入不算）。"""
        seen: list[str] = []
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            seen.append(name)
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            desktop._prewarm_serial_imports()

        # 取每个目标模块的首次出现位置，必须与声明顺序一致。
        firsts = {n: seen.index(n) for n in desktop._PREWARM_MODULES}
        ordered = sorted(firsts, key=firsts.get)
        self.assertEqual(ordered, list(desktop._PREWARM_MODULES))

    def test_import_failure_does_not_raise(self):
        """预热失败不能挡启动——真用到时会再报一次，这里只留痕。"""

        def boom(name, *args, **kwargs):
            raise ImportError(f"no module named {name}")

        with mock.patch("builtins.__import__", side_effect=boom):
            desktop._prewarm_serial_imports()  # 不应抛出任何异常

    def test_preload_order_puts_mfc_module_before_clr(self):
        """win32ui（loader lock 持有者）必须排在 clr（GIL 持有者）之前预热。"""
        names = list(desktop._PREWARM_MODULES)
        self.assertLess(names.index("win32ui"), names.index("clr"))


class QQBindingApiTests(unittest.TestCase):
    """QQ 绑定配置集成：脱敏回显、留空保留密钥、切换平台即时生效。"""

    @staticmethod
    def _status(ok: bool = True, error=None):
        from lingyu.schemas import ConnectionState

        return SimpleNamespace(
            ok=ok,
            state=ConnectionState.CONNECTED if ok else ConnectionState.ERROR,
            detail={"bot_name": "灵语", "bot_id": "bot-1", "app_id": "appid"},
            error=error,
        )

    @staticmethod
    def _engine(running: bool = False):
        class _Engine:
            def __init__(self):
                self.running = running
                self.swapped: list = []
                self.logs: list[tuple[str, str]] = []

            async def start(self):
                self.running = True

            async def stop(self):
                self.running = False

            async def swap_adapter(self, adapter):
                self.swapped.append(adapter)

            async def set_adapters(self, adapters):
                self.swapped.append(list(adapters))

            @staticmethod
            def status_snapshot():
                return {"running": False, "platform": "qq", "adapter": {}, "emotion": {}}

            def log(self, level, message):
                self.logs.append((level, message))

        return _Engine()

    @staticmethod
    def _api(config, adapter=None, engine=None, adapters=None):
        api = desktop.DesktopApi.__new__(desktop.DesktopApi)
        api.config = config
        api.adapter = adapter
        api.engine = engine
        if adapters is not None:
            api.adapters = adapters
        else:
            api.adapters = [adapter] if adapter is not None else []
        return api

    def test_state_masks_app_secret(self):
        import asyncio

        from lingyu.bot import BotConfig

        cfg = BotConfig(platform="qq", qq_app_id="appid", qq_app_secret="secret")
        api = self._api(cfg, SimpleNamespace(get_status=lambda: self._status()), self._engine())

        state = asyncio.run(api._get_state_async())

        self.assertEqual(state["config"]["qq_app_secret"], "")
        self.assertTrue(state["config"]["qq_app_secret_set"])
        self.assertTrue(state["binding"]["secret_set"])
        self.assertEqual(state["binding"]["bot_name"], "灵语")

    def test_save_keeps_existing_secret_when_blank(self):
        import asyncio

        from lingyu.bot import BotConfig

        cfg = BotConfig(platform="wechat", qq_app_id="appid", qq_app_secret="saved")
        api = self._api(cfg, SimpleNamespace(get_status=lambda: self._status()), self._engine())
        incoming = BotConfig.from_dict({**cfg.to_dict(), "qq_app_secret": ""})

        with mock.patch.object(BotConfig, "save", lambda self, path=None: None):
            asyncio.run(api._save_config_async(incoming))

        self.assertEqual(cfg.qq_app_secret, "saved", "留空的密钥不能被清掉")

    def test_terminal_switch_swaps_adapters_without_restart(self):
        import asyncio

        from lingyu.bot import BotConfig

        cfg = BotConfig(platform="wechat")
        old = SimpleNamespace(
            platform="wechat",
            get_status=lambda: self._status(),
            disconnect=mock.AsyncMock(),
        )
        engine = self._engine()
        new_adapter = SimpleNamespace(
            platform="qq",
            get_status=lambda: self._status(),
            connect=mock.AsyncMock(
                return_value=SimpleNamespace(
                    to_dict=lambda: {"ok": True, "state": "connected"}
                )
            ),
        )
        api = self._api(cfg, old, engine)
        incoming = BotConfig.from_dict(
            {
                **cfg.to_dict(),
                "wechat_enabled": False,
                "qq_enabled": True,
                "qq_app_id": "appid",
                "qq_app_secret": "secret",
            }
        )

        with mock.patch.object(
            desktop.DesktopApi, "_build_adapters", return_value=[new_adapter]
        ), mock.patch.object(BotConfig, "save", lambda self, path=None: None):
            state = asyncio.run(api._save_config_async(incoming))

        old.disconnect.assert_awaited_once()
        self.assertEqual(engine.swapped, [[new_adapter]])
        new_adapter.connect.assert_awaited_once()
        self.assertIs(api.adapter, new_adapter)
        self.assertFalse(cfg.wechat_enabled)
        self.assertTrue(cfg.qq_enabled)
        self.assertEqual(cfg.qq_app_secret, "secret")
        self.assertTrue(state["applied"])
        self.assertFalse(state["restart_required"])
        self.assertTrue(state["connect"]["ok"])
        self.assertEqual(state["connect"]["adapters"][0]["platform"], "qq")

    def test_build_adapters_can_enable_both_terminals(self):
        from lingyu.bot import BotConfig

        cfg = BotConfig(wechat_enabled=True, qq_enabled=True, qq_app_id="a")
        api = self._api(cfg, None, self._engine())
        wechat = SimpleNamespace(platform="wechat")
        qq = SimpleNamespace(platform="qq")

        with mock.patch.object(desktop, "WeChatAdapter", lambda *a: wechat), \
             mock.patch.object(desktop, "QQBotAdapter", lambda *a: qq):
            adapters = api._build_adapters(cfg)

        self.assertEqual([item.platform for item in adapters], ["wechat", "qq"])

    def test_build_adapters_respects_single_terminal(self):
        from lingyu.bot import BotConfig

        api = self._api(BotConfig(), None, self._engine())
        qq = SimpleNamespace(platform="qq")
        with mock.patch.object(desktop, "QQBotAdapter", lambda *a: qq):
            adapters = api._build_adapters(
                BotConfig(wechat_enabled=False, qq_enabled=True)
            )
        self.assertEqual([item.platform for item in adapters], ["qq"])

    def test_test_binding_uses_saved_secret_when_blank(self):
        import asyncio

        from lingyu.bot import BotConfig

        cfg = BotConfig(platform="qq", qq_app_id="appid", qq_app_secret="saved")
        captured: dict = {}

        class _FakeQQ:
            def __init__(self, adapter_config, **kwargs):
                captured["config"] = adapter_config

            @staticmethod
            async def probe():
                return {"ok": True, "gateway": "wss://api.bot.qq.com/websocket/"}

            @staticmethod
            async def disconnect():
                return None

        api = self._api(cfg, None, self._engine())
        with mock.patch.object(desktop, "QQBotAdapter", _FakeQQ):
            result = asyncio.run(
                api._test_qq_binding_async({"qq_app_id": "new-id", "qq_app_secret": ""})
            )

        self.assertTrue(result["ok"])
        self.assertEqual(captured["config"].platform, "qq")
        self.assertEqual(captured["config"].qq_app_id, "new-id")
        self.assertEqual(captured["config"].qq_app_secret, "saved")


if __name__ == "__main__":
    unittest.main()
