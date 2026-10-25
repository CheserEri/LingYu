"""任务栏橙色检测：不读数据库、不要求微信窗口可见。

微信有未读消息时，任务栏上的微信按钮会变成橙色背景；未读清零后恢复默认
浅色。实测橙色像素占比：有未读 49%，无未读 0.0%，区分度极高。

检测方式：用 BitBlt 直接抓任务栏上那一小块（约 98x80），统计橙色像素比例。
单次约 16.7ms（受屏幕刷新周期限制），500ms 轮询 CPU 占用约 3%。

用法：
    python build/wx_unread_watch.py --duration 300
"""

from __future__ import annotations

import argparse
import ctypes
import time

import uiautomation as auto
import win32con
import win32gui
import win32ui

ctypes.windll.user32.SetProcessDPIAware()

# 微信任务栏按钮的橙色（Windows 11 强调色）：R 高、G 中、B 低
ORANGE_R_MIN = 200
ORANGE_G_RANGE = (140, 210)
ORANGE_B_RANGE = (80, 160)

THRESHOLD = 0.05  # 橙色占比超过 5% 判定为「有未读」


def find_button() -> tuple[int, int, int, int] | None:
    tl = auto.ToolBarControl(ClassName="MSTaskListWClass")
    if not tl.Exists(0.5):
        return None
    for k in tl.GetChildren():
        if "微信" in (k.Name or ""):
            r = k.BoundingRectangle
            return (r.left, r.top, r.right, r.bottom)
    return None


class Grabber:
    """复用一个位图，避免每次重建 GDI 对象。"""

    def __init__(self, box):
        x, y, r, b = box
        self.box = box
        self.w, self.h = r - x, b - y
        hdc = win32gui.GetDC(0)
        self.mfc = win32ui.CreateDCFromHandle(hdc)
        self.save = self.mfc.CreateCompatibleDC()
        self.bmp = win32ui.CreateBitmap()
        self.bmp.CreateCompatibleBitmap(self.mfc, self.w, self.h)
        self.save.SelectObject(self.bmp)
        self._hdc = hdc

    def orange_ratio(self) -> float:
        x, y = self.box[0], self.box[1]
        self.save.BitBlt((0, 0), (self.w, self.h), self.mfc, (x, y), win32con.SRCCOPY)
        raw = self.bmp.GetBitmapBits(True)
        n = 0
        total = len(raw) // 4
        for i in range(0, len(raw), 4):
            bb = raw[i]
            gg = raw[i + 1]
            rr = raw[i + 2]
            if rr > ORANGE_R_MIN and ORANGE_G_RANGE[0] < gg < ORANGE_G_RANGE[1] \
                    and ORANGE_B_RANGE[0] < bb < ORANGE_B_RANGE[1]:
                n += 1
        return n / total if total else 0.0

    def close(self):
        try:
            win32gui.DeleteObject(self.bmp.GetHandle())
            self.save.DeleteDC()
            self.mfc.DeleteDC()
            win32gui.ReleaseDC(0, self._hdc)
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=300.0)
    ap.add_argument("--interval", type=float, default=0.5)
    args = ap.parse_args()

    auto.InitializeUIAutomationInCurrentThread()
    box = find_button()
    if not box:
        print("没找到任务栏微信按钮（微信没运行？）", flush=True)
        return 2
    print(f"[init] button={box}", flush=True)

    g = Grabber(box)
    ratio = g.orange_ratio()
    state = ratio > THRESHOLD
    print(f"[base] 橙色={ratio*100:.1f}%  未读={'有' if state else '无'}", flush=True)

    deadline = time.time() + args.duration
    polls = 0
    changes = 0
    costs = []

    while time.time() < deadline:
        time.sleep(args.interval)
        polls += 1
        t0 = time.perf_counter()
        ratio = g.orange_ratio()
        costs.append((time.perf_counter() - t0) * 1000)
        now = ratio > THRESHOLD
        if now != state:
            changes += 1
            print(f"[{'收到未读' if now else '未读清零'}] {time.strftime('%H:%M:%S')} "
                  f"橙色={ratio*100:.1f}%", flush=True)
            state = now

    avg = sum(costs) / len(costs) if costs else 0
    print(f"[done] 轮询 {polls} 次，变化 {changes} 次，平均 {avg:.1f}ms/次", flush=True)
    g.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
