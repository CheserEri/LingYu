# 灵语（Lingyu）技术文档

本文是灵语的技术说明：架构分层、工具接口、错误语义、关键实现取舍、验证记录与故障排查。
面向需要阅读或修改代码的人。

- 版本：`0.8.14`
- 运行平台：Windows 10/11 x64
- 目录约定：`%APPDATA%\lingyu` 存放配置、日志与下载缓存

---

## 1. 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│ 接入层                                                        │
│   MCP stdio（server.py）          桌面窗口（desktop.py + webui）│
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│ 服务层  service.py                                            │
│   连接管理 / 发送 / 读取 / 关注列表过滤 / 操作审计             │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│ 适配层  adapters/wechat.py                                    │
│   结构化结果、目标校验与消歧、消息缓冲、串行化发送             │
└───────────────────────────┬──────────────────────────────────┘
                            │  （按文件路径加载，可单独替换）
┌───────────────────────────▼──────────────────────────────────┐
│ 桥接层  bridge/wechat_bridge.py                               │
│   连接微信、收消息、发消息、发文件、读本地库                   │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│ 自动化后端（按需选择）                                         │
│   wechatauto（微信 4.x） / wxauto4（微信 4.x） / wxauto（3.9）  │
└──────────────────────────────────────────────────────────────┘
```

设计原则：

1. **下层失败必须能表达成结构化错误**，不允许「假装成功」。
2. **发送必须显式指定目标**，同名或多候选时拒绝而不是猜。
3. **上层不依赖具体自动化后端**：后端差异全部收敛在桥接层。
4. **桥接层可热替换**：它按文件路径加载，不参与主程序打包，替换单个文件即可生效。

### 常驻助手（bot）在架构中的位置

`bot/` 是桌面模式的业务层，与 MCP 模式并列，复用同一套适配层：

```
bot/engine.py     调度：去重、触发判断、节流、静默、链接处理、回复发送
bot/config.py     配置持久化（%APPDATA%\lingyu\bot.json）
bot/persona.py    人设与提示词
bot/llm.py        大模型客户端（OpenAI 兼容接口）
bot/links.py      链接解析（yt-dlp + 网页兜底）
bot/douyin.py     抖音解析（浏览器桥）
bot/browser.py    无头 Chromium 驱动（CDP）
bot/tweet.py      X 推文抓取与卡片渲染
bot/storage.py    下载目录配额管理
```

---

## 2. 模块职责

| 模块 | 职责 | 关键点 |
|---|---|---|
| `server.py` | MCP 工具注册与 stdio 传输 | 工具名、参数 schema、错误序列化 |
| `service.py` | 面向工具的服务门面 | 连接懒加载、审计、关注列表 |
| `adapters/wechat.py` | 微信能力适配 | 目标校验、同名消歧、消息缓冲、发送串行化 |
| `bridge/__init__.py` | 定位并加载桥接实现 | 源码/冻结两种模式的路径统一 |
| `bridge/wechat_bridge.py` | UI 自动化与本地库读取 | 输入框定位、附件确认、窗口可见性 |
| `desktop.py` | 桌面窗口与 JS 桥 | 后台线程跑 asyncio loop，JS 调用同步返回 |
| `config.py` | 适配层配置 | 环境变量解析 |
| `paths.py` | 用户数据目录 | 唯一的 `%APPDATA%` 入口 |
| `security.py` | 目标/内容/路径校验 | 长度、控制字符、路径穿越 |
| `monitoring.py` | 关注列表 | `allow` / `block`，`block` 优先 |
| `audit.py` | 操作审计 | JSONL，默认不记录正文 |

---

## 3. 桥接层

### 3.1 加载方式

`bridge/wechat_bridge.py` **不参与常规 import**，而是运行期按文件路径加载：

```python
from lingyu.bridge import load_bridge_class
Bridge = load_bridge_class()
```

这样做的原因：

- 桥接层依赖较重的自动化库（UIA、OCR、pywin32），导入失败不应拖垮整个进程，
  而应收敛成一条 `backend_unavailable`。
- PyInstaller 打包时该文件作为**数据文件**落在解包根目录，发布后可以只替换这一个
  文件来修桥接逻辑，不必重新打包约 200 MB 的运行时。

源码模式路径：`src/lingyu/bridge/wechat_bridge.py`
冻结模式路径：`<_MEIPASS>/wechat_bridge.py`

两者都由 `lingyu.bridge.bridge_file()` 给出，调用方不需要区分。

### 3.2 后端选择

`WECHAT_BACKEND` 可取 `auto`（默认）、`wechatauto`、`wxauto4`、`wxauto`。

`auto` 的判定：检测到 `Weixin.exe`（微信 4.x）用 `wechatauto`，缺依赖时退 `wxauto4`；
检测到 `wechat.exe`（微信 3.9）用 `wxauto`；两者都没有则报「未检测到微信进程」。

不同后端的读取能力不同：

- `wechatauto` 可以只读解密本地数据库，因此支持联系人库查询（`search_contact`
  返回 `source=wechat_db`）。
- 其他后端没有本地库，退回为「监听期间观察到的会话名称」（`source=buffer`）。

### 3.3 输入框定位（发送链路的关键）

发送文本/文件前必须知道输入框在哪。这里踩过一个会让**所有安全网同时失效**的坑：

底层默认在探测失败时按窗口高度比例**猜**一个矩形返回。猜出来的坐标点下去是空的，
后续 `Ctrl+A` / `Del` / `Ctrl+V` / 回车全部落空——但每一步都「没有报错」。更糟的是
库里每一处保护都写成 `if not box: 放弃`，而这个返回值永远不会是 `None`。

现在的做法（`_strict_input_box`）：

1. UIA 优先：读 `mmui::ChatInputField` 的 `BoundingRectangle`，换算成窗口内相对坐标；
   失败后进入 30 秒冷却，避免在热路径反复付超时。
2. 像素兜底：从窗口右侧往回扫描一行，判断输入区底色白度；起点取
   `max(右侧面板左边界, 窗口宽 × 0.5)`，**不依赖侧栏宽度校准**（该校准值可能是默认值）。
3. 仍然失败 → 强制重新校准布局一次（忽略「每会话只校准一次」的旧限制）。
4. 最后仍失败 → **返回 `None`**。宁可明确报失败，也不往猜出来的坐标盲粘。

### 3.4 附件发送

流程：确认主窗可见 → 打开会话 → 定位输入框 → 粘贴文件 → 回车 → **回查聊天库确认**。

- **回查确认是必须的**：底层粘贴附件后对非图片文件无条件返回成功，微信静默拒收时
  整条链路会记成「已发送」。确认方式是在发送前取当前消息水位，发送后轮询数据库新增行，
  命中「自己发的、类型属于图片/视频/文件」即算成功，10 秒超时判失败。
- **回车可能被吞**：微信粘贴时只把原始文件拷进自己的目录，点发送才转码上传，
  这期间按下的回车会丢，附件就一直躺在输入框里。因此失败后会**只补按回车、
  不重新粘贴**（草稿还在就发出去，输入框已空则回车无副作用），不会造成重复发送。
- **重试只针对「明确拒绝」**：底层明确返回失败才整条重发；只是「数据库没确认」时
  不重试，避免同一文件发两遍。
- **文件大小上限随会话类型变化**（自聊/文件传输助手最宽松，可达数 GB）。因此超过
  100 MB 只**告警不拦截**——拦掉一个本来发得出去的文件，比白试一轮更糟。

### 3.5 群聊相关

- **@ 识别只能靠文本**：自动化后端不提供「这条消息 @ 了我」的标记，也没有 `is_at`
  字段。微信 4.x 的真 @ 在消息文本里就是 `@<昵称>` 加一个特殊空格，不含 wxid。
  所以识别方式是用**消息文本正则匹配机器人账号的微信昵称**。昵称在连接时读取，
  60 秒刷新一次，改昵称不必重启。
- **群名为空会导致发不出去**：没设群名的群，本地库里 `nick_name` / `remark` 都是空的，
  而微信搜索框只认名字、不认 `xxx@chatroom`。兜底做法是用**群成员昵称**拼出界面上
  显示的名字（至少两个成员有昵称才启用；只有一个名字时容易搜到同名联系人，不猜）。
  可靠做法仍然是在微信里给群设置名称。
- **引用/回复**：引用消息的原始内容里带 `<refermsg>`，桥接层从中解析被引用者的显示名，
  用于判断「是否在回复我」。

---

## 4. 工具接口

所有工具返回结构化 JSON。成功时 `ok=true`；失败时
`{"ok": false, "code": "...", "message": "...", "detail": {...}}`。

### 4.1 `get_wechat_status`

**输入**：无

| 输出字段 | 说明 |
|---|---|
| `ok` | 是否已连接 |
| `state` | `disconnected` / `connecting` / `connected` / `error` |
| `backend` | 生效的后端（`wechatauto` / `wxauto4` / `wxauto`） |
| `listening` | 消息监听是否真正在跑 |
| `window_visible` | 微信主窗口是否可见 |
| `uia_available` | UI Automation 是否可用 |
| `detail` | 附加诊断信息 |
| `error` | 连接失败时的结构化错误 |

### 4.2 `get_chat_list`

> 读取基于监听期间**被动接收**到的消息，不是微信完整会话列表。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `limit` | int | 否 | 最大返回数量 |
| `keyword` | str | 否 | 会话名过滤 |

输出：`ok`、`chats[]`（`name`/`is_group`/`message_count`/`last_message_at`）、
`total`、`complete`、`note`。

### 4.3 `get_chat_history`

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `chat_name` | str | 是 | 必须明确指定会话名 |
| `limit` | int | 否 | 返回条数，默认 20 |

输出：`ok`、`chat`、`messages[]`（`chat`/`sender`/`content`/`message_type`/`is_group`/
`timestamp`/`message_id`）、`complete`、`note`。

### 4.4 `send_message`

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `recipient` | str | 是 | 联系人或群聊名称 |
| `message` | str | 是 | 文本内容 |
| `dry_run` | bool | 否 | 仅校验、不实际发送 |
| `confirm` | bool | 否 | 显式确认（`WECHAT_REQUIRE_CONFIRM=1` 时必需） |

输出：`ok`、`status`（`sent` / `failed` / `dry_run` / `confirmation_required`）、
`recipient`、`resolved_recipient`、`message_preview`、`error`。

安全约定：

- 目标为空、含控制字符或超长 → `target_invalid`。
- 目标不唯一 → `target_ambiguous`，返回候选列表，拒绝发送。
- 未连接微信 → `not_connected`。
- 开启确认但未带 `confirm=true` → `status=confirmation_required`。
- 超时或结果无法确认 → `status=failed`，**不自动重试**。

### 4.5 `search_contact`

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `keyword` | str | 是 | 昵称 / 备注 / 微信号片段 |
| `limit` | int | 否 | 最大返回数量，默认 20 |

输出：`ok`、`keyword`、`candidates[]`（`name`/`username`/`remark`/`is_group`）、
`total`、`source`、`note`。

`source=wechat_db` 表示来自微信本地联系人库（后端 `wechatauto`）；
`source=buffer` 表示退回为「观察到的会话名称」。**同名时原样返回多个候选，不猜测。**

### 4.6 `get_chat_info`

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `chat_name` | str | 是 | 会话名称 |

输出：`ok`、`name`、`is_group`、`message_count`、`last_message_at`、`monitored`、`note`。

### 4.7 `send_file`

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `recipient` | str | 是 | 联系人或群聊名称 |
| `file_path` | str | 是 | 本地文件绝对路径 |
| `dry_run` | bool | 否 | 仅校验文件与目标 |
| `confirm` | bool | 否 | 显式确认 |

输出：`ok`、`status`、`recipient`、`resolved_recipient`、`file_path`、`file_name`、
`file_size`、`dry_run`、`error`。

安全约定：路径不合法 → `file_invalid`；不在允许目录或含 `..` → `path_not_allowed`；
目标不唯一 → `target_ambiguous`；结果无法确认 → `status=failed`。

### 4.8 `get_recent_messages`

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `after_seq` | int | 否 | 上次返回的 `next_seq`，默认 0 |
| `limit` | int | 否 | 单次最大返回条数，默认 20 |
| `chat_name` | str | 否 | 仅返回该会话的消息 |

输出：`ok`、`messages[]`、`next_seq`、`complete`、`note`。

调用方应保存 `next_seq` 并在下次作为 `after_seq` 传入。被关注列表过滤掉的消息会跳过，
但游标仍向前推进。

### 4.9 `set_monitored_chats`

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `add` | list[str] | 否 | 加入当前模式对应列表 |
| `remove` | list[str] | 否 | 从两个列表移除 |
| `mode` | str | 否 | `allow`（空=关注全部）或 `block` |

输出：`ok`、`mode`、`allow`、`block`、`note`。

`block` 优先级高于 `allow`。**仅过滤本服务返回的读取结果，不改变底层监听行为。**

---

## 5. 错误码

| 错误码 | 含义 | 常见原因 |
|---|---|---|
| `backend_unavailable` | 自动化后端或依赖不可用 | 自动化依赖未安装、桥接模块缺失 |
| `not_connected` | 尚未连接微信 | 微信未登录、主窗口不可见 |
| `connect_failed` | 连接失败 | 微信进程/窗口异常、指定了未安装的后端 |
| `target_invalid` | 目标不合法 | 名称为空、含控制字符、超长 |
| `target_ambiguous` | 目标不唯一 | 存在同名或部分匹配的多个会话 |
| `chat_id_unresolved` | 会话 ID 无法解析成可搜索名 | 群没有群名称 |
| `file_invalid` | 文件路径不合法 | 不存在、不是文件、不可读 |
| `path_not_allowed` | 文件不在允许目录 | 未配置 `WECHAT_SEND_DIRS` 或越界 |
| `confirmation_required` | 需要显式确认 | 未传 `confirm=true` |
| `unsupported_operation` | 当前后端不支持 | 该能力依赖特定后端 |
| `wechat_error` | 其他适配层错误 | 兜底 |

---

## 6. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `WECHAT_BACKEND` | `auto` | `auto` / `wechatauto` / `wxauto4` / `wxauto` |
| `WECHAT_SEND_TIMEOUT` | `20` | 单次发送最长等待秒数 |
| `WECHAT_LISTEN_ON_CONNECT` | `1` | 连接后是否启动消息监听 |
| `WECHAT_SEND_DIRS` | 空 | 允许发送文件的目录（逗号分隔）；空 = 拒绝发送文件 |
| `WECHAT_REQUIRE_CONFIRM` | `0` | 发送前是否需 `confirm=true` |
| `WECHAT_AUDIT_ENABLED` | `1` | 是否记录操作审计 |
| `WECHAT_AUDIT_LOG` | `%APPDATA%\lingyu\logs\audit.jsonl` | 审计日志路径 |
| `WECHAT_MONITOR_MODE` | `allow` | 关注列表模式 `allow` / `block` |
| `WECHAT_MONITOR_ALLOW` | 空 | 预设白名单（空 = 全部） |
| `WECHAT_MONITOR_BLOCK` | 空 | 预设黑名单 |
| `WECHAT_BOT_CONFIG` | `%APPDATA%\lingyu\bot.json` | 桌面助手配置文件路径 |

客户端配置示例见仓库根目录 `mcp.config.example.json`。

---

## 7. 关键实现要点

### 7.1 日志体系

三条互不相干的路，改日志时先分清：

1. **引擎日志**：`BotEngine._log` 写内存环形缓冲（界面轮询 `get_logs()`），
   并通过 `on_log` 回调外送。跨线程读写，必须加锁。
2. **loguru 日志**：桥接层与适配层用 `loguru`。桌面模式挂两个 sink——文件
   （`%APPDATA%\lingyu\logs\app-YYYY-MM-DD.log`，INFO 以上，异步写入，保留 7 天）
   与界面 sink（同步）。
3. **`print`**：被打包入口的 `_TeeStream` 接到界面日志。

两个容易踩的点：

- 引擎日志**必须显式接到 loguru**，否则只进内存，进程一退就没了，排查时看不到任何线索。
- 界面 sink 会把 loguru 输出转投回引擎，引擎再通过 `on_log` 转回 loguru，
  若不拦截会**同一条日志落盘两遍**。拦截方式是在界面 sink 转发期间置一个
  `contextvars` 标志，转发函数见到标志直接返回（界面 sink 是同步调用，标志能可靠跨链路）。

### 7.2 链接解析

- `LinkResolver` 负责调度，解析结果带 TTL 缓存，失败结果也有较短 TTL。
- 短链（如 `b23.tv`）先展开再交给 yt-dlp。
- yt-dlp 失败时退回抓网页 `<title>`，但**错误提示以 yt-dlp 原因为主**，兜底信息附在后面。
- 下载目录有总占用上限，每次下载后按修改时间从旧到新清理。

### 7.3 抖音：浏览器桥

抖音接口要求请求签名（`a_bogus`），且签名算法在混淆过的 JS 里。项目的做法是
**借本机浏览器完成签名**，而不是逆向算法：

1. 用临时用户目录启动无头 Edge/Chrome（不碰用户既有配置与登录态）。
2. 通过 CDP 导航到作品页。
3. 在页面内用 **XHR**（不是 `fetch`——SDK 只 hook `XMLHttpRequest`）轮询详情接口。
4. 拿到 `aweme_detail` 后取最高码率的 `play_addr`，带 Referer 直连下载。

因此**不需要登录、不需要 Cookie**，但系统里必须有 Edge 或 Chrome。

已知的几个坑：

- 首次请求必然 403：`Uifid` 由页面第一个响应的 `Set-Cookie` 种下，必须轮询重试。
- 必须进**作品页**，首页不会初始化 SDK。
- 不要等 `readyState=complete`（视频页要加载视频资源，很慢），等域名变成目标站即可。
- CDP 连接必须加 `--remote-allow-origins=*`，否则 WebSocket 握手被拒。
- 导航会替换执行上下文，此时 evaluate 会报「目标已导航」，要吞掉重试。
- 关闭浏览器要杀进程树，否则 renderer/gpu 变孤儿占着临时目录。
- 同一作品可能被「下载回发」与「喂模型上下文」两条路径同时解析，需要**在途合并**
  （同一作品 ID 只跑一次，失败也共享），否则会双开浏览器、后到者超时、并把失败
  结果缓存一段时间，表现为「链接解析失败了」的误报。
- 浏览器进程提前退出（多见于浏览器版本更新期间，命令行被交接给新版本后旧进程退出）
  时，自动重试一次并换调试端口；stderr 落盘到临时目录便于定位。

### 7.4 X 推文卡片

- 数据源用公开的 syndication 接口（免登录），token 由 react-tweet 的算法算出。
- 卡片用 PIL 本地渲染，字体按字符选（中文用雅黑，emoji 用 Segoe UI Emoji）。
- 配图**不做固定尺寸拉伸**，而是按覆盖比例缩放后居中裁切，避免多图变形。

### 7.5 桌面端（pywebview）

- pywebview 6.x 不支持把 `async def` 暴露给 JS，因此采用「后台线程跑专用 asyncio loop，
  JS 调用的都是同步方法，内部 `run_coroutine_threadsafe(...).result()` 等待」的经典模式。
- 打包版是控制台程序（MCP stdio 要靠 stdout），双击运行会弹黑框，桌面模式启动时
  在确认控制台独占的情况下隐藏它。
- **窗口黑屏**通常是 zip 解压带来的「Internet 区域」标记让 .NET 拒绝加载 pythonnet。
  启动时会自动清除该标记，失败会弹提示框而不是静默黑屏。

### 7.6 布局校准

自动化后端会把测量到的布局参数（侧栏宽度比例、发送按钮位置等）缓存到用户目录。
需要注意：**侧栏比例等于默认值（0.22）时通常意味着没测出来**（校准的 OCR 锚点失败后
静默回落），而不是真的 0.22。因此输入框的像素探测不依赖这个值。

OCR 本身有线程约束：WinRT 的识别在工作线程约 100 ms，在主线程会超时返回空结果。
所以「抓屏 + OCR」只在必要时做，按钮位置尽量用输入框矩形加固定偏移算出来。

---

## 8. 验证记录

### 8.1 单元测试

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

当前 **395 项**，覆盖：目标校验与消歧、消息缓冲与游标、关注列表过滤、文件白名单与
路径穿越、附件发送确认与重试、输入框严格定位、窗口可见性、@ 识别与昵称刷新、
群名兜底、链接解析与在途合并、推文卡片排版、日志落盘与去重、配置读写。

### 8.2 端到端（真实微信）

`scripts/phase3_e2e.py` 以真实 MCP stdio 协议驱动 Server，与 Agent 接入时是同一条
协议路径。测试环境：Windows，微信已登录，主窗口可见，后端 `wechatauto`。

| 场景 | 说明 | 结果 |
|---|---|---|
| 0 | MCP 工具发现 | PASS — 发现全部工具 |
| 1 | 查询微信状态 | PASS — `state=connected, backend=wechatauto` |
| 2 | 获取会话列表 | PASS |
| 3 | 读取指定会话消息 | PASS（需窗口内存在他方入站消息） |
| 4 | 向测试目标发送文本 | PASS — `status=sent` |
| 5a | 空目标被拒绝 | PASS — `target_invalid` |
| 5b | 目标不唯一被拒绝 | 单元测试覆盖（实时环境难以构造多会话） |
| 6 | 后端不可用返回结构化错误 | PASS — 不虚报成功 |
| 7 | 连续调用不串扰 | PASS — 连续 3 次发送顺序与结果一致 |

`scripts/phase4_live_check.py` 覆盖 P1 能力（发送均发往「文件传输助手」，不打扰真实联系人）：

| 场景 | 说明 | 结果 |
|---|---|---|
| 0 | 工具发现 | PASS — 9 个工具 |
| 1 | 查询状态 | PASS |
| 2 | 联系人搜索 | PASS — `source=wechat_db` |
| 3 | 会话信息 | PASS |
| 4 | 增量消息读取 | PASS — 游标正确推进 |
| 5 | 关注列表过滤 | PASS |
| 6 | 发送文件 dry-run | PASS — 未实际发送 |
| 7 | 文件越界被拒绝 | PASS — `path_not_allowed` |
| 8 | 真实发送文件（发给自己） | PASS — `status=sent` |
| 9 | 未配置白名单被拒绝 | PASS — `path_not_allowed` |
| 10 | 操作审计落盘 | PASS — 含工具名与结果，无聊天正文 |

> 这两份脚本需要真实微信环境，不属于常规单测；改动发送链路后建议重跑一遍。

---

## 9. 故障排查

按「先看结构化错误码，再定位根因」的顺序来。

### 9.1 连接失败 / `not_connected`

1. 确认微信 PC 客户端已启动并登录。
2. 确认微信主窗口**可见**：最小化到托盘、锁屏、被完全遮挡都可能导致失败。
3. 调 `get_wechat_status` 看 `window_visible`、`uia_available`、`backend`。
4. `backend` 为空或 `uia_available=false` → 检查自动化依赖是否安装：
   `.venv\Scripts\python.exe -m pip install -e ".[wechat]"`。

### 9.2 `get_chat_list` / `get_chat_history` 结果为空

**预期行为，不是 Bug**：

- 读取能力基于监听回调，只能拿到适配层启动后收到的新消息。
- 只包含**来自其他账号的入站消息**；自己发的消息与系统号消息会被过滤。
- 刚启动服务时先等他方发一条消息，或用 `get_recent_messages` 观察增量。

### 9.3 群聊 @ 了却不回复

**先看 @ 的名字对不对。** @ 识别是拿消息文本匹配 `@<机器人昵称>`，名字对不上就永远
不触发。运行日志里有一行 `已识别机器人微信昵称: …`，那才是要 @ 的名字；群消息被忽略时
也会打一行说明。

- **手打 `@某某` 只是普通文字**，不会被当成提醒。正确做法是输入 `@` 后从成员列表里选中。
- 昵称在连接时读取，60 秒刷新一次，改昵称不必重启。

名字对上了还是不回 → 多半是**这个群没有群名称**（见 9.4）。

### 9.4 群里没有回复，但日志显示模型调用成功

群没设名称，本地库里显示名为空，微信搜索框又只认名字，发送前定位不到会话就拒绝发送。

- 日志里会提前警告「群 xxx 没有群名称，微信搜索框无法定位它」。
- 发送失败时错误码是 `chat_id_unresolved`，带明确的处理建议。
- **解决办法**：在微信里给这个群设置群名称。程序也会尝试用成员昵称拼名兜底，但那是
  尽力而为。

### 9.5 发送超时或 `status=failed`

- 向**尚未打开**的会话首次发送需要搜索、切换、输入，耗时较长，超时默认 20 秒，
  可用 `WECHAT_SEND_TIMEOUT` 调整。
- 结果无法确认时返回 `failed` 且**不自动重试**。请先人工确认是否已发出，再决定是否重发。
- 发送操作有串行锁，连续发送会排队，属正常现象。

### 9.6 `target_ambiguous`

名称匹配到多个会话。用更完整的名称重试，或用 `search_contact` 拿到准确名称。

### 9.7 `path_not_allowed` / 发送文件被拒绝

默认拒绝发送任何文件是**有意为之**。配置允许目录后重试：

```
WECHAT_SEND_DIRS=D:\shared,D:\tmp\send
```

文件必须在允许目录内，含 `..` 的穿越会被拒绝；文件不存在或不可读返回 `file_invalid`。

### 9.8 链接解析失败

先用诊断命令确认链路本身（不启动微信）：

```powershell
.\lingyu.exe --selfcheck
.\lingyu.exe --resolve <url> [--download] [--outdir <目录>]
```

- `yt_dlp_version` 显示不可用 → 内置 yt-dlp 导入失败，下方会给出具体原因。
- 普通网页解析不出标题 → 确认网络可达；会自动退回抓 `<title>`。
- **抖音失败提示找不到浏览器** → 需要系统里有 Edge 或 Chrome（Win10/11 自带 Edge）。
  解析期间起的是独立临时配置的无头浏览器，不影响你正在用的浏览器。
- **微博/小红书失败**（错误里带 `cookies` 或 `403`）→ 这些站点的网页是空壳、
  接口要 Cookie，必须在「链接解析」页填一份 **Netscape 格式**的 `cookies.txt`。
  导出方式：登录后用 *Get cookies.txt LOCALLY* 之类的扩展导出。路径填错、指向目录、
  文件为空都会**当成没填**处理。
- **下载失败** → DASH 分离流要靠 ffmpeg 合流，`--selfcheck` 里的 `ffmpeg` 必须可用
  （打包版已内置）。
- **下载目录越来越大** → 由「目录占用上限」（默认 1024 MB）控制，超出后按修改时间
  从旧到新删。填 0 表示不限制。

### 9.9 桌面端启动后黑屏

从 zip 解压出来的文件带「Internet 区域」标记（`ZoneId=3`），.NET 默认拒绝加载带该
标记的程序集，pythonnet 初始化失败，窗口画不出内容。本地构建的产物没有这个标记，
所以常见「本机能跑、发给别人就黑屏」。

1. 新版启动时会自动清除标记，正常无需干预。
2. 仍黑屏就对安装目录解除锁定，或重新解压前先对压缩包
   **右键 → 属性 → 勾选「解除锁定」**：

   ```powershell
   Get-ChildItem -Path <安装目录> -Recurse -File | Unblock-File
   ```

3. `lingyu.exe --selfcheck` 不依赖 GUI，能跑通说明解释器和依赖没问题，问题在窗口层。

### 9.10 发不出去：先数进程

**没有单实例保护**。双击两次就有两个实例，它们抢同一个微信窗口，表现为日志里反复出现
「输入框定位失败」「发送超时」「回复发送失败」，而且自动化会把另一个窗口自动最小化
藏起来，让你以为只开了一个。

排查任何「发不出去」之前，先确认只有一个 `lingyu.exe` 在跑。

### 9.11 日志在哪

- 运行日志：`%APPDATA%\lingyu\logs\app-YYYY-MM-DD.log`（按天，保留 7 天）
- 审计日志：`%APPDATA%\lingyu\logs\audit.jsonl`（JSONL，一行一条，不含聊天正文）
- MCP 模式下日志输出到 **stderr**，不污染 stdio 协议消息；客户端里看不到日志属正常。

---

## 10. 构建与产物

```powershell
.venv\Scripts\python.exe packaging\build.py
```

产物：

| 路径 | 说明 |
|---|---|
| `dist\lingyu\` | PyInstaller onedir 目录 |
| `dist\lingyu-win32-x64.zip` | 免安装压缩包 |
| `dist\lingyu-setup-x64.exe` | Windows 安装包（需本机装 Inno Setup 6，未装则跳过） |

要点：

- **先提交再构建**。构建完才提交会得到不含修复的产物。
- 构建前确认没有实例在跑（`taskkill /F /IM lingyu.exe`）。
- 打包用 `--clean`，整体约 3 分钟。
- `imageio_ffmpeg`（自带 ffmpeg，约 84 MB）为 DASH 合流必需，`winsdk`（约 43 MB）为
  UIA 发送路径必需，**都不能排除**。
- 内置 yt-dlp 走 PyInstaller **静态分析**（`pathex` + `hiddenimports` + 官方 hook），
  只当数据目录分发会漏标准库子模块。
- 安装器按用户安装（`PrivilegesRequired=lowest` + `{localappdata}`），不弹 UAC；
  安装程序自己写文件，天然不带「Internet 区域」标记。
- `ISCC.exe` 的定位顺序：环境变量 `ISCC` → PATH → 卸载信息里登记的安装目录 → 默认目录。
  Inno Setup 允许装到任意路径，所以不写死某个盘符。

### 校验产物里装了什么

- `lingyu.exe` 本身没有版本资源，查版本去 `lingyu-setup-x64.exe` 里搜 UTF-16 字符串。
- `wechat_bridge.py` 是数据文件，落在 `dist\lingyu\_internal\wechat_bridge.py`，
  与 `src\lingyu\bridge\wechat_bridge.py` 直接比对即可确认修复进包；它也可以单独替换
  （日志落盘等改动在 PYZ 里，换不了，必须重装）。

---

## 11. 已知限制与风险

1. 仅支持 Windows 10/11 x64，需微信 PC 客户端已登录且主窗口可见。
2. 微信窗口最小化、被遮挡或锁屏时，读取与发送可能失败。
3. `get_chat_list` / `get_chat_history` 基于被动监听缓冲，缓冲区默认上限 2000 条，
   不是微信完整历史。
4. 自己发出的消息、公众号/系统账号消息会被底层过滤。
5. `send_message` 结果无法确认时返回 `failed`，且不自动重试。
6. 微信客户端或自动化依赖升级后需重新回归验证（UI 结构可能变化）。
7. 自动化操作可能触发微信风控，请使用可控测试账号并控制频率。
