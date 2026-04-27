# EchoPass · 推理引擎：CAM++ 声纹 + FunASR ASR/VAD/标点/KWS。
# CAM++ 模型与权重来源于 3D-Speaker（Apache-2.0），详见仓库根 NOTICE。
import io
import os
import re
import tempfile
import pathlib
import threading
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import torch
import torchaudio

from modelscope.hub.snapshot_download import snapshot_download

from echopass.audio_features import FBank
from echopass.campplus_model import CAMPPlus

# CAM++ 中文通用模型，与 3D-Speaker 官方推理脚本一致
DEFAULT_MODEL_ID = "iic/speech_campplus_sv_zh-cn_16k-common"

CAMPPLUS_COMMON = {
    "feat_dim": 80,
    "embedding_size": 192,
}

MODEL_REGISTRY = {
    DEFAULT_MODEL_ID: {
        "revision": "v1.0.0",
        "model_args": CAMPPLUS_COMMON,
        "model_pt": "campplus_cn_common.bin",
    },
}


def _embedding_dim_for_model(model_id: str) -> int:
    return int(MODEL_REGISTRY[model_id]["model_args"]["embedding_size"])


class CamPlusSpeakerEngine:
    """加载 CAM++，提取声纹；内存中保存已注册说话人向量（L2 归一化后做点积得分）。"""

    def __init__(
        self,
        repo_root: pathlib.Path,
        model_id: str = DEFAULT_MODEL_ID,
        local_model_dir: Optional[pathlib.Path] = None,
        pg_dsn: Optional[str] = None,
    ):
        self.repo_root = pathlib.Path(repo_root)
        self.model_id = model_id
        if model_id not in MODEL_REGISTRY:
            raise ValueError(f"Demo 仅内置 model_id={list(MODEL_REGISTRY.keys())}")
        self._conf = MODEL_REGISTRY[model_id]
        self.local_model_dir = local_model_dir or (self.repo_root / "pretrained")
        self._embedding_dim = _embedding_dim_for_model(model_id)
        self._pg_dsn = (pg_dsn or "").strip() or None
        self._lock = threading.Lock()
        self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self._model = None
        self._fbank = FBank(80, sample_rate=16000, mean_nor=True)
        self._gallery: Dict[str, np.ndarray] = {}
        self._gallery_names: List[str] = []
        self._gallery_matrix = np.empty((0, self._embedding_dim), dtype=np.float32)
        if self._pg_dsn:
            try:
                self._ensure_pg_schema()
                self._load_from_pg()
            except Exception as e:  # noqa: BLE001
                # 启动时连不上 PG 或建表失败：不要让整个服务起不来，
                # 退化为仅内存模式并打 warning；后续 enroll/delete 也只走内存。
                import logging
                logging.getLogger("echopass").warning(
                    "PG 初始化失败，已退化为仅内存模式：%s", e,
                )
                self._pg_dsn = None

    def _rebuild_gallery_cache_locked(self) -> None:
        self._gallery_names = sorted(self._gallery.keys())
        if self._gallery_names:
            self._gallery_matrix = np.stack(
                [self._gallery[name] for name in self._gallery_names]
            ).astype(np.float32, copy=False)
        else:
            self._gallery_matrix = np.empty(
                (0, self._embedding_dim), dtype=np.float32
            )

    def _pg_connect(self):
        import psycopg2

        return psycopg2.connect(self._pg_dsn)

    def _ensure_pg_schema(self) -> None:
        """启动时若表不存在则自动建好；与 sql/schema.sql 等价、幂等。"""
        ddl = """
        CREATE TABLE IF NOT EXISTS echopass_speaker_enrollments (
            id BIGSERIAL PRIMARY KEY,
            speaker_name  TEXT        NOT NULL,
            model_id      TEXT        NOT NULL,
            embedding_dim SMALLINT    NOT NULL,
            embedding     BYTEA       NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_echopass_speaker_model_name UNIQUE (model_id, speaker_name)
        );
        CREATE INDEX IF NOT EXISTS idx_echopass_speaker_model_id
            ON echopass_speaker_enrollments (model_id);
        """
        conn = self._pg_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()
        finally:
            conn.close()

    def _load_from_pg(self) -> None:
        conn = self._pg_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT speaker_name, embedding_dim, embedding
                    FROM echopass_speaker_enrollments
                    WHERE model_id = %s
                    """,
                    (self.model_id,),
                )
                rows = cur.fetchall()
            for name, dim, blob in rows:
                arr = np.frombuffer(memoryview(blob), dtype=np.float32)
                if arr.size != dim or dim != self._embedding_dim:
                    continue
                n = np.linalg.norm(arr)
                if n < 1e-8:
                    continue
                self._gallery[name] = arr / n
            self._rebuild_gallery_cache_locked()
        finally:
            conn.close()

    def _pg_upsert(self, name: str, emb: np.ndarray) -> None:
        blob = np.asarray(emb, dtype=np.float32).tobytes()
        if len(blob) != self._embedding_dim * 4:
            raise ValueError("embedding 维度与当前模型不一致")
        conn = self._pg_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO echopass_speaker_enrollments
                        (speaker_name, model_id, embedding_dim, embedding)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (model_id, speaker_name) DO UPDATE SET
                        embedding_dim = EXCLUDED.embedding_dim,
                        embedding = EXCLUDED.embedding,
                        updated_at = NOW()
                    """,
                    (name, self.model_id, self._embedding_dim, blob),
                )
            conn.commit()
        finally:
            conn.close()

    def _pg_delete(self, name: str) -> None:
        conn = self._pg_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM echopass_speaker_enrollments
                    WHERE model_id = %s AND speaker_name = %s
                    """,
                    (self.model_id, name),
                )
            conn.commit()
        finally:
            conn.close()

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        save_dir = self.local_model_dir / self.model_id.split("/")[1]
        save_dir.mkdir(exist_ok=True, parents=True)
        ckpt = save_dir / self._conf["model_pt"]
        # 避免每次启动都调用 snapshot_download：已有权重（含指向上游缓存的 symlink）
        # 时直接加载，跳过 ModelScope Hub 的 revision/元数据校验与潜在网络等待（内网环境可达 60s+）。
        if not ckpt.exists():
            offline = os.environ.get("MODELSCOPE_OFFLINE", "").strip().lower() in (
                "1", "true", "yes", "on",
            )
            dl_kw: Dict[str, Any] = {"revision": self._conf["revision"]}
            if offline:
                # 离线模式仅使用本地 Hub 缓存，不向公网拉取更新
                dl_kw["local_files_only"] = True
            cache_dir = pathlib.Path(snapshot_download(self.model_id, **dl_kw))
            download_files = ["examples", self._conf["model_pt"]]
            pattern = "|".join(re.escape(x) for x in download_files)
            for src in cache_dir.glob("*"):
                if re.search(pattern, src.name):
                    dst = save_dir / src.name
                    try:
                        dst.unlink()
                    except FileNotFoundError:
                        pass
                    dst.symlink_to(src)
            ckpt = save_dir / self._conf["model_pt"]
        if not ckpt.exists():
            raise FileNotFoundError(
                f"CAM++ 权重缺失: {ckpt}；请确认已下载模型或暂时关闭 MODELSCOPE_OFFLINE 后重启",
            )
        state = torch.load(ckpt, map_location="cpu")
        model = CAMPPlus(**self._conf["model_args"])
        model.load_state_dict(state)
        model.to(self._device)
        model.eval()
        self._model = model

    def _prepare_wav(self, wav: torch.Tensor, sr: int) -> torch.Tensor:
        if sr != 16000:
            wav = torchaudio.functional.resample(wav, sr, 16000)
        if wav.dim() == 1:
            wav = wav.unsqueeze(0)
        if wav.shape[0] > 1:
            wav = wav[0:1, :]
        return wav

    def _wav_to_embedding(self, wav: torch.Tensor) -> np.ndarray:
        self._ensure_model()
        if wav.numel() < 8000:
            raise ValueError("有效音频过短，请至少录制约 0.5 秒以上（16kHz）。")
        if wav.abs().max().item() < 1e-5:
            raise ValueError("音频幅度过小，请检查麦克风。")
        feat = self._fbank(wav).unsqueeze(0).to(self._device)
        with torch.no_grad():
            emb = self._model(feat).detach().squeeze(0).cpu().numpy()
        n = np.linalg.norm(emb)
        if n < 1e-8:
            raise ValueError("提取声纹失败（范数过小）。")
        return emb / n

    def embedding_from_file(self, path: str) -> np.ndarray:
        wav, sr = torchaudio.load(path)
        wav = self._prepare_wav(wav, sr)
        with self._lock:
            return self._wav_to_embedding(wav)

    def embedding_from_upload(self, data: bytes, suffix: str = ".wav") -> np.ndarray:
        buf = io.BytesIO(data)
        try:
            wav, sr = torchaudio.load(buf, format=suffix.lstrip(".").lower() or None)
        except Exception as e:
            raise ValueError(f"无法解码音频（可尝试 wav / 安装 ffmpeg 以支持 webm）: {e}") from e
        wav = self._prepare_wav(wav, int(sr))
        with self._lock:
            return self._wav_to_embedding(wav)

    def embedding_from_pcm_float32(self, pcm: np.ndarray, sample_rate: int) -> np.ndarray:
        if pcm.dtype != np.float32:
            pcm = pcm.astype(np.float32)
        wav = torch.from_numpy(pcm.copy())
        if wav.dim() == 2:
            wav = wav.mean(dim=0)
        wav = wav.unsqueeze(0)
        wav = self._prepare_wav(wav, sample_rate)
        with self._lock:
            return self._wav_to_embedding(wav)

    def enroll(self, name: str, emb: np.ndarray) -> None:
        emb = np.asarray(emb, dtype=np.float32)
        emb = emb / np.linalg.norm(emb)
        with self._lock:
            if name in self._gallery:
                merged = self._gallery[name] + emb
                self._gallery[name] = merged / np.linalg.norm(merged)
            else:
                self._gallery[name] = emb
            self._rebuild_gallery_cache_locked()
            final = np.array(self._gallery[name], copy=True)
        if self._pg_dsn:
            self._pg_upsert(name, final)

    def remove_speaker(self, name: str) -> bool:
        with self._lock:
            ok = self._gallery.pop(name, None) is not None
            if ok:
                self._rebuild_gallery_cache_locked()
        if ok and self._pg_dsn:
            self._pg_delete(name)
        return ok

    def list_speakers(self) -> List[str]:
        with self._lock:
            return list(self._gallery_names)

    def identify(
        self, emb: np.ndarray, threshold: float
    ) -> Tuple[Optional[str], float, List[Tuple[str, float]]]:
        emb = np.asarray(emb, dtype=np.float32)
        emb = emb / np.linalg.norm(emb)
        with self._lock:
            if not self._gallery_names:
                return None, 0.0, []
            similarities = self._gallery_matrix @ emb
            order = np.argsort(similarities)[::-1]
            scores = [
                (self._gallery_names[idx], float(similarities[idx])) for idx in order
            ]
            best_name, best_s = scores[0]
            if best_s < threshold:
                return None, best_s, scores
            return best_name, best_s, scores


# ---------------------------------------------------------------------------
# 流式 ASR：火山引擎云端 WebSocket
# 支持两个后端，通过 SPEAKER_VOLC_ASR_API 切换：
#   - "bigmodel"（默认）：豆包流式 ASR 2.0，走 /api/v3/sauc/bigmodel，
#                        Headers 鉴权 (X-Api-App-Key / X-Api-Access-Key /
#                        X-Api-Resource-Id)，不需要 cluster。
#   - "common"：通用流式 ASR，走 /api/v2/asr，Bearer token 鉴权，需 cluster。
# 必需环境变量：
#   SPEAKER_VOLC_ASR_APPID / _TOKEN
# 可选环境变量（共用）：
#   SPEAKER_VOLC_ASR_API      默认 bigmodel，可选 common
#   SPEAKER_VOLC_ASR_WS_URL   不填则按 API 自动选择
#   SPEAKER_VOLC_ASR_UID      默认 echopass
#   SPEAKER_VOLC_ASR_SEG_MS   默认 200(bigmodel) / 15000(common)，单片最大毫秒
# bigmodel 专属：
#   SPEAKER_VOLC_ASR_RESOURCE_ID 默认 volc.bigasr.sauc.duration
#   SPEAKER_VOLC_ASR_MODEL_NAME  默认 bigmodel
# common 专属：
#   SPEAKER_VOLC_ASR_CLUSTER  默认 volcengine_streaming_common
#   SPEAKER_VOLC_ASR_LANGUAGE 默认 zh-CN
#   SPEAKER_VOLC_ASR_WORKFLOW 默认 audio_in,resample,partition,vad,fe,decode,itn,nlu_punctuate
# ---------------------------------------------------------------------------


class StreamingASREngine:
    """火山引擎云端流式 ASR。

    契约与历史实现一致：每次 ``transcribe_chunk`` 吃一段 16kHz float32 PCM，
    返回一整段带标点的文本。底层为每段单独开一条 WS 会话（保持 app.py
    调用路径无需改动），真正的跨请求流式（常驻 WS）作为后续增量优化。
    """

    DEFAULT_API = "bigmodel"  # "bigmodel"（推荐）或 "common"
    DEFAULT_WS_URL_BIGMODEL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
    DEFAULT_WS_URL_COMMON = "wss://openspeech.bytedance.com/api/v2/asr"
    DEFAULT_RESOURCE_ID = "volc.bigasr.sauc.duration"
    DEFAULT_MODEL_NAME = "bigmodel"
    DEFAULT_WORKFLOW = "audio_in,resample,partition,vad,fe,decode,itn,nlu_punctuate"
    DEFAULT_LANGUAGE = "zh-CN"
    DEFAULT_UID = "echopass"
    DEFAULT_SEG_MS_BIGMODEL = 200   # 大模型版边发边收，小分片；对应实时语音的推荐值
    DEFAULT_SEG_MS_COMMON = 15000   # 通用版一次会话一整段，单片上限较大

    # 不在仓库中内置火山凭据；请在 config 或环境变量中配置 asr.volc.appid / token。
    _DEMO_APPID = ""
    _DEMO_TOKEN = ""
    # 通用版 cluster，仅 API=common 时用；API=bigmodel 时忽略。
    _DEMO_CLUSTER = "volcengine_streaming_common"

    def __init__(self, base_dir: Optional[pathlib.Path] = None) -> None:
        del base_dir  # 仅为了兼容老调用签名，这里不再使用本地权重
        self._model = None
        self._client = None
        self._lock = threading.Lock()

        # 配置来源优先级：环境变量 > config/*.yaml > 类内置默认
        from echopass.config import cfg

        api = cfg("asr.volc.api", "SPEAKER_VOLC_ASR_API", self.DEFAULT_API,
                  lambda v: str(v).strip().lower())
        if api not in ("bigmodel", "common"):
            api = self.DEFAULT_API
        self._api = api

        self._appid = cfg("asr.volc.appid", "SPEAKER_VOLC_ASR_APPID", self._DEMO_APPID, str)
        self._token = cfg("asr.volc.token", "SPEAKER_VOLC_ASR_TOKEN", self._DEMO_TOKEN, str)
        self._cluster = cfg("asr.volc.cluster", "SPEAKER_VOLC_ASR_CLUSTER", self._DEMO_CLUSTER, str)
        default_ws = (self.DEFAULT_WS_URL_BIGMODEL if api == "bigmodel"
                      else self.DEFAULT_WS_URL_COMMON)
        self._ws_url = cfg("asr.volc.ws_url", "SPEAKER_VOLC_ASR_WS_URL", default_ws, str)
        self._resource_id = cfg("asr.volc.resource_id", "SPEAKER_VOLC_ASR_RESOURCE_ID",
                                self.DEFAULT_RESOURCE_ID, str)
        self._model_name = cfg("asr.volc.model_name", "SPEAKER_VOLC_ASR_MODEL_NAME",
                               self.DEFAULT_MODEL_NAME, str)
        self._language = cfg("asr.volc.language", "SPEAKER_VOLC_ASR_LANGUAGE",
                             self.DEFAULT_LANGUAGE, str)
        self._workflow = cfg("asr.volc.workflow", "SPEAKER_VOLC_ASR_WORKFLOW",
                             self.DEFAULT_WORKFLOW, str)
        self._uid = cfg("asr.volc.uid", "SPEAKER_VOLC_ASR_UID", self.DEFAULT_UID, str)
        default_seg = (self.DEFAULT_SEG_MS_BIGMODEL if api == "bigmodel"
                       else self.DEFAULT_SEG_MS_COMMON)
        seg_raw = cfg("asr.volc.seg_ms", "SPEAKER_VOLC_ASR_SEG_MS", 0, int)
        # 0/None 都视为"未设置，按 api 选默认"
        self._seg_ms = seg_raw if (seg_raw and seg_raw > 0) else default_seg

    @property
    def provider(self) -> str:
        return f"volcengine/{self._api}"

    @property
    def ws_url(self) -> str:
        return self._ws_url

    def _local_paths_ok(self) -> bool:
        """云端引擎永远不依赖本地权重。保留方法以兼容 /api/health。"""
        return False

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        missing = [
            name
            for name, val in (
                ("SPEAKER_VOLC_ASR_APPID", self._appid),
                ("SPEAKER_VOLC_ASR_TOKEN", self._token),
            )
            if not val
        ]
        if missing:
            raise RuntimeError(
                "火山引擎 ASR 凭据未配置：请在 config 中填写 asr.volc.appid / asr.volc.token，"
                "或设置环境变量 " + " / ".join(missing)
                + "（并确认 ECHOPASS_CONFIG 指向含上述字段的 yaml）。"
            )

        if self._api == "bigmodel":
            from echopass.volc_bigmodel_asr import VolcBigmodelAsrClient

            self._client = VolcBigmodelAsrClient(
                app_key=self._appid,
                access_key=self._token,
                resource_id=self._resource_id,
                ws_url=self._ws_url,
                model_name=self._model_name,
                uid=self._uid,
                seg_duration_ms=self._seg_ms,
            )
        else:
            if not self._cluster:
                import logging
                logging.getLogger("echopass").warning(
                    "SPEAKER_VOLC_ASR_CLUSTER 未设置：将以空串发送，火山通用版会拒绝。"
                )
            from echopass.volc_asr import VolcAsrClient

            self._client = VolcAsrClient(
                appid=self._appid,
                token=self._token,
                cluster=self._cluster,
                ws_url=self._ws_url,
                uid=self._uid,
                language=self._language,
                workflow=self._workflow,
                seg_duration_ms=self._seg_ms,
            )
        # app.py 的 /api/health 和预加载会用 _model is not None 判断就绪
        self._model = self._client

    def reset_session(self, session_id: str) -> None:
        """每段 PCM = 独立 WS 会话，服务端无跨请求状态。保留接口兼容前端。"""
        del session_id  # noqa: ARG002

    def transcribe_chunk(
        self,
        pcm_16k: np.ndarray,
        session_id: str,
        is_final: bool = False,
        hotword: Optional[str] = None,
    ) -> str:
        """16kHz float32 单声道波形 → 带标点文本。

        `hotword` 为空格分隔的专有名词，透传给火山 request.hotword 字段；
        部分集群不识别该字段时会被静默忽略，不影响主流程。
        """
        del session_id, is_final  # 每段独立识别，无跨请求状态
        if pcm_16k is None or pcm_16k.size < 1600:
            return ""
        self._ensure_model()
        audio = np.asarray(pcm_16k, dtype=np.float32)
        # 加 lock 仅用于限制并发，避免单机同时压出太多 WS；不是保护 self._client 自身
        with self._lock:
            try:
                return self._client.transcribe_pcm16k(audio, hotword=hotword) or ""
            except Exception as e:  # noqa: BLE001
                import logging
                msg = str(e)
                # 下列情况都属于"本段无语音"的正常业务行为，按 DEBUG 静默丢弃，避免刷屏：
                #   - 通用版 code=1013       = No valid speeches found
                #   - 大模型版 code=20000003 / 45000002 = 静音 / 空音频
                #   - str(e) 为空            = Py3.11+ 下 asyncio.TimeoutError /
                #     concurrent.futures.TimeoutError __str__ 返回空串，通常是
                #     火山服务端对静音段既不返回结果也不发 is_last 导致的 recv 超时
                #     （volc_bigmodel_asr.receiver 已做一层容错，这里再兜一层）
                #   - ConnectionClosed / timed out 字样
                # 其他错误码仍走 WARNING 便于排障。
                low = msg.lower()
                is_silent = (
                    not msg
                    or "code=1013" in msg
                    or "code=20000003" in msg
                    or "code=45000002" in msg
                    or "connectionclosed" in low
                    or "timeout" in low
                )
                if is_silent:
                    logging.getLogger("echopass").debug(
                        "火山 ASR 本段无语音，静默丢弃（msg=%r）", msg, exc_info=False,
                    )
                else:
                    logging.getLogger("echopass").warning(
                        "火山 ASR 调用失败，本段返回空串：%s", msg,
                    )
                return ""


# ---------------------------------------------------------------------------
# LLM 纠错（可选，兼容 OpenAI /v1/chat/completions 格式）
# 支持：OpenAI、Qwen-API、通义、GLM、本地 vllm/ollama 等
# ---------------------------------------------------------------------------

_LLM_SYSTEM = (
    "你是一个专业的会议记录助手。"
    "以下是语音识别系统输出的原始文字，可能含有错别字、重复词、口头语、误识别。"
    "请在完整保留原意的前提下，将其润色为准确、通顺的会议记录文本。"
    "只返回修正后的文字，不要添加任何解释或前缀。"
    "如果原文实在无法理解，原样返回即可。"
)


class LLMCorrector:
    """调用 OpenAI-compatible chat API 对 ASR 原始文本做语义纠错。"""

    def __init__(self, api_url: str, api_key: str = "none", model: str = "qwen2.5-7b-instruct") -> None:
        self._url   = api_url.rstrip("/")
        self._key   = api_key
        self._model = model

    async def correct(self, text: str, context: str = "") -> str:
        import json
        import urllib.request

        user_msg = text
        if context:
            user_msg = f"（说话人：{context}）\n{text}"

        payload = json.dumps({
            "model": self._model,
            "messages": [
                {"role": "system", "content": _LLM_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            "temperature": 0.1,
            "max_tokens": 512,
        }).encode()

        req = urllib.request.Request(
            self._url if self._url.endswith("/chat/completions") else self._url + "/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._key}",
            },
            method="POST",
        )

        import asyncio
        loop = asyncio.get_event_loop()

        def _call():
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())

        data = await loop.run_in_executor(None, _call)
        corrected = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        return corrected or text


# ---------------------------------------------------------------------------
# KWS（关键词唤醒）引擎 —— 封装 CTC KWS 模型
# 模型：iic/speech_charctc_kws_phone-xiaoyun（FunASR）
# 用法：持续把麦克风环形缓冲里的音频送来，返回分数；分数 >= threshold 则唤醒
# ---------------------------------------------------------------------------

class KWSEngine:
    """FunASR CTC 关键词唤醒引擎，线程安全。"""

    def __init__(
        self,
        keywords: str = "小云小云",
        model_id: str = "iic/speech_charctc_kws_phone-xiaoyun",
        output_dir: str = "./outputs/kws",
        threshold: float = 0.75,
        device: Optional[str] = None,
        enabled: bool = False,
    ) -> None:
        self.keywords  = keywords
        self.model_id  = model_id
        self.output_dir = output_dir
        self.threshold = threshold
        self.enabled   = enabled
        self._device   = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self._model    = None
        self._lock     = threading.Lock()

    def _ensure_model(self) -> None:
        if not self.enabled:
            return
        if self._model is not None:
            return
        from funasr import AutoModel

        self._model = AutoModel(
            model=self.model_id,
            keywords=self.keywords,
            output_dir=self.output_dir,
            device=self._device,
            disable_update=True,
        )

    def _extract_score(self, result) -> Optional[float]:
        """从 FunASR KWS 返回值中解析最高唤醒分数。

        FunASR KWS 常见返回格式（不同版本略有差异）：
          - str:  "小云小云 0.95"  /  "detected 小云小云 0.95"  /  ""
          - list of dict:  [{'key': ..., 'value': '小云小云 0.95'}]
                           [{'key': ..., 'text': '0.95'}]
                           [{'keyword': '小云小云', 'score': 0.95}]
          - list of str:   ["小云小云 0.95"]
        """
        import logging
        logger = logging.getLogger(__name__)
        logger.debug("KWS raw result: %r", result)

        # 统一收集候选文本/数值
        candidates: List[str] = []

        def _collect(obj) -> None:
            if isinstance(obj, str):
                candidates.append(obj)
            elif isinstance(obj, (int, float)):
                candidates.append(str(obj))
            elif isinstance(obj, dict):
                # 直接数值字段
                for key in ("score", "confidence", "prob"):
                    if key in obj:
                        try:
                            candidates.append(str(float(obj[key])))
                        except (TypeError, ValueError):
                            pass
                # 文本字段
                for key in ("value", "text", "keyword", "kws", "result"):
                    if key in obj and isinstance(obj[key], str):
                        candidates.append(obj[key])
            elif isinstance(obj, (list, tuple)):
                for item in obj:
                    _collect(item)

        _collect(result)

        best: Optional[float] = None
        for cand in candidates:
            s = cand.strip()
            if not s:
                continue
            # 尝试直接解析为浮点数（某些格式只输出分数）
            try:
                v = float(s)
                if 0.0 <= v <= 1.0:
                    best = v if best is None else max(best, v)
                continue
            except ValueError:
                pass
            # 过滤明确拒绝的字符串
            low = s.lower()
            if "reject" in low:
                continue
            # 从字符串末尾提取浮点分数
            parts = s.split()
            for token in reversed(parts):
                try:
                    v = float(token)
                    if 0.0 <= v <= 1.0:
                        best = v if best is None else max(best, v)
                    break
                except ValueError:
                    continue
        return best

    def detect(self, pcm_16k: np.ndarray) -> Tuple[bool, Optional[float], object]:
        """
        输入 16kHz float32 单声道 PCM，返回 (triggered, score, raw_result)。
        triggered=True 表示分数 >= threshold，即唤醒成功。
        raw_result 为模型原始输出，用于调试。
        """
        if not self.enabled:
            return False, None, None
        if pcm_16k.size < 1600:
            return False, None, None
        self._ensure_model()
        import tempfile, soundfile as sf

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = f.name
        try:
            sf.write(tmp, pcm_16k.astype(np.float32), 16000)
            with self._lock:
                res = self._model.generate(input=tmp, cache={}, disable_pbar=True)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

        score = self._extract_score(res)
        triggered = score is not None and score >= self.threshold
        return triggered, score, res
