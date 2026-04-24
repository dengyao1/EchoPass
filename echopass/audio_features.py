# EchoPass · 最小化 Kaldi 风格 fbank 提取器，原始实现来自 3D-Speaker（Apache-2.0）。

from __future__ import annotations

import torch
import torchaudio.compliance.kaldi as kaldi


class FBank:
    """Kaldi 风格 fbank 提取器，为 CAM++ 声纹模型提供输入特征。"""

    def __init__(self, n_mels: int, sample_rate: int, mean_nor: bool = False) -> None:
        self.n_mels = int(n_mels)
        self.sample_rate = int(sample_rate)
        self.mean_nor = bool(mean_nor)

    def __call__(self, wav: torch.Tensor, dither: float = 0.0) -> torch.Tensor:
        if wav.dim() == 1:
            wav = wav.unsqueeze(0)
        if wav.shape[0] > 1:
            wav = wav[:1, :]
        feat = kaldi.fbank(
            wav,
            num_mel_bins=self.n_mels,
            sample_frequency=self.sample_rate,
            dither=dither,
        )
        if self.mean_nor:
            feat = feat - feat.mean(0, keepdim=True)
        return feat
