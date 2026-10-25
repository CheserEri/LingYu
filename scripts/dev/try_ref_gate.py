"""借用参考实现的热激活流程，判定 4.1.15 是否支持。

只做诊断，不进入生产代码路径。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechatauto.uia_driver import (  # noqa: E402
    WeChatUIA,
    _VERIFIED_GATE_RVA,
)


def main() -> int:
    uia = WeChatUIA()
    hwnds = uia._wechat_hwnds()
    print(f"微信主窗口: {hwnds}")
    if not hwnds:
        print("没找到微信主窗口")
        return 2

    hwnd = hwnds[0]
    pid = uia._pid_from_hwnd(hwnd)
    mod = uia._weixin_dll_module(pid)
    print(f"pid={pid} module={mod}")
    if not mod:
        return 3
    base, size, dll_path = mod
    print(f"base=0x{base:X} size=0x{size:X}")
    print(f"dll={dll_path}")
    print(f"version_dir={os.path.basename(os.path.dirname(dll_path))}")

    cands = uia._qaccessible_candidate_rvas(dll_path)
    print(f"\n候选 RVA 数量 = {len(cands)}")
    for i, rva in enumerate(cands[:12]):
        print(f"  [{i}] Weixin.dll+0x{rva:X}")

    print("\n--- 开始热激活 ---")
    ok = uia._hot_activate_accessibility(hwnd, verify=True)
    print(f"hot_activate 结果 = {ok}")
    print(f"VERIFIED_GATE_RVA = {_VERIFIED_GATE_RVA}")

    print("\n--- mmui 检查 ---")
    print(f"_mmui_present = {WeChatUIA._mmui_present(hwnd, timeout=2.0)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
