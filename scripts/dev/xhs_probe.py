"""探测小红书页面结构：__INITIAL_STATE__ 是否可取、字段长什么样。"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

url = sys.argv[1] if len(sys.argv) > 1 else "https://www.xiaohongshu.com/explore"
print("URL:", url)

req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        final = resp.geturl()
        html = resp.read().decode("utf-8", "replace")
    print("final:", final)
    print("len:", len(html))
    print("has __INITIAL_STATE__:", "__INITIAL_STATE__" in html)
    for key in ("noteDetailMap", "og:title", "og:image", "note_id", "xsec_token"):
        print(f"  contains {key}:", key in html)
    idx = html.find("__INITIAL_STATE__")
    if idx >= 0:
        print("--- 片段 ---")
        print(html[idx : idx + 600])
except Exception as exc:
    print("请求失败：", exc)
