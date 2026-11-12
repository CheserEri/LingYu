# 目录结构

```
lingyu/
├── src/lingyu/              源码（src 布局，pip install -e . 后以包形式导入）
│   ├── server.py            MCP 服务端（工具定义与入口）
│   ├── service.py           工具实现：状态/会话/发送/文件
│   ├── desktop.py           桌面助手窗口（--gui）
│   ├── config.py            环境变量与路径
│   ├── security.py          发送白名单、路径穿越校验、审计
│   ├── logging_config.py    日志配置（控制台 + 文件）
│   ├── adapters/            平台适配层（微信桌面自动化 / QQ 机器人 API v2）
│   ├── bot/                 常驻自动回复引擎
│   │   ├── engine.py        消息消费、触发判断、发送闸门
│   │   ├── config.py        bot.json 配置（界面可改）
│   │   ├── llm.py           兼容 OpenAI 的模型客户端
│   │   ├── emotion/         混合人格内核（回复链路主干）
│   │   │   ├── core/        身份 / 兴趣 / 措辞闸门 / 模型客户端 / 路径
│   │   │   ├── affect.py    心境：默认值、衰减、事件评估、主导与策略
│   │   │   ├── memo.py      体感备忘：数值 → 不带数字的短句
│   │   │   ├── episode.py   情节记忆：有界 episodes + 事后复核
│   │   │   ├── persona.py   人格札记账本：全局、可修订、可撤回
│   │   │   ├── writer.py    札记写入策略：规则优先 + 可选模型提议
│   │   │   ├── expression.py 表达：单次模型调用 + 规则兜底 + 沉默
│   │   │   ├── identity/    内置身份（violet.json + .md）
│   │   │   ├── bridge.py    宿主门面：EmotionBridge + AsyncLLMAdapter
│   │   │   ├── runtime.py   一轮周期入口（同步，宿主丢线程池跑）
│   │   │   ├── pacing.py    表达节奏：停顿 / 分条 / 间隔（情绪的副产物，不是配置）
│   │   │   ├── core/interests.py 兴趣：类目词表、注意度、演化公式
│   │   │   ├── session.py   逐会话状态分片（有界 + 有上限）
│   │   │   └── kind.py      微信消息 → 事件类型（微信没有这个字段）
│   │   ├── links.py         链接解析与下载（分发到下面几个模块）
│   │   ├── douyin.py        抖音（浏览器桥 + 签名）
│   │   ├── xiaohongshu.py   小红书（SSR __INITIAL_STATE__）
│   │   ├── tweet.py         X 推文抓取 + 卡片渲染器
│   │   ├── card.py          卡片数据结构（推文/小红书共用）
│   │   ├── browser.py       本机 Chrome/Edge 调试协议桥
│   │   └── storage.py       下载目录配额清理
│   └── webui/               桌面界面静态资源（index.html / app.js / style.css）
├── tests/                   单元测试（unittest，全部基于 mock）
├── docs/TECHNICAL.md        技术手册：接口、错误码、故障排查、构建
├── packaging/               打包链：PyInstaller spec、Inno Setup 脚本、桥接模块
├── scripts/
│   ├── live/                真机联调脚本（需要微信已登录，会真的发消息）
│   └── dev/                 一次性探针与样张（OCR/UIA/像素/卡片渲染等）
├── dsh-plugin/              DSH 插件（把运行时随 npm 包分发）
├── vendor/                  内置 yt-dlp（不打补丁，跟随上游）
├── requirements/
│   └── wechat.txt           无法被 pip 正常解析的微信运行时依赖（--no-deps 安装）
├── .github/workflows/       CI：lint + 单元测试；打 tag 时构建 zip
├── build/                   构建/调试产物（git 忽略）
├── dist/                    打包输出：onedir、zip、安装包（git 忽略）
├── pyproject.toml           元数据、依赖、ruff 配置
├── .editorconfig            缩进/换行/编码约定
├── .env.example             环境变量样例
├── CONTRIBUTING.md          开发与发布流程
└── mcp.config.example.json  MCP 客户端配置样例
```

## 约定

- **`src/` 布局**：包不在仓库根，避免「本地目录遮蔽已安装包」这类问题；测试与打包都通过
  `pip install -e .` 使用同一份代码。
- **`build/` 与 `dist/` 只放产物**：`build/` 是 PyInstaller 的 workpath 与各类调试输出，
  `dist/` 是最终分发包，两者都被 `.gitignore` 忽略。**不要把源码或脚本放进 `build/`**
  （早先的探针脚本就放在那里，`git clean -xdf` 会直接删掉）。
- **`scripts/dev/` 与 `scripts/live/` 的区别**：
  - `dev/`：一次性探针、样张、复现脚本。可能在任意机器上跑不起来（依赖本机微信版本、
    屏幕分辨率、字体），**不进 CI**，也不保证长期可用。
  - `live/`：阶段联调脚本，会真的连接微信并可能发消息，只在手工联调时跑。
- **文档分工**：`README.md` 对外（安装/功能/配置）、`CHANGELOG.md` 按版本记变更、
  `docs/TECHNICAL.md` 记接口与故障排查、`docs/STRUCTURE.md`（本文）记目录与约定。
  新增功能时至少同步 README 与 CHANGELOG。
- **`bot/emotion/` 是 vendor 进来的内核**：`core/` 与 `agents/` 保持上游形态（相对导入
  由 `scripts/dev/vendor_emotion.py` 改写），宿主侧的差异都写在 `bridge.py` /
  `runtime.py` / `session.py` / `kind.py` 这四个文件里。要跟上游同步时重跑那个脚本，
  **不要**在 `core/` 里散着改——下次覆盖就没了。
  - `pacing.py` 是**宿主新增**的一层，上游没有对应物（终端程序不需要「分几条、隔多久」），
    所以同步时不会被覆盖。反过来也别把它塞进 `agents/expression.py`：
    表达 Agent 只管「说什么」，发送节奏是宿主的事。
  - `agents/regulation.py` 里有两处**上游没有**的改动：`NIGHT_START` / `NIGHT_END`
    的深夜规则（按 `local_hour`，宿主传进来而不是读系统时间）与 `reopen_done`。
    这两处属于「微信场景必需」，同步时要注意保留，否则「静默时段」这类配置就得
    重新加回宿主。
