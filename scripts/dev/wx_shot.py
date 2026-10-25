"""抓微信整窗截图，便于人工核对布局与坐标。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
from pathlib import Path

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
        print("没找到微信窗口")
        return 1
    if win32gui.IsIconic(hwnd):
        print("窗口最小化")
        return 2

    wl, wt, wr, wb = work_area()
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    box = (max(l, wl), max(t, wt), min(r, wr), min(b, wb))
    img = ImageGrab.grab(bbox=box, all_screens=True)
    out = Path(__file__).parent / "out"
    out.mkdir(exist_ok=True)
    p = out / "wechat_full.png"
    img.save(p)
    print(f"窗口 {box} 尺寸 {img.size} → {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
