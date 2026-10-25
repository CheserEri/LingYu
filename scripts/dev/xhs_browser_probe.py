"""用浏览器桥探测小红书：登录墙是否存在、__INITIAL_STATE__ 里有哪些数据。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from lingyu.bot import browser  # noqa: E402

url = sys.argv[1] if len(sys.argv) > 1 else "https://www.xiaohongshu.com/explore"
print("navigate:", url)
print("browser:", browser.find_browser())

page = browser.BrowserSession(logger=lambda lvl, msg: print(f"[{lvl}] {msg}"), timeout=40.0)
try:
    page.start()
    page.navigate(url)
    page.wait_ready(timeout=25.0)
    print("location:", page.evaluate("location.href"))
    print("title:", page.evaluate("document.title"))
    print(
        "state keys:",
        page.evaluate("JSON.stringify(Object.keys(window.__INITIAL_STATE__ || {}))"),
    )
    print(
        "note key:",
        page.evaluate("JSON.stringify(Object.keys((window.__INITIAL_STATE__||{}).note || {}))"),
    )
    print(
        "noteDetailMap:",
        page.evaluate(
            "JSON.stringify(Object.keys(((window.__INITIAL_STATE__||{}).note||{}).noteDetailMap||{}))"
        ),
    )
    print("login wall:", page.evaluate("!!document.querySelector('.login-container, .login-modal')"))
    print(
        "dom:",
        page.evaluate(
            "JSON.stringify({title: (document.querySelector('#detail-title')||{}).innerText,"
            " desc: ((document.querySelector('#detail-desc')||{}).innerText||'').slice(0,80),"
            " imgs: document.querySelectorAll('.swiper-slide img, .note-slider-img').length,"
            " og: (document.querySelector('meta[property=\"og:image\"]')||{}).content})"
        ),
    )
    raw = page.evaluate("(window.__INITIAL_STATE__||{}).note ? JSON.stringify(window.__INITIAL_STATE__.note).slice(0,1200) : ''")
    print("note json head:", raw)
finally:
    page.close(blocking=True)
