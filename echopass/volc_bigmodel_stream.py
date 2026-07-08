# EchoPass · 火山豆包大模型 ASR v3 长连接流式会话
#
# 对接 ``bigmodel_async``：会议期间一条 WS，持续发 PCM；
# 云端 VAD（enable_nonstream + end_window_size）产出 definite utterances。
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import struct
import uuid
from typing import Awaitable, Callable, Dict, List, Optional

import numpy as np
import websockets

from echopass.volc_bigmodel_asr import (
    CLIENT_AUDIO_ONLY_REQUEST,
    CLIENT_FULL_REQUEST,
    NEG_WITH_SEQUENCE,
    POS_SEQUENCE,
    VolcBigmodelAsrClient,
    _float32_to_pcm16_bytes,
    _make_header,
    _normalize_volc_period_spam,
    _parse_response,
    _ws_connect,
)

logger = logging.getLogger("echopass.volc_bigmodel_stream")

OnPartialCb = Callable[[str, dict], Awaitable[None]]
OnSentenceCb = Callable[[dict, np.ndarray], Awaitable[None]]


def _pack_first_frame(seq: int, payload: dict) -> bytes:
    body = gzip.compress(json.dumps(payload).encode("utf-8"))
    out = bytearray()
    out += _make_header(CLIENT_FULL_REQUEST, flags=POS_SEQUENCE)
    out += struct.pack(">i", seq)
    out += struct.pack(">I", len(body))
    out += body
    return bytes(out)


def _pack_audio_frame(seq: int, chunk: bytes, is_last: bool) -> bytes:
    flags = NEG_WITH_SEQUENCE if is_last else POS_SEQUENCE
    if is_last:
        seq = -abs(seq)
    body = gzip.compress(chunk or b"")
    out = bytearray()
    out += _make_header(CLIENT_AUDIO_ONLY_REQUEST, flags=flags)
    out += struct.pack(">i", seq)
    out += struct.pack(">I", len(body))
    out += body
    return bytes(out)


def _utterance_key(item: dict) -> str:
    start = int(item.get("start_time") or item.get("start_ms") or 0)
    end = int(item.get("end_time") or item.get("end_ms") or 0)
    text = str(item.get("text") or "").strip()
    return f"{start}:{end}:{text}"


def _min_speaker_samples() -> int:
    return 24000  # 1.5s @16k


class VolcBigmodelStreamingSession:
    """火山 ``bigmodel_async`` 长连接流式会话。"""

    def __init__(
        self,
        *,
        app_key: str,
        access_key: str,
        resource_id: str,
        ws_url: str,
        model_name: str = "bigmodel",
        uid: str = "echopass",
        sample_rate: int = 16000,
        seg_duration_ms: int = 200,
        enable_itn: bool = True,
        enable_punc: bool = True,
        enable_ddc: bool = True,
        enable_nonstream: bool = True,
        show_utterances: bool = True,
        end_window_size: int = 800,
        force_to_speech_time: int = 1000,
        connect_timeout: float = 10.0,
        recv_timeout: float = 60.0,
    ) -> None:
        self.app_key = app_key
        self.access_key = access_key
        self.resource_id = resource_id
        self.ws_url = (ws_url or "").strip()
        self.model_name = model_name
        self.uid = uid
        self.sample_rate = int(sample_rate)
        self.seg_duration_ms = max(20, int(seg_duration_ms))
        self.enable_itn = bool(enable_itn)
        self.enable_punc = bool(enable_punc)
        self.enable_ddc = bool(enable_ddc)
        self.enable_nonstream = bool(enable_nonstream)
        self.show_utterances = bool(show_utterances)
        self.end_window_size = max(200, int(end_window_size))
        self.force_to_speech_time = max(1, int(force_to_speech_time))
        self.connect_timeout = max(1.0, float(connect_timeout))
        self.recv_timeout = max(5.0, float(recv_timeout))

        self._ws = None
        self._seq = 0
        self._send_lock = asyncio.Lock()
        self._recv_task: Optional[asyncio.Task] = None
        self._closed = False
        self._started = False
        self._stopping = False
        self._last_partial = ""

        self._pcm_chunks: List[np.ndarray] = []
        self._pcm_total_samples = 0
        self._seen_utterance_keys: set[str] = set()

        self._on_partial: Optional[OnPartialCb] = None
        self._on_sentence: Optional[OnSentenceCb] = None

        self._bytes_per_chunk = max(
            320,
            int(self.sample_rate * 2 * self.seg_duration_ms / 1000),
        )

    def set_callbacks(
        self,
        *,
        on_partial: Optional[OnPartialCb] = None,
        on_sentence: Optional[OnSentenceCb] = None,
    ) -> None:
        self._on_partial = on_partial
        self._on_sentence = on_sentence

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Access-Key": self.access_key,
            "X-Api-App-Key": self.app_key,
        }

    def _build_first_payload(self, hotword: Optional[str]) -> dict:
        req: Dict[str, object] = {
            "model_name": self.model_name,
            "enable_itn": self.enable_itn,
            "enable_punc": self.enable_punc,
            "enable_ddc": self.enable_ddc,
            "show_utterances": self.show_utterances,
            "enable_nonstream": self.enable_nonstream,
            "end_window_size": self.end_window_size,
            "force_to_speech_time": self.force_to_speech_time,
        }
        ctx = VolcBigmodelAsrClient._hotword_context(hotword)
        if ctx:
            req["corpus"] = {"context": ctx}
        return {
            "user": {"uid": self.uid},
            "audio": {
                "format": "pcm",
                "codec": "raw",
                "rate": self.sample_rate,
                "bits": 16,
                "channel": 1,
            },
            "request": req,
        }

    async def start(self, hotword: Optional[str] = None) -> None:
        if self._started:
            return
        ws = await asyncio.wait_for(
            _ws_connect(self.ws_url, self._auth_headers()),
            timeout=self.connect_timeout,
        )
        self._ws = ws
        self._seq = 1
        await ws.send(_pack_first_frame(self._seq, self._build_first_payload(hotword)))
        self._recv_task = asyncio.create_task(self._receiver_loop(), name="volc-bigmodel-stream-recv")
        self._started = True

    async def feed_pcm16k(self, pcm_16k: np.ndarray) -> None:
        if self._closed or not self._started or self._ws is None or self._stopping:
            return
        arr = np.asarray(pcm_16k, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return
        self._pcm_chunks.append(arr.copy())
        self._pcm_total_samples += int(arr.size)

        audio_bytes = _float32_to_pcm16_bytes(arr)
        async with self._send_lock:
            ws = self._ws
            if ws is None:
                return
            for offset in range(0, len(audio_bytes), self._bytes_per_chunk):
                chunk = audio_bytes[offset:offset + self._bytes_per_chunk]
                self._seq += 1
                await ws.send(_pack_audio_frame(self._seq, chunk, is_last=False))

    def _slice_samples(self, start: int, end: int) -> np.ndarray:
        if end <= start:
            return np.zeros(0, dtype=np.float32)
        out_parts: List[np.ndarray] = []
        cursor = 0
        for chunk in self._pcm_chunks:
            nxt = cursor + chunk.size
            if nxt <= start:
                cursor = nxt
                continue
            if cursor >= end:
                break
            lo = max(0, start - cursor)
            hi = min(chunk.size, end - cursor)
            out_parts.append(chunk[lo:hi])
            cursor = nxt
        if not out_parts:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(out_parts)

    def slice_pcm_ms(self, start_ms: int, end_ms: int) -> np.ndarray:
        start = max(0, int(start_ms * 16))
        end = max(start, int(end_ms * 16))
        end = min(end, self._pcm_total_samples)
        return self._slice_samples(start, end)

    def slice_recent_pcm(self, duration_sec: float) -> np.ndarray:
        n = max(1600, int(duration_sec * 16000))
        start = max(0, self._pcm_total_samples - n)
        return self._slice_samples(start, self._pcm_total_samples)

    async def stop(self) -> None:
        if self._closed or self._stopping:
            return
        self._stopping = True
        ws = self._ws
        if ws is not None:
            try:
                async with self._send_lock:
                    self._seq += 1
                    await ws.send(_pack_audio_frame(self._seq, b"", is_last=True))
            except Exception:  # noqa: BLE001
                pass
        if self._recv_task is not None:
            try:
                await asyncio.wait_for(self._recv_task, timeout=self.recv_timeout + 10.0)
            except asyncio.TimeoutError:
                self._recv_task.cancel()
            except Exception:  # noqa: BLE001
                pass
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._recv_task is not None and not self._recv_task.done():
            self._recv_task.cancel()
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _extract_partial_text(payload_msg: object, *, has_finalized: bool) -> str:
        utterances = VolcBigmodelStreamingSession._extract_utterances(payload_msg)
        in_progress = [
            str(item.get("text") or "").strip()
            for item in utterances
            if not item.get("definite") and str(item.get("text") or "").strip()
        ]
        if in_progress:
            return "".join(in_progress).strip()
        if not utterances:
            if not has_finalized:
                return VolcBigmodelStreamingSession._extract_text(payload_msg)
            return ""
        return ""

    @staticmethod
    def _extract_text(payload_msg: object) -> str:
        if not isinstance(payload_msg, dict):
            return ""
        result = payload_msg.get("result")
        if isinstance(result, dict):
            text = result.get("text")
            if isinstance(text, str):
                return text.strip()
        if isinstance(result, list):
            parts: List[str] = []
            for item in result:
                if isinstance(item, dict):
                    t = item.get("text")
                    if isinstance(t, str) and t.strip():
                        parts.append(t.strip())
            return "".join(parts).strip()
        text = payload_msg.get("text")
        return text.strip() if isinstance(text, str) else ""

    @staticmethod
    def _extract_utterances(payload_msg: object) -> List[dict]:
        if not isinstance(payload_msg, dict):
            return []
        result = payload_msg.get("result")
        if not isinstance(result, dict):
            return []
        utterances = result.get("utterances")
        if not isinstance(utterances, list):
            return []
        return [u for u in utterances if isinstance(u, dict)]

    async def _receiver_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            while not self._closed:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.recv_timeout)
                except asyncio.TimeoutError:
                    continue
                except websockets.exceptions.ConnectionClosed:
                    break

                if not isinstance(raw, (bytes, bytearray)):
                    continue
                parsed = _parse_response(bytes(raw))
                code = parsed.get("code", 0)
                if code and code != 0:
                    payload_msg = parsed.get("payload_msg") or {}
                    err_msg = (
                        payload_msg.get("message") if isinstance(payload_msg, dict) else ""
                    )
                    logger.warning("火山流式 ASR 错误 code=%s message=%s", code, err_msg)
                    continue

                payload_msg = parsed.get("payload_msg")
                partial_text = _normalize_volc_period_spam(
                    self._extract_partial_text(
                        payload_msg,
                        has_finalized=bool(self._seen_utterance_keys),
                    )
                )
                if partial_text != self._last_partial and self._on_partial is not None:
                    self._last_partial = partial_text
                    await self._on_partial(
                        partial_text,
                        {"text": partial_text, "payload": payload_msg},
                    )

                await self._handle_utterances(payload_msg)

                if parsed.get("is_last"):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("火山流式接收异常")
        finally:
            self._closed = True

    async def _handle_utterances(self, payload_msg: object) -> None:
        if self._on_sentence is None:
            return
        for item in self._extract_utterances(payload_msg):
            if not item.get("definite"):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            key = _utterance_key(item)
            if key in self._seen_utterance_keys:
                continue
            self._seen_utterance_keys.add(key)

            start_ms = int(item.get("start_time") or item.get("start_ms") or 0)
            end_ms = int(item.get("end_time") or item.get("end_ms") or 0)
            pcm_slice = self.slice_pcm_ms(start_ms, end_ms)
            min_samples = _min_speaker_samples()
            if pcm_slice.size < min_samples:
                pcm_slice = self.slice_recent_pcm(3.0)
                if start_ms <= 0 and end_ms > start_ms:
                    start_ms = max(0, end_ms - int(pcm_slice.size / 16))

            meta = dict(item)
            meta["start_ms"] = start_ms
            meta["end_ms"] = end_ms if end_ms > start_ms else start_ms + int(pcm_slice.size / 16)
            meta["text"] = text
            meta["payload"] = payload_msg
            await self._on_sentence(meta, pcm_slice)


__all__ = ["VolcBigmodelStreamingSession"]
