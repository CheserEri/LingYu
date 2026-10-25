"""验证 OCR 噪声清洗：先跑纯函数用例，再在真实微信窗口上测定位命中率。

只读（置前 + 截图 + OCR），不点击、不发送。
"""

import ctypes
import importlib.util
import sys
import time
from ctypes import wintypes
from pathlib import Path

import uiautomation  # noqa: F401  复现 STA 主线程

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "wechat_bridge_probe", ROOT / "packaging" / "wechat_bridge.py"
)
bridge = importlib.util.module_from_spec(spec)
sys.modules["wechat_bridge_probe"] = bridge
spec.loader.exec_module(bridge)

bridge._install_wechatauto_uia_compat()

from wechatauto.guia import WeChatGUI  # noqa: E402

print("== 1. 纯函数用例 ==", flush=True)
cases = [
    ("．．绝区", "绝区零群", True),
    ("．．三角", "三角洲群", True),
    ("文件传", "文件传输助手", True),
    ("文件传输助手", "文件传输助手", True),
    ("漆零", "漆零", True),
    ("[文件]pha", "文件传输助手", False),
    ("0到", "绝区零群", False),
    ("魯", "腾讯新闻", False),
]
passed = 0
for ocr_name, target, expect in cases:
    got = WeChatGUI._name_matches(ocr_name, target)
    ok = got == expect
    passed += ok
    print(
        f"   {'PASS' if ok else 'FAIL'}  {ocr_name!r:12} vs {target!r:14} "
        f"-> {got} (期望 {expect})  清洗后={bridge._clean_ocr_name(ocr_name)!r}",
        flush=True,
    )
print(f"   {passed}/{len(cases)} 通过", flush=True)

print("\n== 2. 真实窗口定位 ==", flush=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

gui = WeChatGUI()
user32.ShowWindow(gui.main_hwnd, 9)
user32.SetForegroundWindow(gui.main_hwnd)
time.sleep(0.8)

sessions = gui.get_sessions()
names = [s["name"] for s in sessions]
print(f"   识别到 {len(sessions)} 个会话: {names}", flush=True)

targets = ["文件传输助手", "漆零", "绝区零群", "三角洲群", "腾讯新闻"]
hit = 0
for target in targets:
    t = time.time()
    pos = gui.find_session(target, max_scroll=0)
    hit += bool(pos)
    print(f"   {target!r:14} -> {pos}  ({time.time()-t:.2f}s)", flush=True)
print(f"\n   命中 {hit}/{len(targets)}", flush=True)
