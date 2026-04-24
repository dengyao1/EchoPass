"""EchoPass · 实时语音会议助手后端包。

子模块概览：
- app            FastAPI 入口，HTTP / WebSocket 路由聚合。
- engine         CAM++ 声纹 + FunASR ASR/VAD/标点/KWS 推理封装。
- audio_features Kaldi 风格 fbank 特征。
- campplus_model CAM++ 最小推理网络。
- agent          LLM 对话与状态管理。
- meeting        纪要生成、转录缓冲。
- transport      WebSocket 事件总线与消息 schema。
- static         前端单页。
"""

__version__ = "0.1.0"
