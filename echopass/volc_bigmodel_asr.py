# EchoPass · 火山引擎「豆包流式语音识别模型 2.0」（大模型版 / v3 协议）客户端
#
# 对外只暴露 VolcBigmodelAsrClient.transcribe_pcm16k(pcm, hotword) -> str，
# 同步接口，内部跑 websockets 异步客户端 + 独立事件循环线程，
# 和 FastAPI 主事件循环解耦，可直接在 async handler 的同步代码路径调用。
#
# 协议来自 openspeech v3 大模型 demo（sauc.bigmodel）：
#   wss://openspeech.bytedance.com/api/v3/sauc/bigmodel
# 关键差异（vs 通用版 /api/v2/asr）：
#   - 鉴权走 HTTP Headers：X-Api-App-Key / X-Api-Access-Key /
#     X-Api-Resource-Id / X-Api-Request-Id（不再是 `Authorization: Bearer;`）
#   - 帧结构多 4 字节 seq：header(4B) + seq(4B, big-endian int32) +
#     payload_size(4B, big-endian uint32) + gzip(payload)
#   - 首帧（full client request）flags = POS_SEQUENCE (seq=1)
#   - 中间音频帧 flags = POS_SEQUENCE (seq=2,3,...)
#   - 末帧音频帧 flags = NEG_WITH_SEQUENCE (0b0011)，seq 置为 -N
#   - 首帧 JSON payload 不再包含 `app` 块（appid/cluster/token 从 header 走），
#     核心字段是 `request.model_name = "bigmodel"` + 后处理开关
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import struct
import threading
import uuid
from typing import Any, Dict, List, Optional

import numpy as np
import websockets

logger = logging.getLogger("echopass.volc_bigmodel_asr")

# ── 协议常量（与 openspeech v3 demo 一致）───────────────────────────────
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
NEG_WITH_SEQUENCE = 0b0011

NO_SERIALIZATION = 0b0000
JSON_SERIAL = 0b0001

NO_COMPRESSION = 0b0000
GZIP = 0b0001


def _make_header(
    message_type: int,
    flags: int = POS_SEQUENCE,
    serial_method: int = JSON_SERIAL,
    compression: int = GZIP,
    reserved: int = 0x00,
) -> bytes:
    """拼 4 字节协议头。"""
    h = bytearray(4)
    h[0] = (PROTOCOL_VERSION << 4) | DEFAULT_HEADER_SIZE
    h[1] = (message_type << 4) | flags
    h[2] = (serial_method << 4) | compression
    h[3] = reserved
    return bytes(h)


def _parse_response(res: bytes) -> Dict[str, Any]:
    """解析服务端 bytes → dict(code, seq, is_last, payload_msg)。"""
    header_size = res[0] & 0x0F
    message_type = res[1] >> 4
    flags = res[1] & 0x0F
    serial = res[2] >> 4
    compression = res[2] & 0x0F
    payload = res[header_size * 4:]

    out: Dict[str, Any] = {
        "code": 0,
        "seq": 0,
        "is_last": False,
        "event": 0,
        "payload_msg": None,
    }

    # flags：bit0=带 seq，bit1=末包，bit2=带 event
    if flags & 0x01:
        out["seq"] = struct.unpack(">i", payload[:4])[0]
        payload = payload[4:]
    if flags & 0x02:
        out["is_last"] = True
    if flags & 0x04:
        out["event"] = struct.unpack(">i", payload[:4])[0]
        payload = payload[4:]

    if message_type == SERVER_FULL_RESPONSE:
        if len(payload) >= 4:
            _size = struct.unpack(">I", payload[:4])[0]  # noqa: F841
            payload = payload[4:]
    elif message_type == SERVER_ACK:
        # ACK 不一定带 body；若有，前 4 字节是 size
        if len(payload) >= 4:
            _size = struct.unpack(">I", payload[:4])[0]  # noqa: F841
            payload = payload[4:]
    elif message_type == SERVER_ERROR_RESPONSE:
        out["code"] = struct.unpack(">i", payload[:4])[0]
        if len(payload) >= 8:
            _size = struct.unpack(">I", payload[4:8])[0]  # noqa: F841
        payload = payload[8:]

    if not payload:
        return out

    try:
        if compression == GZIP:
            payload = gzip.decompress(payload)
        if serial == JSON_SERIAL:
            out["payload_msg"] = json.loads(payload.decode("utf-8"))
        elif serial == NO_SERIALIZATION:
            out["payload_msg"] = payload
        else:
            out["payload_msg"] = payload.decode("utf-8", errors="ignore")
    except Exception as e:  # noqa: BLE001
        logger.error("解析 v3 响应失败：%s", e)

    return out


def _float32_to_pcm16_bytes(pcm: np.ndarray) -> bytes:
    arr = np.asarray(pcm, dtype=np.float32)
    if arr.ndim > 1:
        arr = arr.reshape(-1)
    arr = np.clip(arr, -1.0, 1.0)
    return (arr * 32767.0).astype(np.int16).tobytes()


def _slice_bytes(data: bytes, chunk_size: int):
    """yield (chunk, is_last)。"""
    if chunk_size <= 0:
        yield data, True
        return
    if not data:
        yield b"", True
        return
    offset = 0
    n = len(data)
    while offset + chunk_size < n:
        yield data[offset:offset + chunk_size], False
        offset += chunk_size
    yield data[offset:n], True


async def _ws_connect(url: str, headers: Dict[str, str]):
    """兼容 websockets>=13 (additional_headers) 和旧版 (extra_headers)。"""
    try:
        return await websockets.connect(
            url, additional_headers=headers, max_size=1_000_000_000,
        )
    except TypeError:
        return await websockets.connect(
            url, extra_headers=headers, max_size=1_000_000_000,
        )


class VolcBigmodelAsrClient:
    """火山豆包流式 ASR 2.0 客户端（v3 协议）。一次调用 = 一条独立 WS 会话。"""

    def __init__(
        self,
        app_key: str,
        access_key: str,
        resource_id: str = "volc.bigasr.sauc.duration",
        ws_url: str = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel",
        model_name: str = "bigmodel",
        uid: str = "echopass",
        sample_rate: int = 16000,
        seg_duration_ms: int = 200,
        enable_itn: bool = True,
        enable_punc: bool = True,
        enable_ddc: bool = True,
        show_utterances: bool = True,
        enable_nonstream: bool = False,
        connect_timeout: float = 5.0,
        recv_timeout: float = 15.0,
    ) -> None:
        if not app_key or not access_key:
            raise ValueError("VolcBigmodelAsrClient 需要非空的 app_key / access_key")
        self.app_key = app_key
        self.access_key = access_key
        self.resource_id = resource_id
        self.ws_url = ws_url
        self.model_name = model_name
        self.uid = uid
        self.sample_rate = int(sample_rate)
        self.seg_duration_ms = int(seg_duration_ms)
        self.enable_itn = bool(enable_itn)
        self.enable_punc = bool(enable_punc)
        self.enable_ddc = bool(enable_ddc)
        self.show_utterances = bool(show_utterances)
        self.enable_nonstream = bool(enable_nonstream)
        self.connect_timeout = float(connect_timeout)
        self.recv_timeout = float(recv_timeout)

        # 独立事件循环线程：和 FastAPI 主事件循环解耦
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            name="volc-bigmodel-asr-loop",
            daemon=True,
        )
        self._loop_thread.start()

    # ---------- 报文构造 ----------
    # 热词字符串切分：空格 / 中英文逗号 / 顿号 / 分号 / 换行
    _HOTWORD_SPLIT_RE = re.compile(r"[\s,，、;；]+")
    # 火山双向流式热词直传上限 100 tokens，中文按 1 token/字估算，
    # 留点余量，软裁剪到 50 个词。超出后服务端可能截断或拒识。
    _MAX_HOTWORDS = 50

    @classmethod
    def _hotword_context(cls, hotword: Optional[str]) -> Optional[str]:
        """把用户传入的热词字符串转成 request.corpus.context 所需的 JSON 字符串。

        官方格式（双向流式）：
          "context": "{\"hotwords\":[{\"word\":\"热词1\"}, {\"word\":\"热词2\"}]}"
        """
        if not hotword:
            return None
        words: List[str] = []
        seen = set()
        for w in cls._HOTWORD_SPLIT_RE.split(hotword.strip()):
            w = w.strip()
            if not w or w in seen:
                continue
            seen.add(w)
            words.append(w)
            if len(words) >= cls._MAX_HOTWORDS:
                break
        if not words:
            return None
        return json.dumps(
            {"hotwords": [{"word": w} for w in words]},
            ensure_ascii=False,
        )

    def _build_first_payload(self, hotword: Optional[str]) -> Dict[str, Any]:
        req: Dict[str, Any] = {
            "model_name": self.model_name,
            "enable_itn": self.enable_itn,
            "enable_punc": self.enable_punc,
            "enable_ddc": self.enable_ddc,
            "show_utterances": self.show_utterances,
            "enable_nonstream": self.enable_nonstream,
        }
        ctx = self._hotword_context(hotword)
        if ctx:
            # 官方热词直传：request.corpus.context = JSON 字符串
            # {"hotwords":[{"word":"热词1"}, ...]}
            req["corpus"] = {"context": ctx}
        return {
            "user": {"uid": self.uid},
            "audio": {
                # 火山大模型 API audio.format 合法值: pcm / wav / ogg / mp3
                # pcm/wav 内部流必须是 pcm_s16le；我们直接送 PCM16LE raw bytes，
                # 所以用 format=pcm + codec=raw。
                "format": "pcm",
                "codec": "raw",
                "rate": self.sample_rate,
                "bits": 16,
                "channel": 1,
            },
            "request": req,
        }

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Access-Key": self.access_key,
            "X-Api-App-Key": self.app_key,
        }

    def _pack_first_frame(self, seq: int, payload: Dict[str, Any]) -> bytes:
        body = gzip.compress(json.dumps(payload).encode("utf-8"))
        out = bytearray()
        out += _make_header(CLIENT_FULL_REQUEST, flags=POS_SEQUENCE)
        out += struct.pack(">i", seq)
        out += struct.pack(">I", len(body))
        out += body
        return bytes(out)

    def _pack_audio_frame(self, seq: int, chunk: bytes, is_last: bool) -> bytes:
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

    # ---------- 结果提取 ----------
    @staticmethod
    def _extract_text(payload_msg: Any) -> str:
        """尽量宽容地从 v3 响应里挖出 text。

        常见结构（大模型版）：
          {"result": {"text": "...", "utterances": [...]}}
        兼容 v2 风格：
          {"result": [{"text": "..."}]}
        """
        if not isinstance(payload_msg, dict):
            return ""
        result = payload_msg.get("result")
        if isinstance(result, dict):
            t = result.get("text")
            if isinstance(t, str):
                return t.strip()
        if isinstance(result, list):
            parts: List[str] = []
            for it in result:
                if isinstance(it, dict):
                    t = it.get("text")
                    if isinstance(t, str) and t.strip():
                        parts.append(t)
            return "".join(parts).strip()
        text = payload_msg.get("text")
        return text.strip() if isinstance(text, str) else ""

    # ---------- 核心流程 ----------
    async def _run_one(self, pcm_16k: np.ndarray, hotword: Optional[str]) -> str:
        audio_bytes = _float32_to_pcm16_bytes(pcm_16k)
        bytes_per_sec = self.sample_rate * 2  # 16bit mono
        segment_size = max(1, int(bytes_per_sec * self.seg_duration_ms / 1000))

        headers = self._auth_headers()
        ws = await asyncio.wait_for(
            _ws_connect(self.ws_url, headers),
            timeout=self.connect_timeout,
        )

        last_text = ""
        try:
            # 1. 首帧 full client request（seq=1）
            seq = 1
            first_frame = self._pack_first_frame(seq, self._build_first_payload(hotword))
            await ws.send(first_frame)

            # 首帧的 ack 一般会很快返回，但 v3 允许立即追发音频，
            # 为了兼容两种行为，我们不强制等 ack，直接进入边发边收模式。

            async def sender():
                nonlocal seq
                for chunk, is_last in _slice_bytes(audio_bytes, segment_size):
                    seq += 1
                    frame = self._pack_audio_frame(seq, chunk, is_last)
                    await ws.send(frame)
                    if is_last:
                        break

            async def receiver() -> str:
                text = ""
                while True:
                    # 对"本段无语音"场景容错：
                    #   - recv 超时：火山服务端在判定无有效语音时，常常既不发
                    #     is_last 也不返回错误码，而是静默到我们自己超时；
                    #   - ConnectionClosed：服务端直接 1000 OK 关掉连接也是同样含义。
                    # 这两种情况都视作"本段无语音"，按当前已收到的 text 正常收尾，
                    # 不再上抛异常，避免 engine 层打出空串 WARNING 刷屏日志。
                    try:
                        msg = await asyncio.wait_for(
                            ws.recv(), timeout=self.recv_timeout,
                        )
                    except asyncio.TimeoutError:
                        logger.debug(
                            "v3 receiver recv_timeout，当作本段无语音处理（已收 text=%r）",
                            text,
                        )
                        return text
                    except websockets.exceptions.ConnectionClosed:
                        logger.debug(
                            "v3 receiver 连接被服务端关闭，当作本段无语音处理（已收 text=%r）",
                            text,
                        )
                        return text
                    if not isinstance(msg, (bytes, bytearray)):
                        continue
                    parsed = _parse_response(bytes(msg))
                    code = parsed.get("code", 0)
                    if code and code != 0:
                        payload_msg = parsed.get("payload_msg") or {}
                        err_msg = (
                            payload_msg.get("message") if isinstance(payload_msg, dict) else ""
                        )
                        raise RuntimeError(
                            f"volc bigmodel asr error: code={code} message={err_msg}"
                        )
                    t = self._extract_text(parsed.get("payload_msg"))
                    if t:
                        text = t
                    if parsed.get("is_last"):
                        return text
                return text

            send_task = asyncio.create_task(sender())
            try:
                last_text = await receiver()
            finally:
                if not send_task.done():
                    send_task.cancel()
                    try:
                        await send_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
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
        """同步接口：一段 16kHz float32 PCM → 带标点文本。"""
        fut = asyncio.run_coroutine_threadsafe(
            self._run_one(pcm_16k, hotword), self._loop,
        )
        return fut.result(
            timeout=max(self.recv_timeout + self.connect_timeout + 5.0, 20.0),
        )

    def close(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:  # noqa: BLE001
            pass


__all__ = ["VolcBigmodelAsrClient"]
