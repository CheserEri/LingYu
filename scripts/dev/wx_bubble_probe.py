"""检测消息区气泡边界：背景 (250,250,250)，对方气泡纯白 (255,255,255)。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys

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


def is_white(p) -> bool:
    """对方气泡实测是 (238,238,240)，比背景 (250,250,250) 略深。"""
    r, g, b = p
    return 230 <= r <= 245 and 230 <= g <= 245 and 232 <= b <= 247


def is_green(p) -> bool:
    r, g, b = p
    return 130 <= r <= 185 and 205 <= g <= 250 and 85 <= b <= 150


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
    px = area.convert("RGB").load()

    # 每行统计白色/绿色像素数
    rows_white = []
    rows_green = []
    for y in range(ah):
        nw = ng = 0
        for x in range(0, aw, 2):
            p = px[x, y]
            if is_white(p):
                nw += 1
            elif is_green(p):
                ng += 1
        rows_white.append(nw)
        rows_green.append(ng)

    def bands(counts, thr):
        out = []
        y = 0
        while y < len(counts):
            if counts[y] < thr:
                y += 1
                continue
            y0 = y
            while y < len(counts) and counts[y] >= thr:
                y += 1
            out.append((y0, y - 1, max(counts[y0:y])))
        return out

    print(f"消息区 {area.size}")
    print("\n白色气泡行带（阈值 30）：")
    for y0, y1, peak in bands(rows_white, 30):
        if y1 - y0 < 8:
            continue
        # 该带内白像素的 x 范围
        xs = []
        for yy in range(y0, y1 + 1, 4):
            for x in range(aw):
                if is_white(px[x, yy]):
                    xs.append(x)
        if not xs:
            continue
        print(f"  y={y0}..{y1} 高={y1-y0+1} 峰值={peak}  x={min(xs)}..{max(xs)}")

    print("\n绿色气泡行带（阈值 10）：")
    for y0, y1, peak in bands(rows_green, 10):
        if y1 - y0 < 8:
            continue
        print(f"  y={y0}..{y1} 高={y1-y0+1} 峰值={peak}")

    # 深色文字行
    rows_text = []
    for y in range(ah):
        n = 0
        for x in range(0, aw, 2):
            r_, g_, b_ = px[x, y]
            if r_ < 120 and g_ < 120 and b_ < 120:
                n += 1
        rows_text.append(n)
    print("\n深色文字行带（阈值 6）：")
    for y0, y1, peak in bands(rows_text, 6):
        print(f"  y={y0}..{y1} 高={y1-y0+1} 峰值={peak}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
