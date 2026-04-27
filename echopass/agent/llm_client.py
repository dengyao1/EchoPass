from __future__ import annotations

import asyncio
import http.client
import json
import ssl
import threading
import urllib.parse
import urllib.request
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional


class LLMChatClient:
    """OpenAI-compatible chat client，用于唤醒后的问答与纪要抽取。"""

    def __init__(self, api_url: str, api_key: str = "none", model: str = "qwen-plus") -> None:
        self._url = api_url.rstrip("/")
        self._key = api_key
        self._model = model

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

        def _call():
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))

        data = await loop.run_in_executor(None, _call)
        return (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )

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
                p.netloc, timeout=120, context=ssl.create_default_context(),
            )
        else:
            conn = http.client.HTTPConnection(p.netloc, timeout=120)
        try:
            conn.request("POST", path, body=payload, headers=headers)
            resp = conn.getresponse()
            if resp.status != 200:
                err = resp.read(4096)
                raise RuntimeError(
                    f"LLM stream HTTP {resp.status}: {err.decode('utf-8', errors='replace')[:800]}",
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
        self._validate_messages_openai(messages)
        loop = asyncio.get_event_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        err_box: List[Optional[BaseException]] = [None]

        def producer() -> None:
            try:
                for piece in self._iter_chat_deltas_sync_messages(
                    messages, max_tokens, temperature,
                ):
                    fut = asyncio.run_coroutine_threadsafe(q.put(("d", piece)), loop)
                    fut.result(timeout=120)
                asyncio.run_coroutine_threadsafe(q.put(("x", None)), loop).result(timeout=30)
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
            t.join(timeout=0.2)
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
