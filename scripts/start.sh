#!/usr/bin/env bash
# EchoPass 一键启动（需已配置好 config/prod.yaml）
# 等价于在仓库根执行 ./scripts/run.sh；支持向 run.sh 透传环境变量，如：
#   FORCE_ONLINE=1 ./scripts/start.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec "$ROOT/scripts/run.sh" "$@"
