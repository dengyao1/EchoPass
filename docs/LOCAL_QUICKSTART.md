# 本地跑起来（最短）

环境变量与 YAML 键的对应关系、各模块职责见仓库根目录 [TECHNICAL_OVERVIEW.md](../TECHNICAL_OVERVIEW.md)（§9 配置、§11 启动）。

**需要**：Windows 请安装 **Miniconda / Anaconda**；**macOS 推荐 conda + Python 3.8**（见下文「macOS」）；Linux 可用 **Python 3.8** 与 venv。均需能上网（首次下模型）、火山 ASR 的 `appid`+`token`、任意 **OpenAI 兼容 LLM** 的 `url`+`key`+`model`。**不要**装数据库（默认声纹在内存里）。

## Windows

先安装 **Miniconda / Anaconda**。

最省事的方式是直接运行：

```powershell
cd ECHOPASS
.\scripts\run.bat
```

首次运行会自动：

- 若缺少 `config\prod.yaml`，按 `config\prod.yaml.example` 生成模板

如果脚本第一次帮你生成了 `config\prod.yaml`，先把下面这些字段填好，再重新执行一次 `.\scripts\run.bat`：

- `llm.api_url`
- `llm.api_key`
- `llm.model`
- `asr.volc.appid`
- `asr.volc.token`

配置文件就绪后，脚本会自动：

- 创建或复用 conda 环境 `echopass`
- 根据 [environment.yml](../environment.yml) 初始化基础环境
- 安装 `requirements.txt`

如果你想改 conda 环境名，可以先执行：

```powershell
$env:ECHOPASS_CONDA_ENV="my-echopass"
.\scripts\run.bat
```

浏览器优先打开 `https://127.0.0.1:8765`；如果机器上没有 `openssl`，脚本会回退到 `http://127.0.0.1:8765`。

## macOS（推荐 conda）

本仓库依赖锁定在 **Python 3.8**（与 `requirements.txt` / Docker 一致）。在 Apple 芯片上，**不要用系统自带的 Python 3.12+ 去建 `.venv`**，否则 `numpy` 等会装失败；推荐用 **Miniconda/Anaconda** 单独建环境。

### 1. 创建并进入 conda 环境

```bash
conda create -n echopass python=3.8 -y
conda activate echopass
cd /path/to/ECHOPASS   # 换成你的克隆目录
```

### 2. 首次安装依赖

在**已 `conda activate echopass`** 的前提下执行（脚本只使用当前环境的 `python`/`pip`，**不创建 `.venv`**）：

```bash
./scripts/first-run-mac.sh
```

该脚本会：`pip`/`setuptools`/`wheel` 升级、`pip install -r requirements.txt`、固定 `modelscope==1.10.0`；若不存在 `config/prod.yaml` 则从 `prod.yaml.example` 复制一份。

### 3. 填写配置

编辑 `config/prod.yaml`，至少填写 **LLM** 与 **火山 ASR**（`llm.api_url` / `api_key` / `model`，`asr.volc.appid` / `token` 等）。字段说明见仓库根目录 [config/prod.yaml.example](../config/prod.yaml.example)。

### 4. 启动服务

```bash
conda activate echopass
cd /path/to/ECHOPASS
export ECHOPASS_CONFIG=config/prod.yaml
```

**第一次**从 ModelScope 等拉模型权重需要联网，建议：

```bash
FORCE_ONLINE=1 ./scripts/run.sh
```

缓存齐了之后，日常可直接：

```bash
./scripts/run.sh
```

浏览器打开 **`https://127.0.0.1:8765`**（端口默认 **8765**；自签证书在浏览器中选「高级 → 继续访问」）。若本机没有 `openssl`，`run.sh` 可能退成 HTTP，以终端提示为准。

### 5. 可选

- **ffmpeg**（部分音频路径更省事）：`brew install ffmpeg`
- 若曾误用错误 Python 建过仓库下的 `.venv`，可直接删除：`rm -rf .venv`（conda 方案不依赖它）

---

## Linux

```bash
cd ECHOPASS
python3.8 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config/prod.yaml.example config/prod.yaml
# 用编辑器打开 prod.yaml，填 llm 与 asr.volc
export ECHOPASS_CONFIG=config/prod.yaml

# 首次拉模型需联网：
FORCE_ONLINE=1 ./scripts/run.sh
# 之后日常：./scripts/run.sh
```

浏览器打开 `https://127.0.0.1:8765`（自签证书点「继续访问」；端口默认 **8765**）。

`prod.yaml` 里至少要长这样（把引号里换成真值）：

```yaml
llm:
  api_url: "https://你的服务商/v1/chat/completions"
  api_key: "sk-…"
  model: "你的模型名"
asr:
  volc:
    api: "bigmodel"
    appid: "…"
    token: "…"
```

第一次下模型失败就再执行：`FORCE_ONLINE=1 ./scripts/run.sh`。Windows PowerShell 对应写法是：`$env:FORCE_ONLINE=1; .\scripts\run.bat`。
日志里「预加载失败: 火山」= ASR 没配对，改完重启。

### 要让声纹进 PostgreSQL 时

默认 `requirements.txt` **不含** `psycopg2-binary`。只有配置了 `speaker.pg_dsn`（或等价环境变量）并要让声纹落库时，再执行：

```bash
pip install "psycopg2-binary==2.9.10"
```

在 **macOS Apple 芯片 + Python 3.8** 上该版本常无预编译 wheel，会本地编译并需要 `pg_config`：可用 `conda install -c conda-forge libpq` 或 `brew install libpq` 并把 `.../opt/libpq/bin` 加入 `PATH`，或改用 **Python 3.10+** 环境以使用官方 wheel。

更全的说明见仓库根目录 [README.md](../README.md)。
