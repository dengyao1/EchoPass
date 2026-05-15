# EchoPass 技术说明

## 1. 项目定位

**EchoPass** 是一个面向会议场景的实时语音会议助手，目标是把以下能力串成一个可直接运行的端到端系统：

- 声纹注册与说话人识别（CAM++，源自 3D-Speaker）
- 实时语音转写（火山引擎云端流式 ASR / openspeech v2 WebSocket，支持热词偏置）
- 可选的 ASR 文本 LLM 纠错
- 会议纪要自动生成（LLM 结构化输出 + 规则兜底）
- 唤醒词触发的语音助手（FunASR CTC-KWS，默认不启用，见 `kws.enabled`）
- 可选的 TTS 语音播报（火山双向流式 / OpenAI 兼容 HTTP）

后端核心代码位于 `echopass/`，前端单页位于 `echopass/static/`，脚本 `scripts/`，SQL 在 `sql/`。**想先跑起来**请直接看 [docs/LOCAL_QUICKSTART.md](docs/LOCAL_QUICKSTART.md)。

## 2. 功能清单

### 2.1 会议录音与实时转录

- 浏览器采集麦克风音频
- 前端用 RMS 阈值做简单 VAD 切句
- 每段语音发送到后端做：
  - 说话人识别
  - 火山引擎云端 ASR 转写
  - 可选 LLM 纠错
- 前端以“说话人气泡”形式实时展示

### 2.2 声纹注册与管理

- 支持录音注册声纹
- 支持上传音频文件注册声纹
- 支持查看已注册名单
- 支持删除已注册说话人
- 支持纯内存模式和 PostgreSQL 持久化模式

### 2.3 AI 会议纪要

- 从当前会话的转录缓存中提取上下文
- 优先调用 LLM 输出结构化纪要 JSON
- LLM 不可用时，回退到规则摘要
- 前端支持自动刷新和手动刷新
- 支持导出 Markdown

### 2.4 唤醒词助手

- 需 `kws.enabled: true`（或 `SPEAKER_KWS_ENABLED=1`）才加载本地 KWS；默认不加载、不下载唤醒词模型
- 启用后，前端可常驻监听「小云小云」
- 后端 KWS 引擎进行关键词检测
- 唤醒后录制一段语音并转写
- 调用 LLM 返回简洁口语化回复
- 可选调用 TTS 朗读回复

### 2.5 实时状态与控制

- 健康检查接口
- WebSocket 控制通道
- 会议转录查询接口
- 会话级事件广播

## 3. 目录结构

```text
EchoPass/
├── README.md                     # 项目总览与快速开始
├── TECHNICAL_OVERVIEW.md         # 本文档
├── LICENSE                       # MIT
├── NOTICE                        # 第三方代码归属（3D-Speaker 等）
├── docs/
│   ├── LOCAL_QUICKSTART.md       # 各平台最短启动（macOS / Linux / Windows）
│   └── assets/
│       └── echopass-ui-screenshot.png
├── config/
│   └── prod.yaml.example         # 去敏配置模板；本地复制为 prod.yaml
├── environment.yml               # 可选：手动 conda env create 时参考（含 openssl）
├── requirements.txt              # 运行依赖（精确版本锁定）
├── pyproject.toml                # 包元信息（可选，便于 pip install -e .）
├── scripts/
│   ├── first-run.sh              # macOS/Linux：首次装依赖并生成 config/prod.yaml
│   ├── first-run-windows.ps1     # Windows：首次装依赖
│   ├── first-run-windows.bat
│   ├── run.ps1                   # Windows 启动（与 run.sh 对齐）
│   ├── run.sh                    # macOS/Linux 启动脚本
│   └── gen_wake_ack_wavs.py      # 生成唤醒应答音效
├── sql/
│   ├── schema.sql                # PostgreSQL 建表
│   └── migrations/
│       └── 001_rename_speaker_demo_enrollments.sql
├── ssl/                          # 自签 HTTPS 证书
├── pretrained/                   # CAM++ 权重缓存
├── Dockerfile                    # Docker 镜像构建
└── echopass/                     # Python 包
    ├── __init__.py
    ├── app.py                    # FastAPI 应用，汇总配置、接口、全局单例
    ├── config.py                 # 统一配置入口（env > yaml > default）
    ├── engine.py                 # 声纹、ASR、LLM纠错、KWS 核心引擎
    ├── audio_features.py         # 最小化 FBank 特征提取
    ├── campplus_model.py         # 最小化 CAM++ 模型定义（源自 3D-Speaker）
    ├── volc_asr.py               # 火山 v2 通用流式 ASR 客户端
    ├── volc_bigmodel_asr.py      # 火山 v3 大模型 ASR 客户端（豆包 2.0）
    ├── volc_bidirectional_tts.py # 火山双向流式 TTS V3 客户端
    ├── agent/
    │   ├── dialogue_manager.py   # 唤醒会话 TTL 管理
    │   ├── llm_client.py         # OpenAI-compatible 对话客户端
    │   └── participants.py       # 会议参与者白名单（按 session）
    ├── meeting/
    │   ├── transcript_buffer.py  # 转录缓存与去重拼接
    │   ├── summarizer.py         # 会议纪要生成 + AI 章节划分
    │   └── cross_meeting.py      # 跨会议总结（预留实现）
    ├── session/
    │   └── manager.py            # 多会议会话注册表（TTL 自动清理）
    ├── transport/
    │   ├── websocket_server.py   # WebSocket 会话广播
    │   └── schemas.py            # 事件消息结构
    └── static/
        ├── index.html            # 单页前端界面（~5000 行）
        ├── js/
        │   ├── meeting-chapters.js  # 章节可视化
        │   └── ui-theme.js          # 主题切换
        └── audio/                # KWS 唤醒应答音效
```

## 4. 总体架构

```text
Browser
  ├─ 麦克风采集 / 前端 VAD
  ├─ 声纹注册 / 设置抽屉 / 气泡式转录
  ├─ 纪要展示 / Markdown 导出
  └─ FAB 唤醒助手 / TTS 播放
        │
        ▼
FastAPI (app.py)
  ├─ REST API
  ├─ WebSocket Hub
  ├─ TranscriptBuffer
  ├─ DialogueManager
  └─ MeetingSummarizer
        │
        ├─ CAM++ Speaker Engine
        ├─ Volcengine Cloud ASR (WebSocket)
        ├─ FunASR KWS
        ├─ OpenAI-compatible LLM
        ├─ Volcengine bidirectional streaming TTS（默认）
        └─ OpenAI-compatible HTTP TTS（可选）
```

## 5. 核心模块说明

### 5.1 `app.py`

职责：

- 读取 **`ECHOPASS_CONFIG` 指向的 YAML**（未设置时：若存在 `config/prod.yaml` 则读之，否则读 `config/prod.yaml.example`）与环境变量，创建全局单例
- 初始化 FastAPI、CORS、静态文件挂载
- 定义全部 REST / WebSocket 接口
- 负责把引擎能力组合成完整业务流程

主要全局对象：

- `engine`: `CamPlusSpeakerEngine` — 声纹注册与识别
- `asr_engine`: `StreamingASREngine` — 火山云端流式 ASR
- `llm_corrector`: `LLMCorrector` — ASR 文本 LLM 纠错
- `llm_chat`: `LLMChatClient` — LLM 对话客户端
- `kws_engine`: `KWSEngine` — 关键词唤醒
- `ws_hub`: `WebSocketHub` — WebSocket 广播
- `transcript_buffer`: `TranscriptBuffer` — 转录缓存
- `dialogue_manager`: `DialogueManager` — 唤醒对话态管理
- `meeting_summarizer`: `MeetingSummarizer` — 纪要/章节生成
- `cross_meeting_summarizer`: `CrossMeetingSummarizer` — 跨会议总结（预留）
- `session_manager`: `SessionManager` — 多会议会话生命周期
- `participants_registry`: `ParticipantsRegistry` — 参与者白名单

补充说明：

- 模块启动时会尝试关闭 `tqdm` 进度条，避免 FunASR 在服务日志里刷屏。
- 路由中大量使用 `session_id` 作为会话隔离键。
- 支持模型预加载（`on_event("startup")`），默认开启，可通过 `preload_models: false` 关闭。

### 5.2 `engine.py`

包含四类核心运行时引擎：

#### `CamPlusSpeakerEngine`

- 加载 CAM++ 预训练模型
- 支持从文件、上传文件、原始 PCM 提取 embedding
- 注册说话人 embedding
- 用余弦相似度识别说话人
- 支持 PostgreSQL 持久化

实现细节：

- 模型通过 ModelScope `snapshot_download()` 拉取
- embedding 做 L2 归一化
- 内部维护 `_gallery_names + _gallery_matrix`，用矩阵乘法加速识别

#### `StreamingASREngine`

- 封装火山引擎云端流式 ASR（openspeech v2 WebSocket）
- 凭据来自 **`asr.volc`（YAML）** 或等价环境变量（如 `SPEAKER_VOLC_ASR_APPID` / `_TOKEN` / `_CLUSTER`）；启动预加载时缺失即抛错（`asr.volc.api=common` 时需 `cluster`，**bigmodel** 可不配 cluster）
- 每次 `transcribe_chunk` = 一条独立 WS 会话（full client request → 若干 audio-only 分片 → 最后一片带 NEG_SEQUENCE → 收最终带标点文本）
- 协议和连接细节独立在 [`echopass/volc_asr.py`](echopass/volc_asr.py) 中，WS 通过独立事件循环线程驱动，避免干扰 FastAPI 主 loop

#### `LLMCorrector`

- 对 ASR 原文做二次语义修正
- 协议兼容 OpenAI `/chat/completions`
- 输入可附带说话人上下文

#### `KWSEngine`

- 封装 FunASR 关键词唤醒模型；`KWSEngine.enabled` 与配置 `kws.enabled` 一致，为 false 时不下载、不加载权重（默认）
- 输入 16k PCM
- 输出 `(triggered, score, raw_result)`
- 内部适配多种 FunASR 返回格式

### 5.3 `config.py`

统一配置入口，实现**环境变量 > YAML > 内置默认值**的三级优先级。

核心函数 `cfg(path, env, default, cast)`：
- `path`：YAML 中的点分路径（如 `"asr.volc.appid"`）
- `env`：对应的环境变量名（如 `"SPEAKER_VOLC_ASR_APPID"`）
- `default`：回退默认值
- `cast`：可选的类型转换函数（如 `int`、`float`、`to_bool`）

配置文件加载顺序：`ECHOPASS_CONFIG` 环境变量指定路径 → `config/prod.yaml`（若存在）→ `config/prod.yaml.example`。

### 5.4 `audio_features.py`

- 提供最小版 `FBank`
- 基于 `torchaudio.compliance.kaldi.fbank`
- 用于 CAM++ 前处理

### 5.5 `campplus_model.py`

- 提供最小版 CAM++ 模型结构
- 仅保留推理所需的网络定义（剥离 speakerlab 训练栈）
- 包含：
  - FCM 前端卷积模块
  - TDNN / CAMDenseTDNNBlock
  - StatsPool
  - 最终 embedding 投影层

### 5.6 `volc_asr.py`

火山引擎 v2 通用流式 ASR 客户端（`/api/v2/asr`）。

- 每次调用 = 一次独立 WebSocket 会话
- 协议：自定义二进制帧（full client request → audio-only 分片 → 末帧 NEG_SEQUENCE → 收文本）
- 独立事件循环线程，与 FastAPI 主 loop 解耦
- 面向 `StreamingASREngine` 的内部实现，业务代码不直接使用

### 5.7 `volc_bigmodel_asr.py`

火山引擎 v3 大模型 ASR 客户端（豆包流式 2.0，`/api/v3/sauc/bigmodel`）。

- 与 v2 主要差异：鉴权走 Headers（X-Api-App-Key 等），帧结构多 4 字节 seq，支持热词直传
- 大模型版边发边收，小分片（默认 200ms），适合实时语音
- 同样独立事件循环线程，同步接口 `transcribe_pcm16k()`

### 5.8 `volc_bidirectional_tts.py`

火山豆包双向流式 TTS V3 客户端。

- 提供两种接口：
  - `synthesize_pcm_bytes_sync()` — 同步整段合成（用于 `/api/tts`）
  - `BidirectionalTtsStreamSession` — 流式合成（用于 `/api/assistant/stream` 的 LLM+TTS 联动）
- 协议事件：StartSession → TaskRequest → FinishSession，下行收 PCM 分片
- 支持语速、音量调节

### 5.9 `agent/dialogue_manager.py`

- 管理唤醒后的短时“助手会话”
- 为每个 `session_id` 维护 TTL
- 支持：
  - `start`
  - `touch`
  - `stop`
  - `is_active`

用途：

- 限制唤醒后的助手有效时间
- 给前端和接口一个“当前是否处于助手态”的判断依据

### 5.10 `agent/participants.py`

会议参与者白名单管理器（按 `session_id`）。

- 声纹库全局共享，但可在每场会议设置「只识别这些人」
- 白名单为空时不过滤（默认行为）
- 支持设置/增量添加/删除/清空
- 在 `recognize_pcm` 等识别接口中打分后做二次过滤

### 5.11 `agent/llm_client.py`

- 最小 OpenAI-compatible 文本对话客户端
- 用于：
  - 唤醒助手回复
  - 会议纪要生成

### 5.12 `meeting/transcript_buffer.py`

- 以 `session_id` 为键保存转录记录
- 记录字段：
  - `speaker`
  - `text`
  - `text_raw`
  - `llm_corrected`
  - `created_at`

额外能力：

- 对连续同一说话人发言做重叠文本去重拼接
- 给纪要模块提供结构化上下文

### 5.13 `meeting/summarizer.py`

- 会议纪要生成器
- 优先走 LLM
- 失败时回退到规则摘要

统一输出结构（模块化报告式 JSON）：

- `title` / `summary` — 标题与导语
- `background` — 会议背景归纳（2~5 句）
- `modules[]` — 编号模块卡片（bullets/table/actions/callout 四种类型）
- `key_points` / `decisions` / `action_items` / `risks` — 兼容旧字段

章节划分（`chapters()`）：
- LLM 按话题变化切分，每章含标题、2-4 句摘要、起止时间、说话人列表
- 后处理自动合并时间重叠/过短微章
- LLM 不可用时回退规则切分（静默 gap > 90s 或 max > 3min）

### 5.14 `meeting/cross_meeting.py`

跨会议总结模块（预留实现）。当前为 stub 状态：

- 接受多场 `CrossMeetingRef`，返回与单场兼容的空壳 JSON
- `cross_meeting_meta.implementation = "stub"` 标记未接入 LLM
- 接入 LLM 后需实现的步骤已写在代码 TODO 中

### 5.15 `session/manager.py`

多会议会话注册表（进程内单例）。

- 按 `session_id` 持有 `MeetingSession`（标签、创建时间、活跃时间、参与者集合）
- TTL 自动清理长时间无心跳的会话（默认 4 小时）
- 与 transcript / chat history / dialogue 等按 session 分桶的存储协同
- `stop` 时由 app 层调用对应的 clear 清理关联数据

### 5.16 `transport/websocket_server.py`

- 管理 WebSocket 连接
- 以 `session_id` 分组广播
- 支持：
  - 指定会话广播
  - 向 `global` 会话附带广播
  - 发送失败连接自动清理

### 5.17 `transport/schemas.py`

- 统一 WebSocket 事件消息结构
- 格式：

```json
{
  "type": "event_name",
  "session_id": "default",
  "timestamp": "UTC ISO8601",
  "payload": {}
}
```

### 5.18 `static/index.html`

前端是一个单文件 SPA，包含四块主要能力：

- 顶栏：
  - 当前会议标题/时间
  - 显示原始 ASR 切换
  - 复制转录
  - 导出 Markdown
  - 打开设置抽屉
- 左栏：
  - 说话人气泡式实时转录
  - 当前识别中的临时气泡
- 右栏：
  - AI 实时纪要
  - 自动刷新 / 手动刷新
- 底栏：
  - 录音开始 / 暂停 / 停止
- 右下 FAB：
  - 小云小云唤醒
  - 用户问题 / 助手回复气泡
  - TTS 状态显示
- 设置抽屉：
  - 服务健康状态
  - 声纹阈值
  - 声纹注册与删除
  - KWS 阈值
  - VAD 参数

## 6. 核心业务流程

### 6.1 声纹注册流程

1. 前端录音或上传文件
2. 调用 `POST /api/enroll`
3. 后端提取 embedding
4. 写入内存 gallery
5. 若启用 PostgreSQL，则 upsert 到数据库
6. 前端刷新已注册说话人列表

### 6.2 实时转录流程

1. 前端点击开始录音
2. `sharedMic` 统一管理麦克风采集
3. 浏览器本地用 RMS + 静音时长做切句
4. 每个片段发送到 `POST /api/recognize_pcm`
5. 后端执行：
   - 说话人 embedding 提取
   - gallery 相似度匹配
   - 音频重采样到 16k
   - 火山云端 ASR 转写（WebSocket 单段会话）
   - 可选 LLM 纠错
   - 结果写入 `TranscriptBuffer`
   - 通过 WebSocket 发事件
6. 前端将结果渲染成转录气泡
7. 纪要模块按 debounce 自动刷新

### 6.3 会议纪要流程

1. 前端触发自动或手动刷新
2. 调用 `POST /api/meeting/summary`
3. 后端读取当前 `session_id` 的转录缓存
4. `MeetingSummarizer` 生成结构化纪要
5. 前端展示摘要、要点、决议、待办、风险

### 6.4 唤醒助手流程

1. 用户点击 FAB 开启监听
2. 前端维护 3 秒音频环形缓冲
3. 周期调用 `POST /api/kws`
4. 后端检测到唤醒词后：
   - 返回触发分数
   - 更新 `DialogueManager`
   - 广播唤醒事件
5. 前端进入“已唤醒，录音中”
6. 用户说完后，前端调用 `POST /api/recognize_pcm`
7. 识别出文本后调用 `POST /api/assistant/reply`
8. 后端结合会议上下文调用 LLM
9. 如启用 TTS，再调用 `POST /api/tts`
10. 前端展示并播报回复

## 7. 状态管理与会话模型

### 7.1 前端会话

- `liveSessionId`: 实时会议会话，命名形式 `sess_<timestamp>`
- `wakeSessionId`: 唤醒助手会话，命名形式 `wake_<timestamp>`
- `global`: WebSocket 控制通道默认会话

### 7.2 服务端会话

- `TranscriptBuffer`：按 `session_id` 缓存转录
- `DialogueManager`：按 `session_id` 管理助手 TTL
- `WebSocketHub`：按 `session_id` 管理连接组

### 7.3 持久化范围

- 声纹注册：可选 PostgreSQL 持久化
- 会议转录：仅内存缓存
- 会议纪要：按需生成，不单独落库
- 唤醒会话：仅内存 TTL

## 8. API 说明

### 8.1 REST API

| 方法 | 路径 | 关键参数 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/api/health` | — | 服务健康状态、模型加载状态、已注册人数、配置摘要 |
| `GET` | `/api/speakers` | — | 已注册说话人列表 |
| `POST` | `/api/enroll` | `name`(form), `audio`(file) | 注册说话人声纹 |
| `DELETE` | `/api/speakers/{name}` | — | 删除已注册说话人 |
| `POST` | `/api/identify_file` | `audio`(file), `threshold?`, `session_id?` | 上传文件识别说话人 |
| `POST` | `/api/identify_pcm` | `sample_rate`, `threshold?`, `session_id?` | 原始 float32 PCM 识别说话人 |
| `POST` | `/api/recognize_pcm` | `sample_rate`, `session_id`, `is_final?`, `threshold?`, `hotword?`, `offset_ms?` | **核心** — 说话人识别 + ASR 转写 |
| `POST` | `/api/asr_reset` | `session_id?` | 重置 ASR 会话和转录缓存 |
| `POST` | `/api/kws` | `sample_rate`, `session_id?` | 唤醒词检测（需 `kws.enabled: true`） |
| `POST` | `/api/tts` | `{"text","voice?","session_id?","provider?"}` | 文本转语音（OpenAI 兼容 / 火山双向流式） |
| `POST` | `/api/assistant/reply` | `{"text","session_id?","speaker?","use_tts?","meeting_session_id?"}` | 会中助手问答（非流式） |
| `POST` | `/api/assistant/stream` | 同上 | 会中助手流式 SSE（LLM 流式 + TTS 联动） |
| `POST` | `/api/meeting/summary` | `{"session_id?","title?"}` | 生成模块化会议纪要 JSON |
| `POST` | `/api/meeting/summary/cross` | `{"meetings":[...],"title?","focus?"}` | 跨会议总结（预留） |
| `POST` | `/api/meeting/chapters` | `{"session_id?"}` | 生成 AI 章节列表 |
| `GET` | `/api/meeting/transcript` | `session_id?` | 获取当前会话转录明细 |
| `POST` | `/api/meeting/export` | `audio`(file), `transcript_json`(form), `summary_json`(form), `title?`(form), `session_id?`(form) | 导出 ZIP（音频 + 转录 + 纪要 Markdown） |
| `GET` | `/api/meeting/participants` | `session_id` | 获取会议参与者白名单 |
| `POST` | `/api/meeting/participants` | `{"session_id","names":[...]}` | 设置参与者白名单 |
| `GET` | `/api/meeting/sessions` | — | 列出所有活跃会议会话 |
| `POST` | `/api/meeting/sessions/start` | `{"session_id","label?"}` | 启动会议会话 |
| `POST` | `/api/meeting/sessions/stop` | `{"session_id"}` | 停止会议会话（清理转录/白名单/对话态） |

### 8.2 WebSocket

路径：

- `/ws/control?session_id=<sid>`

前端可发送命令：

- `ping`
- `assistant_stop`
- `meeting_summary_requested`

服务端常见事件：

- `ws_connected`
- `pong`
- `audio_chunk_received`
- `asr_interim`
- `asr_final`
- `wakeword_detected`
- `assistant_session_started`
- `assistant_session_stopped`
- `llm_response_ready`
- `tts_started`
- `tts_finished`
- `meeting_summary_requested`
- `meeting_summary_ready`

## 9. 配置项

运行时以 **`ECHOPASS_CONFIG` 指向的 YAML**（未设置时：有 `config/prod.yaml` 则用之，否则 `config/prod.yaml.example`）为主；下表为代码中使用的**环境变量名**，多数在 YAML 中有等价键（如 `asr.volc.appid`）。合并规则：**环境变量覆盖 YAML**。

### 9.1 声纹识别

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SPEAKER_DEMO_THRESHOLD` | `0.45` | 声纹余弦相似度阈值 |
| `SPEAKER_DEMO_MODEL_ID` | `iic/speech_campplus_sv_zh-cn_16k-common` | CAM++ 模型 ID |
| `SPEAKER_DEMO_PG_DSN` | `""`（默认配置模板中为空） | 为空字符串时只用内存，不做声纹持久化 |

### 9.2 ASR / KWS / 会话

| 环境变量 | YAML 路径 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `SPEAKER_VOLC_ASR_APPID` | `asr.volc.appid` | `""` | 火山 ASR AppID（**必配**） |
| `SPEAKER_VOLC_ASR_TOKEN` | `asr.volc.token` | `""` | 火山 ASR Access Token（**必配**） |
| `SPEAKER_VOLC_ASR_API` | `asr.volc.api` | `bigmodel` | ASR 版本：`bigmodel`（豆包2.0）或 `common`（通用版） |
| `SPEAKER_VOLC_ASR_CLUSTER` | `asr.volc.cluster` | `volcengine_streaming_common` | 仅 common 版需要 |
| `SPEAKER_VOLC_ASR_WS_URL` | `asr.volc.ws_url` | 按 api 自动选 | WebSocket 地址 |
| `SPEAKER_VOLC_ASR_RESOURCE_ID` | `asr.volc.resource_id` | `volc.bigasr.sauc.duration` | 仅 bigmodel 版使用 |
| `SPEAKER_VOLC_ASR_MODEL_NAME` | `asr.volc.model_name` | `bigmodel` | 仅 bigmodel 版使用 |
| `SPEAKER_VOLC_ASR_LANGUAGE` | `asr.volc.language` | `zh-CN` | 识别语言 |
| `SPEAKER_VOLC_ASR_WORKFLOW` | `asr.volc.workflow` | `audio_in,resample,...` | 引擎 workflow 串（仅 common） |
| `SPEAKER_VOLC_ASR_UID` | `asr.volc.uid` | `echopass` | 请求 uid |
| `SPEAKER_VOLC_ASR_SEG_MS` | `asr.volc.seg_ms` | 200(bigmodel) / 15000(common) | 单片最大毫秒数 |
| `SPEAKER_ASR_MAX_CONCURRENT` | `asr.max_concurrent` | `32` | 进程内并发 ASR 连接上限（0=不限制） |
| `SPEAKER_ASR_HOTWORD` | `asr.hotword` | `""` | 全局 ASR 热词（空格分隔），前端 query 参数优先级更高 |
| `SPEAKER_FUNASR_BASE` | `asr.funasr_base` | `<repo>/pretrained/funasr` | KWS 本地权重目录 |
| `SPEAKER_SESSION_TTL_SEC` | `session.ttl_sec` | `14400`（4h） | 会议会话 TTL |
| `SPEAKER_CHAPTER_PROMPT_LINES` | `meeting.chapters.prompt_max_lines` | `120` | 章节 LLM prompt 最大转写条数 |
| `SPEAKER_CROSS_SUMMARY_MAX_MEETINGS` | `meeting.cross_summary.max_meetings` | `20` | 跨会议总结单次最大会议数 |
| `SPEAKER_PRELOAD_MODELS` | `preload_models` | `true` | 启动时是否预加载模型 |

### 9.3 LLM

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SPEAKER_LLM_API_URL` | `""` | 任意 OpenAI 兼容 `v1/chat/completions` 基地址 |
| `SPEAKER_LLM_API_KEY` | `""` | LLM API Key；未配时纪要和章节走规则回退、助手可能失败 |
| `SPEAKER_LLM_MODEL` | `""` | 模型名，如 `qwen-plus`、`deepseek-chat` |
| `SPEAKER_ASR_LLM_CORRECTION` | `0` | 是否启用 ASR 文本纠错 |
| `SPEAKER_MEETING_CTX_ITEMS` | `20` | 助手附带的最近会议发言条数 |
| `SPEAKER_MEETING_CTX_CHARS` | `1500` | 助手附带的最近会议发言最大字符数 |

### 9.4 唤醒词助手

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SPEAKER_KWS_ENABLED` | `0` | `1` / `true` 时启用本地 CTC 唤醒与预加载；未设置时以 yaml 的 `kws.enabled` 为准，**代码层默认**为关闭 |
| `SPEAKER_KWS_KEYWORDS` | `小云小云` | 唤醒词（仅当 KWS 启用时生效） |
| `SPEAKER_KWS_THRESHOLD` | `0.75` | 唤醒阈值 |
| `SPEAKER_ASSISTANT_TTL_SEC` | `25` | 唤醒后对话态 TTL |

### 9.5 TTS

| 环境变量 | YAML 路径 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `SPEAKER_TTS_PROVIDER` | `tts.provider` | `volc_bidirection` | `openai` 或 `volc_bidirection` |
| `SPEAKER_TTS_URL` | `tts.url` | `""` | HTTP 类 TTS 的 base URL（openai 模式必配） |
| `SPEAKER_TTS_API_KEY` | `tts.api_key` | `none` | TTS API Key（openai 模式） |
| `SPEAKER_TTS_VOICE` | `tts.voice` | `zh_female_vv_uranus_bigtts` | 兜底音色（请求体 voice 优先） |
| `SPEAKER_TTS_MODEL` | `tts.model` | `tts-1` | TTS 模型（仅 openai 模式） |
| `SPEAKER_TTS_PCM_SAMPLE_RATE` | `tts.pcm.sample_rate` | `24000` | PCM 转 WAV 采样率 |
| `SPEAKER_TTS_PCM_CHANNELS` | `tts.pcm.channels` | `1` | PCM 声道数 |
| `SPEAKER_TTS_PCM_SAMPLE_WIDTH` | `tts.pcm.sample_width` | `2` | PCM 采样字节宽度 |
| `SPEAKER_TTS_VOLC_WS_URL` | `tts.volc.ws_url` | `wss://openspeech.bytedance.com/api/v3/tts/bidirection` | 火山 TTS WebSocket 地址 |
| `SPEAKER_TTS_VOLC_APPID` | `tts.volc.appid` | 沿用 `asr.volc.appid` | 火山 TTS AppID |
| `SPEAKER_TTS_VOLC_ACCESS_KEY` | `tts.volc.access_key` | 沿用 `asr.volc.token` | 火山 TTS Access Key |
| `SPEAKER_TTS_VOLC_RESOURCE_ID` | `tts.volc.resource_id` | `seed-tts-2.0` | 资源 ID（2.0 音色；1.0 公版用 `volc.service_type.10029`） |
| `SPEAKER_TTS_VOLC_SPEAKER` | `tts.volc.speaker` | `zh_female_vv_uranus_bigtts` | 豆包音色 ID（控制台复制） |
| `SPEAKER_TTS_VOLC_SPEECH_RATE` | `tts.volc.speech_rate` | `0` | 语速 -50~100 |
| `SPEAKER_TTS_VOLC_LOUDNESS_RATE` | `tts.volc.loudness_rate` | `0` | 音量 -50~100 |

### 9.6 会中助手

| 环境变量 | YAML 路径 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `SPEAKER_ASSISTANT_TTL_SEC` | `assistant.ttl_sec` | `25` | 唤醒后对话态 TTL（秒） |
| `SPEAKER_ASSISTANT_CHAT_TURNS` | `assistant.chat_history_turns` | `8` | 多轮对话保留轮数（0=关闭记忆） |
| `SPEAKER_WAKE_ACK_AUDIO` | `assistant.wake_ack_audio` | `""` | 唤醒应答音效路径（相对 static/） |
| `SPEAKER_MEETING_CTX_ITEMS` | `assistant.meeting_ctx.max_items` | `20` | 助手附带的最近会议发言条数 |
| `SPEAKER_MEETING_CTX_CHARS` | `assistant.meeting_ctx.max_chars` | `1500` | 助手附带的最近会议发言最大字符数 |

## 10. 依赖说明

`requirements.txt` 中的依赖可分为几组：

- Web 服务：
  - `fastapi`
  - `uvicorn[standard]`
  - `python-multipart`
- 数值与音频：
  - `numpy`
  - `torch`
  - `torchaudio`
  - `soundfile`
- 模型与生态：
  - `modelscope`
  - `funasr`
- 可选持久化（配置了 `speaker.pg_dsn` 时再装，不在默认 `requirements.txt` 中）：
  - `psycopg2-binary`（`pip install "psycopg2-binary==2.9.10"` 或 `pip install -e ".[postgres]"`）

## 11. 启动与部署

### 11.1 本地启动

**各平台逐步说明以 [docs/LOCAL_QUICKSTART.md](docs/LOCAL_QUICKSTART.md) 为准**；此处为摘要。

**macOS（推荐）**：Python **3.8** 与 `requirements.txt` 锁定一致，建议 Miniconda/Anaconda。

```bash
conda create -n echopass python=3.8 -y
conda activate echopass
cd /path/to/ECHOPASS
./scripts/first-run.sh              # macOS/Linux：装依赖、可选复制 prod.yaml、固定 modelscope
# 编辑 config/prod.yaml（火山 ASR、LLM 等）
export ECHOPASS_CONFIG=config/prod.yaml
FORCE_ONLINE=1 ./scripts/run.sh     # 首次拉 CAM++（及 kws.enabled 时 KWS）等权重需联网
./scripts/run.sh                    # 之后日常
```

**Linux**：可用系统 **Python 3.8** 建 venv（勿用 3.12+ 强装本仓库锁定版本）。

```bash
cd /path/to/ECHOPASS
python3.8 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
test -f config/prod.yaml || cp config/prod.yaml.example config/prod.yaml
export ECHOPASS_CONFIG=config/prod.yaml
FORCE_ONLINE=1 ./scripts/run.sh     # 首次拉 CAM++（及 kws.enabled 时 KWS）
./scripts/run.sh
```

**Windows**：与 macOS 相同流程——先 `conda create -n echopass python=3.8 -y` 并 `conda activate echopass`，在项目根执行 `.\scripts\first-run-windows.ps1` 安装依赖（可选复制 `config\prod.yaml`），编辑配置后设 `$env:ECHOPASS_CONFIG="config/prod.yaml"`，首次拉模型用 `$env:FORCE_ONLINE="1"; .\scripts\run.ps1`，日常 `.\scripts\run.ps1`。`run.ps1` 与 `run.sh` 一样只启动 uvicorn，不创建 conda、不读 `environment.yml`。

默认监听：

- `0.0.0.0:8765`
- 若已有证书或可自动生成自签证书，则优先 `https://127.0.0.1:8765`
- 否则回退到 `http://127.0.0.1:8765`

### 11.2 HTTPS

前端浏览器跨设备访问麦克风时，通常需要 HTTPS：

```bash
export SSL_KEYFILE=/path/to/key.pem
export SSL_CERTFILE=/path/to/cert.pem
./scripts/run.sh
```

## 12. 数据库

数据库只用于声纹注册持久化。

表结构：

- `speaker_name`
- `model_id`
- `embedding_dim`
- `embedding`
- `created_at`
- `updated_at`

建表（在仓库根目录执行，路径以本仓库为准）：

```bash
psql -U <user> -d <db> -f sql/schema.sql
```

## 13. 当前实现特点与边界

### 13.1 优点

- 依赖集中，最小可运行
- 前后端耦合清晰，适合快速演示
- 支持说话人识别、纪要、助手、TTS 的完整闭环
- 代码结构扁平，便于二次开发

### 13.2 当前边界

- 无用户认证、无权限隔离
- 大量配置需自行填写（`config/prod.yaml` 或环境变量），无内置云凭据
- 会议转录和助手态仅保存在内存
- 前端 VAD 是启发式 RMS 切句，不是严格的生产级语音活动检测
- 公网/多租户部署时应对 API 与静态资源加认证与限流

## 14. 建议的后续演进方向

- 给 ASR 增加更严格的 no-speech 过滤
- 给转录和纪要增加持久化存储
- 增加用户/会议维度的多租户隔离
- 将前端脚本从单文件 `index.html` 拆为模块化工程
- 为主要 API 和引擎增加自动化测试
