# EchoPass · 火山引擎（字节跳动）云端流式 ASR 客户端
#
# 对外只暴露 VolcAsrClient.transcribe_pcm16k(pcm, hotword) -> str，
# 同步接口，内部走 websockets 异步客户端 + 独立事件循环线程，
# 与 FastAPI 主事件循环解耦，可直接在 async handler 里的同步代码路径调用。
#
# 协议来自 openspeech 流式 ASR v2 demo：
#   wss://openspeech.bytedance.com/api/v2/asr
# 每次调用 = 一次独立 reqid 的 WS 会话，发 full client request + 若干 audio-only
# 分片（最后一片带 NEG_SEQUENCE），收 server full response 拿最终带标点文本。
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import threading
import uuid
from typing import Any, Dict, List, Optional

import numpy as np
import websockets

logger = logging.getLogger("echopass.volc_asr")

# ── 协议常量（与 openspeech demo 完全一致）────────────────────────────
PROTOCOL_VERSION = 0b0001
DEFAULT_HEADER_SIZE = 0b0001

CLIENT_FULL_REQUEST = 0b0001
CLIENT_AUDIO_ONLY_REQUEST = 0b0010
SERVER_FULL_RESPONSE = 0b1001
SERVER_ACK = 0b1011
SERVER_ERROR_RESPONSE = 0b1111

NO_SEQUENCE = 0b0000
POS_SEQUENCE = 0b0001
NEG_SEQUENCE = 0b0010

NO_SERIALIZATION = 0b0000
JSON_SERIAL = 0b0001

NO_COMPRESSION = 0b0000
GZIP = 0b0001


def _generate_header(
    message_type: int = CLIENT_FULL_REQUEST,
    message_type_specific_flags: int = NO_SEQUENCE,
    serial_method: int = JSON_SERIAL,
    compression_type: int = GZIP,
    reserved_data: int = 0x00,
) -> bytearray:
    header = bytearray()
    header_size = 1
    header.append((PROTOCOL_VERSION << 4) | header_size)
    header.append((message_type << 4) | message_type_specific_flags)
    header.append((serial_method << 4) | compression_type)
    header.append(reserved_data)
    return header


def _generate_full_default_header() -> bytearray:
    return _generate_header()


def _generate_audio_default_header() -> bytearray:
    return _generate_header(message_type=CLIENT_AUDIO_ONLY_REQUEST)


def _generate_last_audio_header() -> bytearray:
    return _generate_header(
        message_type=CLIENT_AUDIO_ONLY_REQUEST,
        message_type_specific_flags=NEG_SEQUENCE,
    )


def _parse_response(res: bytes) -> Dict[str, Any]:
    protocol_version = res[0] >> 4  # noqa: F841  # 保留注释，便于排障
    header_size = res[0] & 0x0F
    message_type = res[1] >> 4
    message_type_specific_flags = res[1] & 0x0F  # noqa: F841
    serialization_method = res[2] >> 4
    message_compression = res[2] & 0x0F
    payload = res[header_size * 4:]

    result: Dict[str, Any] = {}
    payload_msg: Any = None
    payload_size = 0

    if message_type == SERVER_FULL_RESPONSE:
        payload_size = int.from_bytes(payload[:4], "big", signed=True)
        payload_msg = payload[4:]
    elif message_type == SERVER_ACK:
        seq = int.from_bytes(payload[:4], "big", signed=True)
        result["seq"] = seq
        if len(payload) >= 8:
            payload_size = int.from_bytes(payload[4:8], "big", signed=False)
            payload_msg = payload[8:]
    elif message_type == SERVER_ERROR_RESPONSE:
        code = int.from_bytes(payload[:4], "big", signed=False)
        result["code"] = code
        payload_size = int.from_bytes(payload[4:8], "big", signed=False)
        payload_msg = payload[8:]

    if payload_msg is None:
        return result
    if message_compression == GZIP:
        payload_msg = gzip.decompress(payload_msg)
    if serialization_method == JSON_SERIAL:
        payload_msg = json.loads(payload_msg.decode("utf-8"))
    elif serialization_method != NO_SERIALIZATION:
        payload_msg = payload_msg.decode("utf-8")

    result["payload_msg"] = payload_msg
    result["payload_size"] = payload_size
    return result


def _float32_to_pcm16_bytes(pcm: np.ndarray) -> bytes:
    """16kHz float32 mono → PCM16LE bytes。"""
    arr = np.asarray(pcm, dtype=np.float32)
    if arr.ndim > 1:
        arr = arr.reshape(-1)
    arr = np.clip(arr, -1.0, 1.0)
    pcm16 = (arr * 32767.0).astype(np.int16)
    return pcm16.tobytes()


def _slice_bytes(data: bytes, chunk_size: int):
    """yield (chunk, is_last)。与 demo 里 AsrWsClient.slice_data 等价。"""
    if chunk_size <= 0:
        yield data, True
        return
    data_len = len(data)
    if data_len == 0:
        yield b"", True
        return
    offset = 0
    while offset + chunk_size < data_len:
        yield data[offset:offset + chunk_size], False
        offset += chunk_size
    yield data[offset:data_len], True


async def _ws_connect(url: str, headers: Dict[str, str]):
    """兼容 websockets>=13 的 additional_headers 与旧版 extra_headers。"""
    try:
        return await websockets.connect(
            url, additional_headers=headers, max_size=1_000_000_000,
        )
    except TypeError:
        return await websockets.connect(
            url, extra_headers=headers, max_size=1_000_000_000,
        )


class VolcAsrClient:
    """火山云端流式 ASR 客户端，一次调用 = 一条独立 WS 会话。"""

    SUCCESS_CODE = 1000

    def __init__(
        self,
        appid: str,
        token: str,
        cluster: str,
        ws_url: str = "wss://openspeech.bytedance.com/api/v2/asr",
        uid: str = "echopass",
        language: str = "zh-CN",
        workflow: str = "audio_in,resample,partition,vad,fe,decode,itn,nlu_punctuate",
        seg_duration_ms: int = 15000,
        sample_rate: int = 16000,
        nbest: int = 1,
        show_utterances: bool = False,
        result_type: str = "full",
        connect_timeout: float = 5.0,
        recv_timeout: float = 15.0,
    ) -> None:
        # 核心必需：appid / token；cluster 允许为空（服务端会明确给出错误，便于排障）
        if not appid or not token:
            raise ValueError("VolcAsrClient 需要非空的 appid / token")
        self.appid = appid
        self.token = token
        self.cluster = cluster or ""
        self.ws_url = ws_url
        self.uid = uid
        self.language = language
        self.workflow = workflow
        self.seg_duration_ms = int(seg_duration_ms)
        self.sample_rate = int(sample_rate)
        self.nbest = int(nbest)
        self.show_utterances = bool(show_utterances)
        self.result_type = result_type
        self.connect_timeout = float(connect_timeout)
        self.recv_timeout = float(recv_timeout)

        # 独立事件循环线程：避免和 FastAPI 主事件循环纠缠
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            name="volc-asr-loop",
            daemon=True,
        )
        self._loop_thread.start()

    def _build_request(self, reqid: str) -> Dict[str, Any]:
        # 音频参数固定为 16kHz / 16bit / 单声道 raw PCM（调用方保证 pcm_16k 已是该格式）
        return {
            "app": {
                "appid": self.appid,
                "cluster": self.cluster,
                "token": self.token,
            },
            "user": {"uid": self.uid},
            "request": {
                "reqid": reqid,
                "nbest": self.nbest,
                "workflow": self.workflow,
                "show_language": False,
                "show_utterances": self.show_utterances,
                "result_type": self.result_type,
                "sequence": 1,
            },
            "audio": {
                "format": "raw",
                "rate": self.sample_rate,
                "language": self.language,
                "bits": 16,
                "channel": 1,
                "codec": "raw",
            },
        }

    @staticmethod
    def _extract_text(payload_msg: Any) -> str:
        """从 server response payload 里抽最终 text。

        火山返回示例：
          {"code":1000, "message":"Success",
           "result":[{"text":"你好世界。"}]}
        """
        if not isinstance(payload_msg, dict):
            return ""
        result = payload_msg.get("result")
        if isinstance(result, list):
            parts: List[str] = []
            for it in result:
                if isinstance(it, dict):
                    t = it.get("text")
                    if isinstance(t, str) and t.strip():
                        parts.append(t)
            return "".join(parts).strip()
        if isinstance(result, dict):
            t = result.get("text")
            return t.strip() if isinstance(t, str) else ""
        if isinstance(result, str):
            return result.strip()
        text = payload_msg.get("text")
        return text.strip() if isinstance(text, str) else ""

    async def _run_one(self, pcm_16k: np.ndarray, hotword: Optional[str]) -> str:
        reqid = str(uuid.uuid4())
        req = self._build_request(reqid)
        if hotword:
            # 火山 v2 ASR 通过 request.context 传热词；不同集群写法略有差异，
            # 这里选择保守地同时放入 request.hotword，服务端未支持时会忽略。
            req["request"]["hotword"] = hotword

        payload_bytes = gzip.compress(json.dumps(req).encode("utf-8"))
        full_client_request = bytearray(_generate_full_default_header())
        full_client_request.extend(len(payload_bytes).to_bytes(4, "big"))
        full_client_request.extend(payload_bytes)

        audio_bytes = _float32_to_pcm16_bytes(pcm_16k)
        bytes_per_sec = self.sample_rate * 2  # 16bit mono
        segment_size = max(1, int(bytes_per_sec * self.seg_duration_ms / 1000))

        headers = {"Authorization": f"Bearer; {self.token}"}

        last_text = ""
        ws = await asyncio.wait_for(
            _ws_connect(self.ws_url, headers),
            timeout=self.connect_timeout,
        )
        try:
            await ws.send(bytes(full_client_request))
            res = await asyncio.wait_for(ws.recv(), timeout=self.recv_timeout)
            result = _parse_response(res)
            msg = result.get("payload_msg")
            if isinstance(msg, dict) and msg.get("code") not in (None, self.SUCCESS_CODE):
                raise RuntimeError(
                    f"volc asr handshake failed: code={msg.get('code')} "
                    f"message={msg.get('message')}"
                )

            for chunk, last in _slice_bytes(audio_bytes, segment_size):
                compressed = gzip.compress(chunk) if chunk else gzip.compress(b"")
                header = (
                    _generate_last_audio_header() if last else _generate_audio_default_header()
                )
                frame = bytearray(header)
                frame.extend(len(compressed).to_bytes(4, "big"))
                frame.extend(compressed)
                await ws.send(bytes(frame))

                res = await asyncio.wait_for(ws.recv(), timeout=self.recv_timeout)
                result = _parse_response(res)
                msg = result.get("payload_msg")
                if isinstance(msg, dict):
                    code = msg.get("code")
                    if code not in (None, self.SUCCESS_CODE):
                        raise RuntimeError(
                            f"volc asr error: code={code} message={msg.get('message')}"
                        )
                    text_now = self._extract_text(msg)
                    if text_now:
                        last_text = text_now
        finally:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

        return last_text

    def transcribe_pcm16k(
        self,
        pcm_16k: np.ndarray,
        hotword: Optional[str] = None,
    ) -> str:
        """同步接口：把一段 16kHz float32 PCM 送给火山 ASR，返回带标点文本。"""
        fut = asyncio.run_coroutine_threadsafe(
            self._run_one(pcm_16k, hotword), self._loop,
        )
        # 网络 + ASR 正常耗时 < 5s；加一点富余量
        return fut.result(timeout=max(self.recv_timeout + self.connect_timeout + 5.0, 20.0))

    def close(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:  # noqa: BLE001
            pass


__all__ = ["VolcAsrClient"]
