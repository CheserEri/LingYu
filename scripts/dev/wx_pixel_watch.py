"""纯像素判断微信有没有新消息：不读取任何数据库。

信号来源是微信窗口里的红色未读徽标：
  * 左侧导航栏微信图标右上角的未读总数；
  * 会话列表首项的未读徽标。

用 PrintWindow 直接把窗口渲染到内存位图，不要求窗口在前台（但**不能最小化**，
最小化后拿到的是空白图）。

用法：
    python build/wx_pixel_watch.py --duration 120
"""

from __future__ import annotations

import argparse
import ctypes
import time

import win32gui
import win32ui
from PIL import Image, ImageChops

# 微信未读徽标的红色：#FA5151 / #F74C31 一类，留足余量
RED_HI = [255 if v > 170 else 0 for v in range(256)]
RED_LO = [255 if v < 100 else 0 for v in range(256)]

# 窗口内区域（按 829x584 的基准尺寸，按实际窗口等比缩放）
NAV_BOX = (0.00, 0.26, 0.19, 1.00)    # 左侧导航栏
FIRST_BOX = (0.19, 0.26, 0.80, 0.40)  # 会话列表首项


def find_wechat() -> int | None:
    hits = []

    def cb(h, _):
        try:
            if win32gui.GetClassName(h) != "Qt51514QWindowIcon":
                return True
            if win32gui.GetWindowText(h) != "微信":
                return True
            if not win32gui.IsWindowVisible(h):
                return True
            if win32gui.IsIconic(h):
                return True
            hits.append(h)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return hits[0] if hits else None


def grab(hwnd: int) -> Image.Image:
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    dc = win32gui.GetWindowDC(hwnd)
    mfc = win32ui.CreateDCFromHandle(dc)
    save = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, w, h)
    save.SelectObject(bmp)
    ctypes.windll.user32.PrintWindow(hwnd, save.GetSafeHdc(), 2)
    info = bmp.GetInfo()
    raw = bmp.GetBitmapBits(True)
    img = Image.frombuffer(
        "RGB", (info["bmWidth"], info["bmHeight"]), raw, "raw", "BGRX", 0, 1
    ).copy()
    win32gui.DeleteObject(bmp.GetHandle())
    save.DeleteDC()
    mfc.DeleteDC()
    win32gui.ReleaseDC(hwnd, dc)
    return img


def red_pixels(img: Image.Image) -> int:
    r, g, b = img.split()
    mask = ImageChops.multiply(
        ImageChops.multiply(r.point(RED_HI), g.point(RED_LO)), b.point(RED_LO)
    )
    return mask.histogram()[255]


def crop_box(img: Image.Image, box) -> Image.Image:
    w, h = img.size
    x0, y0, x1, y1 = box
    return img.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))


def measure(hwnd: int) -> tuple[int, int, float]:
    t0 = time.perf_counter()
    img = grab(hwnd)
    nav = red_pixels(crop_box(img, NAV_BOX))
    first = red_pixels(crop_box(img, FIRST_BOX))
    return nav, first, (time.perf_counter() - t0) * 1000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--tolerance", type=float, default=0.05, help="变化比例阈值")
    args = ap.parse_args()

    hwnd = find_wechat()
    if not hwnd:
        print("没找到可用的微信主窗口（可能最小化或未登录）")
        return 2
    print(f"[init] hwnd={hwnd} rect={win32gui.GetWindowRect(hwnd)}")

    nav0, first0, cost = measure(hwnd)
    print(f"[base] 导航栏={nav0} 首项={first0} 单次 {cost:.1f}ms")
    base = (nav0, first0)

    deadline = time.time() + args.duration
    polls = 0
    events = 0
    costs = []

    while time.time() < deadline:
        time.sleep(args.interval)
        nav, first, cost = measure(hwnd)
        polls += 1
        costs.append(cost)

        def changed(now, before):
            if before <= 0:
                return now > 0
            return abs(now - before) / max(before, 1) > args.tolerance

        if changed(nav, base[0]) or changed(first, base[1]):
            events += 1
            print(f"[变化] 导航栏 {base[0]} -> {nav}   首项 {base[1]} -> {first}   "
                  f"({time.strftime('%H:%M:%S')})")
            base = (nav, first)

    avg = sum(costs) / len(costs) if costs else 0
    print(f"[done] 轮询 {polls} 次，变化 {events} 次，平均 {avg:.1f}ms/次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
