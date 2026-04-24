# EchoPass · 火山引擎豆包「双向流式 TTS」WebSocket V3 客户端
#
# 协议与控制台参数见官方文档：
#   https://www.volcengine.com/docs/6561/1329505
#
# 单条文本合成：建连 → StartSession → TaskRequest(文本) → FinishSession →
# 收包拼 PCM。与 xiaozhi-esp32-server 的 huoshan_double_stream 一致
# （namespace=BidirectionalTTS，事件号 100/102/200/352 等）。
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import List, Optional

import websockets

PROTOCOL_VERSION = 0b0001
DEFAULT_HEADER_SIZE = 0b0001

FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_RESPONSE = 0b1011
FULL_SERVER_RESPONSE = 0b1001
ERROR_INFORMATION = 0b1111

MsgTypeFlagNoSeq = 0b0000
MsgTypeFlagPositiveSeq = 0b1
MsgTypeFlagLastNoSeq = 0b10
MsgTypeFlagNegativeSeq = 0b11
MsgTypeFlagWithEvent = 0b100

NO_SERIALIZATION = 0b0000
JSON = 0b0001
COMPRESSION_NO = 0b0000

EVENT_NONE = 0
EVENT_Start_Connection = 1
EVENT_FinishConnection = 2
EVENT_ConnectionStarted = 50
EVENT_ConnectionFailed = 51
EVENT_ConnectionFinished = 52

EVENT_StartSession = 100
EVENT_CancelSession = 101
EVENT_FinishSession = 102
EVENT_SessionStarted = 150
EVENT_SessionCanceled = 151
EVENT_SessionFinished = 152
EVENT_SessionFailed = 153

EVENT_TaskRequest = 200
EVENT_TTSSentenceStart = 350
EVENT_TTSSentenceEnd = 351
EVENT_TTSResponse = 352


@dataclass
class _ParsedHeader:
    protocol_version: int = 0
    header_size: int = 0
    message_type: int = 0
    message_type_specific_flags: int = 0
    serialization_method: int = 0
    message_compression: int = 0
    reserved: int = 0


@dataclass
class _ParsedOptional:
    event: int = EVENT_NONE
    session_id: Optional[str] = None
    error_code: int = 0


@dataclass
class _ParsedMessage:
    header: _ParsedHeader
    optional: _ParsedOptional
    payload: bytes


def _read_content(res: bytes, offset: int) -> tuple[str, int]:
    size = int.from_bytes(res[offset : offset + 4], "big", signed=True)
    offset += 4
    text = res[offset : offset + size].decode("utf-8")
    offset += size
    return text, offset


def _read_payload(res: bytes, offset: int) -> tuple[bytes, int]:
    size = int.from_bytes(res[offset : offset + 4], "big", signed=True)
    offset += 4
    payload = res[offset : offset + size]
    offset += size
    return payload, offset


def parse_response(res: bytes) -> _ParsedMessage:
    """解析服务端下行二进制帧（与 huoshan_double_stream.parser_response 等价，修正 length 切片）。"""
    h = _ParsedHeader()
    num = 0b00001111
    h.protocol_version = (res[0] >> 4) & num
    h.header_size = res[0] & 0x0F
    h.message_type = (res[1] >> 4) & num
    h.message_type_specific_flags = res[1] & 0x0F
    h.serialization_method = res[2] >> 4
    h.message_compression = res[2] & 0x0F
    h.reserved = res[3]

    optional = _ParsedOptional()
    payload = b""
    offset = 4

    if h.message_type in (FULL_SERVER_RESPONSE, AUDIO_ONLY_RESPONSE):
        if h.message_type_specific_flags & MsgTypeFlagWithEvent:
            optional.event = int.from_bytes(
                res[offset : offset + 4], "big", signed=True,
            )
            offset += 4
            if optional.event == EVENT_NONE:
                return _ParsedMessage(h, optional, payload)
            if optional.event == EVENT_ConnectionStarted:
                _, offset = _read_content(res, offset)
            elif optional.event == EVENT_ConnectionFailed:
                _, offset = _read_content(res, offset)
            elif optional.event in (
                EVENT_SessionStarted,
                EVENT_SessionFailed,
                EVENT_SessionFinished,
            ):
                _, offset = _read_content(res, offset)
                _, offset = _read_content(res, offset)
            else:
                optional.session_id, offset = _read_content(res, offset)
                payload, offset = _read_payload(res, offset)
    elif h.message_type == ERROR_INFORMATION:
        optional.error_code = int.from_bytes(
            res[offset : offset + 4], "big", signed=True,
        )
        offset += 4
        payload, offset = _read_payload(res, offset)

    return _ParsedMessage(h, optional, payload)


def _header_as_bytes(
    message_type: int,
    flags: int,
    serial_method: int = JSON,
    compression: int = COMPRESSION_NO,
) -> bytes:
    return bytes(
        [
            (PROTOCOL_VERSION << 4) | DEFAULT_HEADER_SIZE,
            (message_type << 4) | flags,
            (serial_method << 4) | compression,
            0,
        ],
    )


def _optional_as_bytes(event: int, session_id: Optional[str] = None) -> bytes:
    buf = bytearray()
    buf.extend(event.to_bytes(4, "big", signed=True))
    if session_id is not None:
        sid = session_id.encode("utf-8")
        buf.extend(len(sid).to_bytes(4, "big", signed=True))
        buf.extend(sid)
    return bytes(buf)


async def _ws_connect(url: str, headers: dict):
    try:
        return await websockets.connect(
            url, additional_headers=headers, max_size=100_000_000,
        )
    except TypeError:
        return await websockets.connect(
            url, extra_headers=headers, max_size=100_000_000,
        )


def _build_payload(
    *,
    uid: str,
    event: int,
    text: str,
    speaker: str,
    audio_params: dict,
    additions: dict,
    mix_speaker: Optional[dict] = None,
) -> bytes:
    req_params: dict = {
        "text": text,
        "speaker": speaker,
        "audio_params": audio_params,
        "additions": json.dumps(additions, ensure_ascii=False),
    }
    if mix_speaker:
        req_params["mix_speaker"] = mix_speaker
    body = {
        "user": {"uid": uid},
        "event": event,
        "namespace": "BidirectionalTTS",
        "req_params": req_params,
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


async def _send_msg(ws, header: bytes, optional: bytes, payload: bytes) -> None:
    buf = bytearray(header)
    buf.extend(optional)
    buf.extend(len(payload).to_bytes(4, "big", signed=True))
    buf.extend(payload)
    await ws.send(bytes(buf))


def _raise_if_tts_fatal(msg: _ParsedMessage) -> None:
    if msg.header.message_type == ERROR_INFORMATION:
        raw_pl = msg.payload[:768] if msg.payload else b""
        pl_low = raw_pl.lower()
        hint = ""
        if (
            msg.optional.error_code == 55000000
            or b"mismatched" in pl_low
            or b"resource" in pl_low
        ):
            hint = (
                " | 提示：X-Api-Resource-Id 与音色所属商品不一致。"
                "豆包语音合成 2.0 音色请用 seed-tts-2.0；"
                "1.0 公版常用 volc.service_type.10029 或 seed-tts-1.0（见控制台音色列表/文档）。"
            )
        raise RuntimeError(
            f"volc bidirectional tts error: code={msg.optional.error_code} "
            f"payload={msg.payload[:512]!r}{hint}",
        )
    if msg.optional.event == EVENT_SessionFailed:
        try:
            err_txt = msg.payload.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            err_txt = repr(msg.payload[:200])
        raise RuntimeError(f"volc bidirectional tts session failed: {err_txt}")


async def synthesize_pcm_bytes(
    *,
    text: str,
    app_key: str,
    access_key: str,
    resource_id: str,
    ws_url: str,
    speaker: str,
    sample_rate: int = 24000,
    speech_rate: int = 0,
    loudness_rate: int = 0,
    audio_format: str = "pcm",
    additions: Optional[dict] = None,
    mix_speaker: Optional[dict] = None,
    connect_timeout: float = 10.0,
    recv_timeout: float = 60.0,
) -> bytes:
    """一次 WebSocket 会话：整段文本 → 拼接 PCM(s16le) 字节。"""
    text = (text or "").strip()
    if not text:
        return b""
    if not app_key or not access_key:
        raise ValueError("火山双向 TTS 需要 app_key 与 access_key")
    if not speaker:
        raise ValueError("火山双向 TTS 需要 speaker（音色 ID）")
    if not resource_id:
        raise ValueError("火山双向 TTS 需要 resource_id")

    uid = "echopass"
    add = additions if additions is not None else {
        "post_process": {"pitch": 0},
        "aigc_metadata": {},
        "cache_config": {},
    }
    audio_params = {
        "format": audio_format,
        "sample_rate": int(sample_rate),
        "speech_rate": int(speech_rate),
        "loudness_rate": int(loudness_rate),
    }
    session_id = uuid.uuid4().hex
    headers = {
        "X-Api-App-Key": app_key,
        "X-Api-Access-Key": access_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }

    hdr = _header_as_bytes(
        FULL_CLIENT_REQUEST,
        MsgTypeFlagWithEvent,
        serial_method=JSON,
        compression=COMPRESSION_NO,
    )

    chunks: List[bytes] = []
    ws = await asyncio.wait_for(
        _ws_connect(ws_url, headers),
        timeout=connect_timeout,
    )
    try:
        # StartSession
        pl = _build_payload(
            uid=uid,
            event=EVENT_StartSession,
            text="",
            speaker=speaker,
            audio_params=audio_params,
            additions=add,
            mix_speaker=mix_speaker,
        )
        await _send_msg(ws, hdr, _optional_as_bytes(EVENT_StartSession, session_id), pl)

        # TaskRequest（实际文本）
        pl = _build_payload(
            uid=uid,
            event=EVENT_TaskRequest,
            text=text,
            speaker=speaker,
            audio_params=audio_params,
            additions=add,
            mix_speaker=mix_speaker,
        )
        await _send_msg(ws, hdr, _optional_as_bytes(EVENT_TaskRequest, session_id), pl)

        # FinishSession
        await _send_msg(
            ws, hdr, _optional_as_bytes(EVENT_FinishSession, session_id), b"{}",
        )

        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
            if not isinstance(raw, (bytes, bytearray)):
                continue
            msg = parse_response(bytes(raw))
            _raise_if_tts_fatal(msg)
            if (
                msg.optional.event == EVENT_TTSResponse
                and msg.header.message_type == AUDIO_ONLY_RESPONSE
                and msg.payload
            ):
                chunks.append(msg.payload)
            if msg.optional.event == EVENT_SessionFinished:
                break
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

    return b"".join(chunks)


def synthesize_pcm_bytes_sync(**kwargs) -> bytes:
    """在线程池中调用：避免 FastAPI 主线程已有事件循环时 asyncio.run 冲突。"""
    return asyncio.run(synthesize_pcm_bytes(**kwargs))


class BidirectionalTtsStreamSession:
    """火山双向流式 TTS：同一会话内多次 TaskRequest 推送文本，异步接收 PCM 分片。

    协议见 https://www.volcengine.com/docs/6561/1329505
    """

    def __init__(
        self,
        *,
        app_key: str,
        access_key: str,
        resource_id: str,
        ws_url: str,
        speaker: str,
        sample_rate: int = 24000,
        speech_rate: int = 0,
        loudness_rate: int = 0,
        audio_format: str = "pcm",
        additions: Optional[dict] = None,
        mix_speaker: Optional[dict] = None,
        connect_timeout: float = 10.0,
        recv_timeout: float = 120.0,
    ) -> None:
        if not app_key or not access_key:
            raise ValueError("BidirectionalTtsStreamSession 需要 app_key / access_key")
        if not speaker:
            raise ValueError("BidirectionalTtsStreamSession 需要 speaker")
        if not resource_id:
            raise ValueError("BidirectionalTtsStreamSession 需要 resource_id")
        self._app_key = app_key
        self._access_key = access_key
        self._resource_id = resource_id
        self._ws_url = ws_url
        self._speaker = speaker
        self._sample_rate = int(sample_rate)
        self._speech_rate = int(speech_rate)
        self._loudness_rate = int(loudness_rate)
        self._audio_format = audio_format
        self._additions = additions if additions is not None else {
            "post_process": {"pitch": 0},
            "aigc_metadata": {},
            "cache_config": {},
        }
        self._mix_speaker = mix_speaker
        self._connect_timeout = float(connect_timeout)
        self._recv_timeout = float(recv_timeout)
        self._uid = "echopass"
        self._session_id = uuid.uuid4().hex
        self._ws = None
        self._hdr = _header_as_bytes(
            FULL_CLIENT_REQUEST,
            MsgTypeFlagWithEvent,
            serial_method=JSON,
            compression=COMPRESSION_NO,
        )
        self._audio_params = {
            "format": self._audio_format,
            "sample_rate": self._sample_rate,
            "speech_rate": self._speech_rate,
            "loudness_rate": self._loudness_rate,
        }

    async def connect(self) -> None:
        headers = {
            "X-Api-App-Key": self._app_key,
            "X-Api-Access-Key": self._access_key,
            "X-Api-Resource-Id": self._resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
        self._ws = await asyncio.wait_for(
            _ws_connect(self._ws_url, headers),
            timeout=self._connect_timeout,
        )
        pl = _build_payload(
            uid=self._uid,
            event=EVENT_StartSession,
            text="",
            speaker=self._speaker,
            audio_params=self._audio_params,
            additions=self._additions,
            mix_speaker=self._mix_speaker,
        )
        await _send_msg(
            self._ws, self._hdr, _optional_as_bytes(EVENT_StartSession, self._session_id), pl,
        )

    async def send_text_fragment(self, text: str) -> None:
        text = (text or "").strip()
        if not text or self._ws is None:
            return
        pl = _build_payload(
            uid=self._uid,
            event=EVENT_TaskRequest,
            text=text,
            speaker=self._speaker,
            audio_params=self._audio_params,
            additions=self._additions,
            mix_speaker=self._mix_speaker,
        )
        await _send_msg(
            self._ws, self._hdr, _optional_as_bytes(EVENT_TaskRequest, self._session_id), pl,
        )

    async def finish(self) -> None:
        if self._ws is None:
            return
        await _send_msg(
            self._ws,
            self._hdr,
            _optional_as_bytes(EVENT_FinishSession, self._session_id),
            b"{}",
        )

    async def run_downlink(self, pcm_queue: asyncio.Queue) -> None:
        """下行事件入队：{\"type\":\"pcm\",\"data\":bytes}，结束时 {\"type\":\"audio_done\"}。"""
        if self._ws is None:
            await pcm_queue.put({"type": "audio_done"})
            return
        try:
            while True:
                raw = await asyncio.wait_for(
                    self._ws.recv(), timeout=self._recv_timeout,
                )
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                msg = parse_response(bytes(raw))
                _raise_if_tts_fatal(msg)
                if (
                    msg.optional.event == EVENT_TTSResponse
                    and msg.header.message_type == AUDIO_ONLY_RESPONSE
                    and msg.payload
                ):
                    await pcm_queue.put({"type": "pcm", "data": msg.payload})
                if msg.optional.event == EVENT_SessionFinished:
                    await pcm_queue.put({"type": "audio_done"})
                    return
        except Exception as e:  # noqa: BLE001
            try:
                await pcm_queue.put({"type": "error", "message": str(e)})
            except Exception:  # noqa: BLE001
                pass
            try:
                await pcm_queue.put({"type": "audio_done"})
            except Exception:  # noqa: BLE001
                pass
            raise

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None


__all__ = [
    "synthesize_pcm_bytes",
    "synthesize_pcm_bytes_sync",
    "parse_response",
    "BidirectionalTtsStreamSession",
]
