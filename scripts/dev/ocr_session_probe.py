"""小规模验证：纯 OCR 能否完成「列出会话 + 定位指定会话」。

不点击、不发送，只做识别与坐标计算。
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

bridge._install_wechatauto_ocr_compat()

from wechatauto.guia import WeChatGUI  # noqa: E402


def front(hwnd: int) -> None:
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.ShowWindow(hwnd, 9)
    u.SetForegroundWindow(hwnd)


t0 = time.time()
gui = WeChatGUI()
print(f"== WeChatGUI 构造 {time.time()-t0:.2f}s ==")
print(f"   main={gui.main_hwnd} render={gui.render_hwnd} rect={gui.render_rect}")
front(gui.main_hwnd)
time.sleep(0.6)

print("\n== get_sessions()（纯 OCR）==", flush=True)
t = time.time()
sessions = gui.get_sessions()
print(f"   {time.time()-t:.2f}s -> {len(sessions)} 个会话")
for s in sessions[:15]:
    print(f"   y={int(s['y']):4d} x={int(s['x']):4d} w={int(s['w']):3d}  {s['name'][:32]}")

print("\n== find_session() 定位测试（纯 OCR，不点击）==", flush=True)
for target in ("文件传输助手", "漆零", "绝区零群", "三角洲群"):
    t = time.time()
    pos = gui.find_session(target)
    print(f"   {target!r:16} -> {pos}  ({time.time()-t:.2f}s)")

print("\n== 放大 2 倍的 get_sessions（ocr_zoomed 路线）==", flush=True)
t = time.time()
sessions2 = gui.get_sessions(zoomed=True)
print(f"   {time.time()-t:.2f}s -> {len(sessions2)} 个会话")
for s in sessions2[:15]:
    print(f"   y={int(s['y']):4d}  {s['name'][:32]}")
