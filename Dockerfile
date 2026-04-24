# EchoPass · 实时语音会议助手
#
# 构建（带国内 PyPI 镜像，默认）：
#   docker build -t echopass:1.0 .
# 构建（用官方 PyPI，海外网络好）：
#   docker build --build-arg PIP_INDEX_URL=https://pypi.org/simple/ -t echopass:1.0 .
#
# 运行（GPU；需宿主机 nvidia-container-toolkit）。凭据请写入挂载的 config/prod.yaml，勿把真实密钥写进命令行。
#   docker run -d --name echopass --gpus all -p 8765:8765 \
#     -v $PWD/.docker_data/cache:/app/.cache \
#     -v $PWD/.docker_data/ssl:/app/ssl \
#     -v $PWD/.docker_data/pretrained:/app/pretrained \
#     -v $PWD/config/prod.yaml:/app/config/prod.yaml:ro \
#     -e ECHOPASS_CONFIG=config/prod.yaml \
#     echopass:1.0
#
# 运行（CPU 推理；速度会慢）：把上面 --gpus all 去掉即可。
#
# 浏览器：https://<宿主机IP>:8765/  （首次自签证书点「高级 → 继续」）

FROM python:3.8-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=180

# ── 系统依赖 ──────────────────────────────────────────────
#   libsndfile1: soundfile 运行时需要
#   ffmpeg:      torchaudio / FunASR 在某些音频格式上需要
#   openssl:     scripts/run.sh 自动签发自签证书需要
#   curl + ca-certificates: 健康检查 + HTTPS 出口（modelscope/dashscope）
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libsndfile1 \
        ffmpeg \
        openssl \
        curl \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# ── PyPI 源 ───────────────────────────────────────────────
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app

# ── Python 依赖（先拷 requirements，最大化 layer 缓存）────────
COPY requirements.txt ./
RUN pip install --upgrade pip \
 && pip install -r requirements.txt \
 # funasr 1.3.1 在解析依赖时可能把 modelscope 顺手升到 ≥1.11，
 # 而 1.11+ 在 Python 3.8 import 时会因 PEP 585 写法直接崩。
 # 这里强制把 modelscope 重新固定到 1.10.0（其依赖已在上一步装好）。
 && pip install --force-reinstall --no-deps modelscope==1.10.0 \
 && pip show modelscope | grep Version \
 && python -c "import torch, modelscope, funasr; print('build-check OK |', \
      'torch=', torch.__version__, '| modelscope=', modelscope.__version__, \
      '| funasr=', funasr.__version__)"

# ── 应用代码 ──────────────────────────────────────────────
COPY . .
RUN chmod +x scripts/run.sh

# ── 缓存与持久化目录（建议从宿主机挂卷，避免每次重建容器都重下模型）──
ENV MODELSCOPE_CACHE=/app/.cache/modelscope \
    HF_HOME=/app/.cache/huggingface \
    XDG_CACHE_HOME=/app/.cache
RUN mkdir -p /app/.cache /app/ssl /app/pretrained

# ── 烤一份备用自签证书到 /opt/echopass-default-ssl/ ─────────
# 该目录不是 VOLUME，即便用户把 /app/ssl 绑定到宿主机空目录，这份证书
# 仍然存在；scripts/run.sh 会在运行时把它拷到 /app/ssl/ 并启用 HTTPS。
# 这样"首次跑容器默认就是 HTTPS"不会再因为 bind mount 覆盖而失效。
RUN mkdir -p /opt/echopass-default-ssl \
 && openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 \
        -subj "/CN=echopass" \
        -keyout /opt/echopass-default-ssl/key.pem \
        -out    /opt/echopass-default-ssl/cert.pem \
 && chmod 600 /opt/echopass-default-ssl/key.pem

VOLUME ["/app/.cache", "/app/ssl", "/app/pretrained"]

EXPOSE 8765

# 健康检查：默认 HTTPS 自签，curl -k 跳过校验
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -kfsS https://127.0.0.1:8765/api/health > /dev/null || exit 1

# scripts/run.sh 内部已经处理了：自动签发自签证书 / 模型预加载 / 离线模式
CMD ["./scripts/run.sh"]
