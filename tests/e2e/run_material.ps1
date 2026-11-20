<#
Material/texture parity two-Blender check.

The host builds an image-textured, mapped, bumped material, then changes it
mid-session. The client must rebuild the same graph, receive the packed image
and apply the update.

    powershell -NoProfile -ExecutionPolicy Bypass -File tests/e2e/run_material.ps1
#>
param(
    [string]$Blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe'
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Get-CimInstance Win32_Process -Filter "Name='blender.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*material_host.py*' -or $_.CommandLine -like '*material_client.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500
$hostData = Join-Path $env:TEMP 'bmp-mat-host'
$clientData = Join-Path $env:TEMP 'bmp-mat-client'
$sync = Join-Path $env:TEMP 'bmp-mat-sync.json'
Remove-Item $hostData, $clientData, $sync -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $hostData, $clientData | Out-Null

$env:BMP_VENDOR = Join-Path $env:LOCALAPPDATA 'Blendermp\python'
$env:BMP_ADDON = Join-Path $root 'addon'
$env:BMP_SYNC = $sync
$env:BMP_CLIENT_RESULT = Join-Path $clientData 'client-result.json'

$env:BMP_TEST_DATA = $hostData
$hostArgs = @('--factory-startup', '--window-geometry', '0', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'material_host.py') + '"'))
$hostProc = Start-Process -FilePath $Blender -ArgumentList $hostArgs -PassThru -RedirectStandardOutput (Join-Path $hostData 'stdout.log') -RedirectStandardError (Join-Path $hostData 'stderr.log')
Start-Sleep -Seconds 2

$env:BMP_TEST_DATA = $clientData
$clientArgs = @('--factory-startup', '--window-geometry', '850', '650', '800', '500', '--python', ('"' + (Join-Path $PSScriptRoot 'material_client.py') + '"'))
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

$initial = $client.initial
$settled = $client.settled
$failures = [System.Collections.Generic.List[string]]::new()
function Check([bool]$condition, [string]$message) {
    if (-not $condition) { $script:failures.Add($message) }
}

Check ($client.reason -eq 'changed') "client finished by '$($client.reason)'"
Check ($null -ne $initial) 'client saw MatCube material'
if ($null -ne $initial) {
    Check ($initial.image -eq 'BMPBrick') "image name was '$($initial.image)'"
    Check ($initial.image_size[0] -eq 32 -and $initial.image_size[1] -eq 32) "image size was $($initial.image_size -join 'x')"
    Check ($initial.image_packed -eq $true) 'image was not packed'
    Check ($initial.colorspace -eq 'sRGB') "colorspace was '$($initial.colorspace)'"
    Check ($initial.mapping[0] -eq 2 -and $initial.mapping[1] -eq 3) "initial mapping was $($initial.mapping -join ',')"
    Check ($initial.uv_map -eq 'UVMap') "uv map was '$($initial.uv_map)'"
    Check ($initial.base_linked -eq $true) 'base colour was not linked'
    Check ($initial.normal_linked -eq $true) 'normal was not linked'
    Check ($initial.bump_strength -eq 0.35) "bump strength was $($initial.bump_strength)"
    Check ($initial.nodes -ge 6) "node count was $($initial.nodes)"
}
if ($null -ne $settled) {
    Check ($settled.mapping[0] -eq 4 -and $settled.mapping[1] -eq 5) "changed mapping was $($settled.mapping -join ',')"
    Check ($settled.interpolation -eq 'Closest') "interpolation was '$($settled.interpolation)'"
    Check ($settled.emission -eq 2.5) "emission was $($settled.emission)"
    Check ($settled.roughness -eq 0.21) "roughness was $($settled.roughness)"
}
if ($null -ne $hostResult -and $null -ne $settled) {
    Check ($hostResult.summary.nodes -eq $settled.node_types.Count) "host nodes $($hostResult.summary.nodes) vs client $($settled.node_types.Count)"
    Check ($hostResult.summary.links -eq $settled.links) "host links $($hostResult.summary.links) vs client $($settled.links)"
}

Write-Host ("client: nodes={0} links={1} image={2} size={3} mapping={4} interpolation={5} reason={6}" -f $settled.nodes, $settled.links, $settled.image, ($settled.image_size -join 'x'), ($settled.mapping -join ','), $settled.interpolation, $client.reason)
if ($null -ne $hostResult) {
    Write-Host ("host:   nodes={0} links={1} summary={2}" -f $hostResult.summary.nodes, $hostResult.summary.links, ($hostResult.summary | ConvertTo-Json -Compress))
}
foreach ($process in @($hostProc, $clientProc)) {
    if ($null -ne $process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
}
if ($failures.Count -gt 0) {
    Write-Host 'FAIL' -ForegroundColor Red
    $failures | ForEach-Object { Write-Host " - $_" }
    Write-Host 'client result:'; $client | ConvertTo-Json -Depth 8
    exit 1
}
Write-Host 'MATERIAL E2E OK' -ForegroundColor Green
exit 0
