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
| `src/lingyu/bot/` | 常驻机器人：引擎、模型、人设、链接解析、存储 |
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
| X（Twitter） | 本地渲染推文卡片图（头像/昵称/正文/配图/时间/点赞） | 网络；含视频时再下载视频 |
| 微博 / 小红书 | yt-dlp 直接解析 | 需要 Netscape 格式的 `cookies.txt` |

抖音的浏览器桥会临时起一个无头浏览器，用**独立的临时用户目录**，
不碰既有的浏览器配置和登录态。

### 诊断命令

```powershell
.\lingyu.exe --selfcheck                                    # 版本 / yt-dlp / ffmpeg / webui
.\lingyu.exe --resolve <url> [--download] [--outdir <目录>]  # 直接跑一遍解析
.\lingyu.exe --tweet <推文链接> [--outdir <目录>]             # 抓推文并渲染卡片图
```

### 常见问题

- **`yt_dlp_version` 显示「不可用」**：内置 yt-dlp 导入失败，其下方会给出
  `yt_dlp_error`（冻结包缺标准库子模块的典型症状）。
- **媒体站点解析不出标题**：确认网络可达；普通网页会自动回退到抓取 `<title>`。
- **抖音提示「未找到 Chrome/Edge」**：装 Edge 或 Chrome（Windows 10/11 自带 Edge）。
- **微博/小红书报 `fresh cookies ... are needed` 或 `HTTP 403`**：这类站点网页是空壳、
  接口要 Cookie，必须提供 `cookies.txt`。
  - 在 Chrome/Edge 里登录对应网站，用扩展（如 *Get cookies.txt LOCALLY*）导出
    **Netscape 格式**的 `cookies.txt`（文件以 `# Netscape HTTP Cookie File` 开头）。
  - 不必登录也能导出，但多数站点会持续校验，**登录后导出更稳**；文件会过期，失效重导。
  - 路径填错、指向目录或文件为空时，程序**当成没填**处理，继续走匿名请求。
- **下载音视频失败**：DASH 分离流必须靠 ffmpeg 合流，`--selfcheck` 里的 `ffmpeg`
  必须可用（打包版已内置）。
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
   ```

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
   黑屏就属于窗口/标记层面。

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
```

真实环境联调脚本位于 `scripts/`（需要已登录的微信）：

```powershell
# 阶段3：以 stdio 协议驱动 Server，覆盖连接/读取/发送/错误路径
.venv\Scripts\python.exe scripts\phase3_e2e.py --wait-inbound 60

# 阶段4：P1 能力真实微信联调
.venv\Scripts\python.exe scripts\phase4_live_check.py
```

覆盖范围：

| 脚本 | 内容 |
|---|---|
| `phase1_live_check.py` | 适配层连接、状态、会话列表 |
| `phase2_smoke.py` | stdio 握手、工具发现、状态工具调用 |
| `phase3_e2e.py` | 端到端场景：读取、发送、同名消歧、后端不可用的结构化错误 |
| `phase4_live_check.py` | 搜索、会话信息、文件发送、增量读取、关注列表 |

单元测试不依赖真实微信：适配层通过注入 `FakeBridge` 验证参数校验与错误语义。

---

## 10. 构建与发布

```powershell
# 1. 生成独立运行时（onedir + zip + 安装包），产物在 dist\
#    安装包需要本机装有 Inno Setup 6（未装则自动跳过，只出 onedir + zip）
.venv\Scripts\python.exe packaging\build.py

# 2. 把运行时内置进插件包
node dsh-plugin\scripts\stage-runtime.mjs

# 3. 发布 GitHub Release（需已登录 gh）
gh release create v0.8.13 dist\lingyu-win32-x64.zip dist\lingyu-setup-x64.exe --title "v0.8.13" --notes "…"

# 4. 发布 npm 插件（prepack 会自动执行第 2 步）
cd dsh-plugin
npm publish --access public
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
