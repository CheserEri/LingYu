"""L3 人格札记：跨会话共享、可追溯、可修订、可撤回。

与旧 ``self.addenda`` 的区别：

- 存在全局文件，不随单个会话重置；
- 每条都有来源、反例、置信度与修订历史；
- 每天最多新增一条，允许「今天没有想明白」；
- 只有用户能修订/撤回，模型只能提议。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

from .core.util import clamp

VERSION = 2
MAX_ENTRIES = 200
MAX_ACTIVE = 24
MAX_DAYS = 90
MAX_TEXT = 60


def _safe_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "")).strip("_")
    return name or "default"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    body = " ".join(str(value or "").split())
    return body[:limit]


class PersonaLedger:
    """一个身份对应一本账；同一进程内用 RLock 串行化写入。"""

    def __init__(
        self,
        directory: str | Path,
        identity: str,
        *,
        max_entries: int = MAX_ENTRIES,
        max_active: int = MAX_ACTIVE,
    ):
        self.directory = Path(directory).expanduser()
        self.identity = _safe_name(identity)
        self.path = self.directory / f"persona_{self.identity}.json"
        self.max_entries = max(1, int(max_entries))
        self.max_active = max(1, int(max_active))
        self._lock = threading.RLock()

    # ---------- 读写 ----------

    def _blank(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "identity": self.identity,
            "counter": 0,
            "entries": [],
            "days": {},
            "updated_at": 0.0,
        }

    def _normalize(self, raw: Any) -> dict[str, Any]:
        data = self._blank()
        if not isinstance(raw, dict) or raw.get("version") != VERSION:
            return data
        entries = raw.get("entries")
        if isinstance(entries, list):
            data["entries"] = [item for item in entries if isinstance(item, dict)]
        days = raw.get("days")
        if isinstance(days, dict):
            data["days"] = {
                str(key): value for key, value in days.items() if isinstance(value, dict)
            }
        data["counter"] = max(
            int(_num(raw.get("counter"), 0.0)),
            len(data["entries"]),
        )
        data["updated_at"] = _num(raw.get("updated_at"), 0.0)
        return data

    def load(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return self._blank()
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # 损坏时返回空账本，不覆盖原文件，留给用户自行处理。
                return self._blank()
            return self._normalize(raw)

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            data["version"] = VERSION
            data["identity"] = self.identity
            data["updated_at"] = time.time()
            fd, tmp = tempfile.mkstemp(
                prefix=".persona-",
                suffix=".tmp",
                dir=str(self.directory),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    # ---------- 查询 ----------

    def entries(self, status: str | None = None) -> list[dict[str, Any]]:
        data = self.load()
        entries = data["entries"]
        if status is None:
            return list(entries)
        return [entry for entry in entries if entry.get("status") == status]

    def active(self, limit: int = 3) -> list[dict[str, Any]]:
        items = self.entries("active")
        items.sort(key=lambda item: (_num(item.get("created_at")), str(item.get("id"))), reverse=True)
        return items[: max(0, int(limit))]

    def get(self, entry_id: str) -> dict[str, Any] | None:
        for entry in self.entries():
            if entry.get("id") == entry_id:
                return entry
        return None

    def has_written_today(self, day: str | None = None) -> bool:
        today = day or date.today().isoformat()
        data = self.load()
        record = data["days"].get(today) or {}
        if record.get("status") == "written":
            return True
        return any(
            entry.get("created_day") == today and entry.get("status") in ("active", "superseded")
            for entry in data["entries"]
        )

    # ---------- 写入 ----------

    def _trim(self, data: dict[str, Any]) -> None:
        entries = data["entries"]
        if len(entries) > self.max_entries:
            entries = entries[-self.max_entries :]
        active = [entry for entry in entries if entry.get("status") == "active"]
        if len(active) > self.max_active:
            active.sort(key=lambda item: _num(item.get("created_at")))
            for entry in active[: len(active) - self.max_active]:
                entry["status"] = "archived"
                entry.setdefault("history", []).append(
                    {"at": time.time(), "action": "archived", "by": "system"}
                )
        data["entries"] = entries

    def append(
        self,
        *,
        text: str,
        signature: str,
        sources: list[dict[str, Any]],
        counterexample: str = "",
        uncertainty: float = 0.0,
        confidence: float = 0.5,
        day: str | None = None,
        by: str = "rule",
        now: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            data = self.load()
            stamp = time.time() if now is None else float(now)
            data["counter"] = int(_num(data.get("counter"), 0.0)) + 1
            entry = {
                "id": f"p-{data['counter']:04d}",
                "text": _text(text),
                "status": "active",
                "signature": _text(signature, 80),
                "sources": [item for item in sources if isinstance(item, dict)][:12],
                "counterexample": _text(counterexample, 80),
                "uncertainty": round(clamp(_num(uncertainty, 0.0), 0.0, 1.0), 3),
                "confidence": round(clamp(_num(confidence, 0.5), 0.0, 1.0), 3),
                "created_day": day or date.today().isoformat(),
                "created_at": stamp,
                "revision_of": None,
                "history": [{"at": stamp, "action": "created", "by": str(by or "rule")}],
            }
            data["entries"].append(entry)
            self._trim(data)
            self.save(data)
            return entry

    def revise(
        self,
        entry_id: str,
        text: str,
        *,
        day: str | None = None,
        by: str = "user",
        now: float | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            data = self.load()
            old = next((item for item in data["entries"] if item.get("id") == entry_id), None)
            if old is None or old.get("status") == "retracted":
                return None
            stamp = time.time() if now is None else float(now)
            old["status"] = "superseded"
            old.setdefault("history", []).append(
                {"at": stamp, "action": "superseded", "by": str(by or "user")}
            )
            data["counter"] = int(_num(data.get("counter"), 0.0)) + 1
            entry = {
                "id": f"p-{data['counter']:04d}",
                "text": _text(text),
                "status": "active",
                "signature": old.get("signature", ""),
                "sources": list(old.get("sources") or []),
                "counterexample": old.get("counterexample", ""),
                "uncertainty": old.get("uncertainty", 0.0),
                "confidence": old.get("confidence", 0.5),
                "created_day": day or old.get("created_day") or date.today().isoformat(),
                "created_at": stamp,
                "revision_of": old.get("id"),
                "history": [{"at": stamp, "action": "revised", "by": str(by or "user")}],
            }
            data["entries"].append(entry)
            self._trim(data)
            self.save(data)
            return entry

    def retract(
        self,
        entry_id: str,
        *,
        reason: str = "",
        by: str = "user",
        now: float | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            data = self.load()
            entry = next((item for item in data["entries"] if item.get("id") == entry_id), None)
            if entry is None:
                return None
            stamp = time.time() if now is None else float(now)
            entry["status"] = "retracted"
            entry.setdefault("history", []).append(
                {
                    "at": stamp,
                    "action": "retracted",
                    "by": str(by or "user"),
                    "reason": _text(reason, 80),
                }
            )
            self.save(data)
            return entry

    def record_day(self, day: str, status: str, *, reason: str = "", entry: str = "") -> None:
        with self._lock:
            data = self.load()
            data["days"][day] = {
                "status": str(status),
                "reason": _text(reason, 120),
                "entry": str(entry or ""),
                "at": time.time(),
            }
            days = sorted(data["days"].items())
            data["days"] = dict(days[-MAX_DAYS:])
            self.save(data)

    def reset(self) -> None:
        with self._lock:
            self.save(self._blank())

    def stats(self) -> dict[str, Any]:
        data = self.load()
        entries = data["entries"]
        counts: dict[str, int] = {}
        for entry in entries:
            status = str(entry.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return {
            "identity": self.identity,
            "path": str(self.path),
            "total": len(entries),
            "counts": counts,
            "last_day": max(data["days"], default=""),
        }
