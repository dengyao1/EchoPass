from __future__ import annotations

import asyncio
import http.client
import json
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional


def _http_error_message(body: str) -> str:
    """从 OpenAI 兼容错误 JSON 或纯文本里抽出可读说明。"""
    body = (body or "").strip()
    if not body:
        return ""
    try:
        j = json.loads(body)
        if isinstance(j, dict):
            err = j.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err.get("code") or err)[:1500]
            if isinstance(err, str):
                return err[:1500]
            if "message" in j:
                return str(j["message"])[:1500]
    except json.JSONDecodeError:
        pass
    return body[:1500]


class LLMChatClient:
    """OpenAI-compatible chat client，用于唤醒后的问答与纪要抽取。"""

    def __init__(
        self,
        api_url: str,
        api_key: str = "none",
        model: str = "qwen-plus",
        *,
        timeout_sec: float = 120.0,
    ) -> None:
        self._url = (api_url or "").rstrip("/")
        self._key = api_key
        self._model = model
        self._timeout = max(5.0, float(timeout_sec))

    @staticmethod
    def _validate_messages_openai(
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for m in messages:
            r = str(m.get("role") or "").strip()
            c = str(m.get("content") or "")
            if r not in ("system", "user", "assistant") or not c:
                continue
            out.append({"role": r, "content": c})
        if not out:
            raise ValueError("messages 不能为空")
        return out

    async def chat_complete(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int = 512,
        temperature: float = 0.3,
    ) -> str:
        """OpenAI Chat 多轮。messages 须含 system 与若干 user/assistant（按时间顺序）。"""
        if not self._url:
            raise ValueError("LLM api_url 未配置：请设置 llm.api_url 或 SPEAKER_LLM_API_URL")
        clean = self._validate_messages_openai(messages)
        payload = json.dumps(
            {
                "model": self._model,
                "messages": clean,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        ).encode("utf-8")
        endpoint = self._url if self._url.endswith("/chat/completions") else self._url + "/chat/completions"
        req = urllib.request.Request(
            endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._key}",
            },
            method="POST",
        )
        loop = asyncio.get_event_loop()

        def _call() -> dict:
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", errors="replace")
                hint = _http_error_message(raw) or raw[:800]
                raise RuntimeError(f"LLM HTTP {e.code}: {hint}") from e
            except urllib.error.URLError as e:
                raise RuntimeError(f"LLM 网络错误: {getattr(e, 'reason', e)!s}") from e

        data = await loop.run_in_executor(None, _call)
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict) and err.get("message"):
            raise RuntimeError(f"LLM 返回错误: {err.get('message')}")
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        if content is None:
            return ""
        return str(content).strip()

    async def reply(self, text: str, system_prompt: str = "你是会议语音助手。", max_tokens: int = 512) -> str:
        return await self.chat_complete(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            max_tokens=max_tokens,
            temperature=0.3,
        )

    def _iter_chat_deltas_sync(
        self,
        user_text: str,
        system_prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.3,
    ) -> Iterator[str]:
        """单轮 user+system，兼容旧接口。内部走多轮 messages。"""
        yield from self._iter_chat_deltas_sync_messages(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def _iter_chat_deltas_sync_messages(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int = 512,
        temperature: float = 0.3,
    ) -> Iterator[str]:
        """同步生成器：OpenAI 兼容 SSE，多轮 messages。"""
        if not self._url:
            raise ValueError("LLM api_url 未配置：请设置 llm.api_url 或 SPEAKER_LLM_API_URL")
        clean = self._validate_messages_openai(messages)
        body = {
            "model": self._model,
            "messages": clean,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        payload = json.dumps(body).encode("utf-8")
        endpoint = (
            self._url if self._url.endswith("/chat/completions")
            else self._url + "/chat/completions"
        )
        p = urllib.parse.urlparse(endpoint)
        if p.scheme not in ("http", "https"):
            raise ValueError(f"不支持的 LLM URL scheme: {p.scheme!r}")
        path = p.path or "/"
        if not path.endswith("/chat/completions"):
            path = path.rstrip("/") + "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._key}",
            "Accept": "text/event-stream",
        }
        if p.scheme == "https":
            conn = http.client.HTTPSConnection(
                p.netloc,
                timeout=self._timeout,
                context=ssl.create_default_context(),
            )
        else:
            conn = http.client.HTTPConnection(p.netloc, timeout=self._timeout)
        try:
            conn.request("POST", path, body=payload, headers=headers)
            resp = conn.getresponse()
            if resp.status != 200:
                err = resp.read(8192)
                hint = _http_error_message(err.decode("utf-8", errors="replace"))
                raise RuntimeError(
                    f"LLM stream HTTP {resp.status}: {hint or err.decode('utf-8', errors='replace')[:800]}",
                )
            buf = b""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line or line.startswith(b":"):
                        continue
                    if not line.startswith(b"data: "):
                        continue
                    data = line[6:]
                    if data == b"[DONE]":
                        return
                    try:
                        obj = json.loads(data.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    choices: List[dict] = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield str(content)
        finally:
            conn.close()

    async def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int = 512,
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        """多轮 messages 流式输出（与 chat_complete 使用同一套 messages 约定）。"""
        clean = self._validate_messages_openai(messages)
        loop = asyncio.get_event_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        err_box: List[Optional[BaseException]] = [None]
        tmo = self._timeout

        def producer() -> None:
            try:
                for piece in self._iter_chat_deltas_sync_messages(
                    clean, max_tokens, temperature,
                ):
                    fut = asyncio.run_coroutine_threadsafe(q.put(("d", piece)), loop)
                    fut.result(timeout=tmo)
                asyncio.run_coroutine_threadsafe(q.put(("x", None)), loop).result(timeout=min(60.0, tmo))
            except BaseException as e:  # noqa: BLE001
                err_box[0] = e
                try:
                    asyncio.run_coroutine_threadsafe(q.put(("x", None)), loop).result(timeout=5)
                except Exception:  # noqa: BLE001
                    pass

        t = threading.Thread(target=producer, name="llm-stream", daemon=True)
        t.start()
        try:
            while True:
                kind, data = await q.get()
                if kind == "d":
                    yield data
                else:
                    break
        finally:
            t.join(timeout=min(5.0, tmo))
        if err_box[0] is not None:
            raise err_box[0]

    async def stream_reply(
        self,
        user_text: str,
        system_prompt: str = "你是会议语音助手。",
        max_tokens: int = 512,
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        """单轮流式，兼容旧接口。"""
        async for piece in self.stream_chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        ):
            yield piece
