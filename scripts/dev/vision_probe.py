"""验证 lingyu.vision 的布局解析：会话列表 + 消息区结构还原。

用法：
    python build/vision_probe.py sessions
    python build/vision_probe.py messages
    python build/vision_probe.py open 原神群
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from lingyu.vision import capture, layout  # noqa: E402


def _shot(tag: str):
    hwnd = capture.find_wechat_window()
    img, rect = capture.prepare(hwnd)
    out = Path(__file__).parent / "out"
    out.mkdir(exist_ok=True)
    img.save(out / f"{tag}.png")
    return img, rect


def cmd_sessions() -> int:
    img, rect = _shot("vision_full")
    print(f"窗口 {tuple(rect)}  截图 {img.size}")
    sessions = layout.parse_sessions(img)
    print(f"\n会话 {len(sessions)} 项：")
    for i, s in enumerate(sessions, 1):
        flag = " ●未读" if s.has_unread else ""
        flag += " @我" if s.mentioned else ""
        print(f"  #{i} y={s.top}-{s.bottom}  {s.display_name}{flag}")
        print(f"       预览：{s.preview}")
    print(f"\n当前会话标题：{layout.read_title(img)!r}")
    return 0


def cmd_messages() -> int:
    img, rect = _shot("vision_full")
    area, ox, oy = layout.crop_ratio(img, layout.LIST_X1, layout.MSG_Y0, 1.0, layout.MSG_Y1)
    area.save(Path(__file__).parent / "out" / "vision_msg_area.png")
    msgs = layout.parse_messages(area)
    print(f"消息区 {area.size}  解析出 {len(msgs)} 条：")
    for m in msgs:
        who = "我" if m.outgoing else (m.sender or "对方")
        print(f"  [{who}] {m.time_mark}  y={m.top}-{m.bottom}")
        print(f"        {m.content}")
    return 0


def cmd_open(name: str) -> int:
    hwnd = capture.find_wechat_window()
    img, rect = capture.prepare(hwnd)
    sessions = layout.parse_sessions(img)
    target = next((s for s in sessions if name in s.display_name), None)
    if not target:
        print(f"没找到会话 {name!r}")
        for s in sessions:
            print("  ", s.display_name)
        return 5

    x = rect.left + int(rect.width * layout.NAV_X1) + 200
    y = rect.top + target.center_y
    print(f"点击 {target.display_name!r} @ ({x},{y})")
    capture.click(x, y)
    time.sleep(0.5)

    img2, _rect2 = capture.prepare(hwnd)
    print(f"标题：{layout.read_title(img2)!r}")
    area, _ox, _oy = layout.crop_ratio(
        img2, layout.LIST_X1, layout.MSG_Y0, 1.0, layout.MSG_Y1
    )
    out = Path(__file__).parent / "out"
    out.mkdir(exist_ok=True)
    area.save(out / "probe_open_area.png")
    msgs = layout.parse_messages(area)
    print(f"消息区 {area.size}，解析出 {len(msgs)} 条消息：")
    for m in msgs:
        who = "我" if m.outgoing else (m.sender or "对方")
        print(f"  [{who}] 时间={m.time_mark!r} y={m.top}-{m.bottom}")
        print(f"        {m.content}")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "sessions":
        return cmd_sessions()
    if cmd == "messages":
        return cmd_messages()
    if cmd == "open" and len(sys.argv) > 2:
        return cmd_open(sys.argv[2])
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
