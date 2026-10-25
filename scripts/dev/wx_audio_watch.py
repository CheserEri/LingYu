"""纯音频会话判断微信有没有新消息：不读数据库、不需要窗口。

原理：进程只要播放声音，Windows 就会为它建立音频会话（IAudioSessionControl）。
微信启动时就注册了会话，但状态是 Inactive(0)；播放提示音时变 Active(1)，
播完回 Inactive。轮询这个状态即可，不需要录音、不需要识别音频。

用法：
    python build/wx_audio_watch.py --duration 60
"""

from __future__ import annotations

import argparse
import time

import psutil
from pycaw.pycaw import AudioUtilities

STATE_NAMES = {0: "Inactive", 1: "Active", 2: "Expired"}


def weixin_pids() -> set[int]:
    pids = set()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info["name"] or "").lower() in {"weixin.exe", "wechat.exe"}:
                pids.add(proc.info["pid"])
        except Exception:
            pass
    return pids


def scan(pids: set[int]) -> list[tuple[int, int]]:
    """返回 [(pid, state)]，只含微信进程的会话。"""
    out = []
    try:
        for s in AudioUtilities.GetAllSessions():
            if s.ProcessId in pids:
                out.append((s.ProcessId, int(s.State)))
    except Exception:
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--interval", type=float, default=0.1)
    args = ap.parse_args()

    pids = weixin_pids()
    print(f"[init] 微信进程 {sorted(pids)}")
    if not pids:
        print("微信没在运行")
        return 2

    sessions = scan(pids)
    print(f"[base] 现有会话 {sessions}")

    # 性能测量
    t0 = time.perf_counter()
    for _ in range(20):
        scan(pids)
    cost = (time.perf_counter() - t0) / 20 * 1000
    print(f"[perf] 单次扫描 {cost:.2f} ms")

    prev = {pid: st for pid, st in sessions}
    deadline = time.time() + args.duration
    polls = 0
    events = 0
    print(f"[watch] 监听 {args.duration:.0f}s，间隔 {args.interval*1000:.0f}ms ...")

    while time.time() < deadline:
        time.sleep(args.interval)
        polls += 1
        cur = {pid: st for pid, st in scan(pids)}
        for pid, st in cur.items():
            before = prev.get(pid, 0)
            if st != before:
                events += 1
                mark = "开始播放" if st == 1 else "停止播放"
                print(f"[{mark}] pid={pid} {STATE_NAMES.get(before)} -> "
                      f"{STATE_NAMES.get(st)}  ({time.strftime('%H:%M:%S')})")
        prev = cur

    print(f"[done] 轮询 {polls} 次，状态变化 {events} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
