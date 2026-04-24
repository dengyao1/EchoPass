# 本地跑起来（最短）

**需要**：Windows 请安装 **Miniconda / Anaconda**；macOS / Linux 请准备 Python **3.8+**。另外都需要能上网（首次要下模型）、火山 ASR 的 `appid`+`token`、任意 **OpenAI 兼容 LLM** 的 `url`+`key`+`model`。**不要**装数据库（默认声纹在内存里）。

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

## macOS / Linux

```bash
cd ECHOPASS
python3.8 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config/prod.yaml.example config/prod.yaml
# 用编辑器打开 prod.yaml，填 llm 与 asr.volc
export ECHOPASS_CONFIG=config/prod.yaml

./scripts/run.sh
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

更全的说明见仓库根目录 [README.md](../README.md)。
