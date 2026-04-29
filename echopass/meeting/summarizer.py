from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from echopass.config import cfg
from echopass.meeting.transcript_buffer import TranscriptItem


def _fmt_mmss(ms: int) -> str:
    """毫秒 → mm:ss 或 h:mm:ss，供 prompt/回退展示。"""
    try:
        s = max(0, int((ms or 0) // 1000))
    except (TypeError, ValueError):
        s = 0
    m, s = divmod(s, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class MeetingSummarizer:
    """MVP 纪要生成器：优先调用 LLM，失败则回退规则摘要。"""

    # 章节 LLM 返回格式校验的最大章节数（安全上限，防止爆炸）
    _MAX_CHAPTERS = 50
    # 章节回退规则阈值：超过 90s 静默 / 3min max 切章（与前端一致，便于协同）
    _FALLBACK_GAP_MS = 90_000
    _FALLBACK_MAX_MS = 3 * 60 * 1000
    # 后处理：时间重叠合并、过短「微章」并入相邻章（毫秒 / 行数阈值）
    _CHAPTER_OVERLAP_GRACE_MS = 2_000
    _CHAPTER_TINY_MAX_MS = 50_000
    _CHAPTER_TINY_MAX_LINES = 2
    _CHAPTER_TINY_GAP_MS = 25_000  # 与上一章结束间隔在此内才考虑把微章并入上一章

    def __init__(self, llm_chat_client=None) -> None:
        self._llm = llm_chat_client

    # 模块化纪要的合法 type（前端用来选渲染分支）
    _MODULE_TYPES = {"bullets", "table", "actions", "callout"}
    _MAX_MODULES = 12

    async def summarize(self, items: List[TranscriptItem], title: str = "会议纪要") -> Dict:
        if not items:
            return self._empty_payload(title)
        if self._llm:
            payload = await self._summarize_with_llm(items, title)
            if payload:
                return payload
        return self._fallback_summary(items, title)

    @staticmethod
    def _empty_payload(title: str) -> Dict:
        return {
            "title": title,
            "summary": "",
            "background": "",
            "modules": [],
            "key_points": [],
            "decisions": [],
            "action_items": [],
            "risks": [],
        }

    async def _summarize_with_llm(self, items: List[TranscriptItem], title: str) -> Optional[Dict]:
        # 与 chapters() 类似，软截断，避免超长 prompt
        lines = [f"[{x.speaker}] {x.text}" for x in items[-100:]]
        prompt = (
            "请将以下会议转录整理为【模块化报告式 JSON】，严格按下面 schema 输出，不要任何解释文字。\n\n"
            "顶层字段：\n"
            f"  - title: 字符串，会议主标题（10~20 字，凝练；建议与「{title}」协调）\n"
            "  - summary: 一句话副标题/导语（20~50 字），点出会议主旨与结论走向\n"
            "  - background: 字符串，【会议背景】2~5 句中文。应归纳：开会的动因/业务场景、要解决的问题、"
            "主要参与方或产品范围、若转录中未说明则可合理推断但勿编造具体数字合同名称。\n"
            "  - modules: 数组，每项是一个【编号模块卡片】\n"
            "  - key_points / decisions / action_items / risks: 旧字段（可派生自 modules，便于兼容）\n"
            "    其中 action_items 表示【待办事项】，必须与 modules 里 type=actions 的待办保持一致或为其子集；\n"
            "    每一项为 {task, owner, due_date}，task 用动词开头、可执行、可跟踪；\n"
            "    owner/due_date 在转录未提及时可留空串，不要写「待定」「TBD」敷衍。\n\n"
            "module 字段：\n"
            "  - no: 两位数字符串，例如 \"01\"、\"02\"，从 01 递增\n"
            "  - title: 5~12 字模块名，例如 \"会议背景\"、\"核心议题\"、\"待办与负责人\"\n"
            "  - intro: 可选，1 句话模块导语，不超过 40 字\n"
            "  - type: 必须是 bullets / table / actions / callout 之一\n"
            "  - 根据 type 提供以下不同字段：\n"
            "    * bullets: items=[{label:'<5~10字短词>', desc:'<1-2句详细说明>'}]\n"
            "    * table:   columns=[...], rows=[[...],...]\n"
            "    * actions: items=[{task:'<可执行待办，动词开头>', owner:'<负责人，可空>', due:'<截止，可空>'}]\n"
            "    * callout: items=['<结论/共识/风险>']\n\n"
            "【建议】模块组织（3~8 个为宜，可增减；无内容则省略该模块）：\n"
            "  - 若 background 已写全，可不再单独用模块重复「会议背景」；需要时可用 bullets 写「背景要点」。\n"
            "  01 核心议题（bullets）\n"
            "  02 关键讨论与分歧（bullets 或 table）\n"
            "  03 决议与共识（callout）\n"
            "  04 待办事项（actions）— 全量、具体，优先从发言中显式或隐含的「谁做什么、何时前」提取\n"
            "  05 风险与未决（callout）\n"
            "  06 后续计划（bullets）\n\n"
            "格式硬要求：\n"
            "  - 必须是合法 JSON，不要 markdown 代码块包裹。\n"
            "  - 所有字符串使用中文，不要出现 \"TODO\"、\"待补充\" 这类无信息占位。\n"
            "  - 没有内容的模块直接省略，不要返回空 items；background 在完全无信息时可为空串 "
            "（尽量根据转写作合理归纳）。\n"
            "  - key_points/decisions/action_items/risks 四字段必须返回；若与 modules 重复，以结构化 modules 为准抽取。\n\n"
            f"会议标题建议：{title}\n"
            "转录如下：\n"
            + "\n".join(lines)
        )
        try:
            text = await self._llm.reply(
                prompt,
                system_prompt=(
                    "你是专业会议纪要助手，负责归纳会议背景、讨论要点、决议与可执行待办；"
                    "待办要具体、可跟踪。只输出一个 JSON 对象，不要其他文字。"
                ),
            )
        except Exception:
            return None

        parsed = self._parse_summary_json(text)
        if not parsed:
            return None
        return self._normalize_payload(parsed, title)

    @staticmethod
    def _parse_summary_json(text: str) -> Optional[Dict]:
        """容忍 LLM 返回带 ```json ... ``` 包裹或前后多余文字的情况。"""
        if not text:
            return None
        for attempt in (text, text.strip()):
            try:
                obj = json.loads(attempt)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                return obj
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None

    def _normalize_payload(self, parsed: Dict, title: str) -> Dict:
        out: Dict[str, Any] = {
            "title": str(parsed.get("title") or title).strip() or title,
            "summary": str(parsed.get("summary") or "").strip(),
            "background": str(parsed.get("background") or parsed.get("meeting_background") or "").strip(),
            "modules": self._normalize_modules(parsed.get("modules")),
            "key_points": [str(x) for x in (parsed.get("key_points") or []) if x],
            "decisions": [str(x) for x in (parsed.get("decisions") or []) if x],
            "action_items": self._normalize_actions(parsed.get("action_items")),
            "risks": [str(x) for x in (parsed.get("risks") or []) if x],
        }
        # 旧字段缺失时尝试从 modules 派生，保证 ZIP 导出/旧 UI 不空
        self._derive_legacy_fields(out)
        return out

    def _normalize_modules(self, raw) -> List[Dict[str, Any]]:
        if not isinstance(raw, list):
            return []
        modules: List[Dict[str, Any]] = []
        for idx, m in enumerate(raw[: self._MAX_MODULES]):
            if not isinstance(m, dict):
                continue
            mtype = str(m.get("type") or "").strip().lower()
            if mtype not in self._MODULE_TYPES:
                continue
            mod: Dict[str, Any] = {
                "no": str(m.get("no") or f"{idx + 1:02d}"),
                "title": str(m.get("title") or "").strip() or f"模块 {idx + 1}",
                "intro": str(m.get("intro") or "").strip(),
                "type": mtype,
            }
            if mtype == "bullets":
                items = []
                for it in (m.get("items") or []):
                    if isinstance(it, dict):
                        label = str(it.get("label") or "").strip()
                        desc = str(it.get("desc") or "").strip()
                        if not label and not desc:
                            continue
                        items.append({"label": label, "desc": desc})
                    elif isinstance(it, str) and it.strip():
                        items.append({"label": "", "desc": it.strip()})
                if not items:
                    continue
                mod["items"] = items
            elif mtype == "table":
                cols = [str(c) for c in (m.get("columns") or []) if str(c).strip()]
                rows = []
                for r in (m.get("rows") or []):
                    if isinstance(r, list):
                        rows.append([str(x) for x in r])
                if not cols or not rows:
                    continue
                # 行宽度对齐到列数（多砍少补空字符串）
                rows = [(r + [""] * len(cols))[: len(cols)] for r in rows]
                mod["columns"] = cols
                mod["rows"] = rows
            elif mtype == "actions":
                items = []
                for it in (m.get("items") or []):
                    if not isinstance(it, dict):
                        continue
                    task = str(it.get("task") or "").strip()
                    if not task:
                        continue
                    items.append({
                        "task": task,
                        "owner": str(it.get("owner") or "").strip(),
                        "due": str(it.get("due") or it.get("due_date") or "").strip(),
                    })
                if not items:
                    continue
                mod["items"] = items
            elif mtype == "callout":
                items = [str(x).strip() for x in (m.get("items") or []) if str(x).strip()]
                if not items:
                    continue
                mod["items"] = items
            modules.append(mod)
        # 重新按 01/02/... 编号，保证连续
        for i, mod in enumerate(modules, start=1):
            mod["no"] = f"{i:02d}"
        return modules

    @staticmethod
    def _normalize_actions(raw) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        if not isinstance(raw, list):
            return out
        for it in raw:
            if isinstance(it, dict):
                task = str(it.get("task") or "").strip()
                if not task:
                    continue
                out.append({
                    "task": task,
                    "owner": str(it.get("owner") or "").strip(),
                    "due_date": str(it.get("due_date") or it.get("due") or "").strip(),
                })
            elif isinstance(it, str) and it.strip():
                out.append({"task": it.strip(), "owner": "", "due_date": ""})
        return out

    @staticmethod
    def _derive_legacy_fields(payload: Dict[str, Any]) -> None:
        """旧字段为空时，尽量从 modules 里抽取出有用内容，保证旧 UI/ZIP 导出不丢信息。"""
        modules = payload.get("modules") or []
        if not payload["key_points"]:
            picked: List[str] = []
            for m in modules:
                if m["type"] == "bullets":
                    for it in m["items"]:
                        text = (it.get("label") + "：" if it.get("label") else "") + it.get("desc", "")
                        text = text.strip("：").strip()
                        if text:
                            picked.append(text)
            if picked:
                payload["key_points"] = picked[:10]
        if not payload["decisions"]:
            for m in modules:
                if m["type"] == "callout" and any(k in m["title"] for k in ("决议", "结论", "共识")):
                    payload["decisions"] = list(m["items"])
                    break
        if not payload["action_items"]:
            for m in modules:
                if m["type"] == "actions":
                    payload["action_items"] = [
                        {"task": it["task"], "owner": it["owner"], "due_date": it["due"]}
                        for it in m["items"]
                    ]
                    break
        if not payload["risks"]:
            for m in modules:
                if m["type"] == "callout" and any(k in m["title"] for k in ("风险", "未决", "问题")):
                    payload["risks"] = list(m["items"])
                    break

    def _fallback_summary(self, items: List[TranscriptItem], title: str) -> Dict:
        """LLM 不可用时的极简结构化摘要，也产出 modules 让前端只走一条渲染分支。"""
        texts = [x.text for x in items if x.text]
        joined = " ".join(texts)
        summary = joined[:60] + ("…" if len(joined) > 60 else "")
        key_points = [f"{x.speaker}：{x.text}" for x in items[-5:] if x.text]
        actions = self._extract_action_items(items)
        modules: List[Dict[str, Any]] = []
        if key_points:
            modules.append({
                "no": "01",
                "title": "近期发言要点",
                "intro": "（LLM 未配置，按规则提取最新 5 条发言）",
                "type": "bullets",
                "items": [{"label": "", "desc": p} for p in key_points],
            })
        if actions:
            modules.append({
                "no": f"{len(modules) + 1:02d}",
                "title": "可能的行动项",
                "intro": "（按关键字粗筛，请人工复核）",
                "type": "actions",
                "items": [
                    {"task": a["task"], "owner": a.get("owner", ""), "due": a.get("due_date", "")}
                    for a in actions
                ],
            })
        return {
            "title": title,
            "summary": summary,
            "background": (
                (joined[:200] + "…")
                if len(joined) > 200
                else (joined or "（未配置 LLM，无深度归纳的会议背景）")
            ),
            "modules": modules,
            "key_points": key_points,
            "decisions": [],
            "action_items": actions,
            "risks": [],
        }

    # -------------------------------------------------------------------
    # 章节：基于 LLM 给出 "精炼标题 + 2-4 句摘要 + 起止时间" 的章节列表
    # -------------------------------------------------------------------

    async def chapters(self, items: List[TranscriptItem]) -> List[Dict]:
        """返回 [{idx, start_ms, end_ms, title, summary, speakers, line_count}]。"""
        if not items:
            return []
        if self._llm:
            out = await self._chapters_with_llm(items)
            if out:
                return self._postprocess_chapters(out)
        return self._postprocess_chapters(self._fallback_chapters(items))

    async def _chapters_with_llm(self, items: List[TranscriptItem]) -> Optional[List[Dict]]:
        # 超长会议软截断：prompt 过长易被 LLM 拒绝；行数可配置（默认最近 120 条）
        window_n = max(
            40,
            min(
                200,
                cfg(
                    "meeting.chapters.prompt_max_lines",
                    "SPEAKER_CHAPTER_PROMPT_LINES",
                    120,
                    int,
                ),
            ),
        )
        window = items[-window_n:]
        base_idx = len(items) - len(window)
        numbered: List[str] = []
        for i, it in enumerate(window):
            ts = _fmt_mmss(it.start_ms or 0)
            speaker = it.speaker or "未知"
            text = (it.text or "").replace("\n", " ").strip()
            numbered.append(f"[{i}][{ts}][{speaker}] {text}")

        prompt = (
            "以下是会议转录，每行格式为 [行号][mm:ss][说话人] 内容。\n"
            "请按话题变化切分章节（每章建议覆盖若干连续行，不要跳过或重叠），\n"
            "并为每章生成中文的【精炼标题】+【2-4 句话摘要】。\n"
            "严格只返回 JSON，格式如下：\n"
            '{"chapters":[{"start_idx":0,"end_idx":7,"title":"...","summary":"..."}]}\n'
            "规则：\n"
            "1. start_idx/end_idx 是上面给出的【行号】整数，闭区间，end_idx >= start_idx。\n"
            "2. title 控制在 10~20 个汉字，概括主题，不要以时间/发言人开头。\n"
            "3. summary 2-4 句话，点出这一章谁在讲、讲了什么、得出了什么结论。\n"
            "4. 章节数量通常 3~15 个，不要无限细分。\n"
            "5. 相邻章节在时间轴上不要重叠；若某段内容很少（不足约 40 秒且行数很少），"
            "并入前后同一话题的一章，不要单立成章。\n"
            "6. 不要输出任何解释性文字，只输出 JSON。\n\n"
            "转录：\n" + "\n".join(numbered)
        )
        try:
            text = await self._llm.reply(
                prompt,
                system_prompt="你是专业会议纪要助手，擅长把连续对话切分为有意义的章节并起标题。",
            )
        except Exception:
            return None

        parsed = self._parse_chapters_json(text)
        if not parsed:
            return None

        out: List[Dict[str, Any]] = []
        total = len(window)
        for ch in parsed[: self._MAX_CHAPTERS]:
            try:
                s_idx = int(ch.get("start_idx"))
                e_idx = int(ch.get("end_idx"))
            except (TypeError, ValueError):
                continue
            if s_idx < 0 or e_idx < s_idx or s_idx >= total:
                continue
            e_idx = min(e_idx, total - 1)
            seg = window[s_idx:e_idx + 1]
            if not seg:
                continue
            start_ms = int(seg[0].start_ms or 0)
            end_ms = int(max(x.end_ms or x.start_ms or 0 for x in seg))
            speakers: List[str] = []
            seen = set()
            for x in seg:
                sp = x.speaker or "未知"
                if sp not in seen:
                    seen.add(sp)
                    speakers.append(sp)
            title = str(ch.get("title") or "").strip() or self._first_n(seg[0].text, 16)
            summary = str(ch.get("summary") or "").strip()
            out.append({
                "idx": len(out) + 1,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "title": title[:40],
                "summary": summary,
                "speakers": speakers,
                "line_count": len(seg),
                # 原始 line index（相对 items 全集）便于前端二次联动
                "start_line": base_idx + s_idx,
                "end_line": base_idx + e_idx,
            })
        return out or None

    def _postprocess_chapters(self, chapters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按开始时间排序；合并时间重叠的相邻章；将过短片段并入上一章。"""
        if not chapters:
            return []
        rows = sorted(chapters, key=lambda c: int(c.get("start_ms") or 0))
        grace = self._CHAPTER_OVERLAP_GRACE_MS
        tiny_ms = self._CHAPTER_TINY_MAX_MS
        tiny_lines = self._CHAPTER_TINY_MAX_LINES
        tiny_gap = self._CHAPTER_TINY_GAP_MS

        def _merge_into(prev: Dict[str, Any], cur: Dict[str, Any]) -> None:
            pe = int(prev.get("end_ms") or prev.get("start_ms") or 0)
            ce = int(cur.get("end_ms") or cur.get("start_ms") or 0)
            prev["end_ms"] = max(pe, ce)
            prev["line_count"] = int(prev.get("line_count") or 0) + int(cur.get("line_count") or 0)
            sl_p, el_p = prev.get("start_line"), prev.get("end_line")
            sl_c, el_c = cur.get("start_line"), cur.get("end_line")
            if sl_p is not None and el_c is not None:
                try:
                    prev["start_line"] = min(int(sl_p), int(sl_c or sl_p))
                    prev["end_line"] = max(int(el_p or sl_p), int(el_c or el_p))
                except (TypeError, ValueError):
                    pass
            psum = str(prev.get("summary") or "").strip()
            csum = str(cur.get("summary") or "").strip()
            prev["summary"] = (psum + " " + csum).strip()[:900]
            t_prev = str(prev.get("title") or "").strip()
            t_cur = str(cur.get("title") or "").strip()
            if len(t_cur) > len(t_prev):
                prev["title"] = t_cur[:40]
            sp_a = list(prev.get("speakers") or [])
            seen = set(sp_a)
            for s in cur.get("speakers") or []:
                if s and s not in seen:
                    seen.add(s)
                    sp_a.append(s)
            prev["speakers"] = sp_a

        merged: List[Dict[str, Any]] = []
        for ch in rows:
            cur = dict(ch)
            if not merged:
                merged.append(cur)
                continue
            prev = merged[-1]
            pe = int(prev.get("end_ms") or prev.get("start_ms") or 0)
            cs = int(cur.get("start_ms") or 0)
            ce = int(cur.get("end_ms") or cs)
            dur = max(0, ce - cs)
            lines_c = int(cur.get("line_count") or 0)
            overlap = cs < pe - grace
            gap_after_prev = cs - pe if cs >= pe else 0
            tiny = dur <= tiny_ms and lines_c <= tiny_lines
            merge_tiny = tiny and gap_after_prev <= tiny_gap and not overlap

            if overlap or merge_tiny:
                _merge_into(prev, cur)
            else:
                merged.append(cur)

        for i, x in enumerate(merged, 1):
            x["idx"] = i
        return merged

    @staticmethod
    def _parse_chapters_json(text: str) -> Optional[List[Dict]]:
        """尽量宽容地解析 LLM 返回的 JSON。"""
        if not text:
            return None
        # 先尝试原样解析
        for attempt in (text, text.strip()):
            try:
                obj = json.loads(attempt)
            except Exception:
                obj = None
            if isinstance(obj, dict) and isinstance(obj.get("chapters"), list):
                return obj["chapters"]
            if isinstance(obj, list):
                return obj
        # 抠出最外层 JSON（应对带 ```json ... ``` 这种）
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except Exception:
            return None
        if isinstance(obj, dict) and isinstance(obj.get("chapters"), list):
            return obj["chapters"]
        return None

    def _fallback_chapters(self, items: List[TranscriptItem]) -> List[Dict]:
        """LLM 不可用时的规则章节：静默 gap / 最大时长切章 + 首句截断标题。"""
        out: List[Dict[str, Any]] = []
        if not items:
            return out
        cur_lines: List[TranscriptItem] = [items[0]]
        for it in items[1:]:
            gap = (it.start_ms or 0) - (cur_lines[-1].end_ms or cur_lines[-1].start_ms or 0)
            span = (it.end_ms or it.start_ms or 0) - (cur_lines[0].start_ms or 0)
            if gap > self._FALLBACK_GAP_MS or span > self._FALLBACK_MAX_MS:
                out.append(self._pack_fallback_chapter(cur_lines, len(out) + 1))
                cur_lines = []
            cur_lines.append(it)
        if cur_lines:
            out.append(self._pack_fallback_chapter(cur_lines, len(out) + 1))
        return out

    def _pack_fallback_chapter(self, seg: List[TranscriptItem], idx: int) -> Dict[str, Any]:
        speakers: List[str] = []
        seen = set()
        for x in seg:
            sp = x.speaker or "未知"
            if sp not in seen:
                seen.add(sp)
                speakers.append(sp)
        joined = "；".join(x.text for x in seg if x.text)
        return {
            "idx": idx,
            "start_ms": int(seg[0].start_ms or 0),
            "end_ms": int(max(x.end_ms or x.start_ms or 0 for x in seg)),
            "title": self._first_n(seg[0].text, 16) or "（无文本）",
            "summary": (joined[:160] + ("…" if len(joined) > 160 else "")),
            "speakers": speakers,
            "line_count": len(seg),
        }

    @staticmethod
    def _first_n(text: str, n: int) -> str:
        t = (text or "").replace("\n", " ").strip()
        return t[:n]

    @staticmethod
    def _extract_action_items(items: List[TranscriptItem]) -> List[Dict]:
        marks = ("待办", "行动项", "TODO", "需要", "安排", "下周", "明天", "负责")
        out = []
        for it in items:
            if any(k in it.text for k in marks):
                out.append({"task": it.text, "owner": it.speaker, "due_date": ""})
        return out[:10]
