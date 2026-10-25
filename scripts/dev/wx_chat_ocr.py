"""按会话名定位并点击，再 OCR 读取消息正文。

设计要点（踩过的坑）：
1. ImageGrab 抓的是屏幕，微信被遮挡时会读到别的内容
   → 点击前必须置前 + 用 WindowFromPoint 检查遮挡，失败就中止，绝不盲目点击
2. 按序号算 y 坐标非常脆弱（裁剪坐标 vs 窗口坐标差 124px，错一位就点错会话）
   → 改为 OCR 会话列表、按名称匹配、用匹配行的 y 定位
3. 群聊消息的发送者昵称在气泡上方，字号小、颜色浅，OCR 可能读不到
   → 方向判断退化为「气泡靠左/靠右」，昵称作为可选信息

用法：
    python build/wx_chat_ocr.py --list
    python build/wx_chat_ocr.py --name 原神群
    python build/wx_chat_ocr.py --name 原神群 --shot
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import ctypes.wintypes
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import win32api
import win32con
import win32gui
from PIL import ImageGrab
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import (
    BitmapAlphaMode,
    BitmapPixelFormat,
    SoftwareBitmap,
)
from winrt.windows.media.ocr import OcrEngine

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

SPI_GETWORKAREA = 0x0030

# 窗口内相对布局（本机 200% DPI、窗口 2160x1240 物理像素实测）
LIST_X0, LIST_X1 = 0.063, 0.306
LIST_Y0, LIST_Y1 = 0.100, 0.995
MSG_X0, MSG_X1 = 0.306, 1.000
MSG_Y0, MSG_Y1 = 0.125, 0.935
TITLE_X0, TITLE_X1 = 0.300, 0.620
TITLE_Y0, TITLE_Y1 = 0.055, 0.115

SESSION_H = 130  # 会话项高度（物理像素）

_CJK_GAP = re.compile(
    r"(?<=[\u4e00-\u9fff\u3000-\u303f])\s+(?=[\u4e00-\u9fff\u3000-\u303f])"
)
_AT_RE = re.compile(r"有人\s*@\s*我")


@dataclass
class Line:
    text: str
    left: int
    top: int
    right: int
    bottom: int


@dataclass
class Session:
    name: str
    preview: str
    top: int
    bottom: int

    @property
    def center(self) -> int:
        return (self.top + self.bottom) // 2


def work_area():
    r = ctypes.wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0)
    return r.left, r.top, r.right, r.bottom


def find_wechat() -> int:
    for cls in ("Qt51514QWindowIcon", "mmui::MainWindow", "WeChatMainWndForPC"):
        hwnd = win32gui.FindWindow(cls, "微信")
        if hwnd:
            return hwnd
    return 0


def window_box(hwnd: int):
    wl, wt, wr, wb = work_area()
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    return (max(l, wl), max(t, wt), min(r, wr), min(b, wb))


def force_foreground(hwnd: int, timeout: float = 1.0) -> bool:
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.25)
    if win32gui.GetForegroundWindow() == hwnd:
        return True
    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
    win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.05)
    return win32gui.GetForegroundWindow() == hwnd


def occlusion_ratio(hwnd: int, box) -> float:
    l, t, r, b = box
    xs = [l + int((r - l) * f) for f in (0.15, 0.5, 0.85)]
    ys = [t + int((b - t) * f) for f in (0.2, 0.5, 0.8)]
    bad = total = 0
    for x in xs:
        for y in ys:
            total += 1
            h = win32gui.WindowFromPoint((x, y))
            if not h:
                bad += 1
                continue
            if win32gui.GetAncestor(h, win32con.GA_ROOT) != hwnd:
                bad += 1
    return bad / total if total else 1.0


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
        raise RuntimeError("OCR 引擎创建失败")
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


def group_sessions(rows: list[Line]) -> list[Session]:
    """把会话列表的 OCR 行按项聚类。同项内行距 ≤39px，跨项 ≥87px。"""
    rows = [r for r in rows if r.text]
    rows.sort(key=lambda r: r.top)
    groups: list[list[Line]] = []
    for r in rows:
        if groups and r.top - groups[-1][-1].top < SESSION_H * 0.5:
            groups[-1].append(r)
        else:
            groups.append([r])

    out: list[Session] = []
    for g in groups:
        # 名称取最靠上且 x 偏左的行（时间戳在最右，预览在下方）
        name_cands = [r for r in g if r.left < 300]
        name = name_cands[0].text if name_cands else g[0].text
        preview = " ".join(r.text for r in g[1:])
        out.append(
            Session(
                name=name,
                preview=preview,
                top=min(r.top for r in g),
                bottom=max(r.bottom for r in g),
            )
        )
    return out


def grab(box):
    return ImageGrab.grab(bbox=box, all_screens=True)


def read_sessions(box) -> list[Session]:
    img = grab(box)
    w, h = img.size
    rel = (int(w * LIST_X0), int(h * LIST_Y0), int(w * LIST_X1), int(h * LIST_Y1))
    rows = asyncio.run(ocr_lines(img.crop(rel)))
    # 返回的 y 是裁剪图坐标，转成窗口相对 y 便于后续点击
    for r in rows:
        r.top += rel[1]
        r.bottom += rel[1]
    return group_sessions(rows)


def read_chat(box, shot: Path | None = None) -> tuple[list[Line], str]:
    img = grab(box)
    w, h = img.size
    area = img.crop(
        (
            int(w * MSG_X0),
            int(h * MSG_Y0),
            int(w * MSG_X1),
            int(h * MSG_Y1),
        )
    )
    if shot:
        area.save(shot)

    title_area = img.crop(
        (
            int(w * TITLE_X0),
            int(h * TITLE_Y0),
            int(w * TITLE_X1),
            int(h * TITLE_Y1),
        )
    )
    title = " ".join(
        ln.text for ln in asyncio.run(ocr_lines(title_area)) if ln.text
    ).strip()

    lines = asyncio.run(ocr_lines(area))
    lines.sort(key=lambda ln: (ln.top, ln.left))
    return lines, title


def click(x: int, y: int, restore_cursor: bool = True) -> None:
    ox, oy = win32api.GetCursorPos()
    win32api.SetCursorPos((x, y))
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.03)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.05)
    if restore_cursor:
        win32api.SetCursorPos((ox, oy))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="只列会话")
    ap.add_argument("--name", help="点击名称包含该子串的会话")
    ap.add_argument("--shot", action="store_true")
    ap.add_argument("--wait", type=float, default=0.5)
    args = ap.parse_args()

    hwnd = find_wechat()
    if not hwnd:
        print("没找到微信窗口")
        return 1
    if win32gui.IsIconic(hwnd):
        print("窗口最小化，请先恢复")
        return 2

    if args.name:
        if not force_foreground(hwnd):
            print("无法把微信提到前台，中止")
            return 3
        box = window_box(hwnd)
        occ = occlusion_ratio(hwnd, box)
        print(f"窗口 {box}  遮挡 {occ:.0%}")
        if occ > 0.3:
            print("窗口被遮挡，中止")
            return 4
    else:
        box = window_box(hwnd)

    sessions = read_sessions(box)
    print(f"\n会话列表 {len(sessions)} 项：")
    for i, s in enumerate(sessions, 1):
        at = " [@我]" if _AT_RE.search(s.preview) else ""
        print(f"  #{i} y={s.top}-{s.bottom}  {s.name}  |  {s.preview}{at}")

    if args.list or not args.name:
        return 0

    target = next((s for s in sessions if args.name in s.name), None)
    if target is None:
        print(f"\n没找到名称含 {args.name!r} 的会话")
        return 5

    x = box[0] + int((box[2] - box[0]) * LIST_X0) + 200
    y = box[1] + target.center
    print(f"\n点击 {target.name!r} @ ({x},{y})")
    click(x, y)
    time.sleep(args.wait)

    shot = Path(__file__).parent / "out" / "chat_area.png" if args.shot else None
    if shot:
        shot.parent.mkdir(exist_ok=True)

    lines, title = read_chat(box, shot)
    area_w = int((box[2] - box[0]) * (MSG_X1 - MSG_X0))
    mid = area_w / 2

    print(f"\n会话标题 OCR：{title!r}")
    print(f"消息区 {len(lines)} 行（中线 x={mid:.0f}）：")
    for ln in lines:
        if not ln.text:
            continue
        side = "我" if (ln.left + ln.right) / 2 > mid else "他"
        print(f"  [{side}] x={ln.left:>4}..{ln.right:<4} y={ln.top:>4}  {ln.text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
