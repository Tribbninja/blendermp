<#
Auto-resync check: the host keeps a cube authoritative. The client pauses sync,
deletes the cube locally, and the host's automatic resync must recreate it.
#>
param(
    [string]$Blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe'
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$hostData = Join-Path $env:TEMP 'bmp-resync-host'
$clientData = Join-Path $env:TEMP 'bmp-resync-client'
Get-CimInstance Win32_Process -Filter "Name='blender.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*$hostData*" -or $_.CommandLine -like "*$clientData*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

Remove-Item $hostData, $clientData -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $hostData, $clientData | Out-Null

$env:BMP_VENDOR = Join-Path $env:LOCALAPPDATA 'Blendermp\python'
$env:BMP_ADDON = Join-Path $root 'addon'
$env:BMP_NO_CONSENT_DIALOG = '1'
$env:BMP_CLIENT_RESULT = Join-Path $clientData 'client-result.json'

$env:BMP_TEST_DATA = $hostData
$hostArgs = @('--factory-startup', '--window-geometry', '0', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'resync_host.py') + '"'), '--', '--bmp-data', $hostData)
$hostProc = Start-Process -FilePath $Blender -ArgumentList $hostArgs -PassThru -RedirectStandardOutput (Join-Path $hostData 'stdout.log') -RedirectStandardError (Join-Path $hostData 'stderr.log')
Start-Sleep -Seconds 2

$env:BMP_TEST_DATA = $clientData
$clientArgs = @('--factory-startup', '--window-geometry', '850', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'resync_client.py') + '"'), '--', '--bmp-data', $clientData)
$clientProc = Start-Process -FilePath $Blender -ArgumentList $clientArgs -PassThru -RedirectStandardOutput (Join-Path $clientData 'stdout.log') -RedirectStandardError (Join-Path $clientData 'stderr.log')

$deadline = (Get-Date).AddSeconds(90)
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

Check ($client.reason -eq 'healed') "client finished by '$($client.reason)'"
Check ([bool]$client.diverged) 'client never diverged'
Check ([bool]$client.healed) 'client scene was never healed'
Check ($null -ne $client.heal_seconds -and [double]$client.heal_seconds -le 20.0) "heal took $($client.heal_seconds)s"
Check ([int]$client.resolution_x -eq 64) "resolution healed to $($client.resolution_x)"
Check ($null -ne $client.world_color -and [math]::Abs([double]$client.world_color[0] - 0.2) -le 0.01 -and [math]::Abs([double]$client.world_color[2] - 0.8) -le 0.01) "world colour was $($client.world_color -join ',')"
Check ($null -ne $client.camera_lens -and [math]::Abs([double]$client.camera_lens - 50.0) -le 0.1) "camera lens was $($client.camera_lens)"
Check ($null -ne $client.light_energy -and [math]::Abs([double]$client.light_energy - 100.0) -le 0.1) "light energy was $($client.light_energy)"
Check ($null -ne $client.geo_value -and [math]::Abs([double]$client.geo_value - 0.75) -le 0.01) "geometry node input was $($client.geo_value)"
Check ([int]$client.geo_nodes -ge 3) "geometry graph had $($client.geo_nodes) node(s)"
Check ([int]$client.geo_links -ge 2) "geometry graph had $($client.geo_links) link(s)"
Check ($null -ne $client.comp_glare -and [math]::Abs([double]$client.comp_glare - 0.75) -le 0.01) "compositor glare threshold was $($client.comp_glare)"
Check ($null -ne $client.constraint_distance -and [math]::Abs([double]$client.constraint_distance - 2.0) -le 0.01) "constraint distance was $($client.constraint_distance)"
Check ($null -ne $client.dict_prop -and [int]$client.dict_prop.a -eq 1 -and ($client.dict_prop.b -join ',') -eq '1,2,3') "custom dict was $($client.dict_prop | ConvertTo-Json -Compress)"
Check ($null -ne $client.camera_shift -and [math]::Abs([double]$client.camera_shift - 0.1) -le 0.01) "camera shift was $($client.camera_shift)"
Check ($client.light_shadow -eq $false) "light shadow was $($client.light_shadow)"
Check ($null -ne $client.bone_tail -and [math]::Abs([double]$client.bone_tail - 2.0) -le 0.01) "bone tail was $($client.bone_tail)"
Check ($null -ne $client.prop_value -and [math]::Abs([double]$client.prop_value - 0.5) -le 0.001) "custom property was $($client.prop_value)"
if ($null -ne $hostResult) {
    Check ([bool]$hostResult.auto_resync) 'auto resync was disabled on the host'
    Check ([double]$hostResult.resync_interval -le 0.5) "host resync interval was $($hostResult.resync_interval)"
    Check (($hostResult.objects -join ',') -match 'ResyncCube') 'host lost the cube'
}

Write-Host ("client diverged={0} healed={1} heal_seconds={2} host_interval={3}" -f $client.diverged, $client.healed, $client.heal_seconds, $hostResult.resync_interval)
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
Write-Host 'AUTO RESYNC E2E OK' -ForegroundColor Green
exit 0
