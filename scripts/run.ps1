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

function Test-CommandExists([string]$Name) {
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Has-Text([string]$Value) {
    return -not [string]::IsNullOrWhiteSpace($Value)
}

function Test-NonEmptyFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    return (Get-Item -LiteralPath $Path).Length -gt 0
}

function Invoke-Step([string]$Command, [string[]]$Arguments) {
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "命令执行失败: $Command $($Arguments -join ' ')"
    }
}

function Invoke-Capture([string]$Command, [string[]]$Arguments) {
    $output = & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "命令执行失败: $Command $($Arguments -join ' ')"
    }
    return @($output)
}

function Get-CondaCandidate {
    $candidateSet = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $candidateList = [System.Collections.Generic.List[string]]::new()

    if (Has-Text $env:CONDA_EXE -and (Test-Path -LiteralPath $env:CONDA_EXE)) {
        if ($candidateSet.Add($env:CONDA_EXE)) {
            [void]$candidateList.Add($env:CONDA_EXE)
        }
    }

    foreach ($name in @("conda", "conda.exe")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -eq $cmd) {
            continue
        }
        $source = $null
        if ($cmd -is [System.Management.Automation.ApplicationInfo]) {
            $source = $cmd.Path
        } elseif ($cmd -is [System.Management.Automation.ExternalScriptInfo]) {
            $source = $cmd.Path
        } elseif ($cmd.Source) {
            $source = $cmd.Source
        }
        if (Has-Text $source -and $candidateSet.Add($source)) {
            [void]$candidateList.Add($source)
        }
    }

    foreach ($base in @($env:USERPROFILE, $env:LOCALAPPDATA, "C:\ProgramData")) {
        if (-not (Has-Text $base)) {
            continue
        }
        foreach ($variant in @("miniconda3", "anaconda3", "miniforge3", "mambaforge")) {
            foreach ($relative in @("Scripts\conda.exe", "condabin\conda.bat")) {
                $candidate = Join-Path $base "$variant\$relative"
                if ((Test-Path -LiteralPath $candidate) -and $candidateSet.Add($candidate)) {
                    [void]$candidateList.Add($candidate)
                }
            }
        }
    }

    foreach ($candidate in $candidateList) {
        try {
            $null = Invoke-Capture $candidate @("--version")
            return $candidate
        } catch {
            continue
        }
    }

    throw "未找到 Conda。请先安装 Miniconda / Anaconda，再重新运行 scripts\\run.bat。"
}

function Clear-SslEnv {
    Remove-Item Env:SSL_KEYFILE -ErrorAction SilentlyContinue
    Remove-Item Env:SSL_CERTFILE -ErrorAction SilentlyContinue
}

function Invoke-Conda([string]$CondaCommand, [string[]]$Arguments) {
    Invoke-Step $CondaCommand $Arguments
}

function Invoke-CondaCapture([string]$CondaCommand, [string[]]$Arguments) {
    return Invoke-Capture $CondaCommand $Arguments
}

function Invoke-CondaRun([string]$CondaCommand, [string]$EnvName, [string[]]$CommandLine) {
    Invoke-Conda $CondaCommand (@("run", "-n", $EnvName, "--no-capture-output") + $CommandLine)
}

function Test-CondaEnvExists([string]$CondaCommand, [string]$EnvName) {
    try {
        $raw = Invoke-CondaCapture $CondaCommand @("env", "list", "--json")
        $parsed = ($raw -join "`n") | ConvertFrom-Json
        foreach ($prefix in $parsed.envs) {
            if ((Split-Path -Leaf $prefix) -eq $EnvName) {
                return $true
            }
        }
        return $false
    } catch {
        throw "无法读取 conda 环境列表：$($_.Exception.Message)"
    }
}

function Test-CondaToolExists([string]$CondaCommand, [string]$EnvName, [string]$ToolName) {
    try {
        $null = Invoke-CondaCapture $CondaCommand @("run", "-n", $EnvName, $ToolName, "version")
        return $true
    } catch {
        return $false
    }
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $Root

try {
    $condaCommand = Get-CondaCandidate
    $condaEnvName = if (Has-Text $env:ECHOPASS_CONDA_ENV) { $env:ECHOPASS_CONDA_ENV.Trim() } else { "echopass" }
    $safeCondaEnvName = $condaEnvName -replace '[^A-Za-z0-9._-]', "_"
    $environmentPath = Join-Path $Root "environment.yml"
    $requirementsPath = Join-Path $Root "requirements.txt"
    $condaCacheDir = Join-Path $Root ".cache\conda"
    $installStamp = Join-Path $condaCacheDir "$safeCondaEnvName.requirements-installed"

    if (-not (Has-Text $env:ECHOPASS_CONFIG)) {
        $env:ECHOPASS_CONFIG = "config/prod.yaml"
    }

    $configPath = if ([System.IO.Path]::IsPathRooted($env:ECHOPASS_CONFIG)) {
        $env:ECHOPASS_CONFIG
    } else {
        Join-Path $Root $env:ECHOPASS_CONFIG
    }
    $defaultConfigPath = Join-Path $Root "config\prod.yaml"
    $configTemplatePath = Join-Path $Root "config\prod.yaml.example"

    if (-not (Test-Path -LiteralPath $configPath)) {
        if ($configPath -ne $defaultConfigPath) {
            throw "ECHOPASS_CONFIG 指向的文件不存在：$configPath"
        }
        Copy-Item -LiteralPath $configTemplatePath -Destination $configPath
        Write-WarnMsg "未找到 config/prod.yaml，已根据模板创建：$configPath"
        Write-Host "run.ps1: 请先填写 llm.api_url / api_key / model 与 asr.volc.appid / token，再重新运行 scripts\\run.bat。"
        if (Test-CommandExists "notepad.exe") {
            Start-Process -FilePath "notepad.exe" -ArgumentList $configPath | Out-Null
        }
        throw "已生成配置模板，请填写必需字段后重新运行。"
    }

    Write-Info "检测到 Conda（$condaCommand）"
    Write-Info "目标 conda 环境 = $condaEnvName"

    $envExists = Test-CondaEnvExists $condaCommand $condaEnvName
    $needBootstrap = -not $envExists
    $needEnvRefresh = $needBootstrap
    $needPipInstall = $needBootstrap

    if (-not $envExists) {
        Write-Info "未检测到 conda 环境 '$condaEnvName'，正在创建..."
        Invoke-Conda $condaCommand @("env", "create", "-y", "-f", $environmentPath, "-n", $condaEnvName)
    }

    if (-not (Test-Path -LiteralPath $condaCacheDir)) {
        New-Item -ItemType Directory -Path $condaCacheDir -Force | Out-Null
    }

    if (Test-Path -LiteralPath $installStamp) {
        $stampTime = (Get-Item -LiteralPath $installStamp).LastWriteTimeUtc
        if ((Get-Item -LiteralPath $environmentPath).LastWriteTimeUtc -gt $stampTime) {
            $needEnvRefresh = $true
            $needPipInstall = $true
        }
        if ((Get-Item -LiteralPath $requirementsPath).LastWriteTimeUtc -gt $stampTime) {
            $needPipInstall = $true
        }
    } else {
        $needEnvRefresh = $true
        $needPipInstall = $true
    }

    if ($envExists -and $needEnvRefresh) {
        Write-Info "检测到环境定义已更新，正在同步 conda 环境..."
        Invoke-Conda $condaCommand @("env", "update", "-y", "-f", $environmentPath, "-n", $condaEnvName, "--prune")
    }

    if ($needPipInstall) {
        Write-Info "正在安装/更新 Python 依赖（首次运行会比较久）..."
        Invoke-CondaRun $condaCommand $condaEnvName @("python", "-m", "pip", "install", "--upgrade", "pip")
        Invoke-CondaRun $condaCommand $condaEnvName @("python", "-m", "pip", "install", "-r", $requirementsPath)
        Set-Content -LiteralPath $installStamp -Value ([DateTime]::UtcNow.ToString("o")) -Encoding UTF8
    }

    if (-not (Has-Text $env:FORCE_ONLINE)) {
        if (-not (Has-Text $env:MODELSCOPE_OFFLINE)) {
            $env:MODELSCOPE_OFFLINE = "1"
        }
        if (-not (Has-Text $env:HF_HUB_OFFLINE)) {
            $env:HF_HUB_OFFLINE = "1"
        }
        if (-not (Has-Text $env:TRANSFORMERS_OFFLINE)) {
            $env:TRANSFORMERS_OFFLINE = "1"
        }
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

    $sslDir = Join-Path $Root "ssl"
    $defaultSslKey = Join-Path $sslDir "key.pem"
    $defaultSslCert = Join-Path $sslDir "cert.pem"
    if ((-not (Has-Text $env:SSL_KEYFILE)) -and (-not (Has-Text $env:SSL_CERTFILE)) `
            -and (Test-Path -LiteralPath $defaultSslKey) -and (Test-Path -LiteralPath $defaultSslCert)) {
        $env:SSL_KEYFILE = $defaultSslKey
        $env:SSL_CERTFILE = $defaultSslCert
    }

    if ((-not (Has-Text $env:SSL_KEYFILE)) -and (-not (Has-Text $env:SSL_CERTFILE)) -and (-not (Has-Text $env:NO_SSL))) {
        if (Test-CommandExists "openssl") {
            New-Item -ItemType Directory -Path $sslDir -Force | Out-Null
            Write-Info "未找到 SSL 证书，正在生成自签证书到 $sslDir ..."
            & openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 -subj "/CN=echopass" -keyout $defaultSslKey -out $defaultSslCert
            if (($LASTEXITCODE -eq 0) -and (Test-NonEmptyFile $defaultSslKey) -and (Test-NonEmptyFile $defaultSslCert)) {
                $env:SSL_KEYFILE = $defaultSslKey
                $env:SSL_CERTFILE = $defaultSslCert
                Write-Info "自签证书已生成（CN=echopass，10 年有效）。浏览器首次访问需选择继续访问。"
            } else {
                Write-WarnMsg "自签证书生成失败，将以 HTTP 启动。"
            }
        } elseif (Test-CondaToolExists $condaCommand $condaEnvName "openssl") {
            New-Item -ItemType Directory -Path $sslDir -Force | Out-Null
            Write-Info "系统 PATH 未找到 openssl，改用 conda 环境内 openssl 生成证书..."
            Invoke-CondaRun $condaCommand $condaEnvName @(
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "3650",
                "-subj", "/CN=echopass", "-keyout", $defaultSslKey, "-out", $defaultSslCert
            )
            if ((Test-NonEmptyFile $defaultSslKey) -and (Test-NonEmptyFile $defaultSslCert)) {
                $env:SSL_KEYFILE = $defaultSslKey
                $env:SSL_CERTFILE = $defaultSslCert
                Write-Info "自签证书已生成（CN=echopass，10 年有效）。浏览器首次访问需选择继续访问。"
            } else {
                Write-WarnMsg "自签证书生成失败，将以 HTTP 启动。"
            }
        } else {
            Write-WarnMsg "系统与 conda 环境里都未找到 openssl，将以 HTTP 启动。如需 HTTPS，请安装 openssl 后重试，或手动放置 ssl\\key.pem 与 ssl\\cert.pem。"
        }
    }

    $port = if (Has-Text $env:PORT) { $env:PORT } else { "8765" }
    $uvicornArgs = @("python", "-m", "uvicorn", "echopass.app:app", "--host", "0.0.0.0", "--port", $port)
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

    Write-Info "当前配置文件 = $configPath"
    Write-Info "浏览器可打开 ${scheme}://127.0.0.1:$port/"
    Invoke-CondaRun $condaCommand $condaEnvName $uvicornArgs
} finally {
    Pop-Location
}
