"""生成推文卡片样张：翻译、多语种字形、多图竖排长图。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image, ImageDraw  # noqa: E402

from lingyu.bot import tweet as t  # noqa: E402

OUT = ROOT / "build" / "tweet_demo"
OUT.mkdir(parents=True, exist_ok=True)

# 模拟真实推文的配图比例（横图 / 竖图 / 方图 / 混排）
_SHAPES = {
    "wide": (1600, 900),
    "tall": (900, 1600),
    "square": (1000, 1000),
    "p1": (1600, 1200),
    "p2": (1600, 1100),
    "p3": (1500, 1300),
    "p4": (1600, 1000),
}


def fake_image(url: str, timeout: float):
    name = url.rsplit("/", 1)[-1].split(".")[0]
    w, h = _SHAPES[name]
    img = Image.new("RGB", (w, h), (38, 92, 158))
    draw = ImageDraw.Draw(img)
    for i in range(0, max(w, h), 90):
        draw.line([(i, 0), (i, h)], fill=(96, 156, 214), width=3)
        draw.line([(0, i), (w, i)], fill=(96, 156, 214), width=3)
    draw.text((24, 24), f"{name} {w}x{h}", fill=(255, 255, 255))
    return img


t._load_image = fake_image

samples = [
    t.Tweet(
        id="demo1",
        url="https://x.com/demo/status/1",
        text=(
            "We just shipped the biggest update to our rendering pipeline this year. "
            "It now handles mixed scripts, right-to-left text, and emoji fallback "
            "without any external browser."
        ),
        translation=(
            "我们刚刚发布了今年渲染管线最大的一次更新。它现在能处理混排文字、"
            "从右到左书写的语言，以及 emoji 回退，全程不依赖任何外部浏览器。"
        ),
        author_name="Jane Doe",
        author_handle="janedoe",
        verified="blue",
        created_at="2026-10-05 09:30",
        photos=["https://p/wide.jpg"],
        likes=1234,
        replies=88,
    ),
    t.Tweet(
        id="demo2",
        url="https://x.com/demo/status/2",
        text="新しいレンダリングパイプラインを公開しました。絵文字もフォントも問題なし。",
        translation="新的渲染管线已经上线了，emoji 和字体都没有问题。",
        author_name="山田太郎",
        author_handle="taro",
        verified="blue",
        created_at="2026-10-05 09:30",
        photos=["https://p/square.jpg"],
        likes=42,
    ),
    t.Tweet(
        id="demo3",
        url="https://x.com/demo/status/3",
        text="한국어 테스트입니다.\nمرحبا بالعالم، هذا اختبار للنص العربي.\n"
        "สวัสดีชาวโลก นี่คือการทดสอบภาษาไทย\nनमस्ते दुनिया यह हिंदी परीक्षण है",
        author_name="Kim",
        author_handle="kim",
        created_at="2026-10-05 09:30",
    ),
    # 参考效果：多图竖排合成一张长图，一条消息发完
    t.Tweet(
        id="demo4",
        url="https://x.com/demo/status/4",
        text="四张实拍图：多图不再拼裁切网格，改成按原比例竖排合成长图，一张都不切。",
        author_name="多图测试",
        author_handle="multi",
        created_at="2026-10-05 09:30",
        photos=[
            "https://p/p1.jpg",
            "https://p/p2.jpg",
            "https://p/p3.jpg",
            "https://p/p4.jpg",
        ],
        likes=321,
        replies=12,
    ),
    t.Tweet(
        id="demo5",
        url="https://x.com/demo/status/5",
        text="两张图（横图 + 竖图混排）：各自按原比例竖排，比例都不变。",
        author_name="多图测试",
        author_handle="multi",
        created_at="2026-10-05 09:30",
        photos=["https://p/wide.jpg", "https://p/tall.jpg"],
        likes=9,
    ),
]

for tw in samples:
    print(t.render_card(tw, OUT, timeout=5))
