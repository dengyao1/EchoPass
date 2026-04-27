#!/usr/bin/env bash
# 兼容旧名称：与 first-run.sh 相同（macOS / Linux 统一入口请用 first-run.sh）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT/first-run.sh"
