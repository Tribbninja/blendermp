<#
Scoped undo check: client moves a leased object, then bmp.undo_shared restores
the previous transform on both sides.
#>
param(
    [string]$Blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe'
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Get-CimInstance Win32_Process -Filter "Name='blender.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*undo_host.py*' -or $_.CommandLine -like '*undo_client.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

$hostData = Join-Path $env:TEMP 'bmp-undo-host'
$clientData = Join-Path $env:TEMP 'bmp-undo-client'
Remove-Item $hostData, $clientData -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $hostData, $clientData | Out-Null

$env:BMP_VENDOR = Join-Path $env:LOCALAPPDATA 'Blendermp\python'
$env:BMP_ADDON = Join-Path $root 'addon'
$env:BMP_CLIENT_RESULT = Join-Path $clientData 'client-result.json'

$env:BMP_TEST_DATA = $hostData
$hostArgs = @('--factory-startup', '--window-geometry', '0', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'undo_host.py') + '"'))
$hostProc = Start-Process -FilePath $Blender -ArgumentList $hostArgs -PassThru -RedirectStandardOutput (Join-Path $hostData 'stdout.log') -RedirectStandardError (Join-Path $hostData 'stderr.log')
Start-Sleep -Seconds 2

$env:BMP_TEST_DATA = $clientData
$clientArgs = @('--factory-startup', '--window-geometry', '850', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'undo_client.py') + '"'))
$clientProc = Start-Process -FilePath $Blender -ArgumentList $clientArgs -PassThru -RedirectStandardOutput (Join-Path $clientData 'stdout.log') -RedirectStandardError (Join-Path $clientData 'stderr.log')

$deadline = (Get-Date).AddSeconds(60)
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

Check ($client.reason -eq 'undone') "client finished by '$($client.reason)'"
Check ($client.moved_cube[0] -eq 2.0) "moved cube was $($client.moved_cube -join ',')"
Check ($client.undone_cube[0] -eq 0.0) "undone cube was $($client.undone_cube -join ',')"
if ($null -ne $hostResult) {
    Check ($hostResult.cube[0] -eq 0.0) "host cube was $($hostResult.cube -join ',')"
}

Write-Host ("client moved={0} undone={1} host={2}" -f ($client.moved_cube -join ','), ($client.undone_cube -join ','), ($hostResult.cube -join ','))
foreach ($process in @($hostProc, $clientProc)) {
    if ($null -ne $process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
}
if ($failures.Count -gt 0) {
    Write-Host 'FAIL' -ForegroundColor Red
    $failures | ForEach-Object { Write-Host " - $_" }
    $client | ConvertTo-Json -Depth 8
    exit 1
}
Write-Host 'UNDO E2E OK' -ForegroundColor Green
exit 0
