"""验证 Windows 自带 OCR 能否读出微信消息区域，并区分消息方向。

关键结论（踩过的坑）：
1. PrintWindow 抓不到微信消息区（自绘，返回空白）→ 必须用屏幕截图 ImageGrab
2. 屏幕截图要求微信窗口在前台且不被遮挡
3. 本机 200% 缩放，不声明 DPI 感知时 GetWindowRect 返回逻辑坐标，与物理坐标差一倍
   → 必须先 SetProcessDPIAware()
4. 窗口可能延伸到任务栏下方，截图必须与工作区求交，否则会混入任务栏文本
5. Windows OCR 对中文逐字加空格，需要正则后处理
6. 方向判断：自己发的气泡靠右、对方靠左，用行包围盒中心 x 与消息区中线比较

用法：
    python build/wx_ocr_probe.py            # 识别当前会话
    python build/wx_ocr_probe.py --shot     # 同时把截图存盘
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import ctypes.wintypes
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import win32gui
from PIL import ImageGrab
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import (
    BitmapAlphaMode,
    BitmapPixelFormat,
    SoftwareBitmap,
)
from winrt.windows.media.ocr import OcrEngine

# 必须在使用任何窗口/屏幕 API 之前声明 DPI 感知
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

SPI_GETWORKAREA = 0x0030

# 消息区在窗口内的相对位置（导航 ~7%、会话列表 7~30%、消息区 30%+）
MSG_X0 = 0.30
MSG_X1 = 1.00
MSG_Y0 = 0.12
MSG_Y1 = 0.94

_CJK_GAP = re.compile(
    r"(?<=[\u4e00-\u9fff\u3000-\u303f])\s+(?=[\u4e00-\u9fff\u3000-\u303f])"
)


@dataclass
class Line:
    text: str
    left: int
    top: int
    right: int
    bottom: int

    @property
    def cx(self) -> float:
        return (self.left + self.right) / 2


def work_area() -> tuple[int, int, int, int]:
    r = ctypes.wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0)
    return r.left, r.top, r.right, r.bottom


def find_wechat() -> int:
    for cls in ("Qt51514QWindowIcon", "mmui::MainWindow", "WeChatMainWndForPC"):
        hwnd = win32gui.FindWindow(cls, "微信")
        if hwnd:
            return hwnd
    found: list[int] = []

    def _cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) == "微信":
            found.append(h)

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else 0


def grab_window(hwnd: int):
    """屏幕截图微信窗口，bbox 与工作区求交，避开任务栏。"""
    wl, wt, wr, wb = work_area()
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    box = (max(l, wl), max(t, wt), min(r, wr), min(b, wb))
    return ImageGrab.grab(bbox=box, all_screens=True), box


def crop_message_area(img, box):
    w, h = img.size
    rel = (
        int(w * MSG_X0),
        int(h * MSG_Y0),
        int(w * MSG_X1),
        int(h * MSG_Y1),
    )
    abs_box = (
        box[0] + rel[0],
        box[1] + rel[1],
        box[0] + rel[2],
        box[1] + rel[3],
    )
    return img.crop(rel), abs_box


def _clean(text: str) -> str:
    text = _CJK_GAP.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


async def ocr_lines(img) -> list[Line]:
    img = img.convert("RGBA")
    data = img.tobytes("raw", "BGRA")

    from winrt.windows.storage.streams import DataWriter

    writer = DataWriter()
    writer.write_bytes(data)
    buf = writer.detach_buffer()

    sb = SoftwareBitmap(
        BitmapPixelFormat.BGRA8, img.width, img.height, BitmapAlphaMode.PREMULTIPLIED
    )
    sb.copy_from_buffer(buf)

    engine = OcrEngine.try_create_from_language(Language("zh-Hans-CN"))
    if engine is None:
        raise RuntimeError("OCR 引擎创建失败，可能未安装中文语言包")

    result = await engine.recognize_async(sb)
    out: list[Line] = []
    for line in result.lines:
        words = list(line.words)
        if not words:
            continue
        rects = [w.bounding_rect for w in words]
        out.append(
            Line(
                text=_clean(line.text),
                left=int(min(r.x for r in rects)),
                top=int(min(r.y for r in rects)),
                right=int(max(r.x + r.width for r in rects)),
                bottom=int(max(r.y + r.height for r in rects)),
            )
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot", action="store_true", help="保存截图到 build/out")
    ap.add_argument("--full", action="store_true", help="整窗识别，不裁消息区")
    args = ap.parse_args()

    hwnd = find_wechat()
    if not hwnd:
        print("没找到微信主窗口，请确认微信已启动")
        return 1

    print(f"hwnd={hwnd} class={win32gui.GetClassName(hwnd)}")
    if win32gui.IsIconic(hwnd):
        print("窗口最小化，截图会是空白，请先恢复窗口")
        return 2

    img, box = grab_window(hwnd)
    print(f"截图 bbox {box}  尺寸 {img.size}  工作区 {work_area()}")

    if args.full:
        target, tbox = img, box
    else:
        target, tbox = crop_message_area(img, box)
        print(f"消息区 {tbox}  尺寸 {target.size}")
    # OCR 坐标相对于裁剪图，中线按裁剪图宽度算
    mid = target.size[0] / 2
    print(f"消息区中线 x={mid:.0f}（裁剪图坐标系）")

    if args.shot:
        out = Path(__file__).parent / "out"
        out.mkdir(exist_ok=True)
        p = out / "ocr_input.png"
        target.save(p)
        print(f"截图已存 {p}")

    lines = asyncio.run(ocr_lines(target))
    lines.sort(key=lambda ln: (ln.top, ln.left))

    print(f"\n识别到 {len(lines)} 行：")
    own = other = 0
    for ln in lines:
        if not ln.text:
            continue
        side = "我" if ln.cx > mid else "他"
        if ln.cx > mid:
            own += 1
        else:
            other += 1
        print(f"  [{side}] x={ln.left:>4}..{ln.right:<4} y={ln.top:>4}  {ln.text}")

    print(f"\n统计：靠右(我) {own} 行，靠左(他) {other} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
