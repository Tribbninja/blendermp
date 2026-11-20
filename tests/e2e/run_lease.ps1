<#
Lease check: a client selects the cube (locks it), deselects (must release it),
and reselects (must be available again). The host must never steal the lease.
#>
param(
    [string]$Blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe'
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Get-CimInstance Win32_Process -Filter "Name='blender.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*lease_host.py*' -or $_.CommandLine -like '*lease_client.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

$hostData = Join-Path $env:TEMP 'bmp-lease-host'
$clientData = Join-Path $env:TEMP 'bmp-lease-client'
Remove-Item $hostData, $clientData -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $hostData, $clientData | Out-Null

$env:BMP_VENDOR = Join-Path $env:LOCALAPPDATA 'Blendermp\python'
$env:BMP_ADDON = Join-Path $root 'addon'
$env:BMP_CLIENT_RESULT = Join-Path $clientData 'client-result.json'

$env:BMP_TEST_DATA = $hostData
$hostArgs = @('--factory-startup', '--window-geometry', '0', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'lease_host.py') + '"'))
$hostProc = Start-Process -FilePath $Blender -ArgumentList $hostArgs -PassThru -RedirectStandardOutput (Join-Path $hostData 'stdout.log') -RedirectStandardError (Join-Path $hostData 'stderr.log')
Start-Sleep -Seconds 2

$env:BMP_TEST_DATA = $clientData
$clientArgs = @('--factory-startup', '--window-geometry', '850', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'lease_client.py') + '"'))
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
Check ($client.owner_after_select -eq 'testclient') "owner after select was '$($client.owner_after_select)'"
Check ([bool]$client.owned_after_select) 'client did not own the lease after selecting'
Check ($null -eq $client.owner_after_deselect) "owner after deselect was '$($client.owner_after_deselect)'"
Check (-not [bool]$client.owned_after_deselect) 'client still owned the lease after deselecting'
Check ($client.owner_after_reselect -eq 'testclient') "owner after reselect was '$($client.owner_after_reselect)'"
Check ([bool]$client.owned_after_reselect) 'client could not re-acquire the lease after releasing it'
if ($null -ne $hostResult) {
    Check ($hostResult.owners_seen -contains 'testclient') "host never saw the client own the lease (seen: $($hostResult.owners_seen -join ','))"
    Check (-not ($hostResult.owners_seen -contains 'testhost')) 'host stole the lease while the client owned it'
    Check ($null -eq $hostResult.final_owner) "host final owner was '$($hostResult.final_owner)'"
}

Write-Host ("client select={0} deselect={1} reselect={2} host seen={3}" -f $client.owner_after_select, $client.owner_after_deselect, $client.owner_after_reselect, ($hostResult.owners_seen -join ','))
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
Write-Host 'LEASE E2E OK' -ForegroundColor Green
exit 0
