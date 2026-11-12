"""适配层配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 审计日志默认写到项目根目录下的 logs/audit.jsonl（可用 WECHAT_AUDIT_LOG 覆盖）。
DEFAULT_AUDIT_LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "audit.jsonl"

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _split_list(raw: str) -> tuple[str, ...]:
    """把逗号分隔的环境变量拆成去重、去空的元组，保持输入顺序。"""
    result: list[str] = []
    for item in str(raw or "").split(","):
        value = item.strip()
        if value and value not in result:
            result.append(value)
    return tuple(result)


@dataclass
class AdapterConfig:
    """适配层运行参数（微信 / QQ 机器人共用）。"""

    # wechat / qq —— 决定使用哪个适配器。
    platform: str = "wechat"

    # ---- 微信 ----
    # auto / wechatauto / wxauto4 / wxauto
    backend: str = "auto"
    # 读取能力依赖监听回调，故连接后默认启动监听。
    listen_on_connect: bool = True
    listen_private: bool = True
    # 适配层不做机器人式拟人延迟。
    reply_delay: float = 0.0
    # 单次发送的最长等待秒数。原项目为“秒回”硬编码为 5 秒，但向尚未打开的
    # 会话发送需要搜索、切换、输入，首次往往超过 5 秒，故在适配层放宽。
    send_timeout_seconds: float = 20.0
    # 被动消息缓冲区容量，决定 get_chat_history 最多可回溯的条数。
    history_buffer_size: int = 2000

    # ---- P1：文件发送白名单 ----
    # 允许发送文件的目录。为空表示默认拒绝发送任何文件，需显式配置放宽。
    allow_send_dirs: tuple[Path, ...] = ()
    # 发送前是否需要显式确认（confirm=True）。开启后未确认的发送仅返回预览。
    require_confirmation: bool = False

    # ---- P1：操作审计 ----
    audit_enabled: bool = True
    audit_log_path: Path = DEFAULT_AUDIT_LOG_PATH

    # ---- P1：会话关注列表预设 ----
    # allow（白名单，空=关注全部）/ block（黑名单，优先级更高）
    monitor_mode: str = "allow"
    monitor_allow: tuple[str, ...] = ()
    monitor_block: tuple[str, ...] = ()

    # ---- QQ 机器人（官方 API v2）----
    # 开放平台「开发设置」里的 AppID / AppSecret。
    qq_app_id: str = ""
    qq_app_secret: str = ""
    qq_api_base: str = "https://api.bot.qq.com"
    # 等待网关 READY 的秒数；超时后连接仍在后台重试。
    qq_connect_timeout: float = 15.0
    # Intents 位掩码；0 表示使用安全默认组合
    # （GUILDS | GROUP_AND_C2C_EVENT | PUBLIC_GUILD_MESSAGES）。
    qq_intents: int = 0

    def is_qq(self) -> bool:
        return str(self.platform or "").strip().lower() == "qq"

    @classmethod
    def from_env(cls) -> "AdapterConfig":
        platform = (
            os.environ.get("LINGYU_PLATFORM", "wechat").strip().lower() or "wechat"
        )
        if platform not in ("wechat", "qq"):
            platform = "wechat"
        backend = os.environ.get("WECHAT_BACKEND", "auto").strip() or "auto"
        listen = (
            os.environ.get("WECHAT_LISTEN_ON_CONNECT", "1").strip().lower()
            in _TRUE_VALUES
        )
        raw_timeout = os.environ.get("WECHAT_SEND_TIMEOUT", "").strip()
        try:
            send_timeout = float(raw_timeout)
        except ValueError:
            send_timeout = cls.send_timeout_seconds

        send_dirs = tuple(
            Path(item)
            for item in _split_list(os.environ.get("WECHAT_SEND_DIRS", ""))
        )
        require_confirm = (
            os.environ.get("WECHAT_REQUIRE_CONFIRM", "0").strip().lower()
            in _TRUE_VALUES
        )
        audit_enabled = (
            os.environ.get("WECHAT_AUDIT_ENABLED", "1").strip().lower()
            in _TRUE_VALUES
        )
        raw_audit_log = os.environ.get("WECHAT_AUDIT_LOG", "").strip()
        audit_log_path = (
            Path(raw_audit_log) if raw_audit_log else DEFAULT_AUDIT_LOG_PATH
        )
        monitor_mode = (
            os.environ.get("WECHAT_MONITOR_MODE", "allow").strip().lower() or "allow"
        )
        if monitor_mode not in ("allow", "block"):
            monitor_mode = "allow"

        raw_qq_intents = os.environ.get("QQ_BOT_INTENTS", "").strip()
        try:
            qq_intents = max(0, int(raw_qq_intents or 0))
        except ValueError:
            qq_intents = 0
        raw_qq_timeout = os.environ.get("QQ_BOT_CONNECT_TIMEOUT", "").strip()
        try:
            qq_connect_timeout = float(raw_qq_timeout)
        except ValueError:
            qq_connect_timeout = cls.qq_connect_timeout
        qq_api_base = (
            os.environ.get("QQ_BOT_API_BASE", "").strip() or cls.qq_api_base
        )

        return cls(
            platform=platform,
            backend=backend,
            listen_on_connect=listen,
            send_timeout_seconds=send_timeout,
            allow_send_dirs=send_dirs,
            require_confirmation=require_confirm,
            audit_enabled=audit_enabled,
            audit_log_path=audit_log_path,
            monitor_mode=monitor_mode,
            monitor_allow=_split_list(os.environ.get("WECHAT_MONITOR_ALLOW", "")),
            monitor_block=_split_list(os.environ.get("WECHAT_MONITOR_BLOCK", "")),
            qq_app_id=os.environ.get("QQ_BOT_APP_ID", "").strip(),
            qq_app_secret=os.environ.get("QQ_BOT_APP_SECRET", "").strip(),
            qq_api_base=qq_api_base,
            qq_connect_timeout=max(3.0, qq_connect_timeout),
            qq_intents=qq_intents,
        )
