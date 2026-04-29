"""跨会议总结（预留实现）。

当前进程内单场纪要见 ``MeetingSummarizer``；本模块为「多场会议材料 → 一条综述」
预留统一入口，便于后续接入：

- 持久化层：按 ``session_id`` / 用户维度拉取多场已落库的纪要 JSON 或转写摘要；
- LLM：拼接多场标题 + 单场 summary / modules 的压缩文本，再调用与单场类似的 JSON schema；
- 前端：独立页面或设置里「选择 N 场会议 → 生成跨场总结」。

在实现完成前，``CrossMeetingSummarizer.summarize`` 返回与单场兼容的空壳结构，
并在 ``cross_meeting_meta`` 中标注 ``implementation: stub``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from echopass.config import cfg


@dataclass(frozen=True)
class CrossMeetingRef:
    """单场会议在跨场总结中的输入引用（内存或未来 DB 行映射到此即可）。"""

    session_id: str
    title: str
    # 单场纪要的压缩文本：优先用已有 ``MeetingSummarizer`` 产物的 summary 字段，
    # 或自行拼接 modules；跨场 LLM 主要读这段而非全量转写。
    summary_text: str = ""
    # 可选 ISO8601，用于排序与 prompt 中的时间线（未实现排序时可忽略）。
    captured_at: Optional[str] = None


def _empty_cross_payload(title: str, *, meeting_count: int) -> Dict[str, Any]:
    """与 ``MeetingSummarizer._empty_payload`` 对齐，便于前端复用渲染分支。"""
    return {
        "title": title,
        "summary": "",
        "background": "",
        "modules": [],
        "key_points": [],
        "decisions": [],
        "action_items": [],
        "risks": [],
        "cross_meeting_meta": {
            "implementation": "stub",
            "meeting_count": meeting_count,
            "message": "跨会议总结尚未接入 LLM；请在本模块内实现 summarize 并替换 stub。",
        },
    }


class CrossMeetingSummarizer:
    """跨会议综述生成器占位：与 ``MeetingSummarizer`` 一样可注入 ``llm_chat_client``。"""

    def __init__(self, llm_chat_client: Any = None) -> None:
        self._llm = llm_chat_client
        # 预留：限制一次提交的会议数量，防止超长 prompt（实现 LLM 路径时使用）。
        self._max_meetings = max(1, cfg("meeting.cross_summary.max_meetings", "SPEAKER_CROSS_SUMMARY_MAX_MEETINGS", 20, int))

    def _trim_meetings(self, meetings: Sequence[CrossMeetingRef]) -> List[CrossMeetingRef]:
        if len(meetings) <= self._max_meetings:
            return list(meetings)
        return list(meetings[-self._max_meetings :])

    async def summarize(
        self,
        meetings: Sequence[CrossMeetingRef],
        *,
        title: str = "跨会议总结",
        focus: str = "",
    ) -> Dict[str, Any]:
        """生成跨场综述。

        **TODO（实现时建议顺序）**：
        1. ``refs = self._trim_meetings(meetings)``，过滤 ``summary_text`` 全空的项或给默认提示；
        2. 将各场 ``title`` / ``captured_at`` / ``summary_text`` 格式化为中文多段上下文；
        3. 若 ``focus`` 非空，写入 user 指令侧重主题；
        4. 调用 ``self._llm``（与单场相同 client），要求输出与单场兼容的模块化 JSON；
        5. 校验 JSON 后返回，并设置 ``cross_meeting_meta.implementation = "llm"``。

        ``focus`` 已保留在签名中，stub 阶段仅占位，避免后续改 API 形状。
        """
        _ = focus  # 预留：实现 LLM 路径时写入 prompt
        refs = self._trim_meetings(meetings)
        if not refs:
            return _empty_cross_payload(title, meeting_count=0)

        # --- 以下为 stub：接入 LLM 后删除 early return，改为 _summarize_with_llm ---
        if self._llm is not None:
            # 故意不调用模型，避免未评审的 prompt 在生产误触发；实现时在此分支调用 LLM。
            pass

        return _empty_cross_payload(title, meeting_count=len(refs))
