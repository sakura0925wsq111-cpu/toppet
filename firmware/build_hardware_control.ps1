param(
    [switch]$Upload,
    [string]$Port = 'COM5'
)

$ErrorActionPreference = 'Stop'
$taskRepoRoot = Split-Path -Parent $PSScriptRoot
$taskToolRoots = @(
    (Join-Path $taskRepoRoot 'artifacts\toolchains'),
    (Join-Path (Split-Path -Parent $taskRepoRoot) 'toppet\artifacts\toolchains')
)
$taskToolRoot = $taskToolRoots | Where-Object {
    (Test-Path -LiteralPath (Join-Path $_ 'arduino-cli-1.5.1\arduino-cli.exe')) -and
    (Test-Path -LiteralPath (Join-Path $_ 'arduino-cli.yaml'))
} | Select-Object -First 1
if (-not $taskToolRoot) { throw 'Arduino CLI/config not found in this checkout or the sibling toppet checkout.' }
$taskCli = Join-Path $taskToolRoot 'arduino-cli-1.5.1\arduino-cli.exe'
$taskConfig = Join-Path $taskToolRoot 'arduino-cli.yaml'
$taskSketch = Join-Path $PSScriptRoot 'toppet_pet'
$taskBuild = Join-Path $taskRepoRoot 'artifacts\hardware-mcp-20261028\firmware-build'
$taskFqbn = 'esp32:esp32:esp32s3:UploadSpeed=921600,USBMode=hwcdc,CDCOnBoot=cdc,CPUFreq=240,FlashMode=qio,FlashSize=16M,PartitionScheme=custom,PSRAM=opi'
if (-not (Test-Path -LiteralPath (Join-Path $taskSketch 'net_config.h'))) {
    throw 'net_config.h is missing. Configure the device network before building.'
}
if ($Upload) {
    $taskAvailablePorts = @([System.IO.Ports.SerialPort]::GetPortNames())
    if ($Port -notin $taskAvailablePorts) { throw "Device port $Port is not connected." }
}
New-Item -ItemType Directory -Path $taskBuild -Force | Out-Null
& $taskCli --config-file $taskConfig compile --fqbn $taskFqbn --build-path $taskBuild $taskSketch
if ($LASTEXITCODE -ne 0) { throw 'Hardware-control firmware compilation failed.' }
if ($Upload) {
    # This change adds no expression assets. Upload only the application firmware;
    # preserve the device's existing LittleFS expression/audio assets.
    & $taskCli --config-file $taskConfig upload --fqbn $taskFqbn --port $Port --input-dir $taskBuild $taskSketch
    if ($LASTEXITCODE -ne 0) { throw 'Hardware-control firmware upload failed.' }
}
Write-Output "Hardware-control firmware ready: $taskBuild"
