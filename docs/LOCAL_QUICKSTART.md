# 本地跑起来（最短）

更全的配置说明、环境变量表见仓库根目录 [TECHNICAL_OVERVIEW.md](../TECHNICAL_OVERVIEW.md)（§9、§11）。

---

## 目录

- [macOS / Linux](#macos--linux统一流程)
- [Windows](#windows推荐-conda)
- [必配项小结](#必配项小结复制-prodyaml-后必改)
- [验证安装](#验证安装)
- [故障排错](#故障排错)

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

缓存齐了之后，**日常**直接：

```bash
./scripts/run.sh
```

浏览器打开 **https://127.0.0.1:8765**（默认端口 **8765**；自签证书选「继续访问」）。若未装 `openssl`，脚本可能以 **HTTP** 启动，以终端提示为准。均在仓库根、已激活环境下执行。

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

## 验证安装

启动成功后，浏览器访问 `https://127.0.0.1:8765`，快速检查几个关键功能：

1. **健康检查**：访问 `/api/health`，应返回 JSON 包含 `"ok": true, "asr_ready": true`
2. **注册声纹**：设置抽屉 → 「录音注册」，说 3~5 秒话，名字出现在列表中
3. **实时转写**：主界面点「开始录音」，说话后左侧出现转录气泡
4. **生成纪要**：说几句话后，右侧「AI 纪要」点刷新

---

## 故障排错

### 启动阶段

**启动时报 modelscope 导入错误？**

原因：modelscope >= 1.11 用了 Python 3.9+ 语法，3.8 会崩溃。解决：`pip install --force-reinstall --no-deps modelscope==1.10.0`

**启动时卡在预加载很久？**

首次需从 ModelScope 下载模型。确认网络能访问 modelscope.cn，用 `FORCE_ONLINE=1` 启动。

**启动时报火山 ASR 凭据未配置？**

在 `config/prod.yaml` 填写 `asr.volc.appid` 和 `asr.volc.token` 后重启。

### 运行时问题

**浏览器无法使用麦克风？**

非 localhost 必须 HTTPS。`scripts/run.sh` 会自动生成自签证书，访问时点「高级 → 继续访问」。

**转录结果为空？**

检查 `/api/health` 中 `asr_ready` 状态；确认麦克风权限；确认说的中文；查看终端 ASR 日志。

**纪要一直为空？**

检查 LLM 三件套（api_url / api_key / model）是否可访问。可用 curl 测试连通性。LLM 不可用时会自动回退规则摘要。

**声纹识别不准？**

注册时说 3~5 秒以上；同设备同环境使用；可调低 `speaker.threshold`（默认 0.45）；多人会议可设参与者白名单。

### 其他

**Python 3.8 装 psycopg2 报错？**

Apple Silicon 需要 `brew install libpq` 后设置 PATH。

**能用 Python 3.9+ 吗？**

不建议。依赖版本为 3.8 锁定，高版本需自行解决兼容问题。

---

更全的说明见 [README.md](../README.md)、[TECHNICAL_OVERVIEW.md](../TECHNICAL_OVERVIEW.md)。
