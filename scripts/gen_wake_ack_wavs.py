#!/usr/bin/env python3
"""使用火山双向 TTS 生成唤醒预置语 WAV（读 ECHOPASS_CONFIG 指向的 yaml，未设置则读 config/prod.yaml.example）。

输出到 echopass/static/audio/：
  wake_ack_01_zai_de_qing_shuo.wav  — 在的，请说。
  wake_ack_02_wo_zai.wav            — 我在，需要我做什么？
  wake_ack_03_qing_jiang.wav        — 请讲，我帮你记。

用法：
  cd /path/to/ECHOPASS
  python3 scripts/gen_wake_ack_wavs.py
"""
from __future__ import annotations

import io
import sys
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from echopass.config import cfg  # noqa: E402
from echopass.volc_bidirectional_tts import synthesize_pcm_bytes_sync  # noqa: E402


def _volc_params() -> dict:
    appid = (cfg("tts.volc.appid", "SPEAKER_TTS_VOLC_APPID", "", str) or "").strip()
    if not appid:
        appid = (cfg("asr.volc.appid", "SPEAKER_VOLC_ASR_APPID", "", str) or "").strip()
    access = (cfg("tts.volc.access_key", "SPEAKER_TTS_VOLC_ACCESS_KEY", "", str) or "").strip()
    if not access:
        access = (cfg("asr.volc.token", "SPEAKER_VOLC_ASR_TOKEN", "", str) or "").strip()
    speaker = (cfg("tts.volc.speaker", "SPEAKER_TTS_VOLC_SPEAKER", "", str) or "").strip()
    return {
        "app_key": appid,
        "access_key": access,
        "resource_id": cfg(
            "tts.volc.resource_id", "SPEAKER_TTS_VOLC_RESOURCE_ID", "seed-tts-2.0", str,
        ),
        "ws_url": cfg(
            "tts.volc.ws_url",
            "SPEAKER_TTS_VOLC_WS_URL",
            "wss://openspeech.bytedance.com/api/v3/tts/bidirection",
            str,
        ),
        "speaker": speaker,
        "sample_rate": cfg("tts.pcm.sample_rate", "SPEAKER_TTS_PCM_SAMPLE_RATE", 24000, int),
        "channels": cfg("tts.pcm.channels", "SPEAKER_TTS_PCM_CHANNELS", 1, int),
        "sample_width": cfg("tts.pcm.sample_width", "SPEAKER_TTS_PCM_SAMPLE_WIDTH", 2, int),
        "speech_rate": cfg("tts.volc.speech_rate", "SPEAKER_TTS_VOLC_SPEECH_RATE", 0, int),
        "loudness_rate": cfg("tts.volc.loudness_rate", "SPEAKER_TTS_VOLC_LOUDNESS_RATE", 0, int),
    }


def _pcm_to_wav(pcm: bytes, *, channels: int, sample_width: int, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def main() -> int:
    p = _volc_params()
    if not p["app_key"] or not p["access_key"]:
        print("错误：未配置火山 appid/token（tts.volc 或 asr.volc）", file=sys.stderr)
        return 1
    if not p["speaker"]:
        print("错误：未配置 tts.volc.speaker（音色 ID）", file=sys.stderr)
        return 1

    out_dir = REPO_ROOT / "echopass" / "static" / "audio"
    out_dir.mkdir(parents=True, exist_ok=True)

    phrases = [
        ("wake_ack_01_zai_de_qing_shuo", "在的，请说。"),
        ("wake_ack_02_wo_zai", "我在，需要我做什么？"),
        ("wake_ack_03_qing_jiang", "请讲，我帮你记。"),
    ]

    for stem, text in phrases:
        print("合成：", text)
        pcm = synthesize_pcm_bytes_sync(
            text=text,
            app_key=p["app_key"],
            access_key=p["access_key"],
            resource_id=p["resource_id"],
            ws_url=p["ws_url"],
            speaker=p["speaker"],
            sample_rate=p["sample_rate"],
            speech_rate=p["speech_rate"],
            loudness_rate=p["loudness_rate"],
        )
        if not pcm:
            print("  跳过：返回空音频", file=sys.stderr)
            continue
        wav = _pcm_to_wav(
            pcm,
            channels=p["channels"],
            sample_width=p["sample_width"],
            sample_rate=p["sample_rate"],
        )
        path = out_dir / f"{stem}.wav"
        path.write_bytes(wav)
        print("  ->", path, f"({len(wav)} bytes)")

    print(
        "\n在 config 中任选一条，例如：\n"
        '  assistant:\n'
        '    wake_ack_audio: "audio/wake_ack_01_zai_de_qing_shuo.wav"',
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
