<#
Checkpoint restore check: host saves, moves, restores while a client is
connected; the client must replace its scene and end at the saved transform.
#>
param(
    [string]$Blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe'
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Get-CimInstance Win32_Process -Filter "Name='blender.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*checkpoint_host.py*' -or $_.CommandLine -like '*checkpoint_client.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

$hostData = Join-Path $env:TEMP 'bmp-ckpt-e2e-host'
$clientData = Join-Path $env:TEMP 'bmp-ckpt-e2e-client'
$sync = Join-Path $env:TEMP 'bmp-ckpt-e2e-sync.json'
Remove-Item $hostData, $clientData, $sync -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $hostData, $clientData | Out-Null

$env:BMP_VENDOR = Join-Path $env:LOCALAPPDATA 'Blendermp\python'
$env:BMP_ADDON = Join-Path $root 'addon'
$env:BMP_SYNC = $sync
$env:BMP_CLIENT_RESULT = Join-Path $clientData 'client-result.json'

$env:BMP_TEST_DATA = $hostData
$hostArgs = @('--factory-startup', '--window-geometry', '0', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'checkpoint_host.py') + '"'))
$hostProc = Start-Process -FilePath $Blender -ArgumentList $hostArgs -PassThru -RedirectStandardOutput (Join-Path $hostData 'stdout.log') -RedirectStandardError (Join-Path $hostData 'stderr.log')
Start-Sleep -Seconds 2

$env:BMP_TEST_DATA = $clientData
$clientArgs = @('--factory-startup', '--window-geometry', '850', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'checkpoint_client.py') + '"'))
$clientProc = Start-Process -FilePath $Blender -ArgumentList $clientArgs -PassThru -RedirectStandardOutput (Join-Path $clientData 'stdout.log') -RedirectStandardError (Join-Path $clientData 'stderr.log')

$deadline = (Get-Date).AddSeconds(75)
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
$failures = [System.Collections.Generic.List[string]]::new()
function Check([bool]$condition, [string]$message) {
    if (-not $condition) { $script:failures.Add($message) }
}

Check ($client.reason -eq 'restored') "client finished by '$($client.reason)'"
Check ($null -ne $client.initial) 'client never saw the original cube'
Check ($null -ne $client.moved) 'client never saw the moved cube'
Check ($null -ne $client.restored) 'client never saw the restored scene'
Check ($client.mesh_objects -eq 1) "client had $($client.mesh_objects) mesh objects after restore"
if ($null -ne $client.restored_cube) {
    Check ([math]::Abs($client.restored_cube[0] - 1.0) -le 0.01 -and [math]::Abs($client.restored_cube[1] - 2.0) -le 0.01 -and [math]::Abs($client.restored_cube[2] - 3.0) -le 0.01) "restored cube was $($client.restored_cube -join ',')"
}
if ($null -ne $hostResult) {
    Check ($hostResult.phase.restored -eq $true) 'host restore returned false'
    Check (($hostResult.messages -join '|') -match 'Checkpoint saved') 'host did not announce the checkpoint'
}

Write-Host ("client initial={0} moved={1} restored={2} objects={3} reason={4}" -f ($client.initial -join ','), ($client.moved_cube -join ','), ($client.restored_cube -join ','), ($client.objects -join ','), $client.reason)
if ($null -ne $hostResult) {
    Write-Host ("host cube={0} checkpoint={1} messages={2}" -f ($hostResult.cube -join ','), $hostResult.phase.checkpoint, ($hostResult.messages -join ' | '))
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
Write-Host 'CHECKPOINT E2E OK' -ForegroundColor Green
exit 0
