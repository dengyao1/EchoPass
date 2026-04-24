#!/usr/bin/env bash
# EchoPass · macOS 首次准备（conda 方案，在项目根目录执行）
# Windows 对等脚本：scripts/first-run-windows.ps1（或 first-run-windows.bat）
#
# 请先自行建好并进入 Python 3.8 的 conda 环境，例如：
#   conda create -n echopass python=3.8 -y
#   conda activate echopass
# 本脚本不再查找 brew/venv、不创建 .venv，只用当前 shell 里的 python/pip。
#
# 作用：在当前 conda 环境安装依赖、按需生成 config/prod.yaml；不启动服务。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> EchoPass first-run (macOS, conda)  ROOT=$ROOT"

if ! command -v python >/dev/null 2>&1; then
  echo "错误：当前 shell 找不到 python。请先：conda activate <你的3.8环境>" >&2
  exit 1
fi

read -r vmaj vmin <<< "$(python -c 'import sys; print(sys.version_info[0], sys.version_info[1])')"
if [[ "$vmaj" -ne 3 ]] || [[ "$vmin" -ne 8 ]]; then
  echo "错误：需要 Python 3.8，当前为 ${vmaj}.${vmin}（$(python -V)）。" >&2
  echo "请先：conda create -n echopass python=3.8 -y && conda activate echopass" >&2
  exit 1
fi

if [[ -z "${CONDA_PREFIX:-}" ]]; then
  echo "提示：未检测到 CONDA_PREFIX（可能未 conda activate）。建议先进入 conda 环境再执行本脚本。" >&2
fi

echo "==> 使用解释器: $(command -v python) ($(python -V))"

echo "==> 升级 pip / setuptools / wheel 并安装依赖（首次可能较慢）…"
python -m pip install -U pip setuptools wheel
python -m pip install -r requirements.txt

echo "==> 固定 modelscope==1.10.0 …"
python -m pip install --force-reinstall --no-deps modelscope==1.10.0

if [[ ! -f config/prod.yaml ]]; then
  echo "==> 生成 config/prod.yaml（从 prod.yaml.example 复制）…"
  cp -n config/prod.yaml.example config/prod.yaml
fi

echo ""
echo "────────────────────────────────────────────────────────────"
echo "下一步（保持当前 conda 环境已 activate）："
echo ""
echo "  1) 编辑配置（至少填 llm 与 asr.volc）："
echo "       open -e \"$ROOT/config/prod.yaml\""
echo ""
echo "  2) 首次拉模型需联网："
echo "       cd \"$ROOT\""
echo "       export ECHOPASS_CONFIG=config/prod.yaml"
echo "       FORCE_ONLINE=1 ./scripts/run.sh"
echo ""
echo "  3) 之后日常启动："
echo "       export ECHOPASS_CONFIG=config/prod.yaml"
echo "       ./scripts/run.sh"
echo ""
echo "  浏览器：https://127.0.0.1:8765 （自签证书点「高级 → 继续」）"
echo "────────────────────────────────────────────────────────────"
