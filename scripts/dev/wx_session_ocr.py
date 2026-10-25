"""OCR 读取微信会话列表，提取未读数、@标记、最后一条消息预览。

会话列表区域（相对窗口）：x 6.3%~30.6%，y 10%~100%
每项高度约 64 逻辑像素（128 物理像素）。

用法：
    python build/wx_session_ocr.py
    python build/wx_session_ocr.py --shot
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import ctypes.wintypes
import re
import sys
from dataclasses import dataclass, field
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

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

SPI_GETWORKAREA = 0x0030

LIST_X0 = 0.063
LIST_X1 = 0.306
LIST_Y0 = 0.100
LIST_Y1 = 0.995

_CJK_GAP = re.compile(
    r"(?<=[\u4e00-\u9fff\u3000-\u303f])\s+(?=[\u4e00-\u9fff\u3000-\u303f])"
)
_TIME_RE = re.compile(r"^(昨天|星期[一二三四五六日天]|星期[一二三四五六日天]|\d{1,2}:\d{2}|\d{1,2}/\d{1,2}|\d{4}/\d{1,2}/\d{1,2})$")
_AT_RE = re.compile(r"\[?有人@我\]?|\[?有人\s*@\s*我\]?")


@dataclass
class Row:
    text: str
    left: int
    top: int
    right: int
    bottom: int
    items: list[str] = field(default_factory=list)

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2


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


def grab(hwnd: int):
    wl, wt, wr, wb = work_area()
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    box = (max(l, wl), max(t, wt), min(r, wr), min(b, wb))
    return ImageGrab.grab(bbox=box, all_screens=True), box


def _clean(text: str) -> str:
    text = _CJK_GAP.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


async def ocr_rows(img) -> list[Row]:
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

    rows: list[Row] = []
    for line in result.lines:
        words = list(line.words)
        if not words:
            continue
        rects = [w.bounding_rect for w in words]
        rows.append(
            Row(
                text=_clean(line.text),
                left=int(min(r.x for r in rects)),
                top=int(min(r.y for r in rects)),
                right=int(max(r.x + r.width for r in rects)),
                bottom=int(max(r.y + r.height for r in rects)),
            )
        )
    return rows


def group_into_sessions(rows: list[Row], row_h: int) -> list[list[Row]]:
    """按 y 聚类，同一会话的多行归到一组。

    实测：同一会话内相邻行间距最大 39px，跨会话最小 87px，
    取 row_h*0.5（约 65px）作为分界很安全。
    """
    rows = [r for r in rows if r.text]
    rows.sort(key=lambda r: r.top)
    gap = row_h * 0.5
    groups: list[list[Row]] = []
    for r in rows:
        # 必须用 top 与 top 比较：OCR 行框高度约 26px，
        # 用 bottom 相减会把 87px 的跨会话间距压缩成 61px，导致误合并
        if groups and r.top - groups[-1][-1].top < gap:
            groups[-1].append(r)
        else:
            groups.append([r])
    return groups


RED_MIN_R = 200
RED_MAX_G = 110
RED_MAX_B = 110


def find_unread_dots(img) -> list[tuple[float, float, int]]:
    """在会话列表图里找红色像素簇，返回 [(cx, cy, 像素数)]。"""
    img = img.convert("RGB")
    w, h = img.size
    px = img.load()
    # 按行统计红色像素数，再按 y 聚类
    per_row: list[int] = []
    per_row_x: list[float] = []
    for y in range(h):
        cnt = 0
        sx = 0
        for x in range(w):
            r, g, b = px[x, y]
            if r > RED_MIN_R and g < RED_MAX_G and b < RED_MAX_B:
                cnt += 1
                sx += x
        per_row.append(cnt)
        per_row_x.append(sx / cnt if cnt else 0)

    clusters: list[tuple[float, float, int]] = []
    y = 0
    while y < h:
        if per_row[y] < 8:  # 少于 8 个红像素的行不算
            y += 1
            continue
        y0 = y
        total = 0
        sx = 0
        while y < h and per_row[y] >= 4:
            total += per_row[y]
            sx += per_row_x[y] * per_row[y]
            y += 1
        if total >= 60:  # 整个簇至少 60 个红像素
            clusters.append((sx / total, (y0 + y - 1) / 2, total))
    return clusters


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot", action="store_true")
    ap.add_argument("--row-h", type=int, default=128, help="会话项高度（物理像素）")
    args = ap.parse_args()

    hwnd = find_wechat()
    if not hwnd:
        print("没找到微信窗口")
        return 1
    if win32gui.IsIconic(hwnd):
        print("窗口最小化")
        return 2

    img, box = grab(hwnd)
    w, h = img.size
    rel = (
        int(w * LIST_X0),
        int(h * LIST_Y0),
        int(w * LIST_X1),
        int(h * LIST_Y1),
    )
    area = img.crop(rel)
    print(f"窗口 {box} {img.size}  会话列表裁剪 {rel} 尺寸 {area.size}")

    if args.shot:
        out = Path(__file__).parent / "out"
        out.mkdir(exist_ok=True)
        p = out / "session_list.png"
        area.save(p)
        print(f"已存 {p}")

    rows = asyncio.run(ocr_rows(area))
    groups = group_into_sessions(rows, args.row_h)
    dots = find_unread_dots(area)

    print(f"\n红色像素簇 {len(dots)} 个：")
    for cx, cy, n in dots:
        print(f"    x={cx:.0f} y={cy:.0f} 面积={n}")

    print(f"\n共 {len(groups)} 个会话块：")
    for i, g in enumerate(groups, 1):
        joined = " ".join(r.text for r in g)
        at = bool(_AT_RE.search(joined))
        times = [r.text for r in g if _TIME_RE.match(r.text.strip())]
        # 红点固定贴在会话项头像的顶边，而头像 logo 的红色在项中部。
        # 实测：真红点距标题行顶部 -6px 左右，logo 在 +54px。
        y0 = min(r.top for r in g)
        y1 = max(r.bottom for r in g)
        hit = [d for d in dots if y0 - 25 <= d[1] <= y0 + 40]
        print(f"\n  #{i}  y={y0}-{y1}  {'● 未读' if hit else '  '}")
        for r in g:
            print(f"      x={r.left:>4}..{r.right:<4} y={r.top:>4}  {r.text}")
        if at:
            print("      → 含 @我 标记")
        if times:
            print(f"      → 时间 {times}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
