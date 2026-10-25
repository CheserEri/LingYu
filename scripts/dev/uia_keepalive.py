"""临时诊断脚本：常驻 UIA 客户端，让 UiaClientsAreListening() 保持为真。

微信 4.x（Qt 自绘界面）只在启动瞬间检测「是否有辅助技术客户端在监听」，
有才注册 UIA provider。重启微信前先跑这个，微信启动后 UIA 树才会建起来。

用法：python build/uia_keepalive.py [持续秒数]
"""

from __future__ import annotations

import sys
import time

import comtypes.client
from comtypes.gen import UIAutomationClient as UIA


def main() -> int:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    automation = comtypes.client.CreateObject(
        UIA.CUIAutomation, interface=UIA.IUIAutomation
    )
    root = automation.GetRootElement()
    print(f"UIA 监听已启动，持续 {seconds} 秒…", flush=True)
    deadline = time.time() + seconds
    rounds = 0
    while time.time() < deadline:
        try:
            # 每次真正查一次桌面，系统据此认为有客户端在监听。
            children = root.GetChildren()
            rounds += 1
            if rounds % 30 == 0:
                print(f"  仍在监听（{rounds} 次，桌面 {len(children)} 个顶层窗口）", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  查询失败: {exc}", flush=True)
        time.sleep(0.5)
    print("UIA 监听结束。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
