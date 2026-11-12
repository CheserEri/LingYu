# 灵语（LingYu）

把 **Windows 微信桌面客户端**或 **QQ 机器人（官方 API v2）**的聊天能力封装为
**MCP Server**，供通用 AI Agent（Codex、Claude Code 等支持 MCP 的客户端）通过标准工具调用。

- Agent 负责理解任务、规划步骤、选择工具。
- 本服务只提供稳定、可控、可测试的消息操作工具。
- 不内置大模型，不带固定人设与自动回复循环。

> 前置条件：微信模式需要 PC 客户端已登录，且主窗口可见（最小化 / 锁屏 / 被遮挡会导致操作失败）；
> QQ 模式需要开放平台的 AppID / AppSecret，并让机器人加入目标群或与用户建立会话。
>
> 桌面窗口（`--gui`）依赖 **Microsoft Edge WebView2 运行时**——Windows 11 自带，
> Windows 10 多数机器随 Edge 已装。缺它时程序会明确提示并打开微软官方下载页
> （装一次即可），不会静默白屏。

## 功能

**P0（MVP）**

| 工具 | 说明 |
|---|---|
| `get_wechat_status` | 检查连接状态、窗口可见性与自动化后端 |
| `get_chat_list` | 返回监听期间可读取的会话列表 |
| `get_chat_history` | 读取指定会话缓冲区内最近的消息 |
| `send_message` | 向明确指定的联系人或群聊发送文本 |

**P1（扩展）**

| 工具 | 说明 |
|---|---|
| `search_contact` | 按名称搜索联系人/群聊，同名时返回候选不猜测 |
| `get_chat_info` | 返回会话名称、类型、消息数与是否在关注列表 |
| `send_file` | 发送本地文件（受目录白名单、确认策略约束） |
| `get_recent_messages` | 基于游标增量读取新消息 |
| `set_monitored_chats` | 白名单/黑名单过滤读取结果（不改动底层监听） |

参数、返回结构与错误码见 [docs/TECHNICAL.md](docs/TECHNICAL.md#2-mcp-工具接口)。

## QQ 机器人（官方 API v2）

同一套工具可接入 QQ：适配器通过官方 WebSocket 网关接收事件，通过
`https://api.bot.qq.com` 发送消息。**微信与 QQ 可以同时开启**：两端各自订阅
消息、各自回复，回复始终回到消息来源的那一端；也可以只开任意一端。配置方式二选一：

```powershell
# 环境变量（MCP Server 方式）
$env:LINGYU_PLATFORM = "qq"
$env:QQ_BOT_APP_ID = "你的 AppID"
$env:QQ_BOT_APP_SECRET = "你的 AppSecret"
lingyu
```

或在桌面窗口「运行状态 → 接入终端」里勾选 **微信** / **QQ 机器人**（可同时勾选）
并填写 QQ 凭据：可先点「测试连接」验证 `AppID` / `AppSecret`，再点「保存并应用」
——会立即断开旧连接、按勾选重建适配器并连接，不用重启灵语。AppSecret 只保存在
本机配置文件，界面只回显「已保存」，留空表示沿用原密钥。

- **账号注册**：在 [q.qq.com](https://q.qq.com/) 创建机器人，取得 `AppID` / `AppSecret`；
  `Token` 鉴权已废弃，本服务使用 `Access Token`（自动获取与刷新）。
- **支持的消息**：QQ 单聊（`C2C_MESSAGE_CREATE`）、群聊 @ 机器人
  （`GROUP_AT_MESSAGE_CREATE`）、频道 @（`AT_MESSAGE_CREATE`）、频道私信
  （`DIRECT_MESSAGE_CREATE`）。
- **只能被动回复**：发送时会自动带上收到消息的 `msg_id`，群聊 5 分钟 / 单聊 60 分钟
  内有效，同一条消息最多回复 5 次（单聊 4 次）；超时会返回 `qq_reply_expired`，不伪装成功。
- **会话标识是 openid**：QQ 事件不提供群名/昵称，界面里的「群 &lt;openid&gt;」即会话标识，
  也可显式写成 `group:<openid>` / `user:<openid>` / `channel:<id>` / `dm:<guild_id>`。
- **富媒体发送**：单聊 / 群聊可发图片（png/jpg）、视频（mp4）、语音
  （silk/wav/mp3/flac）与普通文件——先上传到 `/v2/users|groups/{id}/files`，
  再用 `msg_type=7` + `media.file_info` 被动回复。频道与频道私信没有富媒体
  上传接口，会返回 `qq_media_scene_unsupported`。上传与消息发送共用被动回复
  窗口与次数限制，文件需位于允许发送的目录内。

## 安装

### 方式零：Windows 安装包（推荐给普通用户）

下载 `lingyu-setup-x64.exe` 双击安装即可。向导支持中文/英文，会创建开始菜单
（以及可选桌面）快捷方式，并在控制面板「应用」里登记卸载项。

- **按用户安装**到 `%LOCALAPPDATA%\Programs\lingyu`，**不弹 UAC**；
  安装目录可写（程序会在 exe 同级写 `logs\`）。
- 安装程序自己写文件，**不会**带「Internet 区域」标记，不会遇到解压 zip
  才有的「打开黑屏」问题。

### 方式一：预编译独立包（无需 Python）

下载 `lingyu-win32-x64.zip`，解压后把 `lingyu.exe` 配到 MCP 客户端即可：

> 解压前请先对压缩包**右键 → 属性 → 勾选「解除锁定」**，否则解压出来的程序集
> 会被标记为「来自 Internet」，.NET 拒绝加载，界面会黑屏（新版启动时会自动修复，
> 但提前解除更省事）。

```json
{
  "mcpServers": {
    "lingyu": {
      "command": "D:\\path\\to\\lingyu\\lingyu.exe",
      "args": []
    }
  }
}
```

该包已内置微信桥接模块，**不依赖任何外部目录**。

**桌面版（实时自动回复助手）**：同一个 exe 加 `--gui` 启动原生窗口，
无需 Agent 轮询：消息到达即处理，群里有人 @我 / 引用回复我（私聊每条）时，
由**情绪引擎**决定这一轮说不说、说什么；身份、模型、群范围均可在界面修改。

**情绪引擎**（`bot/emotion/`）：回复由十个 Agent（感知 / 需求 / 记忆 / 兴趣 /
评估 / 唤醒 / 情绪 / 他人 / 调节 / 表达）分阶段推进，情绪跨轮累积在
`%APPDATA%\lingyu\emotion\<会话>.json` 里，**每个会话一份**——张三惹它生气不会
改变它对李四的语气。回不回**不再掷骰子**：沉默是表达阶段的一等结论，所以它
有时候真的不说话，而「为什么不说」会写进运行日志。

- 「情绪」页可选**身份**（原则 / 边界 / 说话方式，内置宋暖，可放
  `%APPDATA%\lingyu\identity\<id>.json` 覆盖）与**大模型参与程度**
  （默认只在表达这一步调模型；切成「全量参与」则感知与评估也调模型）。
- **没有「多久回」「怎么表达」的设置项**。冷却、静默时段、群级节流、会话延续
  时间窗、分条条数与间隔、人设输入框，这些参数已经**全部删除**：
  - 什么时候发、分几条、隔多久 —— 由唤醒度、情绪强度与**兴趣**推导
    （`bot/emotion/pacing.py`）。高兴时手快几乎立刻回；被激到先停几秒再回；
    激动且情绪强才一句一句蹦出来；低落时一整段说完。
  - 要不要接话 —— 由**兴趣**决定。话题正对她胃口就更愿意开口、说得更放开；
    提不起劲就收着说；低到极端且关系还不近时，它可以只是听着不回。
  - 几点不说话 —— 内核按本地时间判断深夜（23:00~07:00），不主动开口，
    被叫到也收着说。
  - 说话像谁 —— 只来自身份文件，以及内核 reflection 对 `self.*` 的自我追加。
- **兴趣是怎么长出来的**（`bot/emotion/core/interests.py`）：身份文件
  `interests.roots` 给出**根源**（软件 0.78 / 艺术 0.72 / 历史 0.66 /
  工程机械 0.55 / 情绪关系 0.86…，每格带一句「我为什么一开始就在意这个」），
  任何 Agent 都改不了。**演化值**每轮朝「根源 ± 可塑性/2」走一小步：聊得开心
  朝上、聊得难受朝下、心情平平时朝根源回归。上限是「根源 ± 0.175」，
  所以再喜欢也变不成另一个人。没有输入框可以填——这是她的经历，不是你的设置。
- 唯一保留的人工开关是「允许主动开口」：往真实聊天主动发消息必须由人明确同意。
  说不说、隔多久说，仍然在内核里。

```powershell
.\lingyu.exe --gui
```

**链接解析**：「链接解析」页开启后，消息里的链接会被自动解析并附到上下文，
让机器人能就链接内容回应；还可选把音视频下下来发回当前聊天（默认关闭）。

- **只处理媒体站点**：默认只认抖音 / 快手 / B 站 / YouTube / X / 小红书 / 微博 /
  TikTok / 西瓜 / 好看 / Instagram / Vimeo / 网易云 / QQ 音乐这些平台，
  购物、团购、新闻、公众号文章这类链接**不解析、不下载、也不会回「无法解析」**。
  白名单之外的站点可在同一页「额外支持的站点」里补（子域同样命中，例如填
  `qq.com` 连 `v.qq.com` 一起支持）；关掉「只解析支持的媒体站点」可恢复
  「任何链接都试一遍」的旧行为。
- **抖音**走「浏览器桥」：解析时用本机 Chrome/Edge 打开一次作品页，签名由页面完成，
  **无需登录、无需 Cookie**（要求系统装有 Edge 或 Chrome，Windows 10/11 自带 Edge）。
- **B 站 / YouTube 等**由内置 yt-dlp 取标题、作者、时长、简介。
- **X（Twitter）** 会把推文**本地渲染成卡片图**发回（头像/昵称/认证/正文/时间/点赞），
  **多张配图按原比例竖排合成一张长图**（一条消息发完，不裁切、不拆条），单图按原比例铺满；
  含视频时再下载视频一并回发。
  外文推文会**自动翻译成中文**（可关），卡片正文用译文、原文缩成灰色小字附在下面；
  中日韩英俄阿泰等文字**按字符选字体**，不会出现豆腐块。
- **小红书**直接读笔记页里服务端渲染的 `__INITIAL_STATE__`（标题/正文/作者/图片/
  点赞），不需要签名、也不需要登录——关键是请求要用**移动端 UA**（桌面 UA 会被
  要求登录）。短链 `xhslink.com` / `xhslink.cn` 会自动展开，分享文案里**不带
  `http://` 的裸短链**也能识别。解析结果会和推文一样**渲染成卡片图**（同一套
  版式：头像、昵称、标题+正文、配图竖排长图、赞/评论），超过 4 张图时页脚标
  「共 N 张图」；视频笔记在卡片之后再下载视频回发，图文笔记只发卡片。
  注意：链接必须带 `xsec_token`（App 里「分享 → 复制链接」拿到的就有），手工拼的
  `explore/<id>` 会被跳到「当前笔记暂时无法浏览」；极少数笔记仍要求登录态，这时才
  需要 `cookies.txt`。
- **微博**等站点要求带 Cookie，需在同一页填一份 Netscape 格式的 `cookies.txt`。
- 群里**不必 @ 机器人**，只要消息里有媒体链接就会先回一句固定提示（默认「正在解析链接」，
  不经过大模型，文案可改），再把解析结果发回。
- **失败提示只在真的发不出去时出现**：下载重试用尽仍拿不到文件，才往聊天里回一句
  「下载失败 + 原因」；普通网页链接不在这条路径上。
- 下载目录有**总占用上限**（默认 1 GB），超出后每次下载完自动删掉**最旧**的文件。

**应急撤回**：机器人偶尔会发出不该发的东西（违规视频、解析错的素材）。群里有人发
**「@机器人 撤回」**，机器人就撤回自己最新发出的媒体消息——撤的是消息本身，对方也
看不到了。

- 引擎给每条外发消息记账，撤回时**优先撤媒体**：媒体后面若还跟着「正在解析链接」
  这类提示，会连它一起撤（默认最多 2 条），否则撤不到媒体。
- 只认「指令词 + 允许的后缀」（撤回 / 撤回一下 / 撤回吧 / `/撤回`），
  「别撤回」「撤回的消息看不到」这类日常对话不会被误判。
- 群里默认**必须 @ 机器人**；默认**任何人都能喊停**（可改成仅指定成员），
  因为违规内容越快撤掉越好。
- 撤回时顺带**取消该会话还没发出去的下载**，避免刚撤完又冒出来一条。
- 时间窗默认 110 秒——微信 2 分钟后只允许「删除」（只对自己生效），故上限钳到 120 秒。
- 结果用固定文案回执，不经过大模型；失败常见原因是消息已超时、微信窗口被最小化/遮挡。

**自检与链接解析诊断**（都不启动微信）：

```powershell
.\lingyu.exe --selfcheck                                    # 版本 / 内置 yt-dlp / ffmpeg / webui
.\lingyu.exe --resolve <url> [--download] [--outdir <目录>]  # 直接解析（可选下载）一个链接
.\lingyu.exe --tweet <推文链接> [--outdir <目录>]             # 抓取推文并渲染出卡片图
```

`--selfcheck` 里 `yt_dlp_version` 与 `ffmpeg` 应显示具体版本/路径；`--resolve` 会打印
标题、作者、时长、来源、简介；`--tweet` 会打印作者、时间、是否含视频、点赞数与卡片 PNG 路径。

### 方式二：DSH 插件（输入名称即安装）

```sh
dsh plugin --profile web add dsh-lingyu
```

插件包见 [dsh-plugin/](dsh-plugin/)，运行时随 npm 包分发，微信工具以
`mcp__lingyu__*` 暴露给模型。

### 方式三：源码运行（开发用）

环境要求：Windows 10/11 x64、Python 3.10+。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[wechat]"
# wechatauto-replica 的 winsdk 依赖在 Python 3.12+ 上没有 wheel，必须单独装：
.venv\Scripts\python.exe -m pip install --no-deps -r requirements\wechat.txt
```

`[wechat]` 会安装微信自动化所需的运行时依赖（`wxauto4`、`uiautomation`、`winrt-*`、
`zstandard`）；`requirements\wechat.txt` 补上 `wechatauto-replica`（它的 `winsdk`
依赖已停维护、只到 cp311，运行期由桥接层把 `winsdk.*` 别名到 `winrt.*`，故用
`--no-deps` 装）。源码模式与冻结包走同一条桥接路径，不需要额外配置外部目录。

桌面窗口（API Key 在界面中填写，无需环境变量）：

```powershell
.venv\Scripts\python.exe -m lingyu.desktop
```

## 配置

复制 [.env.example](.env.example) 为 `.env`，或在 MCP 客户端配置的 `env` 中设置。
常用变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WECHAT_BACKEND` | `auto` | `auto` / `wechatauto` / `wxauto4` / `wxauto` |
| `WECHAT_SEND_TIMEOUT` | `20` | 单次发送最长等待秒数 |
| `WECHAT_SEND_DIRS` | 空 | 允许发送文件的目录；**空 = 拒绝发送文件** |
| `WECHAT_REQUIRE_CONFIRM` | `0` | 发送前是否需 `confirm=true` |
| `WECHAT_AUDIT_ENABLED` | `1` | 是否记录操作审计 |

完整列表见 [docs/TECHNICAL.md](docs/TECHNICAL.md#3-环境变量)。

桌面端配置（模型、身份、触发条件、链接解析等）存在 `%APPDATA%\lingyu\bot.json`，
在界面里改即可；改文件后需重启程序。注意配置里**没有**任何「多久回一次」「怎么表达」
的字段——那些是情绪内核的事。情绪状态单独存在
`%APPDATA%\lingyu\emotion\`（可用 `WECHAT_BOT_EMOTION_DIR` 覆盖），
「情绪」页可以按会话或整体清空——想让它「重新认识你」就清掉那一个会话。

## 在 Agent 中接入

在 MCP 客户端配置中加入本地 stdio 服务，示例见
[mcp.config.example.json](mcp.config.example.json)：

```json
{
  "mcpServers": {
    "lingyu": {
      "command": "F:\\Code\\lingyu\\.venv\\Scripts\\python.exe",
      "args": ["-m", "lingyu.server"],
      "env": { "WECHAT_SEND_DIRS": "F:\\Code\\shared" }
    }
  }
}
```

## 安全设计

- **不隐式指定目标**：发送必须给出明确对象；同名或多个候选时拒绝发送并返回候选列表。
- **不虚报成功**：发送结果无法确认时返回 `failed`，**不自动重试**，避免重复消息。
- **文件白名单**：`send_file` 默认拒绝所有路径，需通过 `WECHAT_SEND_DIRS` 显式放宽；
  含 `..` 路径穿越会被拒绝。
- **可配置确认**：`WECHAT_REQUIRE_CONFIRM=1` 时，未带 `confirm=true` 的发送只返回预览。
- **操作审计**：记录工具名、目标、时间与结果，默认不记录聊天正文。
- **关注列表**：可只关注/忽略指定会话，仅过滤本服务读取结果，不改变底层监听。

> 隐私提示：聊天内容可能包含敏感信息。若 Agent 使用在线模型，被读取的内容可能
> 发送给相应服务商，请自行评估并选择模型服务。

## 已知限制

- 依赖 Windows 微信桌面客户端，需已登录且窗口可见。
- 底层库不提供「按需拉取完整会话列表 / 历史消息」接口；`get_chat_list` 与
  `get_chat_history` 返回的是**运行期间被动接收到的消息**，不是微信完整历史。
- `search_contact` 的本地联系人库查询依赖 `wechatauto` 后端；其他后端退回为
  「观察到的会话名称」。
- 自动化可能触发微信风控或受版本变化影响，不保证账号不受限制。

## 测试

```powershell
# 单元测试（全部基于 mock，不需要打开微信）
.venv\Scripts\python.exe -m unittest discover -s tests -t .

# Lint（与 CI 同一条命令）
.venv\Scripts\python.exe -m ruff check src tests packaging scripts
```

真实环境联调脚本位于 [scripts/live/](scripts/live/)（会真的连微信、可能发消息）；
一次性探针与卡片样张在 [scripts/dev/](scripts/dev/)，不进 CI。详见
[docs/TECHNICAL.md](docs/TECHNICAL.md#9-测试)。

## 构建与发布

```powershell
# 1. 生成独立运行时（onedir + zip + 安装包），产物在 dist\
#    安装包需要本机装有 Inno Setup 6（未装则自动跳过，只出 onedir + zip）
.venv\Scripts\python.exe packaging\build.py

# 2. 把运行时内置进 DSH 插件包
node dsh-plugin\scripts\stage-runtime.mjs

# 3. 发布 GitHub Release（需已登录 gh）
gh release create v0.8.16 dist\lingyu-win32-x64.zip dist\lingyu-setup-x64.exe --title "v0.8.16" --notes "…"

# 4. 发布 npm 插件（prepack 会自动执行第 2 步）
cd dsh-plugin
npm publish --access public
```

> 打包会把**仓库内**的 `packaging/wechat_bridge.py` 内置进 exe，
> 构建不依赖任何外部目录，CI 可自动构建。
>
> 发布 npm 后，在 GitHub 仓库的 About → Topics 中添加 `dsh-plugin`，
> 插件即会被 DSH 社区索引收录。

## 文档

- [docs/TECHNICAL.md](docs/TECHNICAL.md) — 架构、工具接口、错误码、故障排查、链接解析、构建
- [docs/STRUCTURE.md](docs/STRUCTURE.md) — 目录结构与代码约定（`build/` 只放产物）
- [CONTRIBUTING.md](CONTRIBUTING.md) — 开发环境、测试、改代码的约定
- [CHANGELOG.md](CHANGELOG.md) — 版本与变更记录

## 风险提示

本项目通过 UI 自动化操作微信桌面客户端，属于第三方自动化手段。使用时请遵守
微信及所在平台的服务条款；自动化操作可能导致账号被限制。建议使用独立的测试账号
或在可控测试会话中验证，避免对真实联系人开展未经确认的自动发送。
