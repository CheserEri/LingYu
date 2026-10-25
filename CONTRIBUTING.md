# 参与开发

面向想改代码 / 自己打包的人。只想用的话看 [README.md](README.md) 就够了。

## 环境

Windows 10/11 x64、Python 3.10+（开发与打包目前都在 3.13 上验证）。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[wechat,dev]"
# wechatauto-replica 的 winsdk 依赖在 Python 3.12+ 上没有 wheel，必须单独装
.venv\Scripts\python.exe -m pip install --no-deps -r requirements\wechat.txt
```

- `[wechat]`：微信自动化运行时（`wxauto4`、`uiautomation`、`winrt-*`、`zstandard`）。
  不装也能跑大部分单元测试，但适配层与真机联调需要它。
- `requirements\wechat.txt`：补上 `wechatauto-replica`（`--no-deps`，原因见文件内注释）。
- `[dev]`：`ruff`（lint）。

## 日常命令

```powershell
# 单元测试（全部基于 mock，不需要打开微信）
.venv\Scripts\python.exe -m unittest discover -s tests -t .

# 只跑某个测试类
.venv\Scripts\python.exe -m unittest tests.test_bot.XhsTests -v

# Lint（CI 跑的就是这条）
.venv\Scripts\python.exe -m ruff check src tests packaging scripts

# 桌面窗口 / MCP 服务
.venv\Scripts\python.exe -m lingyu.desktop
.venv\Scripts\python.exe -m lingyu.server
```

## 改代码时的约定

- **发送类改动必须有测试**：`send_message` / `send_file` 出错就是往真实联系人发错消息，
  比崩溃更糟。测试里用 `FakeAdapter`，不要连真微信。
- **链接解析**新增站点时，放到 `bot/` 下单独模块，由 `links.py` 分发；不要在
  `links.py` 里堆 `if 是某站`。参考 `xiaohongshu.py` 的写法。
- **卡片版式只改一处**：推文与小红书共用 `tweet.render_card()` + `card.CardData`，
  改版式两边同时生效；新平台只需提供 `xxx_to_card()` 转换函数。
- **注释写「为什么」**：踩过的坑（微信版本差异、风控、UA、字段两套命名）都写进
  模块 docstring，比写在提交信息里更容易被下一个人看到。

## 打包与发布

```powershell
# 1. 生成 onedir + zip + 安装包（安装包需要 Inno Setup 6）
.venv\Scripts\python.exe packaging\build.py

# 2. 把运行时内置进 DSH 插件包
node dsh-plugin\scripts\stage-runtime.mjs
```

发版时同步改三处版本号：`pyproject.toml`、`src/lingyu/__init__.py`、`src/lingyu/server.py`
（以及 `packaging/installer.iss` 的默认值），再写 CHANGELOG、打 tag、发 Release。

## 目录结构

见 [docs/STRUCTURE.md](docs/STRUCTURE.md)。**注意 `build/` 是产物目录**（被 git 忽略），
新脚本请放 `scripts/dev/`。
