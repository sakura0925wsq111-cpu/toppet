param(
    [string]$UpstreamProfile = 'ZMGY',
    [string]$Ssid = 'TopPet-Host',
    [switch]$Stop
)

# Run with Windows PowerShell 5.1, which includes the WinRT projection.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]
$null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]

function Wait-WinRtOperation($Operation, [Type]$ResultType) {
    $taskMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
        $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1
    } | Select-Object -First 1
    $taskOperationTask = $taskMethod.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    if (-not $taskOperationTask.Wait(30000)) { throw 'Windows hotspot operation timed out.' }
    return $taskOperationTask.Result
}

function Wait-WinRtAction($Operation) {
    $taskMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and -not $_.IsGenericMethodDefinition -and
        $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction'
    } | Select-Object -First 1
    $taskActionTask = $taskMethod.Invoke($null, @($Operation))
    if (-not $taskActionTask.Wait(30000)) { throw 'Windows hotspot configuration timed out.' }
}

$taskRepo = Split-Path -Parent $PSScriptRoot
$taskPrivate = Join-Path $taskRepo 'artifacts\hardware-mcp-20261028'
$taskProfile = [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles() |
    Where-Object { $_.ProfileName -eq $UpstreamProfile -and $_.IsWlanConnectionProfile } |
    Select-Object -First 1
if (-not $taskProfile -or (-not $Stop -and [string]$taskProfile.GetNetworkConnectivityLevel() -ne 'InternetAccess')) {
    throw 'The requested upstream Wi-Fi is not connected to the internet.'
}
$taskManager = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($taskProfile)
$taskOld = $taskManager.GetCurrentAccessPointConfiguration()
if ($Stop) {
    $taskStopped = Wait-WinRtOperation -Operation ($taskManager.StopTetheringAsync()) -ResultType ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
    if ([string]$taskStopped.Status -ne 'Success') { throw 'Could not stop the hardware hotspot.' }
    [ordered]@{ stopped=$true; upstream=$UpstreamProfile } | ConvertTo-Json -Compress
    return
}
if ([string]$taskManager.TetheringOperationalState -ne 'Off') {
    throw 'An existing hotspot is active; leave its configuration unchanged.'
}
$taskBand = [Windows.Networking.NetworkOperators.TetheringWiFiBand]::TwoPointFourGigahertz
$taskBandSupported = Wait-WinRtOperation -Operation ($taskOld.IsBandSupportedAsync($taskBand)) -ResultType ([bool])
if (-not $taskBandSupported) { throw 'This adapter does not support a 2.4 GHz hotspot.' }
New-Item -ItemType Directory -Path $taskPrivate -Force | Out-Null
$taskBackup = Join-Path $taskPrivate 'hotspot.before.private.json'
if (-not (Test-Path -LiteralPath $taskBackup)) {
    [ordered]@{ ssid=$taskOld.Ssid; passphrase=$taskOld.Passphrase; band=[string]$taskOld.Band; state='Off' } |
        ConvertTo-Json | Set-Content -LiteralPath $taskBackup -Encoding UTF8
}
$taskNew = New-Object Windows.Networking.NetworkOperators.NetworkOperatorTetheringAccessPointConfiguration
$taskNew.Ssid = $Ssid
$taskNew.Passphrase = if ($taskOld.Ssid -eq $Ssid) { $taskOld.Passphrase } else { [Guid]::NewGuid().ToString('N').Substring(0, 16) }
$taskNew.Band = $taskBand
Wait-WinRtAction -Operation ($taskManager.ConfigureAccessPointAsync($taskNew))
$taskStart = Wait-WinRtOperation -Operation ($taskManager.StartTetheringAsync()) -ResultType ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
if ([string]$taskStart.Status -ne 'Success') {
    Wait-WinRtAction -Operation ($taskManager.ConfigureAccessPointAsync($taskOld))
    throw ('Windows hotspot start failed: ' + [string]$taskStart.Status + ' ' + $taskStart.AdditionalErrorMessage)
}
$taskGateway = $null
for ($taskAttempt = 0; $taskAttempt -lt 10 -and -not $taskGateway; $taskAttempt++) {
    $taskVirtual = @(Get-NetAdapter -IncludeHidden | Where-Object {
        $_.InterfaceDescription -like '*Wi-Fi Direct*' -and $_.Status -eq 'Up'
    })
    $taskAddresses = @($taskVirtual | ForEach-Object {
        Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' }
    })
    if ($taskAddresses.Count -eq 1) { $taskGateway = $taskAddresses[0].IPAddress }
    if (-not $taskGateway) { Start-Sleep -Milliseconds 500 }
}
if (-not $taskGateway) { throw 'Hotspot started but its local gateway could not be identified.' }
[ordered]@{ ssid=$Ssid; passphrase=$taskNew.Passphrase; band='2.4GHz'; gateway=$taskGateway; upstream=$UpstreamProfile } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $taskPrivate 'hotspot.current.private.json') -Encoding UTF8
[ordered]@{ started=$true; ssid=$Ssid; band='2.4GHz'; gateway=$taskGateway; upstream=$UpstreamProfile } | ConvertTo-Json -Compress
