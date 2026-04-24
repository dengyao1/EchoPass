#!/usr/bin/env bash
# 在仓库根目录启动 EchoPass 服务（0.0.0.0 便于内网访问）。
# 脚本先定位到仓库根，再启动 uvicorn，避免包导入失败。
#
# 环境变量：
#   PORT           监听端口，默认 8765
#   VERBOSE        非空则打开 uvicorn access log / info 级启动日志
#   SSL_KEYFILE    HTTPS 私钥（与 SSL_CERTFILE 成对；不设则尝试用仓库 ssl/ 下默认文件）
#   SSL_CERTFILE   HTTPS 证书
#   NO_SSL         非空则强制 HTTP 启动（不自动生成自签证书）
#   SPEAKER_DEMO_PG_DSN  Postgres DSN；不设时使用 echopass/app.py 内置默认 DSN
#                        （即声纹默认持久化到 PG）；显式设为空字符串可强制仅用内存。
#
# 默认行为（不设任何 SSL 变量时）：
#   若 ssl/key.pem + ssl/cert.pem 不存在，会自动生成一份自签证书并启 HTTPS。
#   浏览器访问 https://<IP>:8765/，首次点「高级 → 继续」即可。
#   如确实只想跑 HTTP，加 NO_SSL=1。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ── 声纹存储后端 ─────────────────────────────────────────────
# 默认走 PostgreSQL（DSN 写在 echopass/app.py 的 _DEMO_PG_DSN）；
# 想覆盖：    export SPEAKER_DEMO_PG_DSN="postgresql://user:pwd@host:port/db"
# 想关掉 PG：export SPEAKER_DEMO_PG_DSN=""   （强制仅用内存，不连库）
# 这里不再做任何默认 export，保持环境变量未设置 → app.py 内置 DSN 生效。

# ── 模型加载离线模式（默认开启）───────────────────────────────
# 首次启动会从 modelscope/HF 下载权重到 ~/.cache/modelscope；之后再启动时
# 走离线模式可避免 FunASR/AutoModel 反复去 hub 做 master 分支 revision
# 校验，能把 FunASR 预加载从 ~85s 缩到 ~25s。
# 想强制联网（首次下载或换模型时）：FORCE_ONLINE=1 ./scripts/run.sh
# CAM++：echopass/engine.py 在 pretrained/ 下已有权重时跳过 snapshot_download，
# 避免每次启动都去 ModelScope Hub 做 revision 校验（弱网环境可达数十秒）。
if [[ -z "${FORCE_ONLINE:-}" ]]; then
  export MODELSCOPE_OFFLINE="${MODELSCOPE_OFFLINE:-1}"
  export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
  export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"
fi
# SSL：须成对且文件真实存在；否则 unset，再回落到仓库 ssl/*.pem（避免环境里残留错误路径导致 uvicorn 崩）
if [[ -n "${SSL_KEYFILE:-}" && -z "${SSL_CERTFILE:-}" ]] || [[ -z "${SSL_KEYFILE:-}" && -n "${SSL_CERTFILE:-}" ]]; then
  echo "run.sh: 警告: SSL_KEYFILE 与 SSL_CERTFILE 须成对设置，已忽略。" >&2
  unset SSL_KEYFILE SSL_CERTFILE
fi
if [[ -n "${SSL_KEYFILE:-}" && -n "${SSL_CERTFILE:-}" ]]; then
  if [[ ! -f "$SSL_KEYFILE" || ! -f "$SSL_CERTFILE" ]]; then
    echo "run.sh: 警告: SSL 文件不存在，已忽略。（SSL_KEYFILE=${SSL_KEYFILE} SSL_CERTFILE=${SSL_CERTFILE}）" >&2
    unset SSL_KEYFILE SSL_CERTFILE
  fi
fi

# 若用户没显式指定 SSL，且仓库 ssl/ 下有现成证书 → 直接用
if [[ -z "${SSL_KEYFILE:-}" && -z "${SSL_CERTFILE:-}" && -f "$ROOT/ssl/key.pem" && -f "$ROOT/ssl/cert.pem" ]]; then
  export SSL_KEYFILE="$ROOT/ssl/key.pem"
  export SSL_CERTFILE="$ROOT/ssl/cert.pem"
fi

# Docker 场景：如果 /app/ssl 被 bind-mount 成空目录，Dockerfile 会在
# /opt/echopass-default-ssl/ 烤一份备用证书；bind mount 不会影响它，这里拷过去即可。
_DEFAULT_SSL_DIR="${ECHOPASS_DEFAULT_SSL_DIR:-/opt/echopass-default-ssl}"
if [[ -z "${SSL_KEYFILE:-}" && -z "${SSL_CERTFILE:-}" \
      && -f "$_DEFAULT_SSL_DIR/key.pem" && -f "$_DEFAULT_SSL_DIR/cert.pem" ]]; then
  if [[ ! -s "$ROOT/ssl/key.pem" || ! -s "$ROOT/ssl/cert.pem" ]]; then
    mkdir -p "$ROOT/ssl"
    if cp -f "$_DEFAULT_SSL_DIR/key.pem"  "$ROOT/ssl/key.pem"  \
    && cp -f "$_DEFAULT_SSL_DIR/cert.pem" "$ROOT/ssl/cert.pem"; then
      chmod 600 "$ROOT/ssl/key.pem" 2>/dev/null || true
      echo "run.sh: 使用镜像内置备用证书（$_DEFAULT_SSL_DIR → $ROOT/ssl/）" >&2
    fi
  fi
  if [[ -s "$ROOT/ssl/key.pem" && -s "$ROOT/ssl/cert.pem" ]]; then
    export SSL_KEYFILE="$ROOT/ssl/key.pem"
    export SSL_CERTFILE="$ROOT/ssl/cert.pem"
  fi
fi

# 仍然没有 SSL，且未显式 NO_SSL → 自动生成一份自签证书，避免浏览器走 HTTPS 时连不上
if [[ -z "${SSL_KEYFILE:-}" && -z "${SSL_CERTFILE:-}" && -z "${NO_SSL:-}" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    mkdir -p "$ROOT/ssl"
    echo "run.sh: 未找到 SSL 证书，正在生成自签证书到 $ROOT/ssl/ ..." >&2
    # 不再吞错：把 openssl 的 stderr 直通，失败能立刻看见
    if openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 \
         -subj "/CN=echopass" \
         -keyout "$ROOT/ssl/key.pem" -out "$ROOT/ssl/cert.pem"; then
      chmod 600 "$ROOT/ssl/key.pem" 2>/dev/null || true
    fi
    # 校验：文件真实存在且非空，才认为 HTTPS 可用
    if [[ -s "$ROOT/ssl/key.pem" && -s "$ROOT/ssl/cert.pem" ]]; then
      export SSL_KEYFILE="$ROOT/ssl/key.pem"
      export SSL_CERTFILE="$ROOT/ssl/cert.pem"
      echo "run.sh: 自签证书已生成（CN=echopass，10 年有效）。浏览器首次访问需点「高级 → 继续」。" >&2
    else
      echo "run.sh: 警告: 自签证书生成失败（文件不存在或为空，通常是 ssl/ 目录不可写）；将以 HTTP 启动。" >&2
    fi
  else
    echo "run.sh: 警告: 系统未安装 openssl，无法自动生成自签证书；将以 HTTP 启动。" >&2
    echo "run.sh: 如需 HTTPS，请安装 openssl 后重启，或手动放 ssl/key.pem + ssl/cert.pem。" >&2
  fi
fi

UVICORN_ARGS=(echopass.app:app --host 0.0.0.0 --port "${PORT:-8765}")
if [[ -n "${SSL_KEYFILE:-}" && -n "${SSL_CERTFILE:-}" ]]; then
  UVICORN_ARGS+=(--ssl-keyfile "$SSL_KEYFILE" --ssl-certfile "$SSL_CERTFILE")
  echo "run.sh: 启动协议 = HTTPS，监听 0.0.0.0:${PORT:-8765}（--ssl-keyfile=${SSL_KEYFILE}）" >&2
else
  echo "run.sh: 启动协议 = HTTP，监听 0.0.0.0:${PORT:-8765}（未启用 TLS）" >&2
fi
if [[ -z "${VERBOSE:-}" ]]; then
  UVICORN_ARGS+=(--no-access-log --log-level warning)
fi

exec uvicorn "${UVICORN_ARGS[@]}"
