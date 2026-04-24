from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def event_message(event_type: str, session_id: str = "default", payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {
        "type": event_type,
        "session_id": session_id,
        "timestamp": utc_iso(),
        "payload": payload or {},
    }
