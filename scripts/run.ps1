# EchoPass · Windows 启动（与 macOS 的 run.sh 一致：不创建 conda、不装依赖）
# 请先：conda create -n echopass python=3.8 -y → conda activate echopass → .\scripts\first-run-windows.ps1

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "run.ps1: 仅支持 Windows。macOS/Linux 请使用 scripts/run.sh"
}

function Write-Info([string]$Message) {
    Write-Host "run.ps1: $Message"
}

function Write-WarnMsg([string]$Message) {
    Write-Warning "run.ps1: $Message"
}

function Has-Text([string]$Value) {
    return -not [string]::IsNullOrWhiteSpace($Value)
}

function Test-NonEmptyFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    return (Get-Item -LiteralPath $Path).Length -gt 0
}

function Clear-SslEnv {
    Remove-Item Env:SSL_KEYFILE -ErrorAction SilentlyContinue
    Remove-Item Env:SSL_CERTFILE -ErrorAction SilentlyContinue
}

function Test-CommandExists([string]$Name) {
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $Root

try {
    if (-not (Test-CommandExists "python")) {
        throw "run.ps1: PATH 中找不到 python。请先安装 Miniconda/Anaconda，执行 conda create -n echopass python=3.8 -y ，再 conda activate echopass ，并运行 scripts\first-run-windows.ps1 。"
    }

    python -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,8) else 1)"
    if ($LASTEXITCODE -ne 0) {
        $ver = ""
        try {
            $ver = (python -c "import sys; print('{}.{}'.format(sys.version_info[0], sys.version_info[1]))" 2>$null)
        } catch { }
        throw "run.ps1: 需要 Python 3.8，当前为 $ver。请先 conda activate echopass（或你的 3.8 环境）；尚未安装依赖请执行 scripts\first-run-windows.ps1 。"
    }

    if (-not (Has-Text $env:FORCE_ONLINE)) {
        if (-not (Has-Text $env:MODELSCOPE_OFFLINE)) { $env:MODELSCOPE_OFFLINE = "1" }
        if (-not (Has-Text $env:HF_HUB_OFFLINE)) { $env:HF_HUB_OFFLINE = "1" }
        if (-not (Has-Text $env:TRANSFORMERS_OFFLINE)) { $env:TRANSFORMERS_OFFLINE = "1" }
    }

    $sslKey = $env:SSL_KEYFILE
    $sslCert = $env:SSL_CERTFILE
    if ((Has-Text $sslKey) -xor (Has-Text $sslCert)) {
        Write-WarnMsg "SSL_KEYFILE 与 SSL_CERTFILE 须成对设置，已忽略。"
        Clear-SslEnv
    } elseif ((Has-Text $sslKey) -and (Has-Text $sslCert)) {
        if ((-not (Test-Path -LiteralPath $sslKey)) -or (-not (Test-Path -LiteralPath $sslCert))) {
            Write-WarnMsg "SSL 文件不存在，已忽略。（SSL_KEYFILE=$sslKey SSL_CERTFILE=$sslCert）"
            Clear-SslEnv
        }
    }

    $repoSslKey = Join-Path $Root "ssl\key.pem"
    $repoSslCert = Join-Path $Root "ssl\cert.pem"
    if ((-not (Has-Text $env:SSL_KEYFILE)) -and (-not (Has-Text $env:SSL_CERTFILE)) `
            -and (Test-Path -LiteralPath $repoSslKey) -and (Test-Path -LiteralPath $repoSslCert)) {
        $env:SSL_KEYFILE = $repoSslKey
        $env:SSL_CERTFILE = $repoSslCert
    }

    $sslDir = Join-Path $Root "ssl"
    $defaultSslKey = Join-Path $sslDir "key.pem"
    $defaultSslCert = Join-Path $sslDir "cert.pem"
    if ((-not (Has-Text $env:SSL_KEYFILE)) -and (-not (Has-Text $env:SSL_CERTFILE)) -and (-not (Has-Text $env:NO_SSL))) {
        if (Test-CommandExists "openssl") {
            New-Item -ItemType Directory -Path $sslDir -Force | Out-Null
            Write-Info "未找到 SSL 证书，正在生成自签证书到 $sslDir ..."
            & openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 `
                -subj "/CN=echopass" -keyout $defaultSslKey -out $defaultSslCert
            if (($LASTEXITCODE -eq 0) -and (Test-NonEmptyFile $defaultSslKey) -and (Test-NonEmptyFile $defaultSslCert)) {
                $env:SSL_KEYFILE = $defaultSslKey
                $env:SSL_CERTFILE = $defaultSslCert
                Write-Info "自签证书已生成（CN=echopass，10 年有效）。浏览器首次访问需选择继续访问。"
            } else {
                Write-WarnMsg "自签证书生成失败，将以 HTTP 启动。"
            }
        } else {
            Write-WarnMsg "未在 PATH 找到 openssl，无法自动生成自签证书；将以 HTTP 启动。可安装 OpenSSL 或手动放置 ssl\key.pem 与 ssl\cert.pem。"
        }
    }

    $port = if (Has-Text $env:PORT) { $env:PORT } else { "8765" }
    $uvicornArgs = @("-m", "uvicorn", "echopass.app:app", "--host", "0.0.0.0", "--port", $port)
    $scheme = "http"

    if ((Has-Text $env:SSL_KEYFILE) -and (Has-Text $env:SSL_CERTFILE)) {
        $uvicornArgs += @("--ssl-keyfile", $env:SSL_KEYFILE, "--ssl-certfile", $env:SSL_CERTFILE)
        $scheme = "https"
        Write-Info "启动协议 = HTTPS，监听 0.0.0.0:$port（--ssl-keyfile=$($env:SSL_KEYFILE)）"
    } else {
        Write-Info "启动协议 = HTTP，监听 0.0.0.0:$port（未启用 TLS）"
    }

    if (-not (Has-Text $env:VERBOSE)) {
        $uvicornArgs += @("--no-access-log", "--log-level", "warning")
    }

    $cfgHint = if (Has-Text $env:ECHOPASS_CONFIG) { $env:ECHOPASS_CONFIG } else { "（未设置，应用默认读 config/prod.yaml.example）" }
    Write-Info "ECHOPASS_CONFIG = $cfgHint"
    Write-Info "浏览器可打开 ${scheme}://127.0.0.1:$port/"
    & python @uvicornArgs
} finally {
    Pop-Location
}
