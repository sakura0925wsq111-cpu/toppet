param(
    [string]$HostAddress = "0.0.0.0",
    [int]$Port = 8765,
    [string]$Character = "personal",
    [string]$AsrModel = "base",
    [string]$AsrDevice = "cpu",
    [string]$AsrComputeType = "int8"
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepositoryRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Project Python environment not found at $Python. Create .venv and install requirements first."
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($null -ne $listener) {
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2
        if ($health.status -eq "ready" -and $health.service -eq "toppet-host") {
            Write-Output "TopPet host service is already running on port $Port (PID $($listener.OwningProcess))."
            exit 0
        }
    } catch {
        # A different process may own the port; report that below.
    }
    throw "Port $Port is already in use by PID $($listener.OwningProcess). Stop that process or choose another port."
}

& $Python -m host_service.server `
    --host $HostAddress `
    --port $Port `
    --character $Character `
    --asr-model $AsrModel `
    --asr-device $AsrDevice `
    --asr-compute-type $AsrComputeType
