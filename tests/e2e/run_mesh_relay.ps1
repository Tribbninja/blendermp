<#
Mesh relay check: the host hosts through the Worker, the client joins through the
Worker, the mesh coordinator links them directly, and a host edit still reaches
the client. The mesh STUN lookup is stubbed to loopback so two Blender instances
on one machine can establish the direct link.
#>
param(
    [string]$Blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe',
    [string]$RelayUrl = 'wss://blendermp-hub.tribbeisaac.workers.dev'
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Get-CimInstance Win32_Process -Filter "Name='blender.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*mesh_relay_host.py*' -or $_.CommandLine -like '*mesh_relay_client.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

$hostData = Join-Path $env:TEMP 'bmp-mesh-relay-host'
$clientData = Join-Path $env:TEMP 'bmp-mesh-relay-client'
Remove-Item $hostData, $clientData -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $hostData, $clientData | Out-Null

$env:BMP_VENDOR = Join-Path $env:LOCALAPPDATA 'Blendermp\python'
$env:BMP_ADDON = Join-Path $root 'addon'
$env:BMP_RELAY_URL = $RelayUrl
$env:BMP_HOST_CODE = Join-Path $hostData 'host-code.txt'
$relayPort = 7100
$env:BMP_RELAY_PORT = "$relayPort"
$relayProc = Start-Process -FilePath (Join-Path $root '.venv\Scripts\python.exe') -ArgumentList @('-m', 'server.relay', '--host', '127.0.0.1', '--port', "$relayPort") -WorkingDirectory $root -PassThru -RedirectStandardOutput (Join-Path $hostData 'relay.log') -RedirectStandardError (Join-Path $hostData 'relay.err')
Start-Sleep -Seconds 1

$env:BMP_TEST_DATA = $hostData
$hostArgs = @('--factory-startup', '--window-geometry', '0', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'mesh_relay_host.py') + '"'))
$hostProc = Start-Process -FilePath $Blender -ArgumentList $hostArgs -PassThru -RedirectStandardOutput (Join-Path $hostData 'stdout.log') -RedirectStandardError (Join-Path $hostData 'stderr.log')
Start-Sleep -Seconds 3

$env:BMP_TEST_DATA = $clientData
$clientArgs = @('--factory-startup', '--window-geometry', '850', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'mesh_relay_client.py') + '"'))
$clientProc = Start-Process -FilePath $Blender -ArgumentList $clientArgs -PassThru -RedirectStandardOutput (Join-Path $clientData 'stdout.log') -RedirectStandardError (Join-Path $clientData 'stderr.log')

$deadline = (Get-Date).AddSeconds(80)
do {
    Start-Sleep -Seconds 2
    $hostDone = Test-Path (Join-Path $hostData 'host-result.json')
    $clientDone = Test-Path (Join-Path $clientData 'client-result.json')
} while ((Get-Date) -lt $deadline -and -not ($hostDone -and $clientDone))

if (-not $clientDone) {
    Write-Host 'client did not finish' -ForegroundColor Red
    Get-Content (Join-Path $clientData 'stderr.log') -Tail 20 -ErrorAction SilentlyContinue
    Get-Content (Join-Path $clientData 'stdout.log') -Tail 20 -ErrorAction SilentlyContinue
    foreach ($process in @($hostProc, $clientProc, $relayProc)) {
        if ($null -ne $process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    }
    exit 2
}

$client = Get-Content (Join-Path $clientData 'client-result.json') -Raw | ConvertFrom-Json
$hostResult = $null
if ($hostDone) { $hostResult = Get-Content (Join-Path $hostData 'host-result.json') -Raw | ConvertFrom-Json }
$failures = [System.Collections.Generic.List[string]]::new()
function Check([bool]$condition, [string]$message) { if (-not $condition) { $script:failures.Add($message) } }

Check ($client.state -eq 'connected') "client state was '$($client.state)'"
$loaded = @($client.samples | Where-Object { $_.loaded }).Count -gt 0
Check $loaded 'client never loaded the host scene'
$moved = @($client.samples | Where-Object { $null -ne $_.x -and [math]::Abs([double]$_.x - 4.0) -lt 0.5 }).Count -gt 0
Check $moved 'client never saw the host move the cube to x=4'
Check ([bool]$client.mesh.active) 'mesh coordinator was not active on the client'
Check ([int]$client.mesh.received -gt 0) "client received no mesh frames (received $($client.mesh.received))"
if ($null -ne $hostResult) {
    Check ($hostResult.state -eq 'hosting') "host state was '$($hostResult.state)'"
    Check ([bool]$hostResult.mesh.active) 'mesh coordinator was not active on the host'
    Check ([int]$hostResult.mesh.sent -gt 0) "host sent no mesh frames (sent $($hostResult.mesh.sent))"
}

Write-Host ("client state={0} mesh_active={1} mesh_peers={2} host_mesh_peers={3} moved={4} host_sent={5} client_recv={6}" -f $client.state, $client.mesh.active, @($client.mesh.peers).Count, (@($hostResult.mesh.peers).Count), $moved, $hostResult.mesh.sent, $client.mesh.received)
foreach ($process in @($hostProc, $clientProc, $relayProc)) {
    if ($null -ne $process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
}
if ($failures.Count -gt 0) {
    Write-Host 'FAIL' -ForegroundColor Red
    $failures | ForEach-Object { Write-Host " - $_" }
    $client | ConvertTo-Json -Depth 8
    if ($null -ne $hostResult) { $hostResult | ConvertTo-Json -Depth 8 }
    exit 1
}
Write-Host 'MESH RELAY E2E OK' -ForegroundColor Green
exit 0
