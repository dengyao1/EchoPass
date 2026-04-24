from __future__ import annotations

import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List


@dataclass
class TranscriptItem:
    speaker: str
    text: str
    text_raw: str
    llm_corrected: bool
    created_at: str
    start_ms: int = 0          # 相对会议开始的毫秒偏移
    end_ms: int = 0            # 相对会议开始的毫秒偏移
    duration_sec: float = 0.0  # 本条发言的音频时长（秒）

    def as_dict(self) -> dict:
        return asdict(self)


class TranscriptBuffer:
    """按 session 缓存转录文本，用于后续会议纪要。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: Dict[str, List[TranscriptItem]] = {}

    @staticmethod
    def _needs_space(left: str, right: str) -> bool:
        return bool(
            left
            and right
            and left[-1].isascii()
            and left[-1].isalnum()
            and right[0].isascii()
            and right[0].isalnum()
        )

    @classmethod
    def _merge_text(cls, existing: str, incoming: str, min_overlap: int = 4) -> str:
        existing = existing.strip()
        incoming = incoming.strip()
        if not existing:
            return incoming
        if not incoming:
            return existing

        tail_window = existing[-max(len(incoming), 64) :]
        if incoming in tail_window:
            return existing

        max_overlap = min(len(existing), len(incoming))
        for size in range(max_overlap, min_overlap - 1, -1):
            if existing[-size:] == incoming[:size]:
                suffix = incoming[size:].lstrip()
                if not suffix:
                    return existing
                sep = " " if cls._needs_space(existing, suffix) else ""
                return existing + sep + suffix

        sep = " " if cls._needs_space(existing, incoming) else ""
        return existing + sep + incoming

    def append(
        self,
        session_id: str,
        speaker: str,
        text: str,
        text_raw: str,
        llm_corrected: bool,
        start_ms: int = 0,
        end_ms: int = 0,
        duration_sec: float = 0.0,
    ) -> None:
        text = text.strip()
        if not text:
            return
        speaker = speaker or "未知说话人"
        raw_text = text_raw.strip() if text_raw else text
        item = TranscriptItem(
            speaker=speaker,
            text=text,
            text_raw=raw_text,
            llm_corrected=bool(llm_corrected),
            created_at=datetime.now(timezone.utc).isoformat(),
            start_ms=int(start_ms or 0),
            end_ms=int(end_ms or max(start_ms or 0, 0)),
            duration_sec=float(duration_sec or 0.0),
        )
        with self._lock:
            items = self._store.setdefault(session_id, [])
            if items and items[-1].speaker == speaker:
                last = items[-1]
                last.text = self._merge_text(last.text, item.text)
                last.text_raw = self._merge_text(last.text_raw, item.text_raw)
                last.llm_corrected = last.llm_corrected or item.llm_corrected
                last.created_at = item.created_at
                # 时间窗口取并集；时长累加（同说话人连续发言合并成一段）
                if item.start_ms and (not last.start_ms or item.start_ms < last.start_ms):
                    last.start_ms = item.start_ms
                if item.end_ms and item.end_ms > last.end_ms:
                    last.end_ms = item.end_ms
                last.duration_sec = round(float(last.duration_sec) + float(item.duration_sec), 3)
                return
            items.append(item)

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._store.pop(session_id, None)

    def list_items(self, session_id: str) -> List[TranscriptItem]:
        with self._lock:
            return list(self._store.get(session_id, []))

    def list_dicts(self, session_id: str) -> List[dict]:
        return [x.as_dict() for x in self.list_items(session_id)]
