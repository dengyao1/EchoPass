from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Dict, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketHub:
    """维护会话级 WebSocket 连接并广播事件。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._by_session: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, websocket: WebSocket, session_id: str = "default") -> None:
        await websocket.accept()
        async with self._lock:
            self._by_session[session_id].add(websocket)

    async def disconnect(self, websocket: WebSocket, session_id: str = "default") -> None:
        async with self._lock:
            conns: Set[WebSocket] = self._by_session.get(session_id, set())
            conns.discard(websocket)
            if not conns and session_id in self._by_session:
                self._by_session.pop(session_id, None)

    async def emit(self, message: Dict[str, Any], session_id: str = "default", include_global: bool = True) -> None:
        targets: dict[WebSocket, str] = {}
        async with self._lock:
            for ws in self._by_session.get(session_id, set()):
                targets[ws] = session_id
            if include_global and session_id != "global":
                for ws in self._by_session.get("global", set()):
                    targets[ws] = "global"

        dead: list[tuple[WebSocket, str]] = []
        for ws, owner_session_id in targets.items():
            try:
                await ws.send_json(message)
            except Exception:
                dead.append((ws, owner_session_id))
        for ws, sid in dead:
            await self.disconnect(ws, sid)
            logger.debug("websocket disconnected during emit: sid=%s", sid)
