# 技术文档

本文档面向开发者与运维，覆盖架构、MCP 工具接口、配置、故障排查、链接解析与构建流程。
项目简介与安装方式见 [README.md](../README.md)。

---

## 1. 架构

```
MCP 客户端 / 桌面界面
        │
        ├── server.py        MCP 工具注册（stdio 传输）
        └── desktop.py       pywebview 桌面窗口（--gui）
                    │
                service.py    业务层：参数校验、目标消歧、结果封装
                    │
          adapters/wechat.py  适配层：WeChatAdapter
                    │
        packaging/wechat_bridge.py   桥接层（运行期按文件路径加载）
                    │
            wxauto / uiautomation / wechatauto   UI 自动化
                    │
              Windows 微信桌面客户端
```

设计要点：

- **分层解耦**：MCP 层不直接碰 UI 自动化；适配层可在无 MCP 的情况下独立调用。
- **桥接层按路径加载**：用 `importlib.util.spec_from_file_location` 从文件加载，
  不进 `sys.path`，因此可以整体替换而不影响包导入。
- **源码与冻结包同一条路径**：源码模式读仓库内 `packaging/wechat_bridge.py`，
  冻结包读 `_MEIPASS/wechat_bridge.py`，不存在「本机能跑、打包后找不到模块」的分叉。
- **结构化错误**：所有失败都返回稳定错误码，不抛裸异常、不虚报成功。

### 目录结构

| 路径 | 说明 |
|---|---|
| `src/lingyu/server.py` | MCP 服务入口，注册全部工具 |
| `src/lingyu/service.py` | 业务层，工具与适配层之间的编排 |
| `src/lingyu/adapters/wechat.py` | 微信适配层，桥接层加载与调用封装 |
| `src/lingyu/bot/` | 常驻机器人：引擎、模型、人设、链接解析、卡片渲染、存储 |
| `src/lingyu/desktop.py` | 桌面端主程序与 JS API |
| `src/lingyu/webui/` | 桌面端前端（HTML/CSS/JS） |
| `packaging/wechat_bridge.py` | 微信 UI 自动化桥接层 |
| `vendor/yt_dlp/` | 内置的 yt-dlp，用于链接解析 |
| `packaging/` | PyInstaller spec、Inno Setup 脚本、构建入口 |

---

## 2. MCP 工具接口

所有工具返回结构化 JSON，失败时为
`{"ok": false, "code": "...", "message": "...", "detail": {...}}`。

- P0：`get_wechat_status`、`get_chat_list`、`get_chat_history`、`send_message`
- P1：`search_contact`、`get_chat_info`、`send_file`、`get_recent_messages`、`set_monitored_chats`

### get_wechat_status

检查微信连接状态、窗口可见性与自动化后端。**输入**：无。

**输出**

| 字段 | 说明 |
|---|---|
| `ok` | 是否已连接 |
| `state` | `disconnected` / `connecting` / `connected` / `error` |
| `backend` | 生效的自动化后端（`wechatauto` / `wxauto4` / `wxauto`） |
| `listening` | 消息监听是否真正处于活动状态 |
| `window_visible` | 微信主窗口是否可见 |
| `uia_available` | UI Automation 是否可用 |
| `detail` | 附加诊断信息 |
| `error` | 连接失败时的结构化错误 |

### get_chat_list

返回当前可读取的会话列表。

> 读取基于监听期间**被动接收**到的消息，不是微信完整会话列表。

**输入**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `limit` | int | 否 | 最大返回数量 |
| `keyword` | str | 否 | 会话名过滤条件 |

**输出**：`ok`、`chats[]`（`name`/`is_group`/`message_count`/`last_message_at`）、
`total`、`complete`、`note`。

### get_chat_history

读取指定会话在缓冲区内最近的消息。

**输入**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `chat_name` | str | 是 | 必须明确指定会话名 |
| `limit` | int | 否 | 返回条数，默认 20 |

**输出**：`ok`、`chat`、`messages[]`（`chat`/`sender`/`content`/`message_type`/
`is_group`/`timestamp`/`message_id`）、`complete`、`note`。

### send_message

向明确指定的联系人或群聊发送文本。

**输入**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `recipient` | str | 是 | 联系人或群聊名称 |
| `message` | str | 是 | 文本内容 |
| `dry_run` | bool | 否 | 仅校验、不实际发送 |
| `confirm` | bool | 否 | 显式确认发送（当 `WECHAT_REQUIRE_CONFIRM=1` 时必需） |

**输出**：`ok`、`status`（`sent` / `failed` / `dry_run` / `confirmation_required`）、
`recipient`、`resolved_recipient`、`message_preview`、`error`。

**安全约定**

- 目标为空、含控制字符或超长 → `target_invalid`。
- 目标不唯一（同名或多个部分匹配）→ `target_ambiguous`，返回候选列表，拒绝发送。
- 未连接微信 → `not_connected`。
- 开启发送确认且未带 `confirm=true` → `status=confirmation_required`，不发送。
- 超时或结果无法确认 → `status=failed`，**不自动重试**，避免重复消息。

### search_contact

按名称搜索联系人/群聊，返回候选列表。**同名时原样返回多个候选，不自行猜测**。

**输入**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `keyword` | str | 是 | 昵称 / 备注 / 微信号片段 |
| `limit` | int | 否 | 最大返回数量，默认 20 |

**输出**：`ok`、`keyword`、`candidates[]`（`name`/`username`/`remark`/`is_group`）、
`total`、`source`、`note`、`error`。

> `source`：`wechat_db` 表示来自微信本地联系人库（后端为 wechatauto）；
> `buffer` 表示后端无本地库，退回为「监听期间观察到的会话名称」。

### get_chat_info

返回指定会话已确认可获取的信息。

**输入**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `chat_name` | str | 是 | 会话名称 |

**输出**：`ok`、`name`、`is_group`、`message_count`、`last_message_at`、
`monitored`（是否在关注列表内）、`note`、`error`。

### send_file

向明确指定的联系人或群聊发送本地文件。

**输入**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `recipient` | str | 是 | 联系人或群聊名称 |
| `file_path` | str | 是 | 本地文件绝对路径 |
| `dry_run` | bool | 否 | 仅校验文件与目标、不实际发送 |
| `confirm` | bool | 否 | 显式确认发送（当 `WECHAT_REQUIRE_CONFIRM=1` 时必需） |

**输出**：`ok`、`status`、`recipient`、`resolved_recipient`、`file_path`、
`file_name`、`file_size`、`dry_run`、`error`。

**安全约定**

- 路径为空 / 不存在 / 不是文件 / 不可读 → `file_invalid`。
- 未配置可发送目录，或文件不在允许目录内（含 `..` 路径穿越）→ `path_not_allowed`。
- 目标不唯一 → `target_ambiguous`，拒绝发送。
- 发送结果无法确认 → `status=failed`，**不自动重试**。

### get_recent_messages

增量读取监听缓冲区中新增的消息。

**输入**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `after_seq` | int | 否 | 上次返回的 `next_seq`，默认 0（从头） |
| `limit` | int | 否 | 单次最大返回条数，默认 20 |
| `chat_name` | str | 否 | 仅返回该会话的消息 |

**输出**：`ok`、`messages[]`、`next_seq`、`complete`、`note`、`error`。

> 调用方应保存 `next_seq` 并在下次调用时作为 `after_seq` 传入。
> 被关注列表过滤掉的消息会跳过，但游标仍向前推进。

### set_monitored_chats

设置会话关注/忽略列表。**仅过滤本服务返回的读取结果，不改变底层监听行为。**

**输入**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `add` | list[str] | 否 | 加入当前模式对应列表的会话名 |
| `remove` | list[str] | 否 | 从两个列表移除的会话名 |
| `mode` | str | 否 | `allow`（白名单，空=关注全部）或 `block`（黑名单） |

**输出**：`ok`、`mode`、`allow`、`block`、`note`、`error`。

**行为约定**

- `allow` 模式且列表为空 → 关注全部会话。
- `block` 优先级高于 `allow`：命中黑名单的会话一律不出现在读取结果中。
- 可在 `WECHAT_MONITOR_MODE` / `WECHAT_MONITOR_ALLOW` / `WECHAT_MONITOR_BLOCK` 预设，
  运行时通过本工具增删。

---

## 3. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `WECHAT_BACKEND` | `auto` | `auto` / `wechatauto` / `wxauto4` / `wxauto` |
| `WECHAT_SEND_TIMEOUT` | `20` | 单次发送最长等待秒数 |
| `WECHAT_LISTEN_ON_CONNECT` | `1` | 连接后是否启动消息监听 |
| `WECHAT_SEND_DIRS` | 空 | 允许发送文件的目录（逗号分隔）；空=拒绝发送文件 |
| `WECHAT_REQUIRE_CONFIRM` | `0` | 发送前是否需 `confirm=true` |
| `WECHAT_AUDIT_ENABLED` | `1` | 是否记录操作审计 |
| `WECHAT_AUDIT_LOG` | `<项目根>/logs/audit.jsonl` | 审计日志路径 |
| `WECHAT_MONITOR_MODE` | `allow` | 关注列表模式 `allow` / `block` |
| `WECHAT_MONITOR_ALLOW` | 空 | 预设白名单（逗号分隔，空=全部） |
| `WECHAT_MONITOR_BLOCK` | 空 | 预设黑名单（逗号分隔） |

桌面端的模型、人设、链接解析等配置不读环境变量，统一存在
`%APPDATA%\lingyu\bot.json`，界面里改即可。

### 客户端配置示例

```json
{
  "mcpServers": {
    "lingyu": {
      "command": "F:\\Code\\lingyu\\.venv\\Scripts\\python.exe",
      "args": ["-m", "lingyu.server"]
    }
  }
}
```

---

## 4. 错误码

| 错误码 | 含义 | 常见原因 |
|---|---|---|
| `backend_unavailable` | 自动化后端/依赖不可用 | 桥接模块缺失、依赖未安装 |
| `not_connected` | 尚未连接微信 | 微信未登录、主窗口不可见、连接失败 |
| `connect_failed` | 连接失败 | 微信进程/窗口异常 |
| `target_invalid` | 目标不合法 | 名称为空、含控制字符、超长 |
| `target_ambiguous` | 目标不唯一 | 存在同名或部分匹配的多个会话 |
| `file_invalid` | 文件路径不合法 | 文件不存在、不是文件、不可读 |
| `path_not_allowed` | 文件不在允许目录 | 未配置 `WECHAT_SEND_DIRS` 或文件越界 |
| `confirmation_required` | 需要显式确认 | `WECHAT_REQUIRE_CONFIRM=1` 但未传 `confirm=true` |
| `unsupported_operation` | 当前后端不支持 | 该能力依赖特定后端 |

---

## 5. 桌面助手（`--gui`）

同一个 exe 加 `--gui` 启动原生窗口，无需 Agent 轮询：

- **触发**：私聊每条；群里 @ 机器人账号的微信昵称，或引用/回复它的消息。
- **回复**：取最近 N 条上下文 + 人设发给大模型，按配置拟人化（回复概率、
  长回复分条、口语化措辞）。
- **静默与水群**：静默时段、群级回复节流、可选的偶发主动参与（默认关闭）。
- **链接解析**：见第 6 节。
- **图像识别**：开启后（需模型支持视觉）图片随上下文一起送给模型。

**前置条件**：界面靠 Edge WebView2 渲染，系统缺这个运行时会启动失败并明确提示要装什么
（见第 7 节「桌面端提示缺少 WebView2 运行时」）。**MCP 模式不需要 WebView2**。

配置全部落在 `%APPDATA%\lingyu\bot.json`，运行日志在
`%APPDATA%\lingyu\logs\`。

> 改配置文件后必须重启程序：`desktop.py` 只在启动时 `BotConfig.load()` 一次。
> 运行中的实例若在界面里改了任何设置，会把内存里的配置写回磁盘。

---

## 6. 链接解析

### 触发

消息里出现链接就处理，与「这条要不要回」无关：群里没 @ 也会先回一句固定提示
（默认「正在解析链接」，不经过大模型），随后把解析结果发回。

### 各站点路径

| 站点 | 做法 | 依赖 |
|---|---|---|
| B 站 / YouTube 等 | 内置 yt-dlp 取标题/作者/时长/简介，可选下载 | 网络 |
| 抖音 | 借本机 Chrome/Edge 打开作品页，由页面完成 `a_bogus` 签名 | 系统装有 Edge 或 Chrome |
| X（Twitter） | 抓推文数据 → 本地渲染卡片图（头像/昵称/正文/配图/时间/点赞） | 网络；含视频时再下载视频 |
| 小红书 | 读笔记页内嵌的 `window.__INITIAL_STATE__` → 渲染同一套版式的卡片图 | 网络；只有少数要求登录态的笔记才需要 `cookies.txt` |
| 微博 / 其它 | yt-dlp 直接解析 | 需要 Netscape 格式的 `cookies.txt` |

抖音的浏览器桥会临时起一个无头浏览器，用**独立的临时用户目录**，
不碰既有的浏览器配置和登录态。

### 小红书笔记解析

**为什么单独写一个解析器**：小红书笔记页是**服务端渲染**的，首次 HTML 里就带一份
`window.__INITIAL_STATE__`，笔记数据（标题/正文/作者/头像/图片/点赞/评论）全在里面。
所以**不需要签名、也不需要调接口**，一次普通 GET 就能拿全。yt-dlp 没有小红书提取器，
之前只能落到网页兜底——分享短链被当成标题、正文拿不到，等于解析失败。

**移动端 UA 是免登录的前提**（实测）：

| UA | 结果 |
|---|---|
| 桌面 UA（`Windows NT … Chrome/124`） | 302 到 `/login`，真地址被塞进 `redirectPath` |
| iPhone / Android UA | 直接返回完整页面 |

所以 `fetch_note()` 固定用移动 UA。两端页面结构不同，`note_from_state()` 两套都认：

| 端 | 数据路径 | 图片字段 |
|---|---|---|
| 桌面版 | `note.noteDetailMap[<noteId>].note` | `WB_DFT` |
| 移动 H5 | `noteData.data.noteData` | `H5_DTL`（1080 宽） |

**`xsec_token` 不能丢**：从 App/网页「分享」出来的链接带 `?xsec_token=…`。手工拼
`explore/<id>` 而不带 token 会被 302 到
`/404?error_code=300031&error_msg=当前笔记暂时无法浏览`——**这不等于「笔记不存在」**，
只是没有访问凭证，错误信息必须如实区分（`_error_from_url()` 把 `error_msg` 原样带回）。
**短链展开时也要保留 query**，token 就在里面。

**短链展开的时机很关键**：App 分享出来的是 `xhslink.cn/o/xxx`，本身不含笔记 ID。
卡片这条路要先 `LinkResolver.expand_short_link()` 展开再解析，否则 `note_for()`
直接报「未能从链接中识别笔记 ID」；而下载那一路是在 `_attempt_download()` 里
自己展开的——**两条路时机不同**，别指望解析器自己会展开。

**分享文案里的裸短链**：App 复制出来的文案常常不带 `http://`
（`xhslink.com/a/xxx`、`xhslink.cn/o/xxx`），所以 `links.extract_urls()` 额外认
几个已知短链主机的裸域名（`_BARE_HOSTS`）。

**浏览器兜底**：HTML 里拿不到数据（被重定向到 `/404`、`/login`，或状态里是空壳）时，
用浏览器桥再打开一次。注意 `--headless=new` 的 UA 带 `HeadlessChrome`，小红书一眼识破
并甩「IP存在风险」风控页——所以这里**必须换正常 UA 并关掉 `AutomationControlled`**
（走 `BrowserSession(extra_args=…)`）。

**缓存**：`note_for()` 只缓存成功结果，同一条笔记再次出现不重复抓取；
`forget_cached(note_id)` 清单条、`forget_cached()` 全清。

### 卡片版式：推文与笔记共用一套

`bot/card.py` 把「一张社交帖子卡片需要什么」定义成 `CardData`，两边各自转换：

| 来源 | 转换函数 | 差异在哪、怎么抹平 |
|---|---|---|
| 推文 | `Tweet.to_card()` | 有 `@handle`、认证标记、可带译文 |
| 小红书笔记 | `note_to_card()` | 没有 `@用户名` → 副标题改用「小红书 · 图文（5 张）」；正文分标题+描述两段；互动口径是「赞 / 评论」 |

`tweet.render_card()` 只认 `CardData`（也兼容直接传 `Tweet`，内部自动转），
所以两边**版式完全一致**：头像/昵称块 → 正文（有译文用译文）→ 原文块 → 配图 → 页脚。
以后再加平台，只要写一个 `xxx_to_card()`，不用碰渲染器。

几个渲染约定：

- **配图竖排成一条长图**：几张图各按原比例铺满宽度、从上往下摞，不裁切也不拉伸，
  整张卡片一条消息发完。
- **最多画 4 张**（`card.photos[:4]`）：长图不能无限长。超出的在页脚标一句
  「共 N 张图」，`photo_total` 是作品真实图数（不是画了几张）。X 单条最多 4 张图，
  推文侧 `photo_total` 默认 0（= 不提示），所以这句实际只在小红书卡片上出现——
  小红书图文常见 5～9 张，一定会触发截断。
- **页脚**：时间 · 共 N 张图 · 赞 · 评论，空字段不占位。
- **落盘文件名**：`f"{card.prefix or 'card'}-{card.id}.png"`，即 `tweet-<id>.png` /
  `xhs-<id>.png`，在下载目录里一眼能看出是哪个站点来的。
- **视频封面**：`video_poster` 命中的那张画播放角标。

**小红书具体发什么**：由 `link_xhs_card_enabled`（界面「链接解析 → 小红书笔记卡片」）控制。

- **图文笔记只发卡片**，不逐张发原图——原图常有 5～9 张，逐张发等于往聊天里刷 N 条消息，
  与「一条链接回发一份内容」的语义不符。
- **视频笔记**在卡片之后再下载视频回发。
- 解析失败、或卡片渲染失败时**退回通用下载**，至少把失败原因发进聊天，而不是石沉大海。

### 卡片消息为什么要回查数据库

微信里从 App 分享过来的链接、转发的公众号文章，消息类型是 `49`
（`文件/链接/卡片`）。**UIA 监听通道对它只给类型占位符** `[文件/链接/卡片]`，
正文——包含 `<url>` 的 appmsg XML——只存在于数据库的 `StrContent`。

所以桥接层在收到这个占位符时会调 `_lookup_raw_content(chat_id, local_id)`
回查数据库取原文，再走一遍 `normalize_message_content()` 把 `<url>` 提出来：

```
[文件/链接/卡片]  →  回查 DB  →  <?xml…<title>标题</title><url>https://…</url>
                  →  归一化   →  "标题\nhttps://…"  →  extract_urls() 命中
```

- 监听可能略早于落库，查不到时等 `RAW_LOOKUP_RETRY_DELAY`（0.15 秒）再翻一次
- 数据库不可用 / 对不上 `local_id` 时静默降级，沿用占位符，不影响其它消息
- 纯文本链接（直接在输入框粘贴的 URL）本来就有正文，不需要回查

### 下载与重试

开启「下载并回发」后，链接内容由 `LinkResolver` 下载，引擎负责重试与反馈：

| 层 | 职责 |
|---|---|
| `links.LinkResolver.download_detailed()` | 单次尝试，返回 `DownloadOutcome(path, error, retryable)` |
| `engine._download_with_retry()` | 按 `link_download_attempts` 重试 + 退避 |
| `engine._notify_download_failure()` | 用尽次数后往聊天里发一句提示 |

- **次数**：`link_download_attempts`，默认 3（含首次），范围 1–10。
- **退避**：`_RETRY_DELAYS = (1.5, 3.0, 6.0, 10.0)` 秒。yt-dlp 偶发的
  `Unable to extract initial state`（B 站常见）往往要等对面接口缓过来，
  立刻重试基本必挂，所以第一次也要等 1.5 秒。
- **可重试性**：默认**可重试**，只有命中 `_PERMANENT_FAILURE_RE` 的确定性失败
  （`Unsupported URL`、`HTTP 404`、视频已删除/私密、格式不存在）或体积超限才
  立即收手——再试也是同一个结果，只会让用户多等两轮退避。
- **体积超限**：yt-dlp 超 `max_filesize` 时**不抛异常**，只是安静地跳过。
  判定方式是看它被吞掉的告警里有没有 `larger than max-filesize`
  （`_QuietLogger.messages` 留了一份原文）。
- **失败提示**：`link_download_fail_notice`（默认开）控制。文案保持**单行**
  ——`_send_reply` 会按句号/换行分条，多行会让一次提示拆成两条消息。
  提示里带失败原因（截断 80 字），方便直接看出是缺 Cookie、网络问题还是站点不支持。
- **推文视频**：`Tweet.video_url` 直链先按同样次数重试，失败后才退回 yt-dlp；
  这一路**不发**提示，由兜底的 yt-dlp 统一发，避免同一段视频连发两条坏消息。

### 诊断命令

```powershell
.\lingyu.exe --selfcheck                                    # 版本 / yt-dlp / ffmpeg / webui
.\lingyu.exe --resolve <url> [--download] [--outdir <目录>]  # 直接跑一遍解析
.\lingyu.exe --tweet <推文链接> [--outdir <目录>]             # 抓推文并渲染卡片图
```

`--resolve` 走通用解析链路，小红书、B 站、抖音、微博都适用；`--tweet` 只针对推文
（它会真的渲染 PNG，是排查卡片版式最快的办法）。小红书**没有**单独的诊断开关，
解析结果直接看 `--resolve` 的输出即可；需要看卡片成品就设
`link_xhs_card_enabled` 打开后发一条笔记链接，或直接跑 `scripts/dev/xhs_card_demo.py`。

### 常见问题

- **`yt_dlp_version` 显示「不可用」**：内置 yt-dlp 导入失败，其下方会给出
  `yt_dlp_error`（冻结包缺标准库子模块的典型症状）。
- **媒体站点解析不出标题**：确认网络可达；普通网页会自动回退到抓取 `<title>`。
- **抖音提示「未找到 Chrome/Edge」**：装 Edge 或 Chrome（Windows 10/11 自带 Edge）。
- **微博报 `fresh cookies ... are needed` 或 `HTTP 403`**：微博网页是空壳、接口要 Cookie，
  必须提供 `cookies.txt`。
  - 在 Chrome/Edge 里登录微博，用扩展（如 *Get cookies.txt LOCALLY*）导出
    **Netscape 格式**的 `cookies.txt`（文件以 `# Netscape HTTP Cookie File` 开头）。
  - 不必登录也能导出，但多数站点会持续校验，**登录后导出更稳**；文件会过期，失效重导。
  - 路径填错、指向目录或文件为空时，程序**当成没填**处理，继续走匿名请求。
- **小红书提示「当前笔记暂时无法浏览」**：这是 `error_code=300031`，**不是笔记被删**，
  而是链接里缺 `xsec_token`。用 App/网页的「分享 → 复制链接」重新发一次，别手打
  `explore/<id>`；短链（`xhslink.cn/o/xxx`）本身就带着 token，直接发即可。
- **小红书提示「要求登录后才能查看」**：少数笔记只对登录用户开放，在「链接解析」页配上
  `cookies.txt` 再试（只会取 `.xiaohongshu.com` 域下的键，不会把别的站点 Cookie 带过去）。
- **小红书报「IP存在风险」**：浏览器兜底被风控识别。程序内置的兜底已经换成正常 UA 并关掉
  `AutomationControlled`；若仍频繁出现，多半是本机 IP 被临时限流，过一阵再试。
- **小红书卡片发了但图不全**：卡片最多画 4 张，页脚会标「共 N 张图」——这是设计如此，
  长图不能无限长。关掉「小红书笔记卡片」也不会逐张发原图，只会收到「图文笔记暂不支持下载」
  的提示：N 张图等于 N 条消息，与「一条链接回发一份内容」的语义不符。
- **下载音视频失败**：DASH 分离流必须靠 ffmpeg 合流，`--selfcheck` 里的 `ffmpeg`
  必须可用（打包版已内置）。
- **聊天里收到「下载失败（重试 N 次仍不行）」**：说明自动重试已经用尽，看提示里的
  原因对症处理——`fresh cookies ... are needed` / `HTTP 403` 去配 `cookies.txt`；
  `HTTP 404`、视频已删除或私密、`Unsupported URL` 是链接本身的问题，重试也没用；
  `超过体积上限` 调大「单文件上限」；其余（超时、连接被重置、`Unable to extract
  initial state`）多为站点侧抖动，过一会儿重发链接即可。
- **「下载并回发」默认关闭**：开启后下载目录会被自动纳入发送白名单，
  否则回发会被 `path_not_allowed` 拒绝。
- **下载目录越来越大**：由「目录占用上限」（`link_download_quota_mb`，默认 1024 MB）
  控制。每次下载完成后统计目录总大小，超出就按修改时间**从旧到新**删除，
  运行日志会打印「已清理 N 个旧文件」。填 0 表示不清理。
- **只回了「正在解析链接」就没下文**：先确认界面「链接解析」页里的
  **「下载并回发」开关是否打开**——关着时只发提示、不执行解析回发；
  再看「模型设置」里 **API Key 是否已填**（为空时模型调用会报错）。

---

## 7. 故障排查

按「先看结构化错误码，再定位根因」的顺序排查。

### 连接失败 / `not_connected`

1. 确认微信 PC 客户端已启动并登录。
2. 确认微信主窗口**可见**：最小化、锁屏、被其他窗口完全遮挡都可能导致失败。
3. 调用 `get_wechat_status` 查看 `window_visible`、`uia_available`、`backend`。
4. 若 `backend` 为空或 `uia_available=false`，检查自动化依赖是否安装：

   ```powershell
   .venv\Scripts\python.exe -m pip install -e ".[wechat]"
   .venv\Scripts\python.exe -m pip install --no-deps -r requirements\wechat.txt
   ```

   `wechatauto-replica` 声明了 `winsdk>=1.0.0b10`，而 winsdk 的 wheel 只到 cp311，
   Python 3.12/3.13 上 pip 会去源码编译并失败。运行期由桥接层把 `winsdk.*`
   别名到 `winrt.*`，所以它必须用 `--no-deps` 单独装（见 `requirements/wechat.txt`）。

### `get_chat_list` / `get_chat_history` 结果为空

这是**预期行为**，不是 Bug：

- 读取能力基于监听回调收集的消息，只能获取适配层启动后收到的新消息。
- 结果只包含**来自其他账号的入站消息**；自己发送的消息和系统号消息会被过滤。
- 若刚启动服务，请在监听到他方消息后再查询；或调用 `get_recent_messages` 观察增量。

### 群聊里 @ 了机器人却不回复

原因：**@ 的名字和机器人账号的微信昵称不一致**。@ 识别是拿**消息文本**去匹配
`@<机器人昵称>`（后端没有提供「这条消息 @ 了我」的标记），名字对不上就永远不触发。

- 看「运行日志」里的 `已识别机器人微信昵称: …`，那才是要 @ 的名字。
  群消息被忽略时也会打一行：
  `群消息未 @ 机器人，已忽略 [群名] 某人：…｜机器人昵称：…`。
- **手打 `@某某` 只是普通文字**，不会被当成 @ 提醒；正确做法是输入 `@` 后
  **从弹出的成员列表里选中机器人**。想省事就把机器人账号昵称改成你习惯 @ 的名字。
- 昵称在连接微信时读取，改了不必重启（下一条 @ 消息会自动跟上）。
- 除 @ 外还有两种触发：**引用/回复机器人的消息**，以及开启「会话延续」后
  刚被回复过的人继续发言。

#### 名字对上了还是不回

多半是**这个群没有设置群名称**。没设群名时 `contact.db` 里的 `nick_name` /
`remark` 都是空的，而微信搜索框只认名字、不认 `xxx@chatroom` 这种 ID，
发送前定位不到会话就会拒绝发送——现象是日志里**有入站消息、也有模型调用成功**，
但群里始终没有回复。

- 会提前打警告：
  `群 xxx@chatroom 没有群名称，微信搜索框无法定位它，该群里的回复发不出去；请在微信里给这个群设置一个群名称。`
- 发送失败时给出 `chat_id_unresolved` 与具体做法。
- **解决办法**：在微信里给这个群**设置群名称**（群设置 → 群名称）。
- 还会尝试兜底：用**群成员昵称**拼出界面上显示的名字去定位（需至少两个成员有昵称；
  只有一个名字时不猜，因为微信搜索会先命中同名联系人，可能把群消息发进私聊）。

### 发送超时或 `status=failed`

- 向**尚未打开**的会话首次发送需要搜索、切换、输入，耗时较长。底层超时默认
  放宽到 20 秒，可通过 `WECHAT_SEND_TIMEOUT` 调整。
- 结果无法确认时返回 `failed`，**不会自动重试**。请先人工确认该消息是否已发出，
  再决定是否重新发送，避免重复消息。
- 发送失败后底层有短冷却与串行锁，连续发送会自动排队，属正常现象。

### `target_ambiguous`

- 目标名称匹配到多个会话。请使用更完整的名称重新调用。
- 若名称含前导/尾随空格，可用 `search_contact` 获取准确名称。

### `path_not_allowed` / 发送文件被拒绝

- 默认拒绝发送任何文件是**有意为之**。配置允许目录后再试：

  ```
  WECHAT_SEND_DIRS=F:\Code\shared,F:\tmp\send
  ```

- 文件必须位于允许目录内；使用 `..` 尝试越界会被拒绝。
- 确认文件存在、是普通文件且可读，否则返回 `file_invalid`。

### 桌面端提示缺少 WebView2 运行时

现象：双击 `lingyu.exe`，弹出「缺少 Microsoft Edge WebView2 运行时」并自动打开浏览器下载页。

原因：桌面界面由 pywebview + Edge WebView2 渲染。Windows 10/11 通常自带，但**精简版系统、
LTSC、部分企业镜像、或手动卸载过 Edge** 的机器上可能没有。

处理：

1. 在弹窗打开的官方下载页装 **Evergreen Bootstrapper（x64）**，装完重开程序。
2. 想手动确认：注册表
   `…\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}` 下的 `pv`
   存在且不等于 `0.0.0.0` 即为已安装（HKLM/HKCU × WOW6432Node/native 四个位置都会查）。
3. 装完仍提示：确认装的是 **WebView2 Runtime**，不是 Edge 浏览器本体——两者不通用。

> 0.8.17 之前，缺 WebView2 会被兜底文案说成「Python.Runtime.dll 加载失败，请解除文件锁定」，
> 把渲染器初始化失败误判成 .NET 程序集被锁定，用户照着做多少遍都没用。现在启动前先检查，
> 缺了就直接说明要装什么。

### 桌面端启动后黑屏

现象：双击 `lingyu.exe`，窗口出来一片黑（或闪一下就没），`logs\` 里什么都没有。

原因：从 zip 解压的文件会被 Windows 打上「Internet 区域」标记
（`Zone.Identifier`，`ZoneId=3`）。.NET Framework 出于安全**默认拒绝加载**
带该标记的程序集，pythonnet 初始化失败，窗口画不出内容：

```
RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize
from <安装目录>\_internal\pythonnet\runtime\Python.Runtime.dll
```

本地直接构建的 `dist/` 没有这个标记，所以「本机能跑、发给别人就黑屏」。

处理：

1. 新版启动时会**自动清除**内置程序集上的该标记，通常无需干预。
2. 若仍黑屏，把安装目录里所有文件解除锁定，或重新解压前对压缩包
   **右键 → 属性 → 勾选「解除锁定」**。命令行：

   ```powershell
   Get-ChildItem -Path <安装目录> -Recurse -File | Unblock-File
   ```

3. `lingyu.exe --selfcheck` 不依赖 GUI，能跑通说明解释器与依赖没问题，
   黑屏就属于窗口/标记层面（若连 WebView2 都没装，启动前的检查会先拦下并给出明确提示，
   不会走到这一步）。

### 并发与窗口争抢

- 发送操作（文本/文件）通过互斥锁串行化，多个 Agent 调用会排队而非同时操作窗口。
- 期间不要手动操作微信窗口，以免与自动化抢焦点导致失败。
- **不要同时开两个实例**（双击两次就有两个 `lingyu.exe`，目前没有单实例保护）。
  两个实例会抢同一个微信窗口，表现为日志里反复出现
  `未检测到输入框 / 输入框探测连续失败，自动重新校准布局`、`发送消息超时`、
  `回复发送失败：底层发送未成功`，而且自动化还会把另一个窗口「自动最小化」藏起来。
  排查「发不出去」时先数一下进程。

---

## 8. 日志与审计

- MCP 模式：运行日志输出到 **stderr**，不污染 stdio 协议消息。
- 桌面端：运行日志写入 `%APPDATA%\lingyu\logs\app-YYYY-MM-DD.log`。
- 审计默认写入 `<项目根>/logs/audit.jsonl`（JSONL，一行一条），可用
  `WECHAT_AUDIT_LOG` 覆盖，或 `WECHAT_AUDIT_ENABLED=0` 关闭。
- 审计只记录工具名、目标、时间、结果与错误码，默认不记录聊天正文。

---

## 9. 测试

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -t .
.venv\Scripts\python.exe -m ruff check src tests packaging scripts
```

单跑某一组（改了解析器时最常用）：

```powershell
.venv\Scripts\python.exe -m unittest tests.test_bot.XhsTests -v
```

真实环境联调脚本位于 `scripts/live/`（需要已登录的微信）：

```powershell
# 阶段3：以 stdio 协议驱动 Server，覆盖连接/读取/发送/错误路径
.venv\Scripts\python.exe scripts\live\phase3_e2e.py --wait-inbound 60

# 阶段4：P1 能力真实微信联调
.venv\Scripts\python.exe scripts\live\phase4_live_check.py
```

覆盖范围：

| 脚本 | 内容 |
|---|---|
| `live/phase1_live_check.py` | 适配层连接、状态、会话列表 |
| `live/phase2_smoke.py` | stdio 握手、工具发现、状态工具调用 |
| `live/phase3_e2e.py` | 端到端场景：读取、发送、同名消歧、后端不可用的结构化错误 |
| `live/phase4_live_check.py` | 搜索、会话信息、文件发送、增量读取、关注列表 |

一次性探针与卡片样张在 `scripts/dev/`（OCR/UIA/像素探针、推文与小红书卡片样张），
它们依赖本机的微信版本与屏幕环境，**不进 CI**。跟小红书相关的几个：

| 脚本 | 用途 |
|---|---|
| `dev/xhs_probe.py` | 走 HTTP 路线解析单条笔记，打印标题/作者/图片数/赞评 |
| `dev/xhs_browser_probe.py` | 强制走浏览器兜底，验证风控与 UA 相关问题 |
| `dev/xhs_card_demo.py` | 渲染笔记卡片 PNG（只调渲染器，不连微信） |
| `dev/xhs_engine_demo.py` | 走完整引擎链路：解析 → 卡片 → 发送（需已登录微信） |

单元测试不依赖真实微信：适配层通过注入 `FakeBridge` 验证参数校验与错误语义。

---

## 10. 构建与发布

```powershell
# 1. 生成独立运行时（onedir + zip + 安装包），产物在 dist\
#    安装包需要本机装有 Inno Setup 6（未装则自动跳过，只出 onedir + zip）
.venv\Scripts\python.exe packaging\build.py

# 2. 把运行时内置进插件包
node dsh-plugin\scripts\stage-runtime.mjs

# 3. 打 tag 并推送（Release 指向这个 tag）
git tag v0.8.17; git push origin v0.8.17

# 4. 发布 GitHub Release（需已登录 gh；没装 gh 见下面的 REST 方式）
gh release create v0.8.17 dist\lingyu-win32-x64.zip dist\lingyu-setup-x64.exe --title "v0.8.17" --notes "…"

# 5. 发布 npm 插件（prepack 会自动执行第 2 步）
cd dsh-plugin
npm publish --access public
```

没有 `gh` 时用 REST API 建 Release 再传资产。token 可以直接复用 git 已保存的凭据，
不需要另外登录（Git Bash 下）：

```bash
TOKEN=$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill | sed -n 's/^password=//p')

# 建 Release，返回体里的 id / upload_url 后面要用
curl -s -X POST -H "Authorization: token $TOKEN" \
  https://api.github.com/repos/<owner>/<repo>/releases \
  -d '{"tag_name":"v0.8.17","name":"v0.8.17","body":"…"}'

# 上传资产：upload_url 去掉末尾的 {?name,label}，逐个文件传
curl -s -X POST -H "Authorization: token $TOKEN" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @dist/lingyu-setup-x64.exe \
  "https://uploads.github.com/repos/<owner>/<repo>/releases/<id>/assets?name=lingyu-setup-x64.exe"
```

产物：

| 文件 | 说明 |
|---|---|
| `dist\lingyu\` | PyInstaller onedir 目录 |
| `dist\lingyu-win32-x64.zip` | 供 Release 与插件分发的压缩包 |
| `dist\lingyu-setup-x64.exe` | Windows 安装包（按用户安装，不弹 UAC） |

打包会把**仓库内**的 `packaging/wechat_bridge.py` 内置进 exe，
构建不依赖任何外部目录，CI 可自动构建。

---

## 11. 插件分发（DSH）

```sh
dsh plugin --profile web add dsh-lingyu
```

插件通过官方 MCP 客户端桥接以 stdio 启动随包内置的独立运行时，
微信工具以 `mcp__lingyu__*` 暴露给模型。安装后 bundle 自动启用，无需额外配置。

### 运行时路径

插件按以下优先级定位 `lingyu.exe`：

1. 环境变量 `LINGYU_EXE` 指向的可执行文件（最可靠，任何安装布局都适用）；
2. 包内路径 `bin/lingyu/lingyu.exe`（从 npm 安装时位于 profile 的 `node_modules` 下）。

两者都不可用时，MCP 桥接会报 `ENOENT`。此时解压 `lingyu-win32-x64.zip` 并设置：

```sh
set LINGYU_EXE=D:\path\to\lingyu\lingyu.exe
```

### 插件构建

```powershell
.venv\Scripts\python.exe packaging\build.py    # 1. 生成独立运行时
node dsh-plugin\scripts\stage-runtime.mjs      # 2. 复制运行时进插件包
cd dsh-plugin; npm pack                        # 3. 打包（prepack 已绑定第 2 步）
```
