"""视觉通道端到端验证工具。

用法：
    python build/vision_run.py signal          # 采样 10 秒信号
    python build/vision_run.py once --force    # 强制跑一轮（跳过信号判断）
    python build/vision_run.py watch           # 常驻监听
    python build/vision_run.py dump            # 查看库内容
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from lingyu.vision.archive import MessageArchive  # noqa: E402
from lingyu.vision.pipeline import VisionPipeline  # noqa: E402
from lingyu.vision.signal import (  # noqa: E402
    AudioSessionWatcher,
    TaskbarUnreadWatcher,
    weixin_pids,
)


def cmd_signal(seconds: float) -> int:
    audio = AudioSessionWatcher()
    bar = TaskbarUnreadWatcher()
    print(f"微信进程 {sorted(weixin_pids())}")
    print(f"音频会话可用 {audio.available}")
    print(f"任务栏按钮 {bar.locate()}")

    deadline = time.time() + seconds
    while time.time() < deadline:
        t0 = time.perf_counter()
        a = audio.poll()
        ratio = bar.orange_ratio()
        cost = (time.perf_counter() - t0) * 1000
        print(
            f"  {time.strftime('%H:%M:%S')} 提示音={'是' if a else '否'} "
            f"橙色={ratio * 100:5.1f}%  耗时={cost:.1f}ms",
            flush=True,
        )
        time.sleep(0.5)
    bar.close()
    return 0


def cmd_once(force: bool) -> int:
    pipe = VisionPipeline()
    try:
        report = pipe.run_once(force=force)
        print(report.describe())
        if report.error:
            return 1
        return 0
    finally:
        pipe.close()


def cmd_watch() -> int:
    pipe = VisionPipeline()
    try:
        pipe.run_forever()
    except KeyboardInterrupt:
        print("\n停止监听")
    finally:
        pipe.close()
    return 0


def cmd_dump(limit: int) -> int:
    with MessageArchive() as db:
        print(f"库：{db.path}")
        print(f"统计：{db.stats()}")
        for conv in db.conversations():
            print(f"\n== {conv['name']}  共 {conv['message_count']} 条  最近 {conv['last_message_at']}")
            for row in db.recent(conv["name"], limit):
                who = "我" if row["outgoing"] else (row["sender"] or "对方")
                print(f"  #{row['seq']:>3} [{who}] {row['time_mark']}")
                print(f"       {row['content']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["signal", "once", "watch", "dump"])
    ap.add_argument("--force", action="store_true", help="跳过信号判断")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    if args.cmd == "signal":
        return cmd_signal(args.seconds)
    if args.cmd == "once":
        return cmd_once(args.force)
    if args.cmd == "watch":
        return cmd_watch()
    return cmd_dump(args.limit)


if __name__ == "__main__":
    sys.exit(main())
