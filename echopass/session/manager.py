"""会议会话注册与生命周期管理。

进程内（非持久化）多会议管理：每个浏览器/标签页用唯一的 ``session_id``
通过 REST/WS 注册一次后，后端就认得这个会议；TTL 到期或显式 stop 会清理。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


@dataclass
class MeetingSession:
    session_id: str
    label: Optional[str]
    created_at: float
    last_active_at: float
    participants: Set[str] = field(default_factory=set)


class SessionManager:
    """会议会话注册表。

    - 设计为进程内单例，按 ``session_id`` 持有 ``MeetingSession``。
    - TTL 用于「长时间无心跳/调用」自动清理；建议比助手 TTL 大一截。
    - 仅维护元信息；与 transcript / chat history / dialogue 之类按 session 分桶
      的存储是协同关系，stop 时由 app 层调用对应的 clear。
    """

    def __init__(self, ttl_sec: int = 60 * 60 * 4) -> None:
        self._ttl = max(60, int(ttl_sec))
        self._lock = threading.Lock()
        self._sessions: Dict[str, MeetingSession] = {}

    def _prune_locked(self, now: float) -> None:
        stale = [
            sid
            for sid, s in self._sessions.items()
            if now - s.last_active_at > self._ttl
        ]
        for sid in stale:
            self._sessions.pop(sid, None)

    def start(self, session_id: str, label: Optional[str] = None) -> MeetingSession:
        sid = (session_id or "").strip()
        if not sid:
            raise ValueError("session_id 不能为空")
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            existing = self._sessions.get(sid)
            if existing:
                existing.last_active_at = now
                if label:
                    existing.label = label
                return existing
            s = MeetingSession(
                session_id=sid,
                label=(label or "").strip() or None,
                created_at=now,
                last_active_at=now,
            )
            self._sessions[sid] = s
            return s

    def touch(self, session_id: str) -> Optional[MeetingSession]:
        sid = (session_id or "").strip()
        if not sid:
            return None
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            s = self._sessions.get(sid)
            if not s:
                return None
            s.last_active_at = now
            return s

    def get(self, session_id: str) -> Optional[MeetingSession]:
        sid = (session_id or "").strip()
        if not sid:
            return None
        with self._lock:
            s = self._sessions.get(sid)
            return s

    def stop(self, session_id: str) -> bool:
        sid = (session_id or "").strip()
        if not sid:
            return False
        with self._lock:
            return self._sessions.pop(sid, None) is not None

    def list(self) -> List[MeetingSession]:
        with self._lock:
            self._prune_locked(time.time())
            return list(self._sessions.values())

    @property
    def ttl_sec(self) -> int:
        return self._ttl
