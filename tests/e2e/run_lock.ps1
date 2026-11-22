<#
Lock check: the host holds the default cube. The client must see the host's
lease, must not be able to select or move it, and a forced move must revert.
#>
param(
    [string]$Blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe'
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Get-CimInstance Win32_Process -Filter "Name='blender.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*lock_host.py*' -or $_.CommandLine -like '*lock_client.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

$hostData = Join-Path $env:TEMP 'bmp-lock-host'
$clientData = Join-Path $env:TEMP 'bmp-lock-client'
Remove-Item $hostData, $clientData -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $hostData, $clientData | Out-Null

$env:BMP_VENDOR = Join-Path $env:LOCALAPPDATA 'Blendermp\python'
$env:BMP_ADDON = Join-Path $root 'addon'
$env:BMP_CLIENT_RESULT = Join-Path $clientData 'client-result.json'

$env:BMP_TEST_DATA = $hostData
$hostArgs = @('--factory-startup', '--window-geometry', '0', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'lock_host.py') + '"'))
$hostProc = Start-Process -FilePath $Blender -ArgumentList $hostArgs -PassThru -RedirectStandardOutput (Join-Path $hostData 'stdout.log') -RedirectStandardError (Join-Path $hostData 'stderr.log')
Start-Sleep -Seconds 2

$env:BMP_TEST_DATA = $clientData
$clientArgs = @('--factory-startup', '--window-geometry', '850', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'lock_client.py') + '"'))
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
if ($hostDone) { $hostResult = Get-Content (Join-Path $hostData 'host-result.json') -Raw | ConvertFrom-Json }
$failures = [System.Collections.Generic.List[string]]::new()
function Check([bool]$condition, [string]$message) { if (-not $condition) { $script:failures.Add($message) } }

Check ($client.reason -eq 'done') "client finished by '$($client.reason)'"
Check ($client.owner -eq 'lockhost') "client saw owner '$($client.owner)'"
Check ([bool]$client.hide_select) 'locked cube was still selectable on the client'
Check (-not [bool]$client.selected) 'locked cube was selected on the client'
Check ([bool]$client.hidden) 'locked cube was not tracked as hidden on the client'
Check ($client.after_move[0] -eq 5.0) 'client test move did not apply before the revert check'
Check ($client.settled[0] -eq 0.0) "client move was not reverted (settled x=$($client.settled[0]))"
Check ([bool]$client.hide_select_after) 'locked cube became selectable again on the client'
if ($null -ne $hostResult) {
    Check ($hostResult.owner -eq 'lockhost') "host owner was '$($hostResult.owner)'"
    Check ([bool]$hostResult.selected) 'host lost its own selection'
    Check (-not [bool]$hostResult.hide_select) 'host could not select its own locked cube'
}

Write-Host ("client owner={0} hide_select={1} selected={2} settled={3} host owner={4}" -f $client.owner, $client.hide_select, $client.selected, ($client.settled -join ','), $hostResult.owner)
foreach ($process in @($hostProc, $clientProc)) {
    if ($null -ne $process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
}
if ($failures.Count -gt 0) {
    Write-Host 'FAIL' -ForegroundColor Red
    $failures | ForEach-Object { Write-Host " - $_" }
    $client | ConvertTo-Json -Depth 8
    if ($null -ne $hostResult) { $hostResult | ConvertTo-Json -Depth 8 }
    exit 1
}
Write-Host 'LOCK E2E OK' -ForegroundColor Green
exit 0
