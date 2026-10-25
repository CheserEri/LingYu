"""微信新消息探测原型：只判断「有没有新消息」，不读取正文。

数据源是 session/session.db 的 SessionTable——微信每收到一条消息就会更新对应
会话的 last_timestamp / last_msg_locald_id / last_msg_sender / unread_count。
轮询这张表比扫 message_0.db 的 Msg_* 分片快两个数量级。

用法：
    python build/wx_newmsg_watch.py --duration 60
"""

from __future__ import annotations

import argparse
import time

from wechatauto import WeChatDB

SQL = (
    "SELECT username, unread_count, last_timestamp, last_msg_locald_id, "
    "last_msg_type, last_msg_sender, last_sender_display_name "
    "FROM SessionTable WHERE is_hidden=0"
)


def snapshot(conn) -> dict[str, tuple]:
    """返回 {username: (ts, local_id, unread, type, sender)}"""
    out = {}
    for row in conn.execute(SQL):
        username = row[0] or ""
        out[username] = (row[2], row[3], row[1], row[4], row[5])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=60.0, help="监听秒数")
    ap.add_argument("--interval", type=float, default=0.5, help="轮询间隔（秒）")
    args = ap.parse_args()

    t0 = time.perf_counter()
    db = WeChatDB()
    print(f"[init] WeChatDB {time.perf_counter()-t0:.2f}s  wxid={db.wxid}")

    conn = db._open("session\\session.db")
    self_wxid = db.wxid

    baseline = snapshot(conn)
    print(f"[base] {len(baseline)} 个会话，开始监听 {args.duration:.0f}s "
          f"（间隔 {args.interval*1000:.0f}ms）")

    polls = 0
    hits = 0
    last_cost = 0.0
    deadline = time.time() + args.duration

    while time.time() < deadline:
        time.sleep(args.interval)
        t1 = time.perf_counter()
        current = snapshot(conn)
        last_cost = (time.perf_counter() - t1) * 1000
        polls += 1

        for username, cur in current.items():
            prev = baseline.get(username)
            if prev is None:
                print(f"[新会话] {username} ts={cur[0]} unread={cur[2]}")
                hits += 1
                continue
            if (cur[0], cur[1]) == (prev[0], prev[1]):
                continue

            ts, local_id, unread, mtype, sender = cur
            mine = (sender or "") == self_wxid
            direction = "发出" if mine else "收到"
            stamp = time.strftime("%H:%M:%S", time.localtime(ts or 0))
            print(f"[{direction}] {username:32} {stamp} "
                  f"type={mtype} unread={unread} local_id={local_id} "
                  f"sender={sender!r}")
            hits += 1

        baseline = current

    print(f"[done] 轮询 {polls} 次，事件 {hits} 条，单次查询 {last_cost:.2f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
