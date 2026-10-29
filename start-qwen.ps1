param(
    [ValidateSet('host', 'desktop')][string]$Mode = 'host',
    [ValidateSet('qwen', 'voxcpm')][string]$VoiceBackend = 'qwen',
    [ValidateSet('fast', 'baseline')][string]$QwenRuntime = 'fast',
    [int]$Port = 8765,
    [string]$HostAddress = '0.0.0.0'
    ,[ValidateSet('qq-bridge', 'petbrain')][string]$ChatBackend = 'qq-bridge'
    ,[string]$BridgeConfig = 'D:\dafeiyu\qq-bridge\hardware.channel.local.json'
)
$ErrorActionPreference = 'Stop'
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskPython = 'D:\toppet\.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'TopPet Python environment is missing.' }
$taskOldBackend = $env:TOPPET_TTS_ENGINE
$taskOldRuntime = $env:TOPPET_QWEN_RUNTIME
$taskOldKey = $env:DEEPSEEK_API_KEY
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:TEMP = 'D:\dafeiyu\runtime-data\temp'
$env:TMP = $env:TEMP
$env:HF_HOME = 'D:\dafeiyu\runtime-data\cache\huggingface'
$env:TORCH_HOME = 'D:\dafeiyu\runtime-data\cache\torch'
$env:APPDATA = 'D:\dafeiyu\runtime-data\appdata'
$env:LOCALAPPDATA = 'D:\dafeiyu\runtime-data\localappdata'
$env:CUDA_CACHE_PATH = 'D:\dafeiyu\runtime-data\cache\cuda'
$env:TRITON_CACHE_DIR = 'D:\dafeiyu\runtime-data\cache\triton'
$env:TOPPET_TTS_ENGINE = $VoiceBackend
$env:TOPPET_QWEN_RUNTIME = $QwenRuntime
# The deployed board uses ES8311 volume=85: reg32=0xD8, or +12.5 dB.
# Cancel that boost before its DAC instead of clipping clean Qwen PCM there.
# Set this explicitly to 0 when firmware uses a calibrated 0 dB DAC volume.
if (-not $env:TOPPET_SPEAKER_GAIN_DB) { $env:TOPPET_SPEAKER_GAIN_DB = '-12.5' }
if (($ChatBackend -eq 'petbrain' -or $Mode -eq 'desktop') -and -not $env:DEEPSEEK_API_KEY) {
    $taskKey = Join-Path $PSScriptRoot 'api_key.txt'
    if (-not (Test-Path -LiteralPath $taskKey)) { $taskKey = 'D:\toppet\api_key.txt' }
    if (Test-Path -LiteralPath $taskKey) {
        $env:DEEPSEEK_API_KEY = (Get-Content -LiteralPath $taskKey -Raw).Trim()
    }
}
Push-Location $PSScriptRoot
try {
    if ($Mode -eq 'host') {
        if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
            throw "Port $Port is already in use. Stop that host before switching voice backends."
        }
        $taskHostStarted = Get-Date
        & $taskPython -u -X faulthandler -m host_service.server --host $HostAddress --port $Port --character personal --chat-backend $ChatBackend --bridge-config $BridgeConfig
        $taskHostExitCode = $LASTEXITCODE
        $taskExitDir = Join-Path $PSScriptRoot 'logs'
        New-Item -ItemType Directory -Path $taskExitDir -Force | Out-Null
        [ordered]@{ started=$taskHostStarted.ToString('o'); exited=(Get-Date).ToString('o');
            exit_code=$taskHostExitCode; launcher_pid=$PID; chat_backend=$ChatBackend } |
            ConvertTo-Json | Set-Content -LiteralPath (Join-Path $taskExitDir ('host-exit-' + (Get-Date -Format 'yyyyMMdd-HHmmssfff') + '.json')) -Encoding utf8
        if ($taskHostExitCode -ne 0) { throw "TopPet host exited with code $taskHostExitCode; inspect host stderr and logs/host-exit-*.json" }
    } else {
        & $taskPython laoshaozi.py --character personal
    }
    if ($LASTEXITCODE -ne 0) { throw "TopPet exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
    $env:TOPPET_TTS_ENGINE = $taskOldBackend
    $env:TOPPET_QWEN_RUNTIME = $taskOldRuntime
    $env:DEEPSEEK_API_KEY = $taskOldKey
}
