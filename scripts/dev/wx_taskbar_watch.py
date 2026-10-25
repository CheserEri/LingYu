"""联合观测：任务栏按钮颜色 + 微信音频会话状态。

目的是确认任务栏按钮变色到底对应什么（未读消息？窗口激活？），
以及它能否作为「有新消息」的独立信号。

用法：
    python build/wx_taskbar_watch.py --duration 180
"""

from __future__ import annotations

import argparse
import time
from collections import Counter

import psutil
import uiautomation as auto
from PIL import ImageGrab
from pycaw.pycaw import AudioUtilities


def weixin_pids() -> set[int]:
    out = set()
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if (p.info["name"] or "").lower() in {"weixin.exe", "wechat.exe"}:
                out.add(p.info["pid"])
        except Exception:
            pass
    return out


def audio_state(pids: set[int]) -> str:
    try:
        for s in AudioUtilities.GetAllSessions():
            if s.ProcessId in pids:
                return "Active" if int(s.State) == 1 else "Inactive"
    except Exception:
        pass
    return "-"


def find_button():
    tl = auto.ToolBarControl(ClassName="MSTaskListWClass")
    if not tl.Exists(0.5):
        return None
    for k in tl.GetChildren():
        if "微信" in (k.Name or ""):
            return k
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=180.0)
    ap.add_argument("--interval", type=float, default=0.5)
    args = ap.parse_args()

    auto.InitializeUIAutomationInCurrentThread()
    pids = weixin_pids()
    btn = find_button()
    if not btn:
        print("没找到任务栏微信按钮")
        return 2
    r = btn.BoundingRectangle
    box = (r.left, r.top, r.right, r.bottom)
    print(f"[init] pids={sorted(pids)} button={box}", flush=True)

    deadline = time.time() + args.duration
    prev_color = None
    prev_audio = None
    polls = 0

    while time.time() < deadline:
        time.sleep(args.interval)
        polls += 1
        try:
            img = ImageGrab.grab(bbox=box, all_screens=True).convert("RGB")
            c = Counter(img.getdata())
            color, count = c.most_common(1)[0]
            ratio = count / (img.width * img.height)
        except Exception as e:
            print("截图失败", e, flush=True)
            continue
        audio = audio_state(pids)

        color_changed = prev_color is not None and (
            abs(color[0] - prev_color[0]) + abs(color[1] - prev_color[1])
            + abs(color[2] - prev_color[2]) > 12
        )
        audio_changed = prev_audio is not None and audio != prev_audio

        if color_changed or audio_changed or prev_color is None:
            tag = []
            if prev_color is None:
                tag.append("基线")
            if color_changed:
                tag.append("颜色变")
            if audio_changed:
                tag.append("音频变")
            print(f"[{'+'.join(tag)}] {time.strftime('%H:%M:%S')} "
                  f"主色={color} {ratio*100:.0f}%  音频={audio}", flush=True)

        prev_color, prev_audio = color, audio

    print(f"[done] 轮询 {polls} 次", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
