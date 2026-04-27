# EchoPass

**会议语音助手**（录音 → 转写 / 说话人 / 纪要 / 唤醒问答）。**先跑起来只读 [docs/LOCAL_QUICKSTART.md](docs/LOCAL_QUICKSTART.md)**。仓库不自带云密钥。细节见 [TECHNICAL_OVERVIEW.md](TECHNICAL_OVERVIEW.md)。

![EchoPass 界面预览](docs/assets/echopass-ui-screenshot.png)

## 能做什么

实时转写（火山 ASR）、说话人（CAM++，声纹默认内存）、纪要/章节（LLM，可回退规则）、唤醒词（FunASR KWS，默认**关闭**，`kws.enabled: true` 后支持「小云小云」）、TTS、导出 ZIP。架构如下。

## 架构概览

```mermaid
flowchart LR
    A["Browser<br/>WebAudio + AudioWorklet"] --> B["/api/recognize_pcm"]
    A --> C["/api/kws"]
    B --> D["CAM++ Speaker Engine"]
    B --> E["Volcengine Streaming ASR"]
    E --> F["TranscriptBuffer"]
    F --> G["MeetingSummarizer"]
    F --> H["Assistant Context Builder"]
    C --> I["DialogueManager"]
    H --> J["LLMChatClient"]
    I --> J
    J --> K["/api/assistant/reply or /api/assistant/stream"]
    K --> L["TTS Backend"]
    F --> M["/api/meeting/export"]
    N["/ws/control"] --> A
```

## 仓库结构

```text
<仓库根>/
├── README.md
├── TECHNICAL_OVERVIEW.md
├── Dockerfile
├── environment.yml               # 可选：手动 conda env 时参考
├── requirements.txt
├── pyproject.toml
├── docs/
│   └── LOCAL_QUICKSTART.md
├── config/
│   └── prod.yaml.example          # 模板；本地复制为 prod.yaml 并勿提交密钥
├── scripts/
│   ├── first-run-mac.sh           # macOS：conda activate 后首次装依赖
│   ├── first-run-windows.ps1      # Windows：同上
│   ├── first-run-windows.bat
│   ├── run.ps1                   # Windows 启动（与 run.sh 对齐）
│   ├── run.sh
│   └── gen_wake_ack_wavs.py
├── sql/
│   ├── schema.sql
│   └── migrations/
├── echopass/                      # Python 应用包
│   ├── app.py
│   ├── config.py
│   ├── engine.py
│   ├── volc_asr.py
│   ├── volc_bigmodel_asr.py
│   ├── volc_bidirectional_tts.py
│   ├── meeting/
│   ├── agent/
│   ├── transport/
│   └── static/
└── NOTICE
```

## 运行环境

运行时需要 Python **3.8**（与锁定依赖一致）。默认**不**需要数据库。**macOS**：`conda create -n echopass python=3.8` → `conda activate echopass` → `./scripts/first-run-mac.sh` → 配置 `config/prod.yaml` → `./scripts/run.sh`（已存在 `config/prod.yaml` 时会自动使用；亦可用 `export ECHOPASS_CONFIG=...` 指定路径）。**Windows**：`conda create` / `activate` 同上 → `.\scripts\first-run-windows.ps1` → 配置 `config\prod.yaml` → `.\scripts\run.ps1`（与 `run.sh` 一样；有 `config\prod.yaml` 时自动使用，亦可 `$env:ECHOPASS_CONFIG="config\prod.yaml"` 显式指定）。详见 [docs/LOCAL_QUICKSTART.md](docs/LOCAL_QUICKSTART.md)。**Linux** 可用 venv + `scripts/run.sh`。

## 快速开始

**步骤以 [docs/LOCAL_QUICKSTART.md](docs/LOCAL_QUICKSTART.md) 为准**，此处仅作补充。

- 若 `modelscope` / `funasr` 版本冲突：`pip install --force-reinstall --no-deps modelscope==1.10.0 funasr==1.3.1`
- **首次**从 ModelScope 等拉取 **CAM++** 权重要联网。若还启用了 `kws.enabled: true`，会额外拉取 **KWS** 权重；未启用 KWS 时不会下载唤醒词模型。在已 `export ECHOPASS_CONFIG=config/prod.yaml` 的前提下执行 `FORCE_ONLINE=1 ./scripts/run.sh`（Windows：`$env:FORCE_ONLINE=1; .\scripts\run.ps1`）。日常启动见各平台小节。
- 配置优先级：`环境变量` > `ECHOPASS_CONFIG` 指向的 yaml > 自动 `config/prod.yaml`（若存在）> **`config/prod.yaml.example`**。必配：**火山 `asr.volc.appid` + `token`**、**`llm.api_url` + `api_key` + `model`**；`asr.volc.api=common` 时还要 `cluster`。可选：TTS、`speaker.pg_dsn`（声纹落库）等，见 [config/prod.yaml.example](config/prod.yaml.example)。本地复制为 `config/prod.yaml` 后一般**无需**再 `export ECHOPASS_CONFIG`；仍可用其指向其他路径。
- 声纹默认在**内存**；要跨重启保留再配 PostgreSQL（`sql/schema.sql` + `pg_dsn`）。
- **Windows** 与 **macOS** 一致：先用 **`first-run-windows.ps1` / `first-run-mac.sh`** 在已 `conda activate` 的 Python 3.8 环境里装依赖；日常只用 **`run.ps1` / `run.sh`** 启动。可选：`environment.yml` 仅供你手动 `conda env create -f` 时参考（非必选）。

## 使用流程

注册说话人 → 点录音 → 看转写/纪要/章节；在 `kws.enabled: true` 时可通过「小云小云」唤醒助手；结束可导出 ZIP。

## 关键特性说明

### 1. 说话人识别

- 基于 CAM++ 模型
- 支持音频文件注册和上传注册
- 相同姓名重复注册时会做 embedding 融合
- 识别时基于余弦相似度与阈值判断

相关接口：

- `GET /api/speakers`
- `POST /api/enroll`
- `POST /api/identify_file`
- `POST /api/identify_pcm`
- `DELETE /api/speakers/{name}`

### 2. 实时 ASR

- 当前实现为火山引擎云端流式 ASR
- 支持 `bigmodel` 与 `common` 两种后端模式
- 浏览器上传的是 `float32 PCM`
- 服务端会重采样到 `16kHz` 后送 ASR
- 支持热词透传

相关接口：

- `POST /api/recognize_pcm`
- `POST /api/asr_reset`

### 3. 会议纪要与 AI 章节

- 实时转录会写入 `TranscriptBuffer`
- `/api/meeting/summary` 生成模块化纪要
- `/api/meeting/chapters` 按话题切分章节
- LLM 不可用时，两者都有规则回退路径

相关接口：

- `POST /api/meeting/summary`
- `GET /api/meeting/transcript`
- `POST /api/meeting/chapters`
- `POST /api/meeting/export`

### 4. 唤醒词助手

- 需先在配置中设 `kws.enabled: true`（或 `SPEAKER_KWS_ENABLED=1`）才会加载本地 KWS 并支持「小云小云」；默认不启用
- 默认唤醒词是 `小云小云`（在启用 KWS 时）
- 唤醒成功后进入短时对话会话
- 助手回答会自动结合最近会议上下文
- 支持普通文本回答，也支持流式回答 + 流式 TTS

相关接口：

- `POST /api/kws`
- `POST /api/assistant/reply`
- `POST /api/assistant/stream`

### 5. TTS

当前支持两类出口：

- 火山双向流式 TTS（`tts.provider=volc_bidirection`）
- OpenAI 兼容 HTTP TTS（`tts.provider=openai`，`POST .../v1/audio/speech`）

统一入口：

- `POST /api/tts`

如果你要生成唤醒提示音，可以直接运行：

```bash
python scripts/gen_wake_ack_wavs.py
```

生成的文件会写到：

```bash
echopass/static/audio/
```

然后在配置里指定：

```yaml
assistant:
  wake_ack_audio: "audio/wake_ack_01_zai_de_qing_shuo.wav"
```

## WebSocket 事件通道

前端会连接：

```text
/ws/control?session_id=global
```

当前主要用于：

- 状态广播
- 纪要刷新通知
- 助手会话开始/结束
- 唤醒词事件
- TTS 开始/结束

如果你要接自己的前端，可以优先参考：

- `echopass/transport/websocket_server.py`
- `echopass/transport/schemas.py`

## 重要配置项

下面这张表是最常用的一批配置。完整配置结构见 [config/prod.yaml.example](config/prod.yaml.example)。

| 配置项 | 说明 |
| --- | --- |
| `speaker.threshold` | 说话人识别阈值 |
| `speaker.model_id` | CAM++ 模型 ID |
| `speaker.pg_dsn` | 留空=仅内存；填 DSN 则声纹落 PostgreSQL |
| `preload_models` | 启动时是否预加载 CAM++ / ASR；KWS 仅在 `kws.enabled: true` 时一并预加载 |
| `kws.enabled` | 默认 `false`；`true` 时启用「小云小云」本地唤醒并会下载/加载 CTC KWS 模型 |
| `llm.api_url` / `llm.api_key` / `llm.model` | 纪要、章节、助手对话统一使用的 LLM |
| `llm.asr_correction` | 是否对 ASR 文本再做一轮 LLM 纠错 |
| `asr.hotword` | 全局热词；前端参数可覆盖 |
| `asr.funasr_base` | 历史兼容字段；当前云端 ASR 不依赖本地权重 |
| `asr.volc.api` | `bigmodel` 或 `common` |
| `asr.volc.appid` / `token` | 火山 ASR 凭据 |
| `kws.keywords` / `kws.threshold` | 唤醒词和阈值 |
| `assistant.ttl_sec` | 唤醒后助手态持续时间 |
| `assistant.meeting_ctx.max_items` | 带给助手的最近会议发言条数 |
| `tts.provider` | `volc_bidirection`（火山双向）或 `openai`（需配 `tts.url`） |
| `tts.url` | HTTP TTS 地址 |
| `tts.volc.*` | 火山双向流式 TTS 参数 |

常用环境变量示例：

```bash
export ECHOPASS_CONFIG=config/prod.yaml
# export SPEAKER_KWS_ENABLED=1   # 需要「小云小云」本地唤醒时再开（或 yaml 里 kws.enabled: true）
export SPEAKER_PRELOAD_MODELS=1
export SPEAKER_DEMO_PG_DSN='postgresql://user:password@host:5432/dbname'
export SPEAKER_VOLC_ASR_APPID='your-appid'
export SPEAKER_VOLC_ASR_TOKEN='your-token'
export SPEAKER_LLM_API_URL='https://your-llm/v1/chat/completions'
export SPEAKER_LLM_API_KEY='your-api-key'
export SPEAKER_LLM_MODEL='qwen-plus'
export SPEAKER_TTS_PROVIDER='volc_bidirection'
```

## Docker 部署

### 构建

```bash
docker build -t echopass:1.0 .
```

如果你的网络适合直接走官方 PyPI：

```bash
docker build \
  --build-arg PIP_INDEX_URL=https://pypi.org/simple/ \
  -t echopass:1.0 .
```

### 运行

GPU 版本：

```bash
docker run -d \
  --name echopass \
  --gpus all \
  -p 8765:8765 \
  -v $PWD/.docker_data/cache:/app/.cache \
  -v $PWD/.docker_data/ssl:/app/ssl \
  -v $PWD/.docker_data/pretrained:/app/pretrained \
  -v $PWD/config/prod.yaml:/app/config/prod.yaml:ro \
  -e ECHOPASS_CONFIG=config/prod.yaml \
  echopass:1.0
```

CPU 版本：

- 去掉 `--gpus all` 即可

Docker 镜像里已经包含：

- `ffmpeg`
- `openssl`
- `curl`
- 自签证书兜底逻辑
- `scripts/run.sh` 启动逻辑

## API 速览

| 接口 | 用途 |
| --- | --- |
| `GET /api/health` | 健康检查与运行时状态 |
| `GET /api/speakers` | 查看已注册说话人 |
| `POST /api/enroll` | 注册说话人 |
| `POST /api/identify_file` | 用音频文件做说话人识别 |
| `POST /api/identify_pcm` | 用 PCM 做说话人识别 |
| `POST /api/recognize_pcm` | 说话人识别 + ASR |
| `POST /api/asr_reset` | 重置当前会话状态 |
| `POST /api/kws` | 唤醒词检测 |
| `POST /api/tts` | 文本转语音 |
| `POST /api/assistant/reply` | 助手普通回答 |
| `POST /api/assistant/stream` | 助手流式回答 |
| `POST /api/meeting/summary` | 生成会议纪要 |
| `GET /api/meeting/transcript` | 获取当前会话转录 |
| `POST /api/meeting/chapters` | 生成 AI 章节 |
| `POST /api/meeting/export` | 导出会议资料 ZIP |
| `WS /ws/control` | 前端事件广播通道 |

## 常见问题

### 1. 浏览器提示不能访问麦克风

优先检查：

- 是否是 HTTPS
- 是否是跨设备访问
- 浏览器是否授予了麦克风权限

如果你不是在 `localhost` 上打开页面，浏览器通常要求安全上下文。

### 2. 启动很慢

首次启动通常会下载模型，后续会快很多。`scripts/run.sh` 默认会开启离线模式，减少重复访问模型仓库。

如果你需要强制联网更新模型：

```bash
FORCE_ONLINE=1 ./scripts/run.sh
```

Windows PowerShell 等价写法：

```powershell
$env:FORCE_ONLINE=1
.\scripts\run.ps1
```

### 3. `modelscope` 在 Python 3.8 上报错

优先确认版本是否是 `1.10.0`。更高版本在这个项目当前锁定环境下可能触发兼容问题。

### 4. 没装 PostgreSQL 能跑吗

能。这是默认路径：`speaker.pg_dsn` 留空或 `export SPEAKER_DEMO_PG_DSN=''` 即只用内存，无需安装或初始化 PG，也**不必**装 `psycopg2-binary`。

若要让声纹写入 PostgreSQL，除配置 DSN 与执行 `sql/schema.sql` 外，需额外安装驱动：`pip install "psycopg2-binary==2.9.10"`（或 `pip install -e ".[postgres]"`）。在 Apple Silicon + Python 3.8 上编译该包可能需要本机提供 `pg_config`（见 [docs/LOCAL_QUICKSTART.md](docs/LOCAL_QUICKSTART.md)）。

### 5. 录音过程中纪要或章节没有生成

先检查：

- `llm.api_url`
- `llm.api_key`
- `llm.model`

如果 LLM 不可用，纪要和章节会尽量回退到规则模式，但质量会明显下降。

### 6. 启动日志里出现「预加载失败: 火山云端 ASR」

通常表示尚未在配置或环境变量中填写 `asr.volc.appid` 与 `asr.volc.token`（本仓库不内置凭据）。补齐后重启服务即可；在验证配置前也可临时将 `preload_models: false` 以跳过预加载（仍须配置凭据后 `/api/recognize_pcm` 才可用）。

## 安全与公开仓库

- **曾出现在旧提交或外泄副本中的任何真实密钥、DSN 均视为已泄露**，在改为公开或 Fork 前，应在火山、LLM 提供商、数据库等处**轮换或作废**。
- 本仓库的 `config/prod.yaml` 等本地私用路径已列入 [`.gitignore`](.gitignore)；仅提交去敏模板 `config/prod.yaml.example`（本地复制为 `config/prod.yaml` 后，未设置 `ECHOPASS_CONFIG` 时会自动优先读该文件）。
- 若需对外公开**历史提交中也不含**密钥，需另用 [git filter-repo](https://github.com/newren/git-filter-repo) 等工具清洗历史，或新建无历史敏感提交的分支/仓库；本计划只保证**当前工作树**无硬编码秘钥与内网地址。

## 许可证与致谢

- 项目主体采用 [MIT License](LICENSE)
- `echopass/campplus_model.py` 与 `echopass/audio_features.py` 基于 [3D-Speaker](https://github.com/modelscope/3D-Speaker) 裁剪而来，相关归属见 [NOTICE](NOTICE)
- KWS 运行时依赖 [FunASR](https://github.com/modelscope/FunASR)

## 延伸阅读

- [TECHNICAL_OVERVIEW.md](TECHNICAL_OVERVIEW.md)
- [docs/LOCAL_QUICKSTART.md](docs/LOCAL_QUICKSTART.md)
- [config/prod.yaml.example](config/prod.yaml.example)
- [scripts/first-run-mac.sh](scripts/first-run-mac.sh)
- [scripts/first-run-windows.ps1](scripts/first-run-windows.ps1)
- [scripts/run.ps1](scripts/run.ps1)
- [scripts/run.sh](scripts/run.sh)
- [sql/schema.sql](sql/schema.sql)
