"""端到端测试：禁用 UIA 后，靠纯 OCR + 像素坐标能否真的发出消息。

强制堵死两条 UIA 通路：
  1. bridge._uia_available = False        → 跳过 _send_text_with_uia 快路径
  2. gui._get_uia = lambda: None          → wechatauto 内部也拿不到 UIA

剩下只有：OCR 找会话 → 点击 → 像素探测输入框 → 粘贴 → 点发送。
"""

import asyncio
import importlib.util
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "wechat_bridge_e2e", ROOT / "packaging" / "wechat_bridge.py"
)
B = importlib.util.module_from_spec(spec)
sys.modules["wechat_bridge_e2e"] = B
spec.loader.exec_module(B)

TARGET = "文件传输助手"
MSG = f"[灵语 OCR 端到端测试] {time.strftime('%H:%M:%S')}"

# 适配层会把发送超时放宽到 20 秒（WECHAT_SEND_TIMEOUT 默认值）；直接构造
# bridge 时不走适配层，这里手动对齐，否则纯 OCR 路径必然 5 秒超时。
B.WeChatBridge.SEND_TIMEOUT_SECONDS = 20.0


async def main() -> None:
    bridge = B.WeChatBridge(
        target_groups=[],
        on_message=None,
        reply_delay=0.0,
        backend="wechatauto",
        listen_private=False,
    )
    print("== connect ==", flush=True)
    t = time.time()
    ok = await bridge.connect()
    print(f"   connect={ok} backend={bridge._backend} ({time.time()-t:.1f}s)", flush=True)
    if not ok:
        print("连接失败，终止", flush=True)
        return

    gui = bridge._wx._gui
    try:
        before = bridge._target_seq(TARGET)
    except Exception as exc:
        before = -1
        print(f"   _target_seq 取不到: {type(exc).__name__}: {exc}", flush=True)
    print(f"   发送前 seq={before}", flush=True)

    # 堵死 UIA 两条路
    bridge._uia_available = False
    gui._uia = None
    gui._uia_tried = True
    gui._get_uia = lambda *a, **k: None
    print(f"   UIA 已禁用: {gui._get_uia() is None}", flush=True)

    print(f"== 发送到 [{TARGET}] ==", flush=True)
    results = []
    for round_no in range(1, 4):
        msg = f"{MSG} #{round_no}"
        print(f"   [{round_no}] 内容: {msg}", flush=True)
        t = time.time()
        try:
            sent = await bridge.send_text(TARGET, msg)
        except Exception as exc:
            sent = False
            print(f"       抛异常: {type(exc).__name__}: {exc}", flush=True)
        elapsed = time.time() - t
        await asyncio.sleep(1.5)
        try:
            after = bridge._target_seq(TARGET)
        except Exception:
            after = -1
        delta = after - before
        print(f"       send={sent}  {elapsed:.2f}s  seq delta={delta}", flush=True)
        results.append((round_no, bool(sent), delta > 0, elapsed))
        before = after

    print("\n== 汇总 ==", flush=True)
    for round_no, sent, db_ok, elapsed in results:
        print(
            f"   第{round_no}轮: send={sent} db={db_ok} {elapsed:.2f}s "
            f"{'PASS' if sent and db_ok else 'FAIL'}",
            flush=True,
        )
    ok_count = sum(1 for _, s, d, _ in results if s and d)
    print(f"\nRESULT: {ok_count}/{len(results)} 成功", flush=True)

    await bridge.disconnect()


asyncio.run(main())
