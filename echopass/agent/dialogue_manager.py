from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict


@dataclass
class DialogueSession:
    session_id: str
    active: bool
    started_at: float
    expires_at: float


class DialogueManager:
    """唤醒后的短时对话态管理。"""

    def __init__(self, ttl_sec: int = 20) -> None:
        self._ttl = ttl_sec
        self._lock = threading.Lock()
        self._sessions: Dict[str, DialogueSession] = {}

    def _prune_locked(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        stale_ids = [
            sid
            for sid, session in self._sessions.items()
            if (not session.active) or now > session.expires_at
        ]
        for sid in stale_ids:
            self._sessions.pop(sid, None)

    def start(self, session_id: str) -> DialogueSession:
        now = time.time()
        s = DialogueSession(
            session_id=session_id,
            active=True,
            started_at=now,
            expires_at=now + self._ttl,
        )
        with self._lock:
            self._prune_locked(now)
            self._sessions[session_id] = s
        return s

    def touch(self, session_id: str) -> DialogueSession | None:
        with self._lock:
            now = time.time()
            self._prune_locked(now)
            s = self._sessions.get(session_id)
            if not s:
                return None
            s.expires_at = now + self._ttl
            s.active = True
            return s

    def stop(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def is_active(self, session_id: str) -> bool:
        with self._lock:
            now = time.time()
            self._prune_locked(now)
            s = self._sessions.get(session_id)
            if not s:
                return False
            return s.active
