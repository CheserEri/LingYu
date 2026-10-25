"""探测各字体对多语言字符的覆盖情况（用 notdef 掩码对比法）。"""
from __future__ import annotations

from PIL import ImageFont

FONTS = {
    "msyh": r"C:\Windows\Fonts\msyh.ttc",
    "segoeui": r"C:\Windows\Fonts\segoeui.ttf",
    "YuGothR": r"C:\Windows\Fonts\YuGothR.ttc",
    "malgun": r"C:\Windows\Fonts\malgun.ttf",
    "Nirmala": r"C:\Windows\Fonts\Nirmala.ttf",
    "LeelawUI": r"C:\Windows\Fonts\LeelawUI.ttf",
    "seguisym": r"C:\Windows\Fonts\seguisym.ttf",
    "seguiemj": r"C:\Windows\Fonts\seguiemj.ttf",
    "simsun": r"C:\Windows\Fonts\simsun.ttc",
    "Deng": r"C:\Windows\Fonts\Deng.ttf",
}

CHARS = {
    "汉": "中文字符",
    "kana": "あア",
    "hangul": "한글",
    "arabic": "م",
    "hebrew": "א",
    "thai": "ส",
    "devanagari": "न",
    "cyrillic": "П",
    "greek": "Ω",
    "emoji": "🎉",
    "symbol": "✓",
    "latin": "A",
}


def mask_bytes(font: ImageFont.FreeTypeFont, ch: str) -> bytes:
    from PIL import Image

    return Image.Image()._new(font.getmask(ch)).tobytes()


def notdef(font: ImageFont.FreeTypeFont) -> bytes:
    return mask_bytes(font, "\U0010FFFD")


for name, path in FONTS.items():
    try:
        font = ImageFont.truetype(path, 25)
    except OSError as exc:
        print(f"{name}: 加载失败 {exc}")
        continue
    blank = notdef(font)
    row = []
    for label, text in CHARS.items():
        ok = any(mask_bytes(font, ch) != blank for ch in text)
        row.append(f"{label}={'Y' if ok else 'n'}")
    print(f"{name:10s} " + " ".join(row))
