"""采样消息区像素颜色，判断能否用背景色切分气泡边界。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
from collections import Counter

import win32gui
from PIL import ImageGrab

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

SPI_GETWORKAREA = 0x0030


def work_area():
    r = ctypes.wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0)
    return r.left, r.top, r.right, r.bottom


def main() -> int:
    hwnd = win32gui.FindWindow("Qt51514QWindowIcon", "微信")
    if not hwnd:
        print("没找到微信")
        return 1
    wl, wt, wr, wb = work_area()
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    box = (max(l, wl), max(t, wt), min(r, wr), min(b, wb))
    img = ImageGrab.grab(bbox=box, all_screens=True)
    w, h = img.size

    area = img.crop((int(w * 0.306), int(h * 0.125), int(w * 1.0), int(h * 0.935)))
    aw, ah = area.size
    rgb = area.convert("RGB")
    px = rgb.load()

    print(f"窗口 {box} 消息区 {area.size}")

    # 每行的主色（出现最多的颜色）
    print("\n每行主色（每 20 行采样一次）：")
    for y in range(0, ah, 20):
        c = Counter()
        for x in range(0, aw, 4):
            c[px[x, y]] += 1
        top, cnt = c.most_common(1)[0]
        ratio = cnt / (aw // 4)
        print(f"  y={y:>4}  {top}  {ratio:.0%}")

    print("\n整图颜色直方图 top10：")
    c = Counter()
    for y in range(0, ah, 2):
        for x in range(0, aw, 2):
            c[px[x, y]] += 1
    for col, cnt in c.most_common(10):
        print(f"  {col}  {cnt}")

    # 绿色气泡检测
    green = 0
    for y in range(0, ah, 2):
        for x in range(0, aw, 2):
            r_, g_, b_ = px[x, y]
            if 130 < r_ < 180 and 210 < g_ < 250 and 90 < b_ < 140:
                green += 1
    print(f"\n绿色气泡像素（抽样）: {green}")

    area.save("build/out/msg_area_raw.png")
    print("已存 build/out/msg_area_raw.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
