param(
    [string]$Port = "COM5",
    [switch]$CompileOnly
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$ArduinoCli = Join-Path $RepositoryRoot "artifacts\toolchains\arduino-cli-1.5.1\arduino-cli.exe"
$ArduinoConfig = Join-Path $RepositoryRoot "artifacts\toolchains\arduino-cli.yaml"
$ArduinoData = Join-Path $RepositoryRoot "artifacts\toolchains\arduino-data"
$Sketch = Join-Path $PSScriptRoot "toppet_pet"
$DataDirectory = Join-Path $Sketch "data"
$BuildPath = Join-Path $RepositoryRoot "artifacts\firmware-build\toppet_pet"
$Generator = Join-Path $PSScriptRoot "tools\generate_expression_assets.py"
$NetworkConfig = Join-Path $Sketch "net_config.h"
$NetworkConfigExample = Join-Path $Sketch "net_config.example.h"
$LittleFsTool = Join-Path $ArduinoData "packages\esp32\tools\mklittlefs\4.0.2-db0513a\mklittlefs.exe"
$LittleFsImage = Join-Path $BuildPath "toppet-littlefs.bin"
$LittleFsOffset = 0x210000
$LittleFsSize = 0xDF0000
$Fqbn = "esp32:esp32:esp32s3:UploadSpeed=921600,USBMode=hwcdc,CDCOnBoot=cdc,CPUFreq=240,FlashMode=qio,FlashSize=16M,PartitionScheme=custom,PSRAM=opi"

if (-not (Test-Path $ArduinoCli)) {
    throw "Arduino CLI not found at $ArduinoCli"
}
if (-not (Test-Path $LittleFsTool)) {
    throw "mklittlefs not found at $LittleFsTool"
}
if (-not (Test-Path $NetworkConfig)) {
    Copy-Item -LiteralPath $NetworkConfigExample -Destination $NetworkConfig
    Write-Output "Created $NetworkConfig from net_config.example.h; edit Wi-Fi and host settings before uploading."
}

python $Generator
if ($LASTEXITCODE -ne 0) {
    throw "Expression asset generation failed"
}

New-Item -ItemType Directory -Force -Path $BuildPath | Out-Null
& $ArduinoCli --config-file $ArduinoConfig compile `
    --fqbn $Fqbn `
    --build-path $BuildPath `
    $Sketch
if ($LASTEXITCODE -ne 0) {
    throw "Firmware compilation failed"
}

& $LittleFsTool -c $DataDirectory -b 4096 -p 256 -s $LittleFsSize $LittleFsImage
if ($LASTEXITCODE -ne 0) {
    throw "LittleFS image generation failed"
}

if (-not $CompileOnly) {
    & $ArduinoCli --config-file $ArduinoConfig upload `
        --fqbn $Fqbn `
        --port $Port `
        --input-dir $BuildPath `
        $Sketch
    if ($LASTEXITCODE -ne 0) {
        throw "Firmware upload failed"
    }

    $ChunkSize = 0x100000
    $ImageStream = [System.IO.File]::OpenRead($LittleFsImage)
    try {
        $ChunkIndex = 0
        $Buffer = New-Object byte[] $ChunkSize
        while (($Read = $ImageStream.Read($Buffer, 0, $Buffer.Length)) -gt 0) {
            $ChunkPath = Join-Path $BuildPath ("littlefs-{0:D2}.bin" -f $ChunkIndex)
            $ChunkStream = [System.IO.File]::Open($ChunkPath, [System.IO.FileMode]::Create)
            try {
                $ChunkStream.Write($Buffer, 0, $Read)
            } finally {
                $ChunkStream.Dispose()
            }
            $Offset = $LittleFsOffset + $ChunkIndex * $ChunkSize
            $OffsetHex = "0x{0:X}" -f $Offset
            Write-Output "Flashing LittleFS chunk $($ChunkIndex + 1) at $OffsetHex"
            python -m esptool --chip esp32s3 --port $Port --baud 921600 `
                write-flash $OffsetHex $ChunkPath
            if ($LASTEXITCODE -ne 0) {
                throw "LittleFS upload failed at $OffsetHex"
            }
            $ChunkIndex++
        }
    } finally {
        $ImageStream.Dispose()
    }
}
