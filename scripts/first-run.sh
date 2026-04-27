#!/usr/bin/env bash
# EchoPass · macOS / Linux 首次环境准备（在项目根或 scripts/ 下执行均可）
# 先自行创建并进入 Python 3.8 环境（推荐 conda；Linux 亦可用 venv 后执行本脚本）。
# 本脚本：安装依赖、从模板生成 config/prod.yaml、不启动服务。
#
# Windows 请用：scripts/first-run-windows.ps1
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> EchoPass first-run (macOS / Linux)  ROOT=$ROOT"

if ! command -v python >/dev/null 2>&1 && ! command -v python3 >/dev/null 2>&1; then
  echo "错误：未找到 python / python3。请先创建并激活 Python 3.8 环境。" >&2
  exit 1
fi
PY=python3
if ! command -v "$PY" >/dev/null 2>&1; then
  PY=python
fi

read -r vmaj vmin <<< "$($PY -c 'import sys; print(sys.version_info[0], sys.version_info[1])')"
if [[ "$vmaj" -ne 3 ]] || [[ "$vmin" -ne 8 ]]; then
  echo "错误：需要 Python 3.8，当前为 ${vmaj}.${vmin}（$($PY -V)）。" >&2
  echo "建议：conda create -n echopass python=3.8 -y && conda activate echopass" >&2
  exit 1
fi

if [[ -z "${CONDA_PREFIX:-}" && -z "${VIRTUAL_ENV:-}" ]]; then
  echo "提示：未检测到 conda / venv，请自行确认当前 python 为 3.8。" >&2
fi

echo "==> 使用解释器: $(command -v $PY) ($($PY -V))"

echo "==> 升级 pip / setuptools / wheel 并安装依赖（首次可能较慢）…"
$PY -m pip install -U pip setuptools wheel
$PY -m pip install -r requirements.txt

echo "==> 固定 modelscope==1.10.0 …"
$PY -m pip install --force-reinstall --no-deps modelscope==1.10.0

if [[ ! -f config/prod.yaml ]]; then
  echo "==> 生成 config/prod.yaml（从 prod.yaml.example 复制）…"
  cp -n config/prod.yaml.example config/prod.yaml
else
  echo "==> 已存在 config/prod.yaml，跳过复制（不覆盖你的密钥）。"
fi

echo ""
echo "────────────────────────────────────────────────────────────"
echo "配置（必改密钥）"
echo "  用编辑器打开: $ROOT/config/prod.yaml"
if [[ "$(uname -s 2>/dev/null)" = "Darwin" ]]; then
  echo "  示例: open -e \"$ROOT/config/prod.yaml\""
else
  echo "  示例: ${EDITOR:-nano} \"$ROOT/config/prod.yaml\""
fi
echo ""
echo "  必配项（无则无法转写/纪要/助手）见下方「必配项」与 prod.yaml 内 ☆ 标记。"
echo ""
echo "然后一键启动："
echo "  · 首次拉 CAM++ 等模型（需联网）:"
echo "      cd \"$ROOT\" && FORCE_ONLINE=1 ./scripts/run.sh"
echo "  · 之后日常："
echo "      cd \"$ROOT\" && ./scripts/run.sh"
echo "  存在 config/prod.yaml 时一般无需 ECHOPASS_CONFIG。"
echo ""
echo "  浏览器: https://127.0.0.1:8765 （自签证书在浏览器中「继续访问」）"
echo "────────────────────────────────────────────────────────────"
