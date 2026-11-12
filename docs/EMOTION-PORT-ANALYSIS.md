# 把 Emotion 情感多智能体系统移植进 LingYu —— 可行性分析

> 分析对象
> - 源项目：`F:\Code\Emotion`（情感多智能体系统，阶段五实现）
> - 目标项目：`F:\Code\LingYu`（灵语，微信桌面自动化 + 实时自动回复）
> - 分析时间：2026-10-28

> **状态：已落地（0.9.0）。** 本文是动手前的判断记录，落地结果与结论的差异如下：
>
> - **不是"方案 B 的可选开关"**：最终把情绪引擎做成了回复链路的**主干**
>   （`bot/emotion/` + `BotEngine._handle`），不再保留"关掉开关就走旧路"的双栈。
>   旧栈里与它冲突的机制（`reply_probability` 掷骰子、`[SILENT]` 哨兵、
>   `proactive_probability` 水群、硬编码行为提示、手工拼上下文）已删除。
> - **6 处硬阻塞全部解决**：`paths.py` 统一资源定位、`AsyncLLMAdapter` 跨线程调模型、
>   `response_format` 兼容降级、`session.py` 逐会话状态分片 + 容量上限、
>   `runtime.py` 内置种子、vendoring 时把绝对导入改写为相对导入。
> - **两处"升级"而非"照搬"**：新增 `kind.py`（微信侧没有事件类型，内核依赖它）、
>   `runtime.run_turn` 把 `max_rounds` 按 Agent 最大 stage 兜底（表达在 stage=4，
>   上游默认 4 轮会跑不到表达）。
> - **许可证不构成阻塞**：两个项目同属作者自有代码，无需额外授权确认。
> - 具体改动见 `CHANGELOG.md` 的 `[0.9.0]`，架构见 `docs/TECHNICAL.md` 第 5.1 节。

---

## 0. 结论先说

**技术可行，但不是"移植"，是"嵌入 + 适配"。**

Emotion 是一套**同步、单会话、单身份、纯标准库**的情绪计算内核；LingYu 是一套
**异步、多会话、可配置人设、PyInstaller 冻结**的微信回复引擎。两者在"给一条消息、
产出一句回复"这个接口上是天然吻合的（Emotion 的 `expression.text` ↔ LingYu 的 `reply`），
所以不需要重写算法；真正的工作量全在**接缝**上：

| 判断 | 结论 |
|---|---|
| 算法/模型层能否复用 | ✅ 可 100% 复用，`core/` + `agents/` 约 5,000 行几乎不用改逻辑 |
| 接口是否契合 | ✅ 高度契合：一句话进 → 一句话出（+ 可选"不发声"） |
| 依赖是否冲突 | ✅ 零冲突：Emotion 只用标准库，不新增任何第三方依赖 |
| 是否需要改代码 | ⚠️ 必须改 6 处硬阻塞（同步阻塞、LLM 缓存、JSON 模式、单会话状态、资源路径、包内导入） |
| 是否需要改架构 | ⚠️ 需要：`BotEngine._handle` 要多一条"情绪引擎"分支，配置/UI/打包要跟着动 |
| 风险 | 🟡 中：延迟×3、token×2~3、群聊语义不匹配、状态无上限增长 |

**推荐方案**：按"方案 B —— 可选开关 + 逐会话状态 + 规则兜底"落地。它不是替换
现有回复链路，而是在 `LLMClient` 前面插一层可开关的"情绪导演"：开关关闭时行为
与现在完全一致（零回归），开启时由九个 Agent 决定"说不说、说什么、多强"。

---

## 1. 两个项目的技术底账

| 维度 | Emotion | LingYu | 冲突？ |
|---|---|---|---|
| 语言/运行时 | Python 3.10+，纯标准库（`urllib`） | Python 3.10+，httpx / pillow / pywebview / mcp | 无 |
| 代码量 | 7,413 行（core 2,481 + agents 2,469 + tests 1,883 + 入口 580） | 10,270 行（`src/lingyu`） | — |
| 并发模型 | **同步阻塞**（`urllib.request.urlopen`） | **asyncio**（单事件循环 + 适配层回调） | ⚠️ 硬阻塞 |
| 会话模型 | **单会话单状态**（`state/session.json`） | **多会话**（`chat_id` → 历史/冷却/节流） | ⚠️ 硬阻塞 |
| 身份模型 | 结构化 JSON（principles/boundaries/voice） | 自由文本 `persona_custom` | ⚠️ 语义错配 |
| 输出 | `expression.text` 或 `silent=True` | `reply: str`（空串视为失败） | ✅ 可映射 |
| LLM 接口 | DeepSeek `/chat/completions` + `response_format=json_object` + **prompt 哈希落盘缓存** | 任意 OpenAI 兼容端点，无 `response_format`，无缓存 | ⚠️ 两处要改 |
| 状态持久化 | 进程内 + `state/session.json` | `%APPDATA%\lingyu\bot.json` | ⚠️ 需合并策略 |
| 打包 | 源码直接跑 | PyInstaller onedir（`_internal/` + `_MEIPASS`） | ⚠️ 资源路径要改 |
| 测试 | 156 个单测，0.6 秒全绿（本机实测） | 单元测试 + live 脚本 | ✅ 可平移 |
| 许可证 | 无 LICENSE 文件 | MPL-2.0 | ✅ 同作者自有项目，不构成阻塞 |

实测确认：`python -m unittest discover` → `Ran 156 tests in 0.617s / OK`。
源项目是健康的、可运行的、可测试的。

---

## 2. 接口契合度：这是能移植的根本原因

LingYu 的回复生成收敛在一个非常窄的接缝上（`src/lingyu/bot/engine.py:483-499`）：

```python
chat_key = self._chat_key(record)
messages = self._build_messages(chat_key)          # 上下文 → OpenAI messages
client = self._llm_factory(self._config)
reply = await client.chat(messages)                # ← 唯一的生成点
if not await self._send_reply(record.chat, reply): ...
```

Emotion 的入口同样窄（`chat.py:47`）：

```python
result, new_state, llm = run_turn(
    text, state=state, kind=kind, elapsed=elapsed, offline=offline, turn=turn)
reply = result.snapshot.get("expression.text") or ""
silent = result.snapshot.get("expression.silent")
```

两边都是一句话进、一句话出，且 Emotion 额外给出两个 LingYu **现在没有、但很想要**的能力：

1. **`silent` 是一等输出** —— 不是"随机不回"，而是"判定此刻不值得发声"。
   LingYu 目前只有 `reply_probability` 掷骰子（`engine.py:471`）和主动参与时的
   `[SILENT]` 哨兵（`engine.py:1396`），缺的正是这种有理由的沉默。
2. **强度上限 `regulation.intensity_cap`** —— 直接可以映射到
   `_split_reply` 的拆条数量或 `max_tokens`，让"对方很累"时回复自动变短变轻。

所以接缝不是问题，问题在接缝**两侧的运行环境**。

---

## 3. 硬阻塞项（不改就跑不起来）

### 3.1 同步阻塞 → 必须丢进线程池

`core/llm.py:88` 用的是 `urllib.request.urlopen(timeout=60)`，整个 `coordinator.run()`
是同步的，一轮完整周期会发起 **3 次 LLM 调用**（perception / appraisal / expression）。

LingYu 的 `_consume` 是**串行消费队列**的（`engine.py:289`），如果在 `_handle` 里
直接 `await` 一个阻塞 10 秒的同步函数，整个微信消息处理会被卡死——后面所有消息排队。

必须：

```python
loop = asyncio.get_running_loop()
result = await loop.run_in_executor(None, lambda: emotion_engine.turn(...))
```

并且**逐会话串行**（同一会话的两次调用要排队，否则状态快照会互相覆盖；
不同会话可并行）。

### 3.2 LLM 缓存必须关闭（这条最容易踩）

`core/llm.py:62-66` 按 `model|tag|system|user` 哈希落盘缓存，这是为**可重放/可复现**
设计的（README §1 明确说"保证重放时同一输入得到同一输出"）。

放到微信机器人上就是**灾难**：同一个朋友第二次说"在吗"，会拿到一模一样的那句回复；
`temperature=0.0` 又让这种重复稳定复现。用户会立刻发现"这机器人复读"。

必须：生产路径 `cache_dir=None` 或加 `replay=False` 开关，只在测试/场景回放时启用。

### 3.3 `response_format` 不被 LingYu 的客户端支持

Emotion 三个提示词全部要求**严格 JSON 输出**（`core/prompts.py`），
`core/llm.py:73` 依赖 `"response_format": {"type": "json_object"}`。
LingYu 的 `LLMClient.chat`（`bot/llm.py:60-69`）没有这个字段。

两条路：
- **A（推荐）**：给 `LLMClient.chat` 加一个可选 `response_format` 参数，DeepSeek /
  OpenAI 兼容端点都支持；不传时行为不变（零回归）。
- **B**：不依赖服务端强制 JSON，靠提示词 + 解析容错。Emotion 已有降级逻辑
  （`complete_json` 失败返回 `None` → 回落规则版），所以 B 也能跑，但失败率更高。

### 3.4 单会话状态 → 必须改成按会话分片

`chat.py:33` 的 `SESSION_PATH` 是全局单文件，`state` 是一份完整快照
（`self.*` / `needs.*` / `memory.*` / `emotion.*` / `policy.*`）。

LingYu 同时面对几十个私聊和群，必须：
- `%APPDATA%\lingyu\emotion\<hash(chat_id)>.json` 一会话一文件；
- 复用 `chat.py:80 persistable()` 的字段过滤逻辑（它已经把瞬时段
  `emotion.candidates` / `memory.retrieved_episodes` 剔掉了，正好是需要的行为）；
- **加容量上限**：`memory.episodes` 与 `memory.revisions` 在 `agents/memory.py`
  里是**无上限 append**（第 192/207 行），长期运行会线性膨胀。参照
  `policy.history[-MAX_HISTORY:]` 的做法，给 episodes 加 `[-200:]`、revisions 加 `[-200:]`。

### 3.5 资源路径在冻结包里会失效

`core/identity.py:16` 与 `core/config.py:12` 用 `Path(__file__).parent.parent` 定位
`identity/`、`scenarios/`、`cache/`。PyInstaller onedir 下 `__file__` 落在
`_internal/lingyu/...`，而数据文件在 `_MEIPASS`——直接跑会读不到身份文件，
`load_identity` 静默回落到 `NEUTRAL`（中性身份），**表现为"人设莫名其妙没了"，且不报错**。

必须仿照 `desktop.py:35 webui_dir()` 的写法加一个 `resource_dir()`，
并在 `packaging/build.py` / `lingyu.spec` 的 `datas` 里加上
`identity/*.json`、`identity/*.md`、`scenarios/*.json`。

### 3.6 包内导入需要机械重写

Emotion 全部用绝对导入 `from core.board import ...`、`from agents.base import ...`，
依赖"仓库根在 `sys.path`"。作为 `lingyu.emotion` 子包后必须改成相对导入
（`from .core.board import ...`）。

范围：`core/` 16 个文件 + `agents/` 9 个文件 + `chat.py`，约 26 个文件、几十处 import。
用一个一次性脚本 `sed` 式替换即可，风险低但要跑一遍测试确认。

---

## 4. 中等成本项（能跑，但效果会打折）

### 4.1 事件类型（`kind`）没有来源

Emotion 的评估先验完全由 `kind` 驱动（`policy.appraisal.kind_obstruction`）：

| kind | 阻碍先验 | 一致性先验 |
|---|---|---|
| message | 0.30 | +0.10 |
| message_silence | 0.30 | 0.00 |
| criticism | 0.52 | −0.15 |
| rejection | 0.62 | −0.35 |
| praise | 0.08 | +0.55 |
| outcome | 0.05 | +0.30 |

微信里没有这个字段。必须加一层**确定性分类器**（关键词 + 时间间隔）：

- 距上条消息 > N 小时且无回复 → `message_silence`
- 命中"谢谢/太好了/辛苦了"等 → `praise`
- 命中"算了/不想聊了/别说了"等 → `rejection`（Emotion 的 `other_affect` 已有
  疲惫/暖意/疏离/难受四组线索词，可直接复用）
- 其余 → `message`

不做的后果：所有消息都按 `message` 先验处理，`praise` 不会带来正向情绪，
"被拒绝"和"闲聊"情绪曲线一样——系统能跑，但"情绪有记忆"这个卖点消失大半。

### 4.2 群聊语义不匹配（最本质的错配）

Emotion 的整个模型假设**一对一**：`other.*` 是"对方"的一份评分，
`memory.relation` 是"这一份关系"（README §7 第 11 条自己承认"关系估计是单关系模型"）。

群聊里"对方"是谁？三个选项：
1. **只对私聊启用情绪引擎**，群聊维持现有逻辑（最稳，推荐先做）；
2. 群里按 `sender` 分片状态（一个群 N 个人 = N 份关系），`other.*` 只评当前发言人；
3. 群里合成一个"群体"评分（信息损失大，不推荐）。

### 4.3 人设（persona）映射

LingYu 的 `persona_custom` 是一段自由文本，Emotion 的 `identity/song_nuan.json`
是结构化契约（voice/principles/boundaries/growth_policy）。两者不能直接互换：

- 直接把自由文本塞进 `Identity.name` 不生效（Emotion 读的是 JSON 字段）；
- 正确做法是新增一份 **`identity` 选择器**：内置 `song_nuan`（宋暖），
  并允许用户提供自定义 `identity/<id>.json`；UI 上"人设"页与"情绪"页分开，
  情绪页只选身份 id。

### 4.4 延迟与成本

| 项 | 现状 | 接入 Emotion 后 |
|---|---|---|
| 每条触发消息的 LLM 调用 | 1 次 | **3 次**（感知 / 评估 / 表达） |
| 单条回复端到端延迟 | 2~4 秒 | **6~12 秒**（串行三次） |
| token 消耗 | 1× | **2~3×** |
| 无 Key / 断网时 | 报错不回复 | **规则版兜底，仍能回**（这是净收益） |

缓解：
- 感知与评估可合并为一次调用（改 `prompts.py` 出双段 JSON），降到 2 次；
- 或感知走规则词表（Emotion 的 `perception.py` 本来就有规则通路）；
- 群聊不启用（见 4.2），实际成本增量可控。

### 4.5 状态无上限增长

见 3.4。一小时的活跃私聊可能产生 200+ 条 `memory.revisions`，每条含
`then_belief`、`now_review`、`review_question`——单会话 JSON 会长到几百 KB，
每次读写都是全量。必须加 cap + 定期压缩（把 30 天前的 `uncertain` 复核记录丢掉）。

---

## 5. 三个方案与推荐

### 方案 A：最小侵入（"情绪导演"）

```
WeChat 消息 → BotEngine._handle
                 ├─ 开关关 → 原样走 LLMClient（零回归）
                 └─ 开关开 → EmotionAdapter.turn(text, chat_id)
                              → 九个 Agent → expression.text / silent
                              → 有文本则发；silent 则不回
```

- 改动：新增 `src/lingyu/bot/emotion/`（vendor 化）+ `emotion_adapter.py`（约 200 行）
  + `BotConfig` 加 3~4 个字段 + `engine.py` 两处分支。
- 工作量：**2~3 天**。
- 保留现有上下文窗口、链接解析、撤回、节流、分条发送——它们都在 `_handle` 里、
  在接缝之外，完全不受影响。
- 缺点：情绪状态无法观测，用户看不到"她现在什么心情"。

### 方案 B：A + 状态面板 + 持久化（**推荐**）

在 A 之上加：

- `emotion` 配置页：开关、身份选择、缓存开关、群聊是否启用、事件分类阈值；
- 桌面 API 暴露 `get_emotion_state(chat)`：返回 valence/activation/候选情绪/
  调节策略/最近复核——直接把 `chat.py:93 print_chain` 的那套信息做成 JSON；
- 状态按会话落盘 + cap 裁剪；
- 日志里每个周期写一行摘要（"焦虑 0.46 / seek_info / cap 0.32 / 已回"）。

- 额外工作量：**+2 天**（UI 一页 + 桥接方法 + 持久化）。
- 这是"能拿出去讲"的形态：情绪不是黑箱，而是可审计的因果链。

### 方案 C：完全替换（用九 Agent 取代 LLMClient）

把 `_build_messages` / `_render_context` / `persona` 全部弃用，上下文、人设、
分条、节流全部由 Emotion 接管。

- 工作量：**1~2 周**，且要重做群聊、链接上下文注入、视觉输入、撤回记账。
- 不推荐：把 LingYu 已经打磨过的工程能力（白名单、节流、分条、撤回、链接卡片）
  全部推倒重来，收益不抵风险。

### 推荐路径

**先 A 后 B，不做 C。** 分三步走：

1. **第 1 步（可回滚）**：vendor 化 + 相对导入 + 资源路径 + 缓存开关，跑通
   `offline=True` 的规则版。此时不接 LLM，只验证"能跑、能出话、能沉默"。
2. **第 2 步**：接 `LLMClient`（加 `response_format`），只对**私聊**启用，
   群聊保持原样；加 `kind` 分类器。
3. **第 3 步**：加状态面板、持久化、cap 裁剪；用真实聊天跑一周，对比
   "重复率 / 平均长度 / 沉默比例"三项指标再决定是否扩大范围。

---

## 6. 落地清单（按文件）

### 新增

```
src/lingyu/bot/emotion/               # vendor 自 F:\Code\Emotion（约 5,000 行）
    __init__.py
    core/          （16 个模块，相对导入）
    agents/        （9 个 Agent + base）
    identity/      song_nuan.json / song_nuan.md
    scenarios/     conversation.json
    runtime.py     # run_turn 的薄封装：per-chat 状态、缓存开关、线程池
src/lingyu/bot/emotion_adapter.py     # 约 200~300 行，LingYu ↔ Emotion 的翻译层
```

### 修改

| 文件 | 改动 |
|---|---|
| `bot/config.py` | 加 `emotion_enabled` / `emotion_identity` / `emotion_groups` / `emotion_cache` / `emotion_offline` |
| `bot/engine.py:487` | 生成分支：`if config.emotion_enabled and not record.is_group` → adapter |
| `bot/engine.py:1385` | 主动参与同样分支（可选） |
| `bot/llm.py:42` | `chat(..., response_format=None)` |
| `core/config.py`(Emotion) | `PROJECT_ROOT` → `resource_dir()` |
| `core/identity.py:16` | `IDENTITY_DIR` → `resource_dir()/identity` |
| `core/llm.py:62` | 缓存开关（默认生产关闭） |
| `agents/memory.py:192,207` | episodes / revisions 加容量上限 |
| `packaging/build.py` + `lingyu.spec` | `datas` 加 identity / scenarios |
| `webui/index.html` + `desktop.py` | 情绪页 + `get_emotion_state` 桥接方法 |
| `pyproject.toml` | 版本号 + CHANGELOG |

---

## 7. 风险清单

| 风险 | 等级 | 说明与对策 |
|---|---|---|
| **源项目无 LICENSE** | 🔴 高 | `F:\Code\Emotion` 没有 LICENSE/COPYING，而 LingYu 是 MPL-2.0。**在确认授权前不要合入主仓**——先问清来源（自研 / 第三方），自研就补一份与 MPL-2.0 兼容的许可证。这是唯一的"法律级"阻塞。 |
| 延迟翻倍 | 🟡 中 | 6~12 秒/条。对策：感知走规则、或合并感知+评估为一次调用；群聊不启用。 |
| token 成本 ×2~3 | 🟡 中 | 对策：只在私聊 + 只对"需要情绪判断"的消息启用；规则版兜底。 |
| 复读 | 🟡 中 | LLM 缓存必须关（见 3.2），`temperature` 不能沿用 0.0。 |
| 状态膨胀 | 🟡 中 | episodes/revisions 加 cap（见 3.4）。 |
| 群聊语义错配 | 🟡 中 | 先只对私聊启用（见 4.2）。 |
| 微信风控 | 🟢 低 | 回复频率不变（甚至更低，因为多了"沉默"），不新增自动化动作。 |
| 冻结包读不到身份 | 🟡 中 | 静默回落中性身份、不报错，很难排查——务必用 `resource_dir()` 并加一条启动自检。 |
| 现有功能回归 | 🟢 低 | 接缝在 `_handle` 内、开关默认关闭，链接/撤回/节流/分条逻辑完全不经过 Emotion。 |

---

## 8. 验收标准（建议写进测试）

1. **零回归**：`emotion_enabled=False` 时，`tests/` 全绿，且 `_handle` 走的调用路径
   与改动前逐行一致（可用现有 mock 断言 `LLMClient.chat` 被调用、adapter 未被调用）。
2. **规则版可用**：断网 + 无 Key 时，`offline=True` 仍能对"今天有点累"产出一句
   合规回复（复用 Emotion 现有的 156 个测试思路，新增 10~15 个 adapter 级用例）。
3. **沉默是一等输出**：`silent=True` 时**不发任何消息**，且日志里有理由
   （"已经重新开口过一次，不再追问"）。
4. **状态隔离**：会话 A 的情绪不影响会话 B（两份 session 文件互不可见）。
5. **容量有界**：连续 500 轮后单会话 JSON < 200 KB。
6. **不读缓存**：同一句话连发两次，回复**不相同**（生产模式）。
7. **打包可用**：`dist\lingyu\lingyu.exe --gui` 启动后，情绪页能加载到 `song_nuan`
   身份（不是"未命名/中性"）。

---

## 9. 一句话总结

Emotion 的内核（九个 Agent、门禁、收敛、情绪衰减、自我迭代）质量很高且完全
可复用；LingYu 的工程外壳（微信适配、节流、撤回、分条、打包）也是现成的。
两者的接缝只有一处、很干净，所以**能移植**；真正要花的力气不在算法，而在
"把同步单会话内核塞进异步多会话宿主"这六件事上。

**落地结果（0.9.0）**：六件事都已解决，且没有停在"可选开关"——情绪引擎成了
回复链路的主干，回不回由情绪决定而不是掷骰子。原文列出的 7 条验收标准中，
第 1 条（零回归）因"不再保留双栈"而**作废**，其余全部由 `tests/test_bot.py`
与 `tests/test_emotion.py` 覆盖。
