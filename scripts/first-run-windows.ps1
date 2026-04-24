# EchoPass · Windows 首次准备（conda 方案，在项目根目录执行）
#
# 请先自行建好并进入 Python 3.8 的 conda 环境，例如：
#   conda create -n echopass python=3.8 -y
#   conda activate echopass
# 本脚本不创建 conda 环境、不创建 .venv，只用当前 shell 里的 python / pip。
#
# 作用：在当前 conda 环境安装依赖、按需生成 config\prod.yaml；不启动服务。

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

Write-Host "==> EchoPass first-run (Windows, conda)  ROOT=$Root"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "当前 PATH 找不到 python。请先：conda activate <你的3.8环境>"
    exit 1
}

python -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,8) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Error "请先：conda create -n echopass python=3.8 -y  然后  conda activate echopass"
    exit 1
}

if ([string]::IsNullOrWhiteSpace($env:CONDA_PREFIX)) {
    Write-Warning "未检测到 CONDA_PREFIX（可能未 conda activate）。建议先进入 conda 环境再执行本脚本。"
}

Write-Host "==> 使用解释器: $((Get-Command python).Source)  ($(python -V))"

Write-Host "==> 升级 pip / setuptools / wheel 并安装依赖（首次可能较慢）…"
python -m pip install -U pip setuptools wheel
python -m pip install -r requirements.txt

Write-Host "==> 固定 modelscope==1.10.0 …"
python -m pip install --force-reinstall --no-deps modelscope==1.10.0

$prodYaml = Join-Path $Root "config\prod.yaml"
$prodExample = Join-Path $Root "config\prod.yaml.example"
if (-not (Test-Path -LiteralPath $prodYaml)) {
    Write-Host "==> 生成 config\prod.yaml（从 prod.yaml.example 复制）…"
    Copy-Item -LiteralPath $prodExample -Destination $prodYaml
}

Write-Host ""
Write-Host "────────────────────────────────────────────────────────────"
Write-Host "下一步（保持当前 conda 环境已 activate）："
Write-Host ""
Write-Host "  1) 编辑配置（至少填 llm 与 asr.volc）："
Write-Host "       notepad `"$prodYaml`""
Write-Host ""
Write-Host "  2) 首次拉模型需联网："
Write-Host "       cd `"$Root`""
Write-Host '       $env:ECHOPASS_CONFIG = "config/prod.yaml"'
Write-Host '       $env:FORCE_ONLINE = "1"'
Write-Host "       .\scripts\run.ps1"
Write-Host ""
Write-Host "  3) 之后日常启动："
Write-Host '       $env:ECHOPASS_CONFIG = "config/prod.yaml"'
Write-Host "       .\scripts\run.ps1"
Write-Host ""
Write-Host "  浏览器：https://127.0.0.1:8765 （自签证书选「继续访问」）"
Write-Host "────────────────────────────────────────────────────────────"
