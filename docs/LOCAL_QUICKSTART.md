# 本地跑起来（最短）

**需要**：Python **3.8**、能上网（首次要下模型）、火山 ASR 的 `appid`+`token`、任意 **OpenAI 兼容 LLM** 的 `url`+`key`+`model`。**不要**装数据库（默认声纹在内存里）。

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

第一次下模型失败就再执行：`FORCE_ONLINE=1 ./scripts/run.sh`。  
日志里「预加载失败: 火山」= ASR 没配对，改完重启。

更全的说明见仓库根目录 [README.md](../README.md)。
