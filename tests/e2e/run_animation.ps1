<#
Animation/rig check: armature bones, armature modifier binding, vertex groups,
shape keys, action curves and a live pose change.
#>
param(
    [string]$Blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe'
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Get-CimInstance Win32_Process -Filter "Name='blender.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*animation_host.py*' -or $_.CommandLine -like '*animation_client.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

$hostData = Join-Path $env:TEMP 'bmp-anim-host'
$clientData = Join-Path $env:TEMP 'bmp-anim-client'
$sync = Join-Path $env:TEMP 'bmp-anim-sync.json'
Remove-Item $hostData, $clientData, $sync -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $hostData, $clientData | Out-Null

$env:BMP_VENDOR = Join-Path $env:LOCALAPPDATA 'Blendermp\python'
$env:BMP_ADDON = Join-Path $root 'addon'
$env:BMP_SYNC = $sync
$env:BMP_CLIENT_RESULT = Join-Path $clientData 'client-result.json'

$env:BMP_TEST_DATA = $hostData
$hostArgs = @('--factory-startup', '--window-geometry', '0', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'animation_host.py') + '"'))
$hostProc = Start-Process -FilePath $Blender -ArgumentList $hostArgs -PassThru -RedirectStandardOutput (Join-Path $hostData 'stdout.log') -RedirectStandardError (Join-Path $hostData 'stderr.log')
Start-Sleep -Seconds 2

$env:BMP_TEST_DATA = $clientData
$clientArgs = @('--factory-startup', '--window-geometry', '850', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'animation_client.py') + '"'))
$clientProc = Start-Process -FilePath $Blender -ArgumentList $clientArgs -PassThru -RedirectStandardOutput (Join-Path $clientData 'stdout.log') -RedirectStandardError (Join-Path $clientData 'stderr.log')

$deadline = (Get-Date).AddSeconds(90)
do {
    Start-Sleep -Seconds 2
    $hostDone = Test-Path (Join-Path $hostData 'host-result.json')
    $clientDone = Test-Path (Join-Path $clientData 'client-result.json')
} while ((Get-Date) -lt $deadline -and -not ($hostDone -and $clientDone))

if (-not $clientDone) {
    Write-Host 'client did not finish' -ForegroundColor Red
    Get-Content (Join-Path $clientData 'stderr.log') -Tail 30 -ErrorAction SilentlyContinue
    Get-Content (Join-Path $clientData 'stdout.log') -Tail 30 -ErrorAction SilentlyContinue
    exit 2
}
$client = Get-Content (Join-Path $clientData 'client-result.json') -Raw | ConvertFrom-Json
$failures = [System.Collections.Generic.List[string]]::new()
function Check([bool]$condition, [string]$message) { if (-not $condition) { $script:failures.Add($message) } }

Check ($client.reason -eq 'changed') "client finished by '$($client.reason)'"
$initial = $client.initial
$changed = $client.changed
if ($null -ne $initial) {
    Check (($initial.bones -join ',') -eq 'Bone1,Bone2') "bones were $($initial.bones -join ',')"
    Check ($initial.modifier_target -eq 'Rig') "modifier target was $($initial.modifier_target)"
    Check (($initial.groups -join ',') -eq 'Bone1,Bone2') "vertex groups were $($initial.groups -join ',')"
    Check ($initial.shape_keys -contains 'Smile') "shape keys were $($initial.shape_keys -join ',')"
    Check ($initial.smile -eq 0.25) "initial Smile value was $($initial.smile)"
    Check ($initial.action -eq 'RigAction') "action was $($initial.action)"
    Check ($initial.curves -eq 1) "action had $($initial.curves) curves"
    Check ($initial.points -eq 2) "action had $($initial.points) points"
    Check (($initial.uv_layers -join ',') -match 'Second') "UV layers were $($initial.uv_layers -join ',')"
    Check ($initial.uv_active -eq 1) "active UV layer was $($initial.uv_active)"
    Check (($initial.drivers | ConvertTo-Json -Compress) -match 'frame \* 0.1') "drivers were $($initial.drivers | ConvertTo-Json -Compress)"
    Check (($initial.nla | ConvertTo-Json -Compress) -match 'Track1' -and ($initial.nla | ConvertTo-Json -Compress) -match 'NlaAction') "NLA was $($initial.nla | ConvertTo-Json -Compress)"
}
if ($null -ne $changed) {
    Check ([math]::Abs($changed.smile - 0.75) -le 0.01) "changed Smile value was $($changed.smile)"
    Check ($changed.quat[0] -lt 0.95) "pose quaternion was $($changed.quat -join ',')"
}

Write-Host ("client bones={0} modifier={1} shape={2} action={3} curves={4} points={5} quat={6} reason={7}" -f ($initial.bones -join ','), $initial.modifier_target, ($initial.shape_keys -join ','), $initial.action, $initial.curves, $initial.points, ($changed.quat -join ','), $client.reason)
foreach ($process in @($hostProc, $clientProc)) {
    if ($null -ne $process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
}
if ($failures.Count -gt 0) {
    Write-Host 'FAIL' -ForegroundColor Red
    $failures | ForEach-Object { Write-Host " - $_" }
    $client | ConvertTo-Json -Depth 8
    exit 1
}
Write-Host 'ANIMATION E2E OK' -ForegroundColor Green
exit 0
