<#
Live topology two-Blender check: the host subdivides twice in Edit Mode while
the client stays in Object Mode and must rebuild both snapshots.
#>
param(
    [string]$Blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe'
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Get-CimInstance Win32_Process -Filter "Name='blender.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*topology_host.py*' -or $_.CommandLine -like '*topology_client.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

$hostData = Join-Path $env:TEMP 'bmp-topo-host'
$clientData = Join-Path $env:TEMP 'bmp-topo-client'
$sync = Join-Path $env:TEMP 'bmp-topo-sync.json'
Remove-Item $hostData, $clientData, $sync -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $hostData, $clientData | Out-Null

$env:BMP_VENDOR = Join-Path $env:LOCALAPPDATA 'Blendermp\python'
$env:BMP_ADDON = Join-Path $root 'addon'
$env:BMP_SYNC = $sync
$env:BMP_CLIENT_RESULT = Join-Path $clientData 'client-result.json'

$env:BMP_TEST_DATA = $hostData
$hostArgs = @('--factory-startup', '--window-geometry', '0', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'topology_host.py') + '"'))
$hostProc = Start-Process -FilePath $Blender -ArgumentList $hostArgs -PassThru -RedirectStandardOutput (Join-Path $hostData 'stdout.log') -RedirectStandardError (Join-Path $hostData 'stderr.log')
Start-Sleep -Seconds 2

$env:BMP_TEST_DATA = $clientData
$clientArgs = @('--factory-startup', '--window-geometry', '850', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'topology_client.py') + '"'))
$clientProc = Start-Process -FilePath $Blender -ArgumentList $clientArgs -PassThru -RedirectStandardOutput (Join-Path $clientData 'stdout.log') -RedirectStandardError (Join-Path $clientData 'stderr.log')

$deadline = (Get-Date).AddSeconds(70)
do {
    Start-Sleep -Seconds 2
    $hostDone = Test-Path (Join-Path $hostData 'host-result.json')
    $clientDone = Test-Path (Join-Path $clientData 'client-result.json')
} while ((Get-Date) -lt $deadline -and -not ($hostDone -and $clientDone))

if (-not $clientDone) {
    Write-Host 'client did not finish' -ForegroundColor Red
    Get-Content (Join-Path $clientData 'stderr.log') -Tail 20 -ErrorAction SilentlyContinue
    Get-Content (Join-Path $clientData 'stdout.log') -Tail 20 -ErrorAction SilentlyContinue
    exit 2
}

$client = Get-Content (Join-Path $clientData 'client-result.json') -Raw | ConvertFrom-Json
$hostResult = $null
if ($hostDone) {
    $hostResult = Get-Content (Join-Path $hostData 'host-result.json') -Raw | ConvertFrom-Json
}
$first = $client.first
$second = $client.second
$settled = $client.settled
$failures = [System.Collections.Generic.List[string]]::new()
function Check([bool]$condition, [string]$message) {
    if (-not $condition) { $script:failures.Add($message) }
}

Check ($client.reason -eq 'second') "client finished by '$($client.reason)'"
Check ($null -ne $first) 'client saw the first topology snapshot'
if ($null -ne $first) {
    Check (($first.counts -join ',') -eq '26,48,24') "first counts were $($first.counts -join ',')"
}
if ($null -ne $second) {
    Check ($second.counts[0] -gt 26) "second vertex count was $($second.counts[0])"
    Check ($second.counts[1] -gt 48) "second edge count was $($second.counts[1])"
    Check ($second.counts[2] -gt 24) "second face count was $($second.counts[2])"
    Check ($second.mode -eq 'OBJECT') "client mode was $($second.mode)"
    Check ($second.mesh_objects -eq 1) "client had $($second.mesh_objects) mesh objects"
    Check ($second.finite -eq $true) 'client geometry had non-finite coordinates'
}
if ($null -ne $hostResult -and $null -ne $settled) {
    Check (($hostResult.counts -join ',') -eq ($settled.counts -join ',')) "host counts $($hostResult.counts -join ',') vs client $($settled.counts -join ',')"
    Check (($hostResult.phase.first_counts -join ',') -eq ($first.counts -join ',')) "host first counts $($hostResult.phase.first_counts -join ',') vs client $($first.counts -join ',')"
}

Write-Host ("client first={0} second={1} mode={2} meshes={3} reason={4}" -f ($first.counts -join ','), ($second.counts -join ','), $second.mode, $second.mesh_objects, $client.reason)
if ($null -ne $hostResult) {
    Write-Host ("host   first={0} second={1}" -f ($hostResult.phase.first_counts -join ','), ($hostResult.counts -join ','))
}
foreach ($process in @($hostProc, $clientProc)) {
    if ($null -ne $process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
}
if ($failures.Count -gt 0) {
    Write-Host 'FAIL' -ForegroundColor Red
    $failures | ForEach-Object { Write-Host " - $_" }
    $client | ConvertTo-Json -Depth 8
    exit 1
}
Write-Host 'TOPOLOGY E2E OK' -ForegroundColor Green
exit 0
