# 本地跑起来（最短）

更全的配置说明、环境变量表见仓库根目录 [TECHNICAL_OVERVIEW.md](../TECHNICAL_OVERVIEW.md)（§9、§11）。

---

## macOS / Linux（统一流程）

依赖 **Python 3.8**。推荐 **Miniconda/Anaconda**（macOS、Linux 均可）；也可自建 **venv**，只要 `python` 为 3.8 即可。

### 1. 创建并进入环境（示例：conda）

```bash
conda create -n echopass python=3.8 -y
conda activate echopass
cd /path/to/ECHOPASS   # 换成你的克隆目录
```

若用 venv（Linux 常见）：

```bash
cd /path/to/ECHOPASS
python3.8 -m venv .venv && source .venv/bin/activate
```

### 2. 一键准备环境 + 生成配置模板

在**已激活的 3.8 环境**下执行（只装依赖、从模板复制 `config/prod.yaml`，**不启动服务**）：

```bash
./scripts/first-run.sh
```

（旧名 `./scripts/first-run-mac.sh` 与上面等价。）

### 3. 填写 `config/prod.yaml`（必配密钥）

若上一步已生成 `config/prod.yaml`，用编辑器打开并**至少**填好下表（与 [config/prod.yaml.example](../config/prod.yaml.example) 中 `☆` 一致）：

| 配置路径 | 说明 |
| --- | --- |
| `llm.api_url` | OpenAI 兼容 Chat Completions 地址，如 `https://api.xxx/v1/chat/completions` |
| `llm.api_key` | 该 LLM 的 API Key |
| `llm.model` | 模型名，如 `qwen-plus`、`deepseek-chat` |
| `asr.volc.appid` | 火山引擎 openspeech / ASR 项目 AppID |
| `asr.volc.token` | 火山 ASR Access Token |

**常用可选**：`asr.volc.api`（`bigmodel` / `common`）、`tts.*`（播报）、`speaker.pg_dsn`（声纹落库）、`kws.enabled: true`（本地「小云小云」唤醒，会多下 KWS 模型）。

填完后**保存**；不需要再设 `ECHOPASS_CONFIG`（仓库根存在 `config/prod.yaml` 时会自动使用）。

### 4. 启动服务

**首次**在本机拉取 CAM++ 等模型需能访问外网：

```bash
FORCE_ONLINE=1 ./scripts/run.sh
```

或（等价）：

```bash
FORCE_ONLINE=1 ./scripts/start.sh
```

缓存齐了之后，**日常**直接：

```bash
./scripts/run.sh
# 或
./scripts/start.sh
```

浏览器打开 **https://127.0.0.1:8765**（默认端口 **8765**；自签证书选「继续访问」）。若未装 `openssl`，脚本可能以 **HTTP** 启动，以终端提示为准。

**说明**：`run.sh` 与 `start.sh` 功能相同，`start.sh` 便于记忆「启动」；均在仓库根、已激活环境下执行。

---

## Windows（推荐 conda）

本仓库依赖 **Python 3.8**。请用 **「Anaconda Prompt」或已初始化的 PowerShell 里的 conda**。

### 1. 环境

```powershell
conda create -n echopass python=3.8 -y
conda activate echopass
cd C:\path\to\ECHOPASS
```

### 2. 首次安装

```powershell
.\scripts\first-run-windows.ps1
```

会安装依赖、若无 `config\prod.yaml` 则从模板复制。

### 3. 配置

用编辑器打开 `config\prod.yaml`，**必配项与上表相同**（`llm.*`、`asr.volc.appid` / `token`）。

### 4. 启动

首次拉模型（联网）：

```powershell
$env:FORCE_ONLINE = "1"
.\scripts\run.ps1
```

之后日常：

```powershell
.\scripts\run.ps1
```

浏览器：**https://127.0.0.1:8765**。若本机无 `openssl`，可能为 HTTP，以终端为准。

---

## 必配项小结（复制 `prod.yaml` 后必改）

1. `llm.api_url`、`llm.api_key`、`llm.model`  
2. `asr.volc.appid`、`asr.volc.token`  

其余按业务再开：TTS、`kws.enabled`、声纹库等，见 [config/prod.yaml.example](../config/prod.yaml.example)。

---

## 故障与可选

- **首启慢 / 预加载失败「火山」**：多为未填或填错 `asr.volc`，改后重启。  
- **首启要拉模型**：用 `FORCE_ONLINE=1`；日常可去掉。  
- **纪要一直空**：查 `llm` 三件套是否可访问。  
- **ffmpeg**（部分音频更省事）：系统包管理器自行安装。  
- **声纹用 PostgreSQL**：`requirements.txt` 默认不含驱动；需落库时安装 `psycopg2-binary` 并配 `speaker.pg_dsn`，见模板注释；Apple 硅 + Py3.8 下编译问题见 [README](../README.md) 或 TECHNICAL 文档。

更全的说明见 [README.md](../README.md)、[TECHNICAL_OVERVIEW.md](../TECHNICAL_OVERVIEW.md)。
