"""OpenAI 兼容的同步客户端：LLM 只做「提议」，不提供事实、不自报置信度（§2）。

与上游的区别（移植时按宿主环境改写）：
- 端点、模型、密钥全部由宿主注入，不再读 ``key.txt``；
- **默认不落盘缓存**：按 prompt 哈希缓存会让同一句话永远得到同一句回复，
  在微信里表现为复读（上游为「场景可重放」而默认开启，宿主侧必须关掉）；
- 解析容忍 ```json 围栏与前后废话，遇到不支持 ``response_format`` 的端点会
  自动去掉该字段重试一次（不少兼容端点会直接 400）；
- ``tag="expression"`` 时，模型若直接吐了纯文本（没按 JSON 回），就把这段
  文本当作回应本身——比丢掉它再退回通用模板更贴近用户预期。

任何异常信息都不会包含密钥。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .util import sha

#: 从模型输出里挖出 JSON 对象（容忍 ```json 围栏、前后解释性文字）。
_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)


class LLMClient:
    """同步 chat completions 客户端（只在工作线程里调用）。"""

    def __init__(self, cfg: dict | None = None, *, api_key: str | None = None):
        cfg = dict(cfg or {})
        self.api_key = (
            api_key if api_key is not None else str(cfg.get("api_key") or "")
        ).strip()
        self.base_url = str(
            cfg.get("base_url") or "https://api.deepseek.com"
        ).rstrip("/")
        self.model = str(cfg.get("model") or "deepseek-chat")
        self.timeout = float(cfg.get("timeout") or 60.0)
        self.max_tokens = int(cfg.get("max_tokens") or 1200)
        self.temperature = float(cfg.get("temperature") or 0.0)
        self.cache_dir = cfg.get("cache_dir")
        self.calls = 0
        self.cache_hits = 0
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    # ---------- 缓存（仅回放/测试用） ----------

    def _cache_path(self, key: str) -> Path | None:
        if not self.cache_dir:
            return None
        return Path(self.cache_dir) / f"{sha(key, 24)}.json"

    def _read_cache(self, key: str) -> dict | None:
        path = self._cache_path(key)
        if path is None or not path.is_file():
            return None
        try:
            self.cache_hits += 1
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, key: str, data: dict) -> None:
        path = self._cache_path(key)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    # ---------- 请求 ----------

    def complete_json(
        self,
        system: str,
        user: str,
        tag: str,
        *,
        images: list[str] | None = None,
    ) -> dict | None:
        """返回已解析的 JSON 对象；任何失败都返回 None，由调用方降级。

        ``images`` 是 ``data:image/...;base64,...`` 列表，仅在宿主开启视觉时传入。
        """
        if not self.available:
            self.last_error = "no_api_key"
            return None

        cache_key = f"{self.model}|{tag}|{system}|{user}|{len(images or ())}"
        cached = self._read_cache(cache_key)
        if cached is not None:
            return cached

        content: Any = user
        if images:
            content = [{"type": "text", "text": user}]
            content += [
                {"type": "image_url", "image_url": {"url": uri}} for uri in images
            ]
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
            # 情绪内核要求严格 JSON；不支持该字段的端点会被自动重试兜住。
            "response_format": {"type": "json_object"},
        }

        raw = self._post(body, allow_json_retry=True)
        if raw is None:
            return None
        data = _extract_json(raw)
        if data is None:
            self.last_error = "bad_content_json"
            # 表达模块允许模型直接给一句人话：总比退回通用模板更贴近语境。
            text = raw.strip()
            if tag == "expression" and 0 < len(text) <= 200 and "{" not in text:
                data = {"text": text}
            else:
                return None
        self._write_cache(cache_key, data)
        return data

    def _post(self, body: dict[str, Any], *, allow_json_retry: bool) -> str | None:
        """发一次请求，返回 ``choices[0].message.content``。"""
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url=f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            self.calls += 1
        except urllib.error.HTTPError as exc:
            # 端点不认 response_format：去掉它再试一次（很多兼容实现直接 400）。
            if allow_json_retry and exc.code in (400, 404, 422):
                body = {k: v for k, v in body.items() if k != "response_format"}
                return self._post(body, allow_json_retry=False)
            self.last_error = f"http_{exc.code}"  # 不回显请求头
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.last_error = f"network_{type(exc).__name__}"
            return None
        except json.JSONDecodeError:
            self.last_error = "bad_response_json"
            return None

        try:
            message = (raw.get("choices") or [{}])[0].get("message") or {}
            return str(message.get("content") or "")
        except (AttributeError, IndexError, TypeError):
            self.last_error = "bad_response_shape"
            return None


def _extract_json(raw: str) -> dict | None:
    """从模型输出里取出第一个 JSON 对象。"""
    text = (raw or "").strip()
    if not text:
        return None
    for candidate in (text, *_FENCE_RE.findall(text)):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict):
            return data
    return None


class NullClient:
    """没有可用模型时的占位：``available=False``，所有调用直接降级到规则。"""

    available = False
    last_error: str | None = "disabled"
    calls = 0

    def complete_json(self, *_args: Any, **_kwargs: Any) -> dict | None:
        return None
