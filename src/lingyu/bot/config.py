"""常驻自动回复 bot 的配置与本地持久化。

配置以 JSON 存在用户私有目录（默认 ``%APPDATA%\\lingyu\\bot.json``），
可用环境变量 ``WECHAT_BOT_CONFIG`` 指定其他路径。``api_key`` 仅落盘在该
本地私有文件中，不进入仓库、不随包分发。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .recall import parse_keywords as parse_recall_keywords


def default_config_path() -> Path:
    """返回 bot 配置文件路径：WECHAT_BOT_CONFIG 优先，否则 %APPDATA%\\lingyu。"""
    raw = os.environ.get("WECHAT_BOT_CONFIG", "").strip()
    if raw:
        return Path(raw)
    appdata = os.environ.get("APPDATA", "").strip()
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "lingyu" / "bot.json"


def _env_platform() -> str:
    value = os.environ.get("LINGYU_PLATFORM", "").strip().lower()
    return value if value in ("wechat", "qq") else "wechat"


@dataclass
class BotConfig:
    """自动回复运行参数（均可在桌面界面修改）。"""

    # 自动回复总开关。
    enabled: bool = False

    # ---- 接入平台 ----
    # wechat：桌面微信；qq：QQ 机器人官方 API v2（AppID / AppSecret）。
    # 桌面端可以同时启用多个终端（两个开关都打开即并行运行）；
    # platform 只作为环境变量 / 旧配置的默认值与 MCP 的默认终端。
    # 切换平台后由桌面端重建适配器（保存并应用，无需重启）。
    # 首次运行（还没有 bot.json）时从环境变量读取；保存后以界面配置为准。
    platform: str = field(default_factory=_env_platform)
    wechat_enabled: bool = True
    qq_enabled: bool = False
    qq_app_id: str = field(
        default_factory=lambda: os.environ.get("QQ_BOT_APP_ID", "").strip()
    )
    qq_app_secret: str = field(
        default_factory=lambda: os.environ.get("QQ_BOT_APP_SECRET", "").strip()
    )

    # ---- 触发条件（什么算「在跟我说话」，不是「多久回一次」）----
    reply_private: bool = True   # 私聊：每条入站消息都交给情绪引擎判断
    trigger_at: bool = True      # 群聊：@我 时交给情绪引擎
    trigger_reply: bool = True   # 群聊：引用/回复我的消息时交给情绪引擎

    # ---- 作用范围 ----
    # True=所有群；False=仅 groups 白名单内的群。
    all_groups: bool = True
    groups: list[str] = field(default_factory=list)

    # ---- 大模型（OpenAI 兼容 /chat/completions）----
    api_base: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.9
    max_tokens: int = 300
    timeout_seconds: float = 30.0

    # ---- 上下文 ----
    # 链接解析要回看多少条消息（不是「模型看多少」，那是内核自己的记忆）。
    context_messages: int = 20

    # ---- 情绪引擎（混合人格内核）----
    # 四层结构：身份内核 / 心境 / 关系与情节记忆 / 人格札记。
    # 不再是十个 Agent 分阶段协作。
    emotion_enabled: bool = True
    # 身份 id：内置 violet，用户可放 %APPDATA%\lingyu\identity\<id>.json 覆盖。
    emotion_identity: str = "violet"
    # 大模型参与程度：
    #   expression 只有表达调模型，人格札记走规则（一轮 1 次，快且便宜）
    #   all        表达 + 每天最多一次札记 LLM
    #   none       全部走规则（没有 Key 时的默认降级路径）
    emotion_llm_mode: str = "expression"
    # 人格札记目录；空串表示 %APPDATA%\lingyu\persona。
    emotion_persona_dir: str = ""
    # 是否启用全局人格札记；关闭后只保留身份 + 心境 + 情节记忆。
    emotion_persona_enabled: bool = True
    # 情绪状态目录；空串表示 %APPDATA%\lingyu\emotion。
    emotion_state_dir: str = ""

    # ---- 重新开口（自己起头说话）----
    # 这里**没有开关**。说不说、隔多久说全在内核里：
    #   · 沉默时长 ≥ 身份文件的 silence_reopen_hours 才够格；
    #   · 同一个「没被回应」的沉默只开口一次（regulation.reopen_done）；
    #   · 深夜不主动开口（regulation 的夜间规则）。
    # 设计文档里「是否保持沉默」是表达 Agent 的职责（§3.7），不是宿主配置项，
    # 所以宿主只提供一个时钟，不提供「允不允许开口」的开关。

    # ---- 图像识别（需模型支持视觉）----
    # 关闭时图片只以「[图片]」文本形式进入上下文，不发送图片数据。
    vision_enabled: bool = False
    # 单次请求最多附带几张上下文图片（0 = 不带图）。
    vision_max_images: int = 3
    # current_message=只看触发本轮的消息图片；conversation=回看当前会话上下文图片。
    vision_scope: str = "conversation"

    # ---- 链接解析（检测到链接时自动解析并喂给模型）----
    # 开启后，上下文里出现的链接会被解析（标题/作者/时长/简介），
    # 结果作为「[链接] …」注入模型上下文，让机器人能就链接内容回应。
    link_parse_enabled: bool = True
    # 单次请求最多解析多少个链接（含已缓存的）。
    link_parse_max: int = 3
    # 单个链接的解析超时（秒）。
    link_parse_timeout: float = 20.0
    # Netscape 格式 cookies.txt 的路径（留空 = 不使用）。
    # 微博、小红书等站点会对未带 Cookie 的请求返回 403，yt-dlp 明确要求
    # 「新鲜 cookie（不必登录）」；用浏览器扩展导出一次 cookies.txt 填到这里即可。
    # 注意**抖音不需要**：它另有 a_bogus 签名门槛，走浏览器桥解决（见 bot/douyin.py）。
    link_cookies_file: str = ""
    # 下载音视频并发回当前聊天。默认**关闭**：会向真实聊天发送文件。
    link_download_enabled: bool = False
    # 单次触发最多下载几个链接。
    link_download_max: int = 1
    # 下载目录；空串表示使用默认目录（%APPDATA%\lingyu\downloads）。
    link_download_dir: str = ""
    # 单文件体积上限（MB），超过则跳过下载（0 = 不限制）。
    link_download_max_mb: int = 100
    # 下载目录**总占用**上限（MB）：超出后自动删除最旧的文件，直到降到上限以内。
    # 0 = 不限制。默认 1 GB——长期运行不至于把磁盘吃满，正常使用也很少触发。
    link_download_quota_mb: int = 1024
    # 下载失败后最多尝试几次（含首次，默认 3）。
    # 只对**值得重试**的失败生效：B 站偶发的「Unable to extract initial state」、
    # 超时、连接被重置、5xx 这些换个时机可能就好了；体积超限、图文作品、
    # 站点不支持属于确定性失败，不会白重试，直接放弃。
    link_download_attempts: int = 3
    # 下载最终失败时往聊天里发一句提示（含失败原因），免得链接发出去像石沉大海。
    # 关闭后只写日志。
    link_download_fail_notice: bool = True
    # 附件**没发成功**时最多再试几次（0 = 不重试，默认 2）。
    # 每次重试都会回查数据库确认，两个方向都会用上这个次数：
    #   1) 微信明确拒绝（附件压根没粘上去）→ 整条重来；
    #   2) 附件粘进去了、但回车被微信吞掉（大文件粘贴后微信还在拷原始文件、
    #      生成卡片，这期间的回车会被丢掉）→ 只补按回车、不重新粘贴，
    #      所以**不会重复发送**。
    # 调大更耐折腾，代价是发送失败时占用发送闸门的时间更久。
    link_send_attempts: int = 2
    # X（Twitter）推文额外发一张本地渲染的卡片图（头像/昵称/正文/配图）。
    # 仅对 X 推文生效；推文含视频时还会下载视频一并回发。
    link_tweet_card_enabled: bool = True
    # 推文正文**自动翻译**：外文推文先翻成中文再渲染卡片，译文当正文、
    # 原文缩成灰色小字附在下面。走已配置的大模型；未配 Key 时自动跳过，
    # 卡片仍按原文渲染（不会因为翻译失败而不发卡片）。
    link_tweet_translate: bool = True
    # 译文目标语言。
    link_tweet_translate_target: str = "简体中文"
    # 小红书笔记额外发一张本地渲染的卡片图（与推文卡片同一套版式：
    # 头像/昵称/正文/竖排长图/页脚）。图文笔记只发卡片（不下载原图），
    # 视频笔记在卡片之后再下载视频回发。
    link_xhs_card_enabled: bool = True
    # 识别到链接时先回一句**固定**提示（不经过大模型），让用户知道正在处理。
    # 私聊与群聊都生效（群聊里不必 @ 机器人）。
    link_ack_enabled: bool = True
    # 固定提示的文案；留空则不发送提示。
    link_ack_text: str = "正在解析链接"
    # 发完固定提示后，是否仍让大模型就这条消息接话。
    # 默认 True：私聊等原本会回复的场景保持原样（多一条提示）。
    # 设为 False 则「带链接的消息只回固定提示 + 回发解析结果」，不烧 token。
    link_llm_followup: bool = True
    # 只处理「能真正解析出东西」的媒体站点链接（抖音/B 站/YouTube/X/小红书…）。
    # 购物、团购、新闻、公众号等普通网页链接**不解析、不下载、也不提示失败**——
    # 它们既没有音视频可发，逐条解析只会多一次无用的网络请求和一句噪音。
    link_supported_only: bool = True
    # 白名单之外还想解析的站点（逗号/换行分隔的域名，子域同样命中）。
    # 命中这些域名的链接按「其它媒体」处理，走内置 yt-dlp 通用提取。
    link_extra_sites: str = ""

    # ---- 撤回（违规内容应急下架）----
    # 开启后，聊天里有人发「@机器人 撤回」就会撤回机器人最新发出的媒体消息。
    recall_enabled: bool = True
    # 指令词（逗号分隔）。识别时只认「指令词 + 允许的后缀」（撤回一下 / 撤回吧…），
    # 像「别撤回」「撤回的消息看不到」这类日常对话不会被误判。
    recall_keywords: str = "撤回"
    # 群里是否必须 @ 机器人才算指令（推荐开启，避免把日常聊天里的「撤回」当指令）。
    recall_require_at: bool = True
    # 只允许指定成员下指令（默认 False：群里任何人都能喊停，违规内容越快撤掉越好）。
    recall_admin_only: bool = False
    recall_admins: list[str] = field(default_factory=list)
    # 只撤这个时间窗内发出的消息（微信撤回时限 2 分钟，留点余量）。
    recall_window_seconds: float = 110.0
    # 最多一次撤几条：要撤到媒体，可能得连它后面发的提示一起撤。
    recall_max_messages: int = 2
    # 撤回时顺带取消该会话还没发出去的下载任务（避免刚撤完又冒出来一条）。
    recall_stop_download: bool = True
    # 撤回后是否回一句结果（固定文案，不经过大模型）。
    recall_notice_enabled: bool = True
    recall_ok_text: str = "已撤回上一条消息"
    recall_fail_text: str = "撤回失败，可能需要手动撤回"

    def normalized_api_base(self) -> str:
        """去掉末尾斜杠，便于拼接 /chat/completions。"""
        return self.api_base.strip().rstrip("/")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BotConfig":
        """从 dict 构造；忽略未知键、补齐缺失键，容忍旧版本配置。"""
        if not isinstance(data, dict):
            return cls()
        valid = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {key: value for key, value in data.items() if key in valid}
        config = cls(**clean)
        # 类型纠正，避免坏配置导致后续崩溃。
        config.groups = [str(item) for item in config.groups if str(item).strip()]
        # 接入平台与 QQ 凭据。
        platform = str(config.platform or "").strip().lower()
        config.platform = platform if platform in ("wechat", "qq") else "wechat"
        # 旧配置没有这两个开关：按 platform 推导，避免升级后突然双端同时上线。
        if "wechat_enabled" not in data and "qq_enabled" not in data:
            config.wechat_enabled = config.platform == "wechat"
            config.qq_enabled = config.platform == "qq"
        config.wechat_enabled = bool(config.wechat_enabled)
        config.qq_enabled = bool(config.qq_enabled)
        if not config.wechat_enabled and not config.qq_enabled:
            # 至少保留一个终端；界面也会阻止把最后一个关掉。
            config.wechat_enabled = config.platform != "qq"
            config.qq_enabled = config.platform == "qq"
        config.qq_app_id = str(config.qq_app_id or "").strip()
        config.qq_app_secret = str(config.qq_app_secret or "").strip()
        config.context_messages = max(1, int(config.context_messages))
        config.temperature = float(config.temperature)
        config.max_tokens = max(1, int(config.max_tokens))
        config.timeout_seconds = max(1.0, float(config.timeout_seconds))
        # 情绪引擎参数。
        config.emotion_enabled = bool(config.emotion_enabled)
        config.emotion_identity = (
            str(config.emotion_identity or "").strip() or "violet"
        )
        # 兼容旧配置：旧身份 id 继续指向改名后的内置身份。
        if config.emotion_identity == "song_nuan":
            config.emotion_identity = "violet"
        mode = str(config.emotion_llm_mode or "").strip().lower()
        # 未知取值按「只让表达走模型」处理：默认省 token，也不静默退化成纯规则。
        config.emotion_llm_mode = mode if mode in ("all", "expression", "none") else "expression"
        config.emotion_persona_dir = str(config.emotion_persona_dir or "").strip()
        config.emotion_persona_enabled = bool(config.emotion_persona_enabled)
        config.emotion_state_dir = str(config.emotion_state_dir or "").strip()
        config.vision_enabled = bool(config.vision_enabled)
        config.vision_max_images = max(0, int(config.vision_max_images))
        config.vision_scope = str(config.vision_scope or "conversation").strip().lower()
        if config.vision_scope not in ("current_message", "conversation"):
            config.vision_scope = "conversation"
        # 链接解析参数。
        config.link_parse_enabled = bool(config.link_parse_enabled)
        config.link_parse_max = max(0, int(config.link_parse_max))
        config.link_parse_timeout = max(1.0, float(config.link_parse_timeout))
        config.link_cookies_file = str(config.link_cookies_file or "").strip()
        config.link_download_enabled = bool(config.link_download_enabled)
        config.link_download_max = max(0, int(config.link_download_max))
        config.link_download_dir = str(config.link_download_dir or "").strip()
        config.link_download_max_mb = max(0, int(config.link_download_max_mb))
        config.link_download_quota_mb = max(0, int(config.link_download_quota_mb))
        # 至少 1 次（0 会让「下载」这件事直接不执行，语义上说不通）。
        config.link_download_attempts = min(
            10, max(1, int(config.link_download_attempts))
        )
        config.link_download_fail_notice = bool(config.link_download_fail_notice)
        # 重试次数上限给到 10：再多也只是让发送闸门被占更久，收益递减。
        config.link_send_attempts = min(10, max(0, int(config.link_send_attempts)))
        config.link_tweet_card_enabled = bool(config.link_tweet_card_enabled)
        config.link_tweet_translate = bool(config.link_tweet_translate)
        config.link_xhs_card_enabled = bool(config.link_xhs_card_enabled)
        # 目标语言留空即回到默认值——空串会让提示词变成「翻译成」这种半句话。
        config.link_tweet_translate_target = (
            str(config.link_tweet_translate_target or "").strip() or "简体中文"
        )
        config.link_ack_enabled = bool(config.link_ack_enabled)
        # 文案留空即视为「不发提示」，故不做默认值回填（只去空白与换行）。
        config.link_ack_text = " ".join(str(config.link_ack_text or "").split())
        config.link_llm_followup = bool(config.link_llm_followup)
        config.link_supported_only = bool(config.link_supported_only)
        config.link_extra_sites = str(config.link_extra_sites or "").strip()
        # 撤回参数。
        config.recall_enabled = bool(config.recall_enabled)
        # 指令词留空会变成「没有指令」，这与「关掉撤回」重复，故空串回填默认值。
        config.recall_keywords = (
            " ".join(str(config.recall_keywords or "").split()) or "撤回"
        )
        config.recall_require_at = bool(config.recall_require_at)
        config.recall_admin_only = bool(config.recall_admin_only)
        # 手改配置文件时可能把名单写成一行字符串，这里一并兼容。
        raw_admins = config.recall_admins
        if isinstance(raw_admins, str):
            raw_admins = re.split(r"[,，、;；\s]+", raw_admins)
        config.recall_admins = [
            str(item).strip() for item in (raw_admins or []) if str(item).strip()
        ]
        # 超过 120 秒微信就不让撤了，钳到上限免得给出一个做不到的承诺。
        config.recall_window_seconds = min(
            120.0, max(0.0, float(config.recall_window_seconds))
        )
        config.recall_max_messages = min(
            5, max(1, int(config.recall_max_messages))
        )
        config.recall_stop_download = bool(config.recall_stop_download)
        config.recall_notice_enabled = bool(config.recall_notice_enabled)
        config.recall_ok_text = " ".join(str(config.recall_ok_text or "").split())
        config.recall_fail_text = " ".join(
            str(config.recall_fail_text or "").split()
        )
        return config

    def link_extra_site_list(self) -> tuple[str, ...]:
        """白名单之外的站点域名（配置里用逗号/换行分隔）。"""
        raw = re.split(r"[,，、;；\s]+", str(self.link_extra_sites or ""))
        return tuple(item.strip().lower() for item in raw if item.strip())

    def recall_keyword_list(self) -> list[str]:
        """撤回指令词列表。"""
        return parse_recall_keywords(self.recall_keywords)

    def download_dir(self) -> Path:
        """链接下载目录：显式配置优先，否则用 %APPDATA%\\lingyu\\downloads。"""
        raw = self.link_download_dir.strip()
        if raw:
            return Path(raw).expanduser()
        return default_config_path().parent / "downloads"

    def download_quota_bytes(self) -> int:
        """下载目录总占用上限（字节）；0 表示不限制。"""
        return max(0, int(self.link_download_quota_mb)) * 1024 * 1024

    def emotion_dir(self) -> Path:
        """情绪状态目录：显式配置优先，否则 %APPDATA%\\lingyu\\emotion。

        导入放在函数里：``bot.config`` 被 ``bot.emotion`` 的宿主侧依赖，
        模块级互相导入会绕成环。
        """
        raw = self.emotion_state_dir.strip()
        if raw:
            return Path(raw).expanduser()
        from .emotion.session import default_state_dir

        return default_state_dir()

    def save(self, path: Path | None = None) -> Path:
        target = path or default_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    @classmethod
    def load(cls, path: Path | None = None) -> "BotConfig":
        target = path or default_config_path()
        if not target.is_file():
            return cls()
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return cls()
        return cls.from_dict(data)
