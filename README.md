# 灵语（LingYu）

把 Windows 微信桌面客户端的自动化能力封装为 **MCP Server**，供通用 AI Agent
（Codex、Claude Code 等支持 MCP 的客户端）通过标准工具调用。

- Agent 负责理解任务、规划步骤、选择工具。
- 本服务只提供稳定、可控、可测试的微信操作工具。
- 不内置大模型，不带固定人设与自动回复循环。

> 前置条件：微信 PC 客户端已登录，且主窗口可见（最小化 / 锁屏 / 被遮挡会导致操作失败）。

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
取最近 N 条上下文 + 人设发给大模型并自动回复；人设、模型、群范围均可在界面修改。
拟人化（回复概率、长回复分条发送、口语化措辞）默认开启，可在「拟人化」页调整或关闭。
「静默与水群」页可设置静默时段、群级回复节流，以及可选的「偶发主动参与」（默认关闭）。

```powershell
.\lingyu.exe --gui
```

**链接解析**：「链接解析」页开启后，消息里的链接会被自动解析并附到上下文，
让机器人能就链接内容回应；还可选把音视频下下来发回当前聊天（默认关闭）。

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
- 群里**不必 @ 机器人**，只要消息里有链接就会先回一句固定提示（默认「正在解析链接」，
  不经过大模型，文案可改），再把解析结果发回。
- 下载目录有**总占用上限**（默认 1 GB），超出后每次下载完自动删掉**最旧**的文件。

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
```

`[wechat]` 会安装微信自动化所需的运行时依赖（`wechatauto-replica`、`wxauto4`、
`uiautomation`、`loguru`）。源码模式与冻结包走同一条桥接路径，
不需要额外配置外部目录。

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

桌面端配置（模型、人设、链接解析、静默时段等）存在 `%APPDATA%\lingyu\bot.json`，
在界面里改即可；改文件后需重启程序。

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
.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

真实环境端到端与联调脚本位于 [scripts/](scripts/)，详见
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
- [CHANGELOG.md](CHANGELOG.md) — 版本与变更记录

## 风险提示

本项目通过 UI 自动化操作微信桌面客户端，属于第三方自动化手段。使用时请遵守
微信及所在平台的服务条款；自动化操作可能导致账号被限制。建议使用独立的测试账号
或在可控测试会话中验证，避免对真实联系人开展未经确认的自动发送。
