# EchoPass · ASR 长连接流式会话池（按 session_id）
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("echopass.asr_stream_pool")


class AsrStreamPool:
    """维护 EchoPass session_id → ASR 长连接（火山 bigmodel_async 等）。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sessions: Dict[str, Any] = {}

    async def get(self, session_id: str) -> Optional[Any]:
        sid = (session_id or "").strip()
        if not sid:
            return None
        async with self._lock:
            return self._sessions.get(sid)

    async def replace(self, session_id: str, session: Any) -> None:
        sid = (session_id or "").strip()
        if not sid:
            return
        async with self._lock:
            old = self._sessions.pop(sid, None)
            self._sessions[sid] = session
        if old is not None and old is not session:
            await old.close()

    async def close_session(self, session_id: str) -> None:
        sid = (session_id or "").strip()
        if not sid:
            return
        async with self._lock:
            session = self._sessions.pop(sid, None)
        if session is not None:
            try:
                await session.stop()
            except Exception:  # noqa: BLE001
                await session.close()
            logger.debug("ASR 流式会话已关闭 session_id=%s", sid)

    async def close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            try:
                await session.stop()
            except Exception:  # noqa: BLE001
                await session.close()


__all__ = ["AsrStreamPool"]
