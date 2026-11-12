"""逐会话的情绪状态持久化。

上游只有一份 ``state/session.json``（单会话、单身份）。微信机器人同时面对几十个
私聊和群，必须按会话分片——否则「张三惹我生气」会直接改变「李四」那一轮的读法。

落盘位置：``%APPDATA%\\lingyu\\emotion\\<hash>.json``（可用
``WECHAT_BOT_EMOTION_DIR`` 覆盖，测试靠它隔离）。三个约束：

1. **有界**：单会话状态超过 ``MAX_STATE_BYTES`` 时从最旧的记忆开始裁，
   避免长期运行后每次读写都是几百 KB 的全量 JSON；
2. **有上限**：会话数超过 ``max_chats`` 时按最后活跃时间淘汰最旧的，
   不然一年下来目录里会攒几千个文件；
3. **读一次就够**：内存里缓存，进程内不重复读盘；损坏的文件当作「没有状态」
   处理（重新开始一段关系，而不是让整条回复链路崩掉）。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .core.util import sha

#: 当前 state schema；旧 schema 直接当作「没有状态」处理。
SCHEMA = 2

#: 单会话状态的文件体积上限；超出后从最旧的记忆开始裁。
MAX_STATE_BYTES = 256 * 1024

#: 同时保留多少个会话的情绪状态。
MAX_CHATS = 300

#: 体积超限时每次裁剪的粒度。
_TRIM_STEP = 40


def default_state_dir() -> Path:
    raw = os.environ.get("WECHAT_BOT_EMOTION_DIR", "").strip()
    if raw:
        return Path(raw)
    appdata = os.environ.get("APPDATA", "").strip()
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "lingyu" / "emotion"


class SessionStore:
    """按会话键读写情绪状态。"""

    def __init__(self, root: Path | str | None = None, *, max_chats: int = MAX_CHATS):
        self.root = Path(root) if root else default_state_dir()
        self.max_chats = max(1, int(max_chats))
        self._cache: dict[str, dict[str, Any]] = {}
        self._dirty: set[str] = set()

    # ---------- 路径 ----------

    def path_for(self, chat_key: str) -> Path:
        return self.root / f"{sha(str(chat_key), 16)}.json"

    # ---------- 读 ----------

    def load(self, chat_key: str) -> dict[str, Any]:
        key = str(chat_key)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        data = self._read_file(self.path_for(key))
        if data is None or not _is_current(data):
            data = {}
        self._cache[key] = data
        return data

    @staticmethod
    def _read_file(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    # ---------- 写 ----------

    def save(
        self,
        chat_key: str,
        *,
        turn: int,
        state: dict[str, Any],
        chat_name: str = "",
        last_kind: str = "",
        last_at: float | None = None,
        display: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = str(chat_key)
        payload = {
            "schema": SCHEMA,
            "key": key,
            "chat": chat_name or key,
            "turn": int(turn),
            "state": _shrink(state),
            "last_kind": last_kind,
            "last_at": float(last_at if last_at is not None else time.time()),
            "updated_at": time.time(),
        }
        if display:
            # 展示用的摘要单独存：`emotion.candidates` 这类字段是每轮重算的
            # 瞬时段，不参与跨轮恢复，但界面要能看到「上一轮是什么心情」。
            payload["display"] = dict(display)
        self._cache[key] = payload
        self._dirty.add(key)
        self._write(key, payload)
        self._enforce_count()
        return payload

    def touch(self, chat_key: str, **fields: Any) -> None:
        """更新少量元数据（例如 last_at），不重写整份状态。"""
        key = str(chat_key)
        payload = self._cache.get(key) or self.load(key)
        if not payload:
            return
        payload.update(fields)
        payload["updated_at"] = time.time()
        self._dirty.add(key)
        self._write(key, payload)

    def _write(self, key: str, payload: dict[str, Any]) -> None:
        path = self.path_for(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix=f".{path.stem}-",
                suffix=".tmp",
                dir=str(path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._dirty.discard(key)
        except OSError:
            # 写盘失败不该影响这一轮回复：内存里还有状态，下次再试。
            pass

    # ---------- 清理 ----------

    def clear(self, chat_key: str) -> bool:
        key = str(chat_key)
        existed = key in self._cache or self.path_for(key).is_file()
        self._cache.pop(key, None)
        self._dirty.discard(key)
        try:
            self.path_for(key).unlink(missing_ok=True)
        except OSError:
            pass
        return existed

    def clear_all(self) -> int:
        count = len(self.list_chats())
        self._cache.clear()
        self._dirty.clear()
        try:
            for path in self.root.glob("*.json"):
                path.unlink(missing_ok=True)
        except OSError:
            pass
        return count

    def list_chats(self) -> list[dict[str, Any]]:
        """已知会话清单（内存优先，再补磁盘上还没载入的）。

        文件名是会话键的哈希，所以「真正的键」只能从文件内容里读回来——
        不能拿 ``path.stem`` 当键，否则同一个会话会在清单里出现两次。
        """
        seen: dict[str, dict[str, Any]] = {}
        try:
            paths = list(self.root.glob("*.json"))
        except OSError:
            paths = []
        for path in paths:
            data = self._read_file(path) or {}
            if not _is_current(data):
                continue
            key = str(data.get("key") or path.stem)
            seen[key] = _entry(key, data)
        for key, data in self._cache.items():
            if not data or not _is_current(data):
                continue
            seen[key] = _entry(key, data)
        return sorted(seen.values(), key=lambda item: item["updated_at"], reverse=True)

    def _enforce_count(self) -> None:
        chats = self.list_chats()
        if len(chats) <= self.max_chats:
            return
        for item in chats[self.max_chats:]:
            self.clear(item["key"])


def _entry(key: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": key,
        "chat": data.get("chat") or key,
        "turn": int(data.get("turn") or 0),
        "last_at": data.get("last_at"),
        "updated_at": data.get("updated_at") or 0.0,
    }


def _is_current(data: dict[str, Any]) -> bool:
    try:
        return int(data.get("schema") or 0) == SCHEMA
    except (TypeError, ValueError):
        return False


def _shrink(state: dict[str, Any]) -> dict[str, Any]:
    """状态超过体积上限时，从最旧的情节记忆开始裁。

    只裁 ``episodes``：心境、关系与兴趣是下一轮真正要读的东西，不能动。
    """
    data = dict(state)
    try:
        size = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return data
    if size <= MAX_STATE_BYTES:
        return data

    episodes = data.get("episodes")
    if isinstance(episodes, list) and len(episodes) > _TRIM_STEP:
        data["episodes"] = episodes[-_TRIM_STEP:]
    return data
