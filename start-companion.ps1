param([switch]$ReplaceLegacyHost)
$ErrorActionPreference = 'Stop'
$taskBridgeRoot = 'D:\dafeiyu\qq-bridge'
$taskNode = 'D:\node js\node.exe'
$env:DSH_HOME = 'D:\dafeiyu\dsh-home'
$env:DSH_AGENTS_HOME = 'D:\dafeiyu\runtime-data\agents'
$env:QQ_BRIDGE_DSH_PROFILE = 'companion'
$env:TEMP = 'D:\dafeiyu\runtime-data\temp'
$env:TMP = $env:TEMP
$env:XDG_CACHE_HOME = 'D:\dafeiyu\runtime-data\cache'
$env:NODE_COMPILE_CACHE = 'D:\dafeiyu\runtime-data\node-compile-cache'
$env:APPDATA = 'D:\dafeiyu\runtime-data\appdata'
$env:LOCALAPPDATA = 'D:\dafeiyu\runtime-data\localappdata'
$env:CUDA_CACHE_PATH = 'D:\dafeiyu\runtime-data\cache\cuda'
$env:TRITON_CACHE_DIR = 'D:\dafeiyu\runtime-data\cache\triton'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:HF_HOME = 'D:\dafeiyu\runtime-data\cache\huggingface'
$env:TORCH_HOME = 'D:\dafeiyu\runtime-data\cache\torch'
& (Join-Path $taskBridgeRoot 'start-dsh-local.ps1')
$taskConfigFile = Join-Path $taskBridgeRoot 'hardware.channel.local.json'
& $taskNode (Join-Path $taskBridgeRoot 'scripts\setup-hardware-channel.mjs')
if ($LASTEXITCODE -ne 0) { throw 'qq-bridge hardware setup failed.' }
$taskConfig = Get-Content -LiteralPath $taskConfigFile -Raw | ConvertFrom-Json
$taskDshListener = Get-NetTCPConnection -LocalPort 8784 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $taskDshListener) { throw 'The migrated D-drive DeepSeek Harness backend is not running.' }
$taskLogRoot = Join-Path $PSScriptRoot ('artifacts\qq-hardware-chat-runtime-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $taskLogRoot -Force | Out-Null
$taskGatewayToken = (Get-Content -LiteralPath $taskConfig.gatewayTokenFile -Raw).Trim()
$taskGatewayHeaders = @{ Authorization = 'Bearer ' + $taskGatewayToken }
$taskGateway = "http://127.0.0.1:$($taskConfig.port)"
$taskGatewayListener = Get-NetTCPConnection -LocalPort $taskConfig.port -State Listen -ErrorAction SilentlyContinue
if (-not $taskGatewayListener) {
    if (Get-NetTCPConnection -LocalPort 3100 -State Listen -ErrorAction SilentlyContinue) {
        throw 'An existing qq-bridge process lacks the hardware channel; restart that bridge after preserving its state.'
    }
    $taskOldDshEnv = $env:QQ_BRIDGE_DSH_BASE_URL
    try {
        $env:QQ_BRIDGE_DSH_BASE_URL = "http://127.0.0.1:$($taskDshListener.LocalPort)"
        $taskBridgeProcess = Start-Process -FilePath $taskNode -ArgumentList 'src/bridge.js' -WorkingDirectory $taskBridgeRoot -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $taskLogRoot 'bridge.stdout.log') -RedirectStandardError (Join-Path $taskLogRoot 'bridge.stderr.log') -PassThru
    } finally { $env:QQ_BRIDGE_DSH_BASE_URL = $taskOldDshEnv }
}
$taskGatewayReady = $false
for ($taskAttempt = 0; $taskAttempt -lt 50; $taskAttempt++) {
    try {
        $taskHealth = Invoke-RestMethod ($taskGateway + '/health') -Headers $taskGatewayHeaders -TimeoutSec 1
        if ($taskHealth.service -eq 'qq-bridge-hardware' -and $taskHealth.dsh_ready) { $taskGatewayReady = $true; break }
    } catch {}
    Start-Sleep -Milliseconds 500
}
if (-not $taskGatewayReady) { throw "qq-bridge hardware gateway is not ready. Inspect $taskLogRoot" }
$taskHostListener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($taskHostListener) {
    $taskHostHealth = Invoke-RestMethod 'http://127.0.0.1:8765/health' -TimeoutSec 3
    if ($taskHostHealth.service -ne 'toppet-host') { throw 'Port 8765 belongs to another service.' }
    if ($taskHostHealth.chat_backend -ne 'qq-bridge') {
        if (-not $ReplaceLegacyHost) { throw 'Legacy PetBrain host is active. Use -ReplaceLegacyHost to replace only that verified host.' }
        $taskHostProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($taskHostListener.OwningProcess)"
        if ($taskHostProcess.CommandLine -notmatch '-m host_service.server') { throw 'Host process identity changed.' }
        Stop-Process -Id $taskHostListener.OwningProcess
        $taskHostListener = $null
    }
}
if (-not $taskHostListener) {
    $taskShell = (Get-Process -Id $PID).Path
    $taskHostProcess = Start-Process -FilePath $taskShell -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        (Join-Path $PSScriptRoot 'start-qwen.ps1'), '-ChatBackend', 'qq-bridge', '-BridgeConfig', $taskConfigFile) `
        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskLogRoot 'host.stdout.log') `
        -RedirectStandardError (Join-Path $taskLogRoot 'host.stderr.log') -PassThru
}
for ($taskAttempt = 0; $taskAttempt -lt 80; $taskAttempt++) {
    try {
        $taskHostHealth = Invoke-RestMethod 'http://127.0.0.1:8765/health' -TimeoutSec 1
        if ($taskHostHealth.chat_backend -eq 'qq-bridge') {
            [ordered]@{ status='ready'; chat_backend='qq-bridge'; gateway=$taskGateway; userQQ=$taskConfig.userQQ;
                device=$taskConfig.deviceId; logs=$taskLogRoot } | ConvertTo-Json -Compress
            return
        }
    } catch {}
    Start-Sleep -Milliseconds 500
}
throw "TopPet qq-bridge host not ready. Inspect $taskLogRoot"
