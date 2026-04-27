# EchoPass · 实时语音会议助手后端 HTTP/WS API
#
# 启动前务必 cd 到仓库根目录，否则找不到包 echopass：
#   cd /path/to/EchoPass
#   pip install -r requirements.txt
#   export MODELSCOPE_CACHE=~/.cache/modelscope        # 可选
#
# 直接启动：
#   ./scripts/run.sh                                    # 推荐
# 或：
#   uvicorn echopass.app:app --host 0.0.0.0 --port 8765
#
# HTTPS（自签证书示例，麦克风跨机访问常用）：
#   uvicorn echopass.app:app --host 0.0.0.0 --port 8765 \
#     --ssl-keyfile /path/to/key.pem --ssl-certfile /path/to/cert.pem
#
# 说话人声纹：默认不连库（纯内存，重启后注册丢失）。需要持久化时再配 PostgreSQL：
#   export SPEAKER_DEMO_PG_DSN="postgresql://..."      # 启用 PG（env 名沿用，向后兼容）
#   export SPEAKER_DEMO_PG_DSN=""                      # 与默认一致，仅用内存
# 建表：psql ... -f sql/schema.sql
#
# ASR 后端：火山引擎云端流式 ASR（openspeech v2 WebSocket）
#   export SPEAKER_VOLC_ASR_APPID=...         # 必需
#   export SPEAKER_VOLC_ASR_TOKEN=...         # 必需
#   export SPEAKER_VOLC_ASR_CLUSTER=...       # 必需
#   export SPEAKER_VOLC_ASR_WS_URL=wss://openspeech.bytedance.com/api/v2/asr   # 可选
#   export SPEAKER_VOLC_ASR_LANGUAGE=zh-CN    # 可选
#   export SPEAKER_VOLC_ASR_WORKFLOW=...      # 可选
#   export SPEAKER_VOLC_ASR_UID=echopass      # 可选
#   export SPEAKER_VOLC_ASR_SEG_MS=15000      # 可选
#
# KWS（关键词唤醒）仍走本地 FunASR，如需指定权重目录可保留：
#   export SPEAKER_FUNASR_BASE=/path/to/dir
#
# LLM 纠错（可选，兼容 OpenAI API 格式）：
#   export SPEAKER_LLM_API_URL=http://your-llm:8080/v1/chat/completions
#   export SPEAKER_LLM_API_KEY=your-api-key
#   export SPEAKER_LLM_MODEL=qwen2.5-7b-instruct
#   export SPEAKER_ASR_LLM_CORRECTION=0                # 0=关闭(默认), 1=开启 ASR 每段纠错
#   export SPEAKER_ASR_HOTWORD="张三 李四 产品A"        # 热词，空格分隔；前端 query 参数优先级更高
#
# 唤醒词（可选，默认「小云小云」，模型 iic/speech_charctc_kws_phone-xiaoyun）：
#   export SPEAKER_KWS_KEYWORDS=小云小云
#   export SPEAKER_KWS_THRESHOLD=0.75
#
# TTS：HTTP 模式为 OpenAI 兼容 POST /v1/audio/speech（需配置 SPEAKER_TTS_URL）；
# 或火山豆包双向流式（见下行）。PCM 转 WAV 参数见 SPEAKER_TTS_PCM_*。
#   export SPEAKER_TTS_URL=http://your-tts:8080/v1
#   export SPEAKER_TTS_API_KEY=none
#   export SPEAKER_TTS_VOICE=default
#   export SPEAKER_TTS_MODEL=tts-1
#   export SPEAKER_TTS_PROVIDER=openai                 # openai | volc_bidirection
#   export SPEAKER_TTS_PCM_SAMPLE_RATE=24000
#   export SPEAKER_TTS_PCM_CHANNELS=1
#   export SPEAKER_TTS_PCM_SAMPLE_WIDTH=2
#
# 火山豆包「双向流式 TTS」V3（https://www.volcengine.com/docs/6561/1329505）：
#   export SPEAKER_TTS_PROVIDER=volc_bidirection
#   export SPEAKER_TTS_VOLC_WS_URL=wss://openspeech.bytedance.com/api/v3/tts/bidirection  # 可选
#   export SPEAKER_TTS_VOLC_APPID=...                  # 可选；空则沿用 SPEAKER_VOLC_ASR_APPID
#   export SPEAKER_TTS_VOLC_ACCESS_KEY=...             # 可选；空则沿用 SPEAKER_VOLC_ASR_TOKEN
#   export SPEAKER_TTS_VOLC_RESOURCE_ID=seed-tts-2.0   # 2.0 音色；1.0 公版可用 volc.service_type.10029
#   export SPEAKER_TTS_VOLC_SPEAKER=zh_female_xxx_moon_bigtts     # 豆包音色 ID（见控制台音色列表）
#
# 浏览器：HTTP 为 http://<IP>:8765/ ，HTTPS 为 https://<IP>:8765/（自签证书需点「高级」继续）
import asyncio
import base64
import datetime as _dt
import io
import json
import os
import re
import sys
import logging
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

# ── 在 funasr 导入之前彻底关闭所有 tqdm 进度条 ─────────────────────────
# FunASR 内部 VAD/ASR/标点/KWS 会各自创建 tqdm；disable_pbar 在部分子模型上不生效。
# 这里做三层防护：
#   1) __init__ 里强制注入 disable=True
#   2) display/refresh/update 打印相关方法改为空操作，彻底兜底
#   3) 对 tqdm.std / tqdm.auto / tqdm.asyncio / tqdm.notebook 同步打补丁
os.environ.setdefault("TQDM_DISABLE", "1")


def _silence_tqdm() -> None:
    import importlib

    _noop = lambda *a, **k: None

    for mod_name in ("tqdm.std", "tqdm.auto", "tqdm.asyncio", "tqdm.notebook", "tqdm"):
        try:
            mod = importlib.import_module(mod_name)
        except Exception:  # noqa: BLE001
            continue
        cls = getattr(mod, "tqdm", None)
        if cls is None or not isinstance(cls, type):
            continue
        orig_init = cls.__init__

        def _patched_init(self, *args, __orig=orig_init, **kwargs):
            kwargs["disable"] = True
            return __orig(self, *args, **kwargs)

        try:
            cls.__init__ = _patched_init
            cls.display = _noop
            cls.refresh = _noop
            cls.update = _noop
            cls.close = _noop
            cls.set_description = _noop
            cls.set_description_str = _noop
            cls.set_postfix = _noop
            cls.set_postfix_str = _noop
        except (TypeError, AttributeError):
            pass


try:
    _silence_tqdm()
except Exception:  # noqa: BLE001
    pass

import threading
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import torch
import torchaudio
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from echopass.engine import (
    CamPlusSpeakerEngine,
    DEFAULT_MODEL_ID,
    StreamingASREngine,
    LLMCorrector,
    KWSEngine,
)
from echopass.agent.dialogue_manager import DialogueManager
from echopass.agent.llm_client import LLMChatClient
from echopass.meeting.summarizer import MeetingSummarizer
from echopass.meeting.transcript_buffer import TranscriptBuffer
from echopass.transport.schemas import event_message
from echopass.transport.websocket_server import WebSocketHub
from echopass.config import cfg, config_path, to_bool

SIM_THRESHOLD = cfg("speaker.threshold", "SPEAKER_DEMO_THRESHOLD", 0.45, float)
MODEL_ID = cfg("speaker.model_id", "SPEAKER_DEMO_MODEL_ID", DEFAULT_MODEL_ID, str)
logger = logging.getLogger("echopass")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s", "%H:%M:%S"))
    logger.addHandler(_h)
    logger.propagate = False
logger.info("配置文件: %s", config_path())

# PostgreSQL 仅用于说话人持久化；勿在代码中写真实 DSN。可用 SPEAKER_DEMO_PG_DSN 或 yaml 配置；
# 空字符串表示禁用持久化、仅用内存。
_DEMO_PG_DSN = ""
_pg_dsn_raw = cfg("speaker.pg_dsn", "SPEAKER_DEMO_PG_DSN", _DEMO_PG_DSN, str, allow_empty=True)
PG_DSN = _pg_dsn_raw if _pg_dsn_raw else None

engine = CamPlusSpeakerEngine(
    repo_root=REPO_ROOT, model_id=MODEL_ID, pg_dsn=PG_DSN
)
# ASR 引擎：火山引擎云端流式 ASR。base_dir 保留仅为向后兼容签名（已不再使用）。
# KWS 仍用本地 FunASR，权重目录沿用 asr.funasr_base / SPEAKER_FUNASR_BASE。
_funasr_base = cfg("asr.funasr_base", "SPEAKER_FUNASR_BASE", "", str)
FUNASR_BASE = Path(_funasr_base).resolve() if _funasr_base else (REPO_ROOT / "pretrained" / "funasr")
asr_engine = StreamingASREngine(base_dir=FUNASR_BASE)

# LLM：仓库不含密钥；在 config 或环境变量中填写 llm.api_url / api_key / model。
_llm_url   = cfg("llm.api_url", "SPEAKER_LLM_API_URL", "", str)
_llm_key   = cfg("llm.api_key", "SPEAKER_LLM_API_KEY", "", str)
_llm_model = cfg("llm.model",   "SPEAKER_LLM_MODEL",   "", str)
_asr_llm_correction = cfg("llm.asr_correction", "SPEAKER_ASR_LLM_CORRECTION", False, to_bool)
# ASR 热词（前端 query 参数 hotword 优先级更高）
_asr_hotword_env = cfg("asr.hotword", "SPEAKER_ASR_HOTWORD", "", str)
llm_corrector = LLMCorrector(api_url=_llm_url, api_key=_llm_key, model=_llm_model)
llm_chat = LLMChatClient(api_url=_llm_url, api_key=_llm_key, model=_llm_model)

# 唤醒词引擎（可选，关键词通过 yaml/env 配置）
# kws.enabled 默认 false：不加载本地 CTC 唤醒模型；设为 true 才启用「小云小云」检测
_kws_enabled = cfg("kws.enabled", "SPEAKER_KWS_ENABLED", False, to_bool)
_kws_keywords = cfg("kws.keywords", "SPEAKER_KWS_KEYWORDS", "小云小云", str)
_kws_threshold = cfg("kws.threshold", "SPEAKER_KWS_THRESHOLD", 0.75, float)
kws_engine = KWSEngine(
    keywords=_kws_keywords, threshold=_kws_threshold, enabled=_kws_enabled
)

def _normalize_tts_provider(val: object) -> str:
    s = str(val or "").strip().lower()
    return s if s in ("openai", "volc_bidirection") else "volc_bidirection"


# TTS：默认不连任何内网服务；在 yaml/env 中配置 tts.url（OpenAI 兼容）或火山 volc_bidirection 凭据。
# tts.url 允许空串，表示未配置 HTTP TTS（例如仅用火山 TTS）。
_tts_url   = cfg(
    "tts.url", "SPEAKER_TTS_URL",
    "", str,
    allow_empty=True,
)
_tts_key   = cfg("tts.api_key",     "SPEAKER_TTS_API_KEY",     "none", str)
_tts_voice = cfg("tts.voice",       "SPEAKER_TTS_VOICE",       "default", str)
_tts_model = cfg("tts.model",       "SPEAKER_TTS_MODEL",       "tts-1", str)
_tts_provider = cfg(
    "tts.provider", "SPEAKER_TTS_PROVIDER", "volc_bidirection", _normalize_tts_provider,
)
_tts_pcm_sample_rate  = cfg("tts.pcm.sample_rate",  "SPEAKER_TTS_PCM_SAMPLE_RATE",  24000, int)
_tts_pcm_channels     = cfg("tts.pcm.channels",     "SPEAKER_TTS_PCM_CHANNELS",     1, int)
_tts_pcm_sample_width = cfg("tts.pcm.sample_width", "SPEAKER_TTS_PCM_SAMPLE_WIDTH", 2, int)

# 火山豆包「双向流式 TTS」V3（https://www.volcengine.com/docs/6561/1329505）
# appid / access_key 留空时自动回落到 asr.volc.appid / asr.volc.token
_tts_volc_ws_url = cfg(
    "tts.volc.ws_url", "SPEAKER_TTS_VOLC_WS_URL",
    "wss://openspeech.bytedance.com/api/v3/tts/bidirection", str,
)
_tts_volc_appid = (cfg("tts.volc.appid", "SPEAKER_TTS_VOLC_APPID", "", str) or "").strip()
if not _tts_volc_appid:
    _tts_volc_appid = (cfg("asr.volc.appid", "SPEAKER_VOLC_ASR_APPID", "", str) or "").strip()
_tts_volc_access_key = (cfg("tts.volc.access_key", "SPEAKER_TTS_VOLC_ACCESS_KEY", "", str) or "").strip()
if not _tts_volc_access_key:
    _tts_volc_access_key = (cfg("asr.volc.token", "SPEAKER_VOLC_ASR_TOKEN", "", str) or "").strip()
# 默认 seed-tts-2.0：与多数新公版「大模型 2.0」音色一致；若只用 1.0 音色请改 volc.service_type.10029
_tts_volc_resource_id = cfg(
    "tts.volc.resource_id", "SPEAKER_TTS_VOLC_RESOURCE_ID",
    "seed-tts-2.0", str,
)
_tts_volc_speaker = (cfg("tts.volc.speaker", "SPEAKER_TTS_VOLC_SPEAKER", "", str) or "").strip()
_tts_volc_speech_rate = cfg("tts.volc.speech_rate", "SPEAKER_TTS_VOLC_SPEECH_RATE", 0, int)
_tts_volc_loudness_rate = cfg("tts.volc.loudness_rate", "SPEAKER_TTS_VOLC_LOUDNESS_RATE", 0, int)


def _tts_backend_ready() -> bool:
    if _tts_provider == "volc_bidirection":
        return bool(_tts_volc_appid and _tts_volc_access_key)
    return bool(_tts_url)


ws_hub = WebSocketHub()
transcript_buffer = TranscriptBuffer()
_assistant_ttl_sec = cfg("assistant.ttl_sec", "SPEAKER_ASSISTANT_TTL_SEC", 25, int)
# 会中助手多轮对话：仅保留最近 N 轮 user+assistant（按 session_id 分桶，进程内内存，重启即失）
_assistant_chat_turns = max(0, cfg("assistant.chat_history_turns", "SPEAKER_ASSISTANT_CHAT_TURNS", 8, int))
_assistant_chat_lock = threading.Lock()
_assistant_chat_history: Dict[str, List[Dict[str, str]]] = {}


def _assistant_history_get(session_id: str) -> List[Dict[str, str]]:
    with _assistant_chat_lock:
        h = _assistant_chat_history.get(session_id)
        return list(h) if h else []


def _assistant_history_append(session_id: str, user_content: str, assistant_content: str) -> None:
    if _assistant_chat_turns <= 0:
        return
    if not session_id or not user_content or not (assistant_content or "").strip():
        return
    with _assistant_chat_lock:
        h = _assistant_chat_history.setdefault(session_id, [])
        h.append({"role": "user", "content": user_content})
        h.append({"role": "assistant", "content": (assistant_content or "").strip()})
        max_pair_msgs = _assistant_chat_turns * 2
        if len(h) > max_pair_msgs:
            _assistant_chat_history[session_id] = h[-max_pair_msgs:]


def _assistant_history_clear_session_ids(*session_ids: str) -> None:
    with _assistant_chat_lock:
        for sid in session_ids:
            if sid:
                _assistant_chat_history.pop(sid, None)


def _assistant_messages_for_llm(
    session_id: str, system_prompt: str, current_user_prompt: str,
) -> List[Dict[str, str]]:
    return (
        [{"role": "system", "content": system_prompt}]
        + _assistant_history_get(session_id)
        + [{"role": "user", "content": current_user_prompt}]
    )


def _safe_wake_ack_audio_path(raw: str) -> str:
    """仅允许 static 目录下的相对路径，防止路径穿越。"""
    s = (raw or "").strip().replace("\\", "/")
    if not s or ".." in s:
        return ""
    s = s.lstrip("/")
    parts = [p for p in s.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        return ""
    return "/".join(parts)


_wake_ack_audio = _safe_wake_ack_audio_path(
    cfg("assistant.wake_ack_audio", "SPEAKER_WAKE_ACK_AUDIO", "", str),
)
dialogue_manager = DialogueManager(ttl_sec=_assistant_ttl_sec)
meeting_summarizer = MeetingSummarizer(llm_chat_client=llm_chat)

app = FastAPI(title="EchoPass 实时语音会议助手", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 启动预热：把 CAM++ / FunASR ASR / KWS 三个模型在服务起来之前就加载好 ──
# 默认开启；如需保留懒加载（例如调试时想快速重启），设 SPEAKER_PRELOAD_MODELS=0
_PRELOAD_MODELS = cfg("preload_models", "SPEAKER_PRELOAD_MODELS", True, to_bool)


@app.on_event("startup")
async def _preload_models() -> None:
    if not _PRELOAD_MODELS:
        logger.info("模型预加载已关闭（SPEAKER_PRELOAD_MODELS=0），将使用懒加载")
        return

    import asyncio
    import time

    loop = asyncio.get_event_loop()

    async def _warm(name: str, fn) -> None:
        t0 = time.perf_counter()
        try:
            # 兼容 Python 3.8（无 asyncio.to_thread）：用默认线程池执行同步加载
            await loop.run_in_executor(None, fn)
            logger.info("预加载完成: %s (%.2fs)", name, time.perf_counter() - t0)
        except Exception as e:  # noqa: BLE001
            logger.exception("预加载失败: %s -> %s", name, e)

    logger.info(
        "开始预加载模型 (CAM++ / 火山 ASR%s) ...",
        " / KWS" if _kws_enabled else "",
    )
    tasks = [
        _warm("CAM++ 说话人模型", engine._ensure_model),
        _warm("火山云端 ASR", asr_engine._ensure_model),
    ]
    if _kws_enabled:
        tasks.append(_warm("KWS 唤醒词模型", kws_engine._ensure_model))
    await asyncio.gather(*tasks)
    logger.info("全部模型预加载完成")


class ScoreItem(BaseModel):
    name: str
    score: float


class IdentifyResponse(BaseModel):
    speaker: Optional[str] = None
    score: float
    threshold: float
    scores: List[ScoreItem]
    message: Optional[str] = None


class RecognizeResponse(BaseModel):
    speaker: Optional[str] = None
    score: float
    threshold: float
    scores: List[ScoreItem]
    text: str = ""          # 最终文本（经 LLM 纠错后，若未开启则与 text_raw 相同）
    text_raw: str = ""      # ASR 原始文本
    llm_corrected: bool = False
    start_ms: int = 0       # 相对会议开始的毫秒偏移（前端上传的 offset_ms）
    end_ms: int = 0         # start_ms + 本段音频时长
    duration_sec: float = 0.0
    message: Optional[str] = None


class AssistantReplyRequest(BaseModel):
    text: str
    session_id: str = "default"
    speaker: Optional[str] = None
    use_tts: bool = False
    # 可选：实时会议的 session_id，用于把已有的会议纪要作为上下文带给 LLM。
    # 典型来自前端 liveSessionId（形如 sess_xxx），缺省时退回 session_id 自己。
    meeting_session_id: Optional[str] = None
    # 仅火山 TTS：覆盖 config 中的 tts.volc.speaker / tts.voice
    tts_voice: Optional[str] = None


class MeetingSummaryRequest(BaseModel):
    session_id: str = "default"
    title: str = "会议纪要"


class MeetingChaptersRequest(BaseModel):
    session_id: str = "default"


async def emit_event(event_type: str, session_id: str = "default", payload: Optional[Dict[str, Any]] = None) -> None:
    await ws_hub.emit(event_message(event_type=event_type, session_id=session_id, payload=payload or {}), session_id=session_id)


_MEETING_CTX_MAX_ITEMS = cfg("assistant.meeting_ctx.max_items", "SPEAKER_MEETING_CTX_ITEMS", 20, int)
_MEETING_CTX_MAX_CHARS = cfg("assistant.meeting_ctx.max_chars", "SPEAKER_MEETING_CTX_CHARS", 1500, int)


def _volc_tts_speaker_effective(tts_voice: Optional[str]) -> str:
    v = (tts_voice or "").strip()
    if v:
        return v
    if (_tts_volc_speaker or "").strip():
        return _tts_volc_speaker.strip()
    if _tts_voice and str(_tts_voice).strip().lower() != "default":
        return str(_tts_voice).strip()
    return ""


def _flush_volc_stream_tts_buf(buf: str) -> Tuple[List[str], str]:
    """按标点切分 TTS 片段；剩余留在 buf。"""
    segs: List[str] = []
    rest = buf
    strong = set("。！？\n")
    weak = set("，、；：")
    while rest:
        cut = -1
        for i, ch in enumerate(rest):
            if ch in strong:
                cut = i
                break
            if ch in weak and i >= 6:
                cut = i
                break
        if cut >= 0:
            piece = rest[: cut + 1].strip()
            rest = rest[cut + 1 :]
            if piece:
                segs.append(piece)
            continue
        if len(rest) >= 36:
            segs.append(rest[:18].strip())
            rest = rest[18:]
            continue
        break
    return [s for s in segs if s], rest


def _build_meeting_context(session_id: str) -> str:
    """取 transcript_buffer 里最近的若干条发言，拼成可读的上下文喂给 LLM。"""
    items = transcript_buffer.list_items(session_id)
    if not items:
        return ""
    items = items[-_MEETING_CTX_MAX_ITEMS:]
    lines: List[str] = []
    total = 0
    for it in reversed(items):  # 优先保留最近的发言，向前累加到字符上限
        line = f"{it.speaker}：{it.text.strip()}"
        if not line.strip():
            continue
        if total + len(line) > _MEETING_CTX_MAX_CHARS and lines:
            break
        lines.append(line)
        total += len(line)
    lines.reverse()
    return "\n".join(lines)


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "model_ready": engine._model is not None,
        "asr_ready": asr_engine._model is not None,
        "asr_provider": "volcengine",
        "asr_ws_url": asr_engine.ws_url,
        # 兼容保留（前端 status lamp 仍引用这两个字段）：云端引擎下值固定
        "funasr_base": str(FUNASR_BASE),
        "funasr_local_weights": True,
        "llm_correction": bool(llm_corrector) and _asr_llm_correction,
        "kws_enabled": _kws_enabled,
        "kws_keywords": _kws_keywords,
        "kws_threshold": _kws_threshold,
        "kws_ready": (not _kws_enabled) or (kws_engine._model is not None),
        "tts_enabled": _tts_backend_ready(),
        "tts_provider": _tts_provider,
        "device": str(engine._device),
        "model_id": MODEL_ID,
        "threshold": SIM_THRESHOLD,
        "persistence": "postgresql" if engine._pg_dsn else "memory",
        "speakers": engine.list_speakers(),
        # 相对 echopass/static/，如 audio/wake_ack.mp3；空表示不播放
        "wake_ack_audio": _wake_ack_audio,
    }


@app.websocket("/ws/control")
async def ws_control(websocket: WebSocket, session_id: str = "global"):
    await ws_hub.connect(websocket, session_id=session_id)
    # 注意：send_json 也要包在 try 里。客户端在握手完成后立刻关页/刷新/掉网，
    # 首帧 send 就会抛 WebSocketDisconnect(code=1006)；不捕的话会冒成红色 ERROR。
    try:
        await websocket.send_json(
            event_message(
                event_type="ws_connected",
                session_id=session_id,
                payload={"assistant_active": dialogue_manager.is_active(session_id)},
            )
        )
        while True:
            msg = await websocket.receive_json()
            cmd = str(msg.get("command", "")).strip().lower()
            if cmd == "ping":
                await websocket.send_json(event_message("pong", session_id=session_id, payload={"ok": True}))
            elif cmd == "assistant_stop":
                dialogue_manager.stop(session_id)
                await emit_event("assistant_session_stopped", session_id, {"source": "ws_command"})
            elif cmd == "meeting_summary_requested":
                await emit_event("meeting_summary_requested", session_id, {"source": "ws_command"})
            else:
                await websocket.send_json(event_message("ws_unknown_command", session_id=session_id, payload={"command": cmd}))
    except WebSocketDisconnect:
        # 客户端正常断开（关页/刷新/网络抖断），属预期行为，不打日志
        pass
    except Exception:  # noqa: BLE001
        # 非预期异常才上报，仍走 finally 做清理
        logger.exception("/ws/control 未预期异常 session_id=%s", session_id)
    finally:
        await ws_hub.disconnect(websocket, session_id=session_id)


@app.get("/api/speakers")
def list_speakers():
    return {"speakers": engine.list_speakers()}


@app.post("/api/enroll")
async def enroll(
    name: str = Form(...),
    audio: UploadFile = File(...),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "name 不能为空")
    raw = await audio.read()
    if not raw:
        raise HTTPException(400, "未收到音频")
    suffix = Path(audio.filename or "x.wav").suffix or ".wav"
    try:
        emb = engine.embedding_from_upload(raw, suffix=suffix)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    engine.enroll(name, emb)
    return {"ok": True, "name": name, "speakers": engine.list_speakers()}


@app.post("/api/identify_file")
async def identify_file(
    audio: UploadFile = File(...),
    threshold: Optional[float] = None,
):
    th = threshold if threshold is not None else SIM_THRESHOLD
    raw = await audio.read()
    if not raw:
        raise HTTPException(400, "未收到音频")
    suffix = Path(audio.filename or "x.wav").suffix or ".wav"
    try:
        emb = engine.embedding_from_upload(raw, suffix=suffix)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    spk, best, scores = engine.identify(emb, th)
    score_items = [ScoreItem(name=n, score=s) for n, s in scores]
    return IdentifyResponse(
        speaker=spk,
        score=best,
        threshold=th,
        scores=score_items,
        message=None if spk else ("未匹配已注册说话人（可提高阈值或重新注册）" if scores else "请先注册说话人"),
    )


@app.post("/api/identify_pcm")
async def identify_pcm(
    request: Request,
    sample_rate: int,
    threshold: Optional[float] = None,
):
    """原始 float32 小端 PCM，单声道；sample_rate 为浏览器 AudioContext 采样率（常见 48000）。"""
    th = threshold if threshold is not None else SIM_THRESHOLD
    body = await request.body()
    if len(body) < 256:
        raise HTTPException(400, "PCM 过短")
    if len(body) % 4 != 0:
        raise HTTPException(400, "PCM 长度须为 4 的倍数（float32）")
    pcm = np.frombuffer(body, dtype=np.float32)
    try:
        emb = engine.embedding_from_pcm_float32(pcm, sample_rate)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    spk, best, scores = engine.identify(emb, th)
    score_items = [ScoreItem(name=n, score=s) for n, s in scores]
    return IdentifyResponse(
        speaker=spk,
        score=best,
        threshold=th,
        scores=score_items,
        message=None if spk else ("未匹配已注册说话人" if scores else "请先注册说话人"),
    )


@app.post("/api/recognize_pcm")
async def recognize_pcm(
    request: Request,
    sample_rate: int,
    session_id: str = "default",
    is_final: bool = False,
    threshold: Optional[float] = None,
    hotword: Optional[str] = None,
    offset_ms: int = 0,
):
    """说话人识别 + 流式语音转录。浏览器每 2.5s 发一段 PCM，服务端返回说话人和文本。"""
    th = threshold if threshold is not None else SIM_THRESHOLD
    body = await request.body()
    if len(body) < 256:
        raise HTTPException(400, "PCM 过短")
    if len(body) % 4 != 0:
        raise HTTPException(400, "PCM 长度须为 4 的倍数（float32）")
    pcm = np.frombuffer(body, dtype=np.float32)
    # 本段音频时长（按请求里的 sample_rate 折算；与重采样无关）
    duration_sec = float(pcm.size) / float(sample_rate) if sample_rate > 0 else 0.0
    start_ms = int(offset_ms or 0)
    end_ms = int(start_ms + round(duration_sec * 1000))
    await emit_event("audio_chunk_received", session_id=session_id, payload={"sample_rate": sample_rate, "samples": int(pcm.size), "is_final": bool(is_final)})

    # 说话人识别
    try:
        emb = engine.embedding_from_pcm_float32(pcm, sample_rate)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    spk, best, scores = engine.identify(emb, th)
    score_items = [ScoreItem(name=n, score=s) for n, s in scores]

    # ASR：先重采样到 16kHz（前端 AudioWorklet 已 16k，则是 no-op）
    wav_t = torch.from_numpy(pcm.copy())
    if sample_rate != 16000:
        wav_t = torchaudio.functional.resample(wav_t, sample_rate, 16000)
    pcm_16k = wav_t.numpy()
    hw = (hotword or _asr_hotword_env or "").strip() or None
    text_raw = asr_engine.transcribe_chunk(pcm_16k, session_id, is_final=is_final, hotword=hw)
    if text_raw.strip():
        logger.info(
            "ASR [%s] speaker=%s score=%.3f hotword=%s text=%s",
            session_id, spk or "未知", float(best), (hw or "-"), text_raw.strip(),
        )

    # LLM 纠错（可选）
    text_final = text_raw
    corrected = False
    if _asr_llm_correction and llm_corrector and text_raw.strip():
        try:
            text_final = await llm_corrector.correct(
                text_raw, context=spk or "未知说话人"
            )
            corrected = True
        except Exception:
            text_final = text_raw

    if text_final.strip():
        transcript_buffer.append(
            session_id=session_id,
            speaker=spk or "未知说话人",
            text=text_final,
            text_raw=text_raw,
            llm_corrected=corrected,
            start_ms=start_ms,
            end_ms=end_ms,
            duration_sec=duration_sec,
        )
        await emit_event(
            "asr_final" if is_final else "asr_interim",
            session_id=session_id,
            payload={
                "speaker": spk or "未知说话人",
                "text": text_final,
                "text_raw": text_raw,
                "llm_corrected": corrected,
                "score": float(best),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "duration_sec": duration_sec,
            },
        )

    return RecognizeResponse(
        speaker=spk,
        score=best,
        threshold=th,
        scores=score_items,
        text=text_final,
        text_raw=text_raw,
        llm_corrected=corrected,
        start_ms=start_ms,
        end_ms=end_ms,
        duration_sec=duration_sec,
        message=None if spk else ("未匹配已注册说话人" if scores else "请先注册说话人"),
    )


@app.post("/api/asr_reset")
def asr_reset(session_id: str = "default"):
    """重置 ASR 流式 session（开始新一轮对话时调用）。"""
    asr_engine.reset_session(session_id)
    transcript_buffer.clear(session_id)
    dialogue_manager.stop(session_id)
    # 同一会话 id 的会中助手多轮记忆一并清空（与前端 chat_ 前缀 id 均尝试）
    _assistant_history_clear_session_ids(session_id, f"chat_{session_id}")
    return {"ok": True}


@app.post("/api/kws")
async def kws(request: Request, sample_rate: int, session_id: str = "default"):
    """关键词唤醒检测。Body 为 float32 小端单声道 PCM。"""
    body = await request.body()
    if len(body) < 256 or len(body) % 4 != 0:
        return {
            "triggered": False,
            "score": None,
            "kws_enabled": _kws_enabled,
            "message": "音频过短或格式错误",
        }
    pcm = np.frombuffer(body, dtype=np.float32).copy()
    # 重采样到 16kHz
    if sample_rate != 16000:
        wav_t = torchaudio.functional.resample(
            torch.from_numpy(pcm), sample_rate, 16000
        )
        pcm = wav_t.numpy()
    triggered, score, raw = kws_engine.detect(pcm)
    if triggered:
        logger.info("KWS [%s] triggered keywords=%s score=%.3f", session_id, _kws_keywords, float(score) if score is not None else -1.0)
        dialogue_manager.start(session_id)
        await emit_event("wakeword_detected", session_id=session_id, payload={"score": score, "keywords": _kws_keywords})
        await emit_event("assistant_session_started", session_id=session_id, payload={"ttl_sec": _assistant_ttl_sec})
    return {
        "kws_enabled": _kws_enabled,
        "triggered": triggered,
        "score": score,
        "threshold": _kws_threshold,
        "keywords": _kws_keywords,
        "message": (None if _kws_enabled else "语音唤醒未启用（kws.enabled 为 false 或未设置；设为 true 才使用本地 KWS）"),
        "debug_raw": str(raw) if raw is not None else ("" if _kws_enabled else "kws_disabled"),
    }


@app.post("/api/tts")
async def tts_proxy(request: Request):
    """
    将文本转发给配置的 TTS 服务并返回音频流。
    支持两种后端：
      1) OpenAI 兼容：POST /v1/audio/speech（需配置 tts.url / SPEAKER_TTS_URL）
      2) 火山豆包双向流式 TTS V3：provider=volc_bidirection（需 tts.volc.* 或与 ASR 共用 appid/token）
    Body:
      {
        "text": "...",
        "voice": "optional",
        "session_id": "optional",
        "provider": "optional 覆盖默认 tts.provider（openai | volc_bidirection）"
      }
    """
    import json as _json
    import io as _io
    import urllib.request as _ur
    import wave as _wave
    from fastapi.responses import StreamingResponse
    import asyncio

    loop = asyncio.get_event_loop()

    def _pcm_to_wav_bytes(pcm_bytes: bytes) -> bytes:
        buf = _io.BytesIO()
        with _wave.open(buf, "wb") as wf:
            wf.setnchannels(_tts_pcm_channels)
            wf.setsampwidth(_tts_pcm_sample_width)
            wf.setframerate(_tts_pcm_sample_rate)
            wf.writeframes(pcm_bytes)
        return buf.getvalue()

    body = await request.json()
    text = body.get("text", "").strip()
    session_id = str(body.get("session_id", "default"))
    if not text:
        raise HTTPException(400, "text 不能为空")
    voice = body.get("voice", _tts_voice)

    provider = _normalize_tts_provider(str(body.get("provider", _tts_provider)).strip() or _tts_provider)
    logger.info(
        "tts_proxy called: sid=%s provider=%s text_len=%d",
        session_id,
        provider,
        len(text),
    )

    if provider == "volc_bidirection":
        if not _tts_volc_appid or not _tts_volc_access_key:
            raise HTTPException(
                503,
                "火山双向 TTS 未配置：请设置 tts.volc.appid / tts.volc.access_key，"
                "或与 ASR 共用 asr.volc.appid / asr.volc.token",
            )
        spk = (
            (str(voice).strip() if voice is not None else "")
            or _tts_volc_speaker
            or (_tts_voice if _tts_voice and str(_tts_voice).strip().lower() != "default" else "")
        )
        if not spk:
            raise HTTPException(
                400,
                "未配置豆包音色：请在 config 中设置 tts.volc.speaker，或请求体传入 voice（官方音色 ID）",
            )
        from echopass.volc_bidirectional_tts import synthesize_pcm_bytes_sync

        await emit_event(
            "tts_started",
            session_id=session_id,
            payload={"text_len": len(text), "backend": "volc_bidirection", "speaker": spk},
        )

        def _call_volc() -> bytes:
            return synthesize_pcm_bytes_sync(
                text=text,
                app_key=_tts_volc_appid,
                access_key=_tts_volc_access_key,
                resource_id=_tts_volc_resource_id,
                ws_url=_tts_volc_ws_url,
                speaker=spk,
                sample_rate=_tts_pcm_sample_rate,
                speech_rate=_tts_volc_speech_rate,
                loudness_rate=_tts_volc_loudness_rate,
            )

        try:
            pcm = await loop.run_in_executor(None, _call_volc)
        except Exception as e:  # noqa: BLE001
            logger.exception("火山双向 TTS 调用失败: %s", e)
            raise HTTPException(502, f"火山 TTS 失败：{e}") from e
        if not pcm:
            raise HTTPException(502, "火山 TTS 返回空音频")
        audio_bytes = _pcm_to_wav_bytes(pcm)
        await emit_event(
            "tts_finished",
            session_id=session_id,
            payload={"content_type": "audio/wav", "bytes": len(audio_bytes)},
        )
        return StreamingResponse(
            iter([audio_bytes]),
            media_type="audio/wav",
            headers={"Content-Disposition": "inline; filename=tts.wav"},
        )

    if not _tts_url:
        raise HTTPException(
            503,
            "OpenAI 兼容 TTS 未配置：请设置 tts.url 或环境变量 SPEAKER_TTS_URL；"
            "或改用 tts.provider=volc_bidirection 使用火山双向 TTS。",
        )

    url = _tts_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    payload = _json.dumps(
        {
            "model": _tts_model,
            "input": text,
            "voice": voice,
        }
    ).encode()
    if not url.endswith("/audio/speech"):
        url += "/audio/speech"
    headers["Authorization"] = f"Bearer {_tts_key}"
    req = _ur.Request(url, data=payload, headers=headers, method="POST")

    def _call():
        with _ur.urlopen(req, timeout=30) as resp:
            return resp.read(), resp.headers.get("Content-Type", "audio/mpeg")

    await emit_event("tts_started", session_id=session_id, payload={"text_len": len(text)})
    audio_bytes, ct = await loop.run_in_executor(None, _call)
    media_type = ct
    filename = "tts.mp3"
    if "audio/pcm" in (ct or "").lower():
        audio_bytes = _pcm_to_wav_bytes(audio_bytes)
        media_type = "audio/wav"
        filename = "tts.wav"
    await emit_event("tts_finished", session_id=session_id, payload={"content_type": media_type, "bytes": len(audio_bytes)})
    return StreamingResponse(
        iter([audio_bytes]),
        media_type=media_type,
        headers={"Content-Disposition": f"inline; filename={filename}"},
    )


@app.post("/api/assistant/reply")
async def assistant_reply(req: AssistantReplyRequest):
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "text 不能为空")
    if not dialogue_manager.is_active(req.session_id):
        dialogue_manager.start(req.session_id)
        await emit_event("assistant_session_started", req.session_id, {"source": "api_assistant_reply"})
    else:
        dialogue_manager.touch(req.session_id)

    # 会议上下文：优先取实时识别的 session，回退到 wake 自己的 session。
    meeting_sid = (req.meeting_session_id or "").strip() or req.session_id
    meeting_ctx = _build_meeting_context(meeting_sid)

    system_prompt = "你是会议语音助手，回答需要简洁准确、口语化、可直接口播。"
    if meeting_ctx:
        system_prompt += (
            "下面是当前会议正在讨论的内容（按发言顺序，可能截断）；"
            "如果用户的问题与会议内容相关，请结合上下文回答；"
            "否则忽略上下文，只回答用户问题。"
            f"\n\n【会议实时纪要】\n{meeting_ctx}"
        )

    speaker_tag = (req.speaker or "").strip()
    user_msg = f"【{speaker_tag}】{text}" if speaker_tag else text
    prompt = f"用户说：{user_msg}\n请给出简洁、可口播的中文回复。"
    messages = _assistant_messages_for_llm(req.session_id, system_prompt, prompt)
    llm_text = await llm_chat.chat_complete(messages, max_tokens=256, temperature=0.3)
    _assistant_history_append(req.session_id, prompt, llm_text)
    logger.info(
        "Assistant [%s] ctx_chars=%d use_tts=%s user=%s reply=%s",
        req.session_id, len(meeting_ctx), bool(req.use_tts), text, (llm_text or "").strip(),
    )
    await emit_event("llm_response_ready", req.session_id, {"input": text, "response": llm_text})
    return {
        "session_id": req.session_id,
        "assistant_text": llm_text,
        "use_tts": bool(req.use_tts and _tts_backend_ready()),
        "tts_enabled": _tts_backend_ready(),
    }


@app.post("/api/assistant/stream")
async def assistant_stream(req: AssistantReplyRequest):
    """LLM 流式输出（SSE）；在 provider=volc_bidirection 且 use_tts 时串联火山双向流式 TTS。

    事件行：`data: {"type":"text_delta"|"audio_pcm_b64"|"done"|"error", ...}\\n\\n`
    协议参考：https://www.volcengine.com/docs/6561/1329505
    """
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "text 不能为空")
    if not dialogue_manager.is_active(req.session_id):
        dialogue_manager.start(req.session_id)
        await emit_event(
            "assistant_session_started", req.session_id, {"source": "api_assistant_stream"},
        )
    else:
        dialogue_manager.touch(req.session_id)

    meeting_sid = (req.meeting_session_id or "").strip() or req.session_id
    meeting_ctx = _build_meeting_context(meeting_sid)

    system_prompt = "你是会议语音助手，回答需要简洁准确、口语化、可直接口播。"
    if meeting_ctx:
        system_prompt += (
            "下面是当前会议正在讨论的内容（按发言顺序，可能截断）；"
            "如果用户的问题与会议内容相关，请结合上下文回答；"
            "否则忽略上下文，只回答用户问题。"
            f"\n\n【会议实时纪要】\n{meeting_ctx}"
        )

    speaker_tag = (req.speaker or "").strip()
    user_msg = f"【{speaker_tag}】{text}" if speaker_tag else text
    prompt = f"用户说：{user_msg}\n请给出简洁、可口播的中文回复。"
    llm_messages = _assistant_messages_for_llm(req.session_id, system_prompt, prompt)

    stream_volc_tts = (
        bool(req.use_tts)
        and _tts_provider == "volc_bidirection"
        and bool(_tts_volc_appid)
        and bool(_tts_volc_access_key)
    )
    spk = _volc_tts_speaker_effective(req.tts_voice)

    async def event_gen():
        full_parts: List[str] = []
        try:
            if stream_volc_tts and not spk:
                yield (
                    "data: "
                    + json.dumps(
                        {"type": "error", "message": "未配置豆包 TTS 音色（tts.volc.speaker）"},
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                return

            if not stream_volc_tts:
                async for delta in llm_chat.stream_chat(
                    llm_messages, max_tokens=256, temperature=0.25,
                ):
                    full_parts.append(delta)
                    yield (
                        "data: "
                        + json.dumps({"type": "text_delta", "content": delta}, ensure_ascii=False)
                        + "\n\n"
                    )
                full = "".join(full_parts)
                _assistant_history_append(req.session_id, prompt, full)
                logger.info(
                    "Assistant stream [%s] ctx_chars=%d stream_tts=0 user=%s reply_len=%d",
                    req.session_id, len(meeting_ctx), text, len(full),
                )
                await emit_event(
                    "llm_response_ready", req.session_id, {"input": text, "response": full},
                )
                yield (
                    "data: "
                    + json.dumps(
                        {"type": "done", "full_text": full, "stream_tts": False},
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                return

            from echopass.volc_bidirectional_tts import BidirectionalTtsStreamSession

            out_q: asyncio.Queue = asyncio.Queue(maxsize=512)
            tts = BidirectionalTtsStreamSession(
                app_key=_tts_volc_appid,
                access_key=_tts_volc_access_key,
                resource_id=_tts_volc_resource_id,
                ws_url=_tts_volc_ws_url,
                speaker=spk,
                sample_rate=_tts_pcm_sample_rate,
                speech_rate=_tts_volc_speech_rate,
                loudness_rate=_tts_volc_loudness_rate,
            )
            await tts.connect()
            down_task = asyncio.create_task(tts.run_downlink(out_q))

            async def llm_worker() -> None:
                buf = ""
                try:
                    async for delta in llm_chat.stream_chat(
                        llm_messages, max_tokens=256, temperature=0.25,
                    ):
                        await out_q.put({"type": "text", "content": delta})
                        buf += delta
                        frags, buf = _flush_volc_stream_tts_buf(buf)
                        for fg in frags:
                            await tts.send_text_fragment(fg)
                    if buf.strip():
                        await tts.send_text_fragment(buf.strip())
                    await tts.finish()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    await out_q.put({"type": "error", "message": str(e)})
                finally:
                    await out_q.put({"type": "llm_done"})

            llm_task = asyncio.create_task(llm_worker())
            try:
                while True:
                    item = await out_q.get()
                    t = item.get("type")
                    if t == "text":
                        c = str(item.get("content", ""))
                        full_parts.append(c)
                        yield (
                            "data: "
                            + json.dumps({"type": "text_delta", "content": c}, ensure_ascii=False)
                            + "\n\n"
                        )
                    elif t == "pcm":
                        raw = item.get("data") or b""
                        if raw:
                            yield (
                                "data: "
                                + json.dumps(
                                    {
                                        "type": "audio_pcm_b64",
                                        "b64": base64.b64encode(raw).decode("ascii"),
                                        "sample_rate": _tts_pcm_sample_rate,
                                        "channels": _tts_pcm_channels,
                                        "sample_width": _tts_pcm_sample_width,
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n\n"
                            )
                    elif t == "error":
                        yield (
                            "data: "
                            + json.dumps(
                                {"type": "error", "message": item.get("message", "")},
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                        break
                    elif t == "audio_done":
                        break
            finally:
                await tts.close()
                for tsk in (llm_task, down_task):
                    if not tsk.done():
                        tsk.cancel()
                    try:
                        await tsk
                    except asyncio.CancelledError:
                        pass
                    except Exception:  # noqa: BLE001
                        pass

            full = "".join(full_parts)
            _assistant_history_append(req.session_id, prompt, full)
            logger.info(
                "Assistant stream [%s] ctx_chars=%d stream_tts=1 user=%s reply_len=%d",
                req.session_id, len(meeting_ctx), text, len(full),
            )
            await emit_event(
                "llm_response_ready", req.session_id, {"input": text, "response": full},
            )
            yield (
                "data: "
                + json.dumps(
                    {"type": "done", "full_text": full, "stream_tts": True},
                    ensure_ascii=False,
                )
                + "\n\n"
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("assistant stream: %s", e)
            yield (
                "data: "
                + json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False)
                + "\n\n"
            )

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/meeting/summary")
async def meeting_summary(req: MeetingSummaryRequest):
    await emit_event("meeting_summary_requested", req.session_id, {"title": req.title})
    items = transcript_buffer.list_items(req.session_id)
    summary = await meeting_summarizer.summarize(items, title=req.title)
    await emit_event("meeting_summary_ready", req.session_id, {"title": summary.get("title", req.title)})
    return summary


@app.get("/api/meeting/transcript")
def meeting_transcript(session_id: str = "default"):
    return {"session_id": session_id, "items": transcript_buffer.list_dicts(session_id)}


@app.post("/api/meeting/chapters")
async def meeting_chapters(req: MeetingChaptersRequest):
    """根据当前转录生成"AI 章节"列表（精炼标题 + 2-4 句摘要 + 起止时间）。

    返回 {"session_id": ..., "generated_at": iso8601, "chapters": [...]}.
    """
    items = transcript_buffer.list_items(req.session_id)
    chapters = await meeting_summarizer.chapters(items)
    return {
        "session_id": req.session_id,
        "generated_at": _dt.datetime.utcnow().isoformat() + "Z",
        "chapters": chapters,
    }


# ---------------------------------------------------------------------------
# 会议导出：把 原始音频 + 语音识别内容 + 最终会议纪要 打成 ZIP 返回
# 前端在"结束录音"后把 state 里最新的 WAV/transcript/summary 一起传上来；
# 后端不再调用 LLM、不再落库，只做拼包，保证和用户看到的一致。
# ---------------------------------------------------------------------------


def _fmt_mmss(sec: float) -> str:
    """秒 → mm:ss 或 h:mm:ss。"""
    try:
        sec = max(0, int(float(sec or 0)))
    except (TypeError, ValueError):
        sec = 0
    m, s = divmod(sec, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _sanitize_filename(s: str, fallback: str = "meeting") -> str:
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", (s or "").strip())
    s = s.strip(" .")
    return s or fallback


def _build_transcript_txt(items: List[Dict[str, Any]]) -> str:
    """把 transcript 列表渲染成人类可读文本。"""
    if not items:
        return "(无转录内容)\n"
    lines: List[str] = []
    for it in items:
        start_ms = it.get("start_ms") or 0
        ts = _fmt_mmss(start_ms / 1000.0) if start_ms else "--:--"
        who = it.get("who") or it.get("speaker") or "未知"
        text = (it.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{ts}] {who}：{text}")
    return "\n".join(lines) + "\n"


def _build_summary_md(summary: Optional[Dict[str, Any]], title: str, generated_at: str) -> str:
    """把结构化纪要渲染成 Markdown。

    优先按新版 modules 渲染（编号模块化报告，与前端 UI 一致）；
    无 modules 时回退到旧的 key_points/decisions/... 五段式。
    """
    md: List[str] = [f"# {title}", "", f"**导出时间**：{generated_at}", ""]
    if not summary:
        md.append("_（未生成纪要）_")
        return "\n".join(md) + "\n"

    if summary.get("summary"):
        md += ["> " + str(summary["summary"]).strip(), ""]

    modules = summary.get("modules") or []
    if isinstance(modules, list) and modules:
        for m in modules:
            if not isinstance(m, dict):
                continue
            no = str(m.get("no") or "").strip()
            mtitle = str(m.get("title") or "").strip()
            heading = f"## {no} / {mtitle}" if no else f"## {mtitle}"
            md += [heading, ""]
            if m.get("intro"):
                md += ["_" + str(m["intro"]).strip() + "_", ""]
            mtype = (m.get("type") or "").strip().lower()
            if mtype == "bullets":
                for it in (m.get("items") or []):
                    if isinstance(it, dict):
                        label = (it.get("label") or "").strip()
                        desc = (it.get("desc") or "").strip()
                        if label and desc:
                            md.append(f"- **{label}**：{desc}")
                        elif label:
                            md.append(f"- **{label}**")
                        elif desc:
                            md.append(f"- {desc}")
                    elif isinstance(it, str):
                        md.append(f"- {it}")
                md.append("")
            elif mtype == "table":
                cols = m.get("columns") or []
                rows = m.get("rows") or []
                if cols and rows:
                    md.append("| " + " | ".join(str(c) for c in cols) + " |")
                    md.append("| " + " | ".join("---" for _ in cols) + " |")
                    for r in rows:
                        if isinstance(r, list):
                            md.append("| " + " | ".join(str(c) for c in r) + " |")
                    md.append("")
            elif mtype == "actions":
                for it in (m.get("items") or []):
                    if not isinstance(it, dict):
                        continue
                    task = (it.get("task") or "").strip()
                    if not task:
                        continue
                    extra = ""
                    if it.get("owner"):
                        extra += f"（负责人：{it['owner']}）"
                    due = it.get("due") or it.get("due_date")
                    if due:
                        extra += f"  截止：{due}"
                    md.append(f"- {task}{extra}")
                md.append("")
            elif mtype == "callout":
                for x in (m.get("items") or []):
                    md.append(f"> - {x}")
                md.append("")
        return "\n".join(md) + "\n"

    # ─ 旧字段回退（无 modules 时）
    def _bullets(key: str, heading: str) -> None:
        arr = summary.get(key) or []
        if not arr:
            return
        md.append(f"## {heading}")
        md.append("")
        for x in arr:
            if isinstance(x, dict):
                t = x.get("task") or x.get("text") or x.get("title") or json.dumps(x, ensure_ascii=False)
                owner = x.get("owner")
                due = x.get("due_date") or x.get("due")
                extra = ""
                if owner:
                    extra += f"（负责人：{owner}）"
                if due:
                    extra += f"  截止：{due}"
                md.append(f"- {t}{extra}")
            else:
                md.append(f"- {x}")
        md.append("")

    _bullets("key_points", "要点")
    _bullets("decisions", "决议")
    _bullets("action_items", "待办")
    _bullets("risks", "风险")
    return "\n".join(md) + "\n"


@app.post("/api/meeting/export")
async def meeting_export(
    audio: UploadFile = File(..., description="整段 WAV 音频（16kHz 16bit mono）"),
    transcript_json: str = Form("[]", description="前端 transcriptLines 的 JSON 字符串"),
    summary_json: str = Form("null", description="前端 summaryData 的 JSON 字符串"),
    title: str = Form("会议记录", description="纪要标题，将影响 ZIP 文件名"),
    session_id: str = Form("default"),
):
    """把原始音频 / 转录 / 纪要打包成 ZIP 供用户下载。"""
    audio_bytes = await audio.read()
    if not audio_bytes or len(audio_bytes) < 44:
        raise HTTPException(400, "音频为空或长度异常，请确认已完成录音")

    try:
        transcript_items = json.loads(transcript_json or "[]")
        if not isinstance(transcript_items, list):
            transcript_items = []
    except json.JSONDecodeError:
        raise HTTPException(400, "transcript_json 不是合法 JSON")

    try:
        summary_obj = json.loads(summary_json or "null")
        if summary_obj is not None and not isinstance(summary_obj, dict):
            summary_obj = None
    except json.JSONDecodeError:
        summary_obj = None

    now = _dt.datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    generated_at = now.strftime("%Y-%m-%d %H:%M:%S")
    safe_title = _sanitize_filename(title, fallback="meeting")
    zip_name = f"{safe_title}_{stamp}.zip"

    transcript_txt = _build_transcript_txt(transcript_items)
    summary_md = _build_summary_md(summary_obj, title=safe_title, generated_at=generated_at)

    readme = (
        f"{safe_title}\n"
        f"导出时间：{generated_at}\n"
        f"Session: {session_id}\n"
        f"\n"
        f"文件说明：\n"
        f"  audio.wav       - 整场会议原始音频（16kHz / 16bit / 单声道）\n"
        f"  transcript.txt  - 语音识别内容（人类可读，时间戳 + 说话人）\n"
        f"  transcript.json - 语音识别结构化数据（完整字段，便于程序化处理）\n"
        f"  summary.md      - 最终会议纪要（Markdown）\n"
        f"  summary.json    - 最终会议纪要结构化数据\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 音频不压缩（WAV 是 PCM，压缩效果差且拖慢导出）
        zi = zipfile.ZipInfo(filename="audio.wav", date_time=now.timetuple()[:6])
        zi.compress_type = zipfile.ZIP_STORED
        zf.writestr(zi, audio_bytes)
        zf.writestr("transcript.txt", transcript_txt)
        zf.writestr(
            "transcript.json",
            json.dumps(transcript_items, ensure_ascii=False, indent=2),
        )
        zf.writestr("summary.md", summary_md)
        zf.writestr(
            "summary.json",
            json.dumps(summary_obj or {}, ensure_ascii=False, indent=2),
        )
        zf.writestr("README.txt", readme)

    zip_bytes = buf.getvalue()
    logger.info(
        "Meeting export [%s]: audio=%.1fKB transcript=%d lines summary=%s zip=%.1fKB",
        session_id, len(audio_bytes) / 1024.0, len(transcript_items),
        "yes" if summary_obj else "no", len(zip_bytes) / 1024.0,
    )

    # Content-Disposition 的 filename 要兼容 ASCII + UTF-8
    ascii_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", zip_name) or "meeting.zip"
    from urllib.parse import quote
    utf8_name = quote(zip_name)
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": disposition},
    )


@app.delete("/api/speakers/{name}")
def delete_speaker(name: str):
    if not engine.remove_speaker(name):
        raise HTTPException(404, "无此说话人")
    return {"ok": True, "speakers": engine.list_speakers()}


STATIC = Path(__file__).resolve().parent / "static"
if STATIC.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
