"""一次性移植脚本：把 F:\\Code\\Emotion 的 core/ 与 agents/ 搬到 lingyu.bot.emotion。

- 复制 .py 与 identity 数据
- 把 `from core.X` / `from agents.X` 改写成相对导入
- 幂等：可重复执行（覆盖目标目录下的同名文件）

**但不会静默覆盖宿主改过的文件**：目标文件内容与「刚生成的内容」不一致时，
说明移植后在宿主侧动过（例如 ``agents/regulation.py`` 加了深夜规则），
脚本会跳过并打印差异提示。确认要以上游为准时加 ``--force``。
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

SRC = Path(r"F:\Code\Emotion")
DST = Path(r"F:\Code\LingYu\src\lingyu\bot\emotion")


def rewrite(text: str, *, in_agents: bool) -> str:
    """把绝对包导入改成相对导入。"""
    core_prefix = "..core." if in_agents else "."
    agent_prefix = "." if in_agents else "..agents."
    text = re.sub(r"\bfrom core\.", f"from {core_prefix}", text)
    text = re.sub(r"\bfrom agents\.", f"from {agent_prefix}", text)
    return text


def sync(path: Path, target: Path, *, in_agents: bool, force: bool) -> str:
    """写入一个文件；返回 ``written`` / ``same`` / ``skipped``。"""
    text = rewrite(path.read_text(encoding="utf-8"), in_agents=in_agents)
    if target.is_file() and not force:
        current = target.read_text(encoding="utf-8")
        if current == text:
            return "same"
        # 内容不同：要么上游更新了，要么宿主改过。分不清就都不覆盖——
        # 覆盖掉宿主改动会静默丢掉「深夜不开口」这类规则，表现是行为莫名其妙变了。
        print(f"  ! 跳过 {target.name}：与上游不一致（宿主改过或上游已更新）")
        return "skipped"
    target.write_text(text, encoding="utf-8")
    return "written"


def main() -> None:
    force = "--force" in sys.argv
    if not (SRC / "core").is_dir():
        raise SystemExit(f"找不到上游源码：{SRC}")

    (DST / "core").mkdir(parents=True, exist_ok=True)
    (DST / "agents").mkdir(parents=True, exist_ok=True)
    (DST / "identity").mkdir(parents=True, exist_ok=True)

    counts = {"written": 0, "same": 0, "skipped": 0}
    for folder, in_agents in (("core", False), ("agents", True)):
        for path in sorted((SRC / folder).glob("*.py")):
            result = sync(path, DST / folder / path.name, in_agents=in_agents, force=force)
            counts[result] += 1

    for path in (SRC / "identity").iterdir():
        if path.is_file():
            target = DST / "identity" / path.name
            if target.is_file() and not force and target.read_bytes() != path.read_bytes():
                print(f"  ! 跳过 identity/{path.name}：与上游不一致")
                counts["skipped"] += 1
                continue
            shutil.copy2(path, target)
            counts["written"] += 1

    print(
        f"写入 {counts['written']} 个，未变 {counts['same']} 个，"
        f"跳过 {counts['skipped']} 个"
    )
    if counts["skipped"]:
        print("跳过的文件里可能有上游的更新。逐个 diff 后再决定，"
              "确认要以上游为准时加 --force。")


if __name__ == "__main__":
    main()
