"""复现推文卡片渲染问题：文字/配图挤压、非中英文字体缺字、无翻译。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image  # noqa: E402

from lingyu.bot import tweet as t  # noqa: E402

OUT = ROOT / "build" / "tweet_repro"
OUT.mkdir(parents=True, exist_ok=True)


def fake_image(url: str, timeout: float):
    if "wide" in url:
        return Image.new("RGB", (1200, 675), (40, 90, 160))
    if "tall" in url:
        return Image.new("RGB", (800, 1400), (160, 90, 40))
    return Image.new("RGB", (1000, 1000), (90, 160, 90))


t._load_image = fake_image

CASES = {
    "01_long_en_with_photo": t.Tweet(
        id="01",
        url="https://x.com/a/status/1",
        text=(
            "We just shipped the biggest update to our rendering pipeline this year. "
            "It now handles mixed scripts, right-to-left text, and emoji fallback "
            "without any external browser. This is a long sentence designed to wrap "
            "across many lines so we can see whether the body text overlaps the image."
        ),
        author_name="Jane Doe",
        author_handle="janedoe",
        verified="blue",
        created_at="2026-10-05 09:30",
        photos=["https://p/wide.jpg"],
        likes=1234,
        replies=88,
    ),
    "02_ja_with_photo": t.Tweet(
        id="02",
        url="https://x.com/a/status/2",
        text="日本語のツイートです。ひらがな・カタカナ・漢字が混ざっています。"
        "フォントのフォールバックが無いと豆腐になります。",
        author_name="山田太郎",
        author_handle="taro",
        created_at="2026-10-05 09:30",
        photos=["https://p/square.jpg"],
    ),
    "03_ko_ar_th": t.Tweet(
        id="03",
        url="https://x.com/a/status/3",
        text="한국어 테스트입니다.\nمرحبا بالعالم، هذا اختبار للنص العربي.\n"
        "สวัสดีชาวโลก นี่คือการทดสอบภาษาไทย\nनमस्ते दुनिया यह हिंदी परीक्षण है",
        author_name="Kim",
        author_handle="kim",
        created_at="2026-10-05 09:30",
    ),
    "04_multi_photo_text": t.Tweet(
        id="04",
        url="https://x.com/a/status/4",
        text="四张图的推文，看看文字和图片之间的间距是否合理。" * 3,
        author_name="某人",
        author_handle="someone",
        photos=[
            "https://p/wide.jpg",
            "https://p/tall.jpg",
            "https://p/square.jpg",
            "https://p/wide2.jpg",
        ],
    ),
    "05_ru_emoji": t.Tweet(
        id="05",
        url="https://x.com/a/status/5",
        text="Привет мир! Это тест кириллицы 🎉🚀 и emoji.",
        author_name="Иван",
        author_handle="ivan",
    ),
    # 段落空行 + 配图：旧实现里空行不计高度，正文会压到图上
    "06_blank_lines_with_photo": t.Tweet(
        id="06",
        url="https://x.com/a/status/6",
        text="第一段：这里说明背景。\n\n第二段：这里说明做法。\n\n第三段：这里说明结论。",
        author_name="段落测试",
        author_handle="para",
        created_at="2026-10-05 09:30",
        photos=["https://p/wide.jpg"],
        likes=10,
    ),
    # 带译文：译文当正文，原文缩成灰色引用块
    "07_translated": t.Tweet(
        id="07",
        url="https://x.com/a/status/7",
        text="We just shipped the biggest update to our rendering pipeline this year. "
        "It now handles mixed scripts, right-to-left text, and emoji fallback.",
        translation="我们刚刚发布了今年渲染管线最大的一次更新。它现在能处理混排文字、"
        "从右到左书写的语言，以及 emoji 回退，全程不依赖任何外部浏览器。",
        author_name="Jane Doe",
        author_handle="janedoe",
        verified="blue",
        created_at="2026-10-05 09:30",
        photos=["https://p/wide.jpg"],
        likes=1234,
        replies=88,
    ),
    # 超长昵称 / 超长 URL 单词
    "08_long_name_url": t.Tweet(
        id="08",
        url="https://x.com/a/status/8",
        text="Look at this: https://example.com/very/long/path/that/never/breaks/"
        "and/keeps/going/forever/until/it/overflows/the/card/width?a=1&b=2&c=3",
        author_name="A Very Long Display Name That Should Be Truncated Nicely",
        author_handle="a_very_long_handle_name",
        created_at="2026-10-05 09:30",
    ),
}

for name, tw in CASES.items():
    path = t.render_card(tw, OUT, timeout=5)
    print(name, "->", path)
