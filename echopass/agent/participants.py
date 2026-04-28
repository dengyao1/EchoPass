"""会议参与者白名单（按 session_id）。

声纹库本身全局共享，但每场会议可由用户在会前选择「只识别这些人」。
前端在开会前 POST 一次列表（即使全选也写入）；识别 endpoint 在打分后
对返回的 ``speaker`` 再做一层「必须在白名单里」的过滤。
"""

from __future__ import annotations

import threading
from typing import Dict, FrozenSet, Iterable, List, Optional, Set


def _normalize_names(names: Optional[Iterable[str]]) -> Set[str]:
    out: Set[str] = set()
    if not names:
        return out
    for n in names:
        s = str(n or "").strip()
        if s:
            out.add(s)
    return out


class ParticipantsRegistry:
    """按 session 持有「允许识别的说话人姓名集合」。

    - ``set(session_id, names)``：替换该会议的白名单。
    - ``add(session_id, names)`` / ``remove``：增量。
    - ``get(session_id)``：返回 frozenset；空集表示「未配置」（识别不过滤）。
    - ``is_allowed(session_id, name)``：白名单为空集时一律放行。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._whitelist: Dict[str, Set[str]] = {}

    def set(self, session_id: str, names: Optional[Iterable[str]]) -> FrozenSet[str]:
        sid = (session_id or "").strip()
        if not sid:
            raise ValueError("session_id 不能为空")
        ns = _normalize_names(names)
        with self._lock:
            if ns:
                self._whitelist[sid] = ns
            else:
                self._whitelist.pop(sid, None)
            return frozenset(self._whitelist.get(sid, set()))

    def add(self, session_id: str, names: Optional[Iterable[str]]) -> FrozenSet[str]:
        sid = (session_id or "").strip()
        if not sid:
            raise ValueError("session_id 不能为空")
        with self._lock:
            cur = self._whitelist.setdefault(sid, set())
            cur.update(_normalize_names(names))
            return frozenset(cur)

    def remove(self, session_id: str, names: Optional[Iterable[str]]) -> FrozenSet[str]:
        sid = (session_id or "").strip()
        if not sid:
            raise ValueError("session_id 不能为空")
        with self._lock:
            cur = self._whitelist.get(sid)
            if not cur:
                return frozenset()
            for n in _normalize_names(names):
                cur.discard(n)
            if not cur:
                self._whitelist.pop(sid, None)
                return frozenset()
            return frozenset(cur)

    def get(self, session_id: str) -> FrozenSet[str]:
        sid = (session_id or "").strip()
        if not sid:
            return frozenset()
        with self._lock:
            return frozenset(self._whitelist.get(sid, set()))

    def is_allowed(self, session_id: str, name: Optional[str]) -> bool:
        """白名单为空 => 全放行；非空 => name 必须命中。"""
        if not name:
            return True
        wl = self.get(session_id)
        if not wl:
            return True
        return str(name).strip() in wl

    def clear(self, session_id: str) -> None:
        sid = (session_id or "").strip()
        if not sid:
            return
        with self._lock:
            self._whitelist.pop(sid, None)

    def list(self, session_id: str) -> List[str]:
        return sorted(self.get(session_id))
