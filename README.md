# 灵语（Lingyu）

把 Windows 微信桌面客户端的操作能力，封装成 **MCP Server** 与**桌面助手**两种形态，
供 AI Agent 或本机常驻进程使用。

- **MCP 模式**：Agent 负责理解任务、规划步骤、选择工具；本服务只提供稳定、可控、
  可测试的微信操作工具，不内置大模型。
- **桌面模式**（`--gui`）：不需要 Agent 轮询，消息到达即按人设调用大模型自动回复。

> 前置条件：Windows 10/11 x64，微信 PC 客户端已登录，且主窗口可见
> （最小化到托盘、锁屏、被完全遮挡都会导致操作失败）。

---

## 能力一览

### MCP 工具

| 工具 | 说明 |
|---|---|
| `get_wechat_status` | 检查连接状态、窗口可见性与自动化后端 |
| `get_chat_list` | 返回监听期间可读取的会话列表 |
| `get_chat_history` | 读取指定会话缓冲区内最近的消息 |
| `send_message` | 向明确指定的联系人或群聊发送文本 |
| `search_contact` | 按名称搜索联系人/群聊，同名时返回候选而不猜测 |
| `get_chat_info` | 返回会话名称、类型、消息数与是否在关注列表 |
| `send_file` | 发送本地文件（受目录白名单、确认策略约束） |
| `get_recent_messages` | 基于游标增量读取新消息 |
| `set_monitored_chats` | 白名单/黑名单过滤读取结果（不改动底层监听） |

参数、返回结构与错误码见 [docs/TECHNICAL.md](docs/TECHNICAL.md#4-工具接口)。

### 桌面助手

同一个程序加 `--gui` 启动原生窗口，消息到达即处理：

- **触发方式**：私聊每条；群聊 @我、引用/回复我的消息；可选「会话延续」
  （刚被回复过的人继续发言时接着回）。
- **人设与模型**：人设、模型、群范围均可在界面修改，API Key 只落在本机私有配置里。
- **拟人化**：回复概率、长回复分条发送、口语化措辞，默认开启，可随时关闭。
- **静默与水群**：静默时段、群级回复节流，以及可选的「偶发主动参与」（默认关闭）。
- **链接解析**：消息里的链接自动解析（标题/作者/时长/简介）并附到上下文；
  可选把音视频下回来发回当前聊天（默认关闭），下载目录有总占用上限。
- **X（Twitter）推文卡片**：推文本地渲染成卡片图发回聊天（头像/昵称/认证/正文/
  配图/时间/点赞，无需登录），含视频时再下载视频一并回发。
- **图像识别**：开启后图片随上下文一起送给支持视觉的模型。

群聊触发要 **@ 机器人账号的微信昵称**（输入 `@` 后从成员列表里选中它；手打
`@某某` 只是普通文字，不会触发）。改了微信昵称不必重启，下一条 @ 消息会自动跟上。

> 群聊**建议设置群名称**：没有群名称的群，微信搜索框定位不到它，回复会发不出去
> （程序会尝试用群成员昵称兜底定位，并在运行日志里明确提示）。

---

## 安装

### 方式一：Windows 安装包（推荐）

从 [Releases](https://github.com/CheserEri/lingyu/releases) 下载
`lingyu-setup-x64.exe` 双击安装。向导支持中文/英文，会创建开始菜单
（以及可选桌面）快捷方式，并在控制面板「应用」里登记卸载项。

- **按用户安装**到 `%LOCALAPPDATA%\Programs\lingyu`，**不弹 UAC**。
- 安装程序自己写文件，**不会**带「Internet 区域」标记，因此不会遇到解压 zip
  才有的「打开黑屏」问题。

### 方式二：免安装压缩包

从 [Releases](https://github.com/CheserEri/lingyu/releases) 下载
`lingyu-win32-x64.zip`，解压后直接把 `lingyu.exe` 配到 MCP 客户端即可。

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

桌面模式：`.\lingyu.exe --gui`

### 方式三：源码运行（开发用）

环境要求：Windows 10/11 x64、Python 3.10+。

```powershell
cd <项目目录>
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[wechat]"
```

`[wechat]` 会安装微信自动化所需的运行时依赖（`wechatauto-replica`、`wxauto4`、
`uiautomation`、`loguru`）。桥接层实现已随包内置，源码模式同样不需要任何外部项目。

```powershell
.venv\Scripts\python.exe -m lingyu.desktop      # 桌面窗口
.venv\Scripts\python.exe -m lingyu.server       # stdio MCP 服务
```

### 方式四：DeepSeek Harness 插件

```sh
dsh plugin --profile web add dsh-lingyu
```

插件包见 [dsh-plugin/](dsh-plugin/)，运行时随 npm 包分发，微信工具以
`mcp__lingyu__*` 暴露给模型。

---

## 自检与诊断

不确定打包版是否正常时，可跑这几条命令（都不启动微信）：

```powershell
.\lingyu.exe --selfcheck                                      # 版本 / 内置 yt-dlp / ffmpeg / webui
.\lingyu.exe --resolve <url> [--download] [--outdir <目录>]      # 直接解析（可选下载）一个链接
.\lingyu.exe --tweet <推文链接> [--outdir <目录>]                # 抓取推文并渲染出卡片图
```

`--selfcheck` 里 `yt_dlp_version` 与 `ffmpeg` 应显示具体版本/路径。

---

## 配置

MCP 模式复制 [.env.example](.env.example) 为 `.env`，或在 MCP 客户端配置的 `env`
中设置；桌面模式大部分选项直接在界面里改，保存在
`%APPDATA%\lingyu\bot.json`。

| 变量 | 默认 | 说明 |
|---|---|---|
| `WECHAT_BACKEND` | `auto` | `auto` / `wechatauto` / `wxauto4` / `wxauto` |
| `WECHAT_SEND_TIMEOUT` | `20` | 单次发送最长等待秒数 |
| `WECHAT_SEND_DIRS` | 空 | 允许发送文件的目录；**空 = 拒绝发送文件** |
| `WECHAT_REQUIRE_CONFIRM` | `0` | 发送前是否需 `confirm=true` |
| `WECHAT_AUDIT_ENABLED` | `1` | 是否记录操作审计 |
| `WECHAT_AUDIT_LOG` | `%APPDATA%\lingyu\logs\audit.jsonl` | 审计日志路径 |

完整列表见 [docs/TECHNICAL.md](docs/TECHNICAL.md#6-环境变量)。

运行日志写在 `%APPDATA%\lingyu\logs\app-YYYY-MM-DD.log`（按天切分，保留 7 天）。

---

## 安全设计

- **不隐式指定目标**：发送必须给出明确对象；同名或多个候选时拒绝发送并返回候选列表。
- **不虚报成功**：发送结果无法确认时返回 `failed`，**不自动重试**，避免重复消息。
- **文件白名单**：`send_file` 默认拒绝所有路径，需通过 `WECHAT_SEND_DIRS` 显式放宽；
  含 `..` 的路径穿越会被拒绝。
- **可配置确认**：`WECHAT_REQUIRE_CONFIRM=1` 时，未带 `confirm=true` 的发送只返回预览。
- **操作审计**：记录工具名、目标、时间与结果，默认不记录聊天正文。
- **关注列表**：可只关注/忽略指定会话，仅过滤本服务读取结果，不改变底层监听。
- **数据只在本机**：配置、日志、下载缓存都在 `%APPDATA%\lingyu`，不上传。

> 隐私提示：聊天内容可能包含敏感信息。若 Agent 使用在线模型，被读取的内容可能
> 发送给相应模型服务商，请自行评估并选择模型服务。

---

## 已知限制

- 依赖 Windows 微信桌面客户端，需已登录且窗口可见。
- 底层不提供「按需拉取完整会话列表 / 历史消息」接口；`get_chat_list` 与
  `get_chat_history` 返回的是**监听期间被动接收到的消息**，不是微信完整历史。
- `search_contact` 的本地联系人库查询依赖 `wechatauto` 后端；其他后端退回为
  「观察到的会话名称」。
- 自动化可能触发微信风控或受版本变化影响，不保证账号不受限制。
- 没有单实例保护：同时开两个实例会抢同一个微信窗口，表现为发送失败或重复回复。

---

## 目录结构

```
.
├── src/lingyu/
│   ├── server.py           MCP Server（工具注册）
│   ├── service.py          服务层（连接、发送、读取的统一入口）
│   ├── adapters/wechat.py  微信适配层（结构化接口 + 目标校验）
│   ├── bridge/             微信 UI 自动化桥接层（运行期按路径加载）
│   ├── bot/                常驻助手（引擎、人设、模型、链接解析、推文卡片）
│   ├── desktop.py          桌面窗口（pywebview）
│   ├── webui/              前端静态资源
│   ├── config.py           适配层配置
│   ├── paths.py            用户数据目录
│   ├── security.py         目标/内容/路径校验
│   ├── monitoring.py       关注列表
│   └── audit.py            操作审计
├── packaging/              PyInstaller spec、Inno Setup 脚本、构建脚本、图标
├── dsh-plugin/             DeepSeek Harness 插件
├── scripts/                真实环境联调脚本
├── tests/                  单元测试
├── vendor/yt_dlp/          内置 yt-dlp
└── docs/TECHNICAL.md       技术文档
```

---

## 测试

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

真实环境联调脚本位于 [scripts/](scripts/)：

```powershell
.venv\Scripts\python.exe scripts\phase3_e2e.py --wait-inbound 60   # stdio 协议端到端
.venv\Scripts\python.exe scripts\phase4_live_check.py              # P1 能力真实联调
```

---

## 构建与发布

```powershell
# 1. 生成独立运行时（onedir + zip + 安装包），产物在 dist\
#    安装包需要本机装有 Inno Setup 6（未装则自动跳过，只出 onedir + zip）
.venv\Scripts\python.exe packaging\build.py

# 2. 把运行时内置进 DSH 插件包
node dsh-plugin\scripts\stage-runtime.mjs

# 3. 发布（需已登录 gh）
gh release create v0.8.14 dist\lingyu-win32-x64.zip dist\lingyu-setup-x64.exe --title "灵语 v0.8.14"

# 4. 发布 npm 插件（prepack 会自动执行第 2 步）
cd dsh-plugin
npm publish --access public
```

---

## 文档

- [docs/TECHNICAL.md](docs/TECHNICAL.md) — 架构、工具接口、错误码、故障排查、测试记录
- [CHANGELOG.md](CHANGELOG.md) — 版本与变更记录

## 免责声明

本项目通过 UI 自动化复用微信桌面客户端，属于第三方自动化手段。使用时请遵守
微信及所在平台的服务条款；自动化操作可能导致账号被限制。建议使用独立的测试账号
或在可控测试会话中验证，避免对真实联系人开展未经确认的自动发送。
